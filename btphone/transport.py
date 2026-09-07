"""串口传输层:帧收发线程、请求-响应匹配、音频流控、断连感知。

支持两种后端:
- 真实硬件: pyserial(传 COM 口名)
- 模拟器/测试: 任何带 read(n)/write(bytes)/close() 的对象
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Callable, Optional

import serial  # type: ignore

from . import protocol
from .events import EventBus
from .exceptions import CommandError, TransportError


class SerialTransport:
    def __init__(
        self,
        port: str,
        baudrate: int = 2_000_000,
        io: Optional[Any] = None,
        on_disconnect: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._port_name = port
        self._baudrate = baudrate
        self._io: Optional[Any] = io
        self._injected_io = io  # 模拟器/测试注入的后端:重开必须复用(建不了真实串口)
        self._on_disconnect = on_disconnect
        self.events = EventBus()

        # 收口后第一条命令的自动自救(request 触发),见 _auto_recover
        self._recover_lock = threading.Lock()
        self._recover_stamp = 0.0
        # 恢复进行中标记:恢复路径自身的握手请求(request)必须直接失败,
        # 不得递归再进 _auto_recover——否则在已持有的非重入锁上自死锁
        # (实测:板子重启期 music.play 重试线程把整个 transport 拖死)
        self._recovering = False

        self._rx_parser = protocol.FrameParser()
        self._rx_thread: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, threading.Event] = {}
        self._responses: dict[int, dict] = {}
        # 请求 id 用时间种子(毫秒级单调):跨进程不重复——固件对"同 id 重试"
        # 只重发缓存应答(v0.3.7+),id 若跨会话撞车会拿到上一个进程的旧应答。
        self._ctrl_seq = itertools.count(int(time.time() * 1000) % 1_000_000_000)
        self._audio_seq = itertools.count(1)
        self._closed = threading.Event()
        self.rx_bytes = 0  # 累计收到字节数:握手期判断"句柄僵死"(有口无数据)用
        self.reset_quiet_s = 10.0  # 复位脉冲后链路静默稳定时间(实测 3s 仍撞过渡态,10s 稳)
        self.silent_escalate_s = 12.0  # 温和握手持续零字节多久后升级脉冲复位
        # 句柄活着但零字节的宽限:板子复位后启动静默 ~1s,射频上电瞬断窗
        # (复位后 ~3.7s)也落在这段里。宽限期内不收口——收口/重开本身
        # 会给弱供电链路制造新的过渡态。
        self.first_byte_grace_s = 8.0
        self._session_started_at = 0.0

        # 音频流控(信用点):PC 侧记录"可发送字节数",发送扣减,设备上报绝对空闲量时重置
        self._audio_credit_lock = threading.Lock()
        self._audio_credit_cond = threading.Condition(self._audio_credit_lock)
        self._audio_credit = 0

        self.crc_errors = 0
        # 链路健康度:最近一次收到任意字节/心跳事件的时刻。固件每 50ms 发
        # audio.buffer 心跳,空闲超秒级 = USB 链路进入高延迟病理态(HUB 转发
        # 芯片/驱动批量滞留数据),此时命令往返可达 3~10s,需要放大超时。
        self._last_rx_mono = 0.0

    def link_idle_s(self) -> float:
        """距最近一次收到板子数据过了多少秒(心跳 50ms 一条,健康链路 <0.1s)。"""
        if self._last_rx_mono == 0.0:
            return float("inf")
        return time.monotonic() - self._last_rx_mono

    def link_sick(self) -> bool:
        """链路是否处于高延迟病理态(空闲 >1.5s 视为病;数据不丢只是迟到)。
        从未收到过数据(握手初期)不算病。"""
        idle = self.link_idle_s()
        return idle != float("inf") and idle > 1.5

    # ---- 生命周期 -------------------------------------------------

    def open(self, reset: bool = True) -> None:
        """打开串口并握手。reset=False 跳过复位相关逻辑,用于芯片未重启的
        场景(如 net 启动命令引发 USB 掉线后重新枚举,板子还在跑)。

        reset=True 的策略是"先礼后兵":板子上电自启、通常已在运行,先直接
        开句柄握手(实测任何 DTR/RTS 扰动都会把弱供电的 USB 链路踹进死相
        位);只有持续完全静默(板子卡死/被闷在下载模式)才升级为脉冲复位。
        """
        if reset and self._io is None:
            self._start_session()
            # 120s:链路死相位可持续数十秒,只能靠等,预算放大才能熬过去
            if self._handshake_tolerant_blip(total_timeout=120.0) == "silent":
                self.close()
                self._pulse_reset()
                time.sleep(self.reset_quiet_s)
                self._start_session()
                self._handshake_tolerant_blip()
        else:
            self._start_session()
            self.handshake()

    def _pulse_reset(self) -> None:
        """开门 + RTS 脉冲复位 + 恢复默认线态 + 收口。

        收尾必须把 DTR/RTS 恢复成与下次 open() 默认一致的 asserted 态:
        若留在放开态,重开瞬间两根线各跳变一次,瞬态经自动复位电路打出
        EN 脉冲,会把刚稳住的板子再复位一遍(弱供电下顺带拖掉 CH340)。
        """
        try:
            io_obj = serial.Serial(self._port_name, self._baudrate, timeout=0.1)
            # IO0=DTR 保持高(SPI启动),EN=RTS 脉冲复位
            io_obj.dtr = False
            io_obj.rts = True
            time.sleep(0.1)
            io_obj.rts = False
            io_obj.flush()
            # 恢复 open() 的默认电平(asserted),重开时零跳变
            io_obj.dtr = True
            io_obj.rts = True
            io_obj.close()
        except serial.SerialException as exc:
            raise TransportError(f"打开串口 {self._port_name} 失败: {exc}") from exc

    def _start_session(self) -> None:
        if self._io is None:
            try:
                # 注意:打开后绝不能碰 DTR/RTS(连"放开"也不行)——驱动设线的
                # 瞬态经自动复位电路会打出 EN 脉冲复位板子,启动电流冲击又把
                # 弱供电的 CH340 拖下线。默认电平两线同态,不会复位,别动它。
                self._io = serial.Serial(self._port_name, self._baudrate, timeout=0.05)
            except serial.SerialException as exc:
                raise TransportError(f"打开串口 {self._port_name} 失败: {exc}") from exc
        self._closed.clear()
        self._session_started_at = time.monotonic()
        self._rx_thread = threading.Thread(
            target=self._rx_loop, name="btphone-rx", daemon=True
        )
        self._rx_thread.start()
        # 订阅内部流控事件
        self.events.on("audio.buffer", self._on_audio_buffer)

    def _handshake_tolerant_blip(self, total_timeout: float = 60.0) -> str:
        """握手并容忍 USB 链路摆动,返回 "ready" 或 "silent"。

        容忍三类断链:句柄已死(读抛异常)、句柄僵死(有口无数据)、端口消
        失。判据是窗口期内有无真实字节流入;断链时收口静默 2s 再重建会话,
        频繁重开只会让弱供电的过渡态一直续命。若自始至终零字节且句柄从未
        报错(板子真没声音:卡死/被闷在下载模式),返回 "silent" 让调用方
        升级为脉冲复位。
        """
        from serial.tools import list_ports

        from .exceptions import WaitTimeout

        def port_present() -> bool:
            return any(p.device == self._port_name for p in list_ports.comports())

        started = time.monotonic()
        started_bytes = self.rx_bytes  # rx_bytes 跨会话累计,静默判断只看本轮增量
        deadline = started + total_timeout
        saw_broken = False
        reopen_quiet = 2.0  # 重开间隔逐次翻倍封顶 20s:频繁重开会给弱供电
        # 链路的过渡态续命,只有足够长的静默才能让 hub/驱动真正恢复。
        while True:
            baseline = self.rx_bytes
            try:
                self.events.wait_event("*", timeout=3.0)
                self._verify_ready()
                return "ready"
            except WaitTimeout:
                if time.monotonic() >= deadline:
                    if saw_broken:
                        raise TransportError(
                            "USB 链路反复断开(疑似板卡供电不足):"
                            "请换粗短 USB 线/电脑后置 USB 口/带供电的 HUB 后重试"
                        ) from None
                    raise TransportError(
                        f"设备未就绪:{total_timeout:.0f}s 内无任何事件"
                        "(检查固件是否已烧录/串口是否被占用)"
                    ) from None
                if not self.is_open:
                    saw_broken = True
                # 健康判定:句柄未断、端口在、且窗口内有真实字节流入。
                # 固件冷启动(挂载+蓝牙栈)最慢数秒,期间有字节就继续等。
                if self.is_open and port_present() and self.rx_bytes != baseline:
                    continue
                # 持续零字节且从未报错:板子真没声音,交给调用方脉冲复位
                if (
                    time.monotonic() - started >= self.silent_escalate_s
                    and self.rx_bytes == started_bytes
                    and not saw_broken
                ):
                    return "silent"
                # 句柄活着、端口在、但零字节:可能正处启动静默期或射频上电
                # 瞬断窗(复位后 ~3.7s),超过宽限才按断链收口处理。
                if (
                    self.is_open
                    and port_present()
                    and time.monotonic() - self._session_started_at < self.first_byte_grace_s
                ):
                    continue
                # 断链(异常死亡/僵死句柄/端口消失):收口后静默再试。
                self.close()
                time.sleep(reopen_quiet)
                reopen_quiet = min(reopen_quiet * 2.0, 20.0)
                if not port_present():
                    continue
                try:
                    self._start_session()
                except TransportError:
                    continue  # 打不开(枚举未完成),下一轮再试

    def _verify_ready(self) -> None:
        """握手后半程:sys.info 往返校验。"""
        last_error: Optional[Exception] = None
        for _ in range(15):
            try:
                self.request("sys.info", {}, timeout=1.5)
                return
            except (TransportError, CommandError) as exc:
                last_error = exc
                time.sleep(0.5)
        raise TransportError(f"设备已就绪但 sys.info 校验失败: {last_error}")

    def handshake(self, timeout: float = 45.0) -> None:
        """等待设备就绪。

        关键时序:打开串口触发 ESP32 复位,固件冷启动(挂载 FAT 资产分区 +
        蓝牙栈)需要数秒;若在 ROM 引导窗口就写入命令,字节会被引导阶段的
        下载检测器误认,导致芯片停在"等待下载"状态。因此先等固件主动发出的
        事件(audio.buffer 信用点每 50ms 一发,是天然的"我已就绪"信标),
        再做 sys.info 往返校验。
        """
        from .exceptions import WaitTimeout
        try:
            self.events.wait_event("*", timeout=timeout)
        except WaitTimeout as exc:
            raise TransportError(f"设备未就绪:{exc}(检查固件是否已烧录/串口是否被占用)") from exc
        self._verify_ready()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._io is not None:
            try:
                # 退出前释放 DTR/RTS,避免把板子留在钳复位/下载模式
                self._io.dtr = False
                self._io.rts = False
            except Exception:
                pass
            try:
                self._io.close()
            except Exception:
                pass
            self._io = None  # 允许 open() 再次创建串口(wait_reconnect 依赖)
        self._fail_pending("传输已关闭")
        if self._on_disconnect is not None:
            self._on_disconnect(self._port_name)

    @property
    def is_open(self) -> bool:
        return not self._closed.is_set() and self._io is not None

    # ---- 控制通道 -------------------------------------------------

    def request(self, cmd: str, args: Optional[dict] = None, timeout: float = 10.0) -> dict:
        """发送控制命令并等待响应。成功返回 result,失败抛 CommandError。

        链路处于高延迟病理态(HUB/驱动批量滞留,往返 3~10s)时自动放大
        等待预算,命令迟到但能到,不该误报失败。
        """
        if not self.is_open:
            if self._recovering:
                raise TransportError("串口链路恢复中(板子重启/USB 断链),请稍后重试")
            self._auto_recover()
        if not self.is_open:
            raise TransportError("串口未打开(自动重连未成功:检查板子/USB 线,或重新连接)")
        req_id = next(self._ctrl_seq)
        frame_payload = protocol.pack_ctrl({"id": req_id, "cmd": cmd, "args": args or {}}, req_id)
        done = threading.Event()
        with self._pending_lock:
            self._pending[req_id] = done
        try:
            self._write(frame_payload)
            # 等待期间动态判断链路病理态:心跳断流超 1.5s 说明数据被批量滞留
            # (往返 3~10s+),自动续期等待预算——命令迟到但能到,不该误报失败。
            # 另:串口链路偶发误码会静默丢帧(2026-09-01 实测空闲态丢帧率
            # 8.7%),0.6s 未见应答即同 id 重发一次——固件 v0.3.7+ 对重复 id
            # 只重发缓存应答,命令不会执行两遍;旧固件重发也基本幂等。
            start = time.monotonic()
            last_send = start
            while not done.wait(0.5):
                sick = self.link_sick()
                now = time.monotonic()
                if now - last_send >= 0.6 and not sick and self.is_open:
                    try:
                        self._write(frame_payload)
                    except TransportError:
                        pass  # 链路瞬断:交给既有断链恢复路径,不误杀等待中的请求
                    last_send = now
                if now - start >= timeout + (15.0 if sick else 0.0):
                    raise TransportError(
                        f"命令 {cmd} 响应超时({timeout}s)"
                        + (";链路高延迟(USB 批量滞留),建议重插板子/换 HUB 口" if sick else "")
                    )
            resp = self._responses.pop(req_id)
            if not resp.get("ok"):
                raise CommandError(cmd, str(resp.get("error", "unknown")))
            return resp.get("result", {})
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)
                self._responses.pop(req_id, None)

    # ---- 音频通道 -------------------------------------------------

    def send_audio(self, codec: int, data: bytes, eos: bool = False) -> None:
        if not self.is_open:
            raise TransportError("串口未打开")
        self._write(protocol.pack_frame(protocol.TYPE_AUDIO, protocol.audio_payload(codec, data, eos), next(self._audio_seq)))
        if not eos:
            with self._audio_credit_cond:
                self._audio_credit -= len(data)

    def set_audio_budget(self, buffer_size: int) -> None:
        """audio.open 成功后设置初始信用=设备缓冲大小。"""
        with self._audio_credit_cond:
            self._audio_credit = int(buffer_size)
            self._audio_credit_cond.notify_all()

    def audio_credit(self) -> int:
        with self._audio_credit_lock:
            return self._audio_credit

    def wait_audio_buffer(self, min_credit: int, timeout: float = 5.0) -> bool:
        """阻塞直到可发送信用 >= min_credit。返回 False 表示超时。"""
        deadline = time.monotonic() + timeout
        with self._audio_credit_cond:
            while self._audio_credit < min_credit:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._audio_credit_cond.wait(remaining)
            return True

    def _on_audio_buffer(self, _name: str, data: dict) -> None:
        with self._audio_credit_cond:
            self._audio_credit = int(data.get("free", 0))
            self._audio_credit_cond.notify_all()

    # ---- 内部 -----------------------------------------------------

    def _write(self, frame: bytes) -> None:
        io_obj = self._io
        if io_obj is None:
            raise TransportError("串口未打开")
        with self._write_lock:
            try:
                io_obj.write(frame)
                io_obj.flush()
            except (OSError, serial.SerialException) as exc:
                self._handle_broken(f"串口写入失败: {exc}")
                raise TransportError(str(exc)) from exc

    def _rx_loop(self) -> None:
        io_obj = self._io
        assert io_obj is not None
        try:
            while not self._closed.is_set():
                # 会话身份校验:close()+重开会清 _closed 并换新句柄,若只看
                # _closed 标志,本线程(拿着旧死句柄)会在下一次 read 抛错后
                # 调 _handle_broken 把刚建好的新会话再杀一次(僵尸 rx 竞态,
                # 2026-09 实测表现为断链后自动恢复永远失败)。被取代即退场。
                if self._io is not io_obj:
                    return
                try:
                    # 只读"已到达"的字节数,空闲时读 1 字节等首字节。不能
                    # 请求大块读:ch341ser 的挂起 ReadFile 会攒满请求字节数
                    # 才完成(总超时打不断),4KB 读 × 1.3KB/s 心跳流 = 3.3s
                    # 一批的假"高延迟"(2026-09-04 定案,tools/read_pattern_probe.py)。
                    chunk = io_obj.read(io_obj.in_waiting or 1)
                except (OSError, serial.SerialException) as exc:
                    if self._io is io_obj:
                        self._handle_broken(f"串口读取失败: {exc}")
                    return
                if not chunk:
                    continue
                self.rx_bytes += len(chunk)
                self._last_rx_mono = time.monotonic()
                for frame in self._rx_parser.feed(chunk):
                    self._dispatch(frame)
        except Exception as exc:  # 阅读线程兜底
            self._handle_broken(f"接收线程异常: {exc}")

    def _dispatch(self, frame: protocol.Frame) -> None:
        if not frame.is_ctrl:
            return  # 音频帧方向为 PC→设备,下行忽略
        try:
            obj = protocol.unpack_ctrl(frame)
        except ValueError:
            self.crc_errors += 1
            return
        if "id" in obj and ("ok" in obj):
            req_id = int(obj["id"])
            with self._pending_lock:
                self._responses[req_id] = obj
                done = self._pending.get(req_id)
                if done is not None:
                    done.set()
        elif "evt" in obj:
            self.events.emit(str(obj["evt"]), obj.get("data", {}) or {})

    def _fail_pending(self, reason: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.items())
        for _req_id, done in pending:
            with self._pending_lock:
                self._responses.setdefault(_req_id, {"ok": False, "error": reason})
            done.set()

    def _handle_broken(self, reason: str) -> None:
        was_open = self.is_open
        self._closed.set()
        # 真串口:句柄已死必须立刻丢弃,否则 close()(见 _closed 已置早退)
        # 清不掉 _io,_start_session 会复用死句柄,重连永远失败。
        # 注入后端(sim):对象有状态必须复用,不能关。
        io_obj = self._io
        if self._injected_io is None and io_obj is not None:
            self._io = None
            try:
                io_obj.dtr = False
                io_obj.rts = False
            except Exception:
                pass
            try:
                io_obj.close()
            except Exception:
                pass
        self._fail_pending(reason)
        if was_open and self._on_disconnect is not None:
            self._on_disconnect(self._port_name)

    def _auto_recover(self, quiet_s: float = 5.0) -> None:
        """收口后第一条命令的温和自救。

        场景:A2DP 推流/射频上电瞬态打断 USB,链路稍后自愈,但库若永久装死,
        用户点什么都报"串口未打开"。真机:断链句柄作废(close),走
        open(reset=True) 的强重连——blip 循环能熬过 hub/驱动的惩罚态;
        注入后端(sim):对象有状态必须复用,走轻握手。
        刚失败的 5s 内不重复尝试,防止点击风暴把多次 2 分钟重连叠在一起。
        """
        if not self._recover_lock.acquire(timeout=15.0):
            raise TransportError("串口恢复进行中(另一线程正在重连),请稍后重试")
        self._recovering = True
        try:
            if self.is_open:
                return
            if time.monotonic() - self._recover_stamp < quiet_s:
                return
            self._recover_stamp = time.monotonic()
            try:
                if self._injected_io is None:
                    self.close()
                    self.open(reset=True)
                else:
                    self.open(reset=False)
            except Exception:
                pass  # 没救回来:让 request 抛出明确错误,下次命令再试
        finally:
            self._recovering = False
            self._recover_lock.release()
