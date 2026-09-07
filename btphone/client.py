"""BtPhone 客户端:脚本全控的"虚拟手机"。

用法::

    from btphone import BtPhone

    phone = BtPhone("COM3")
    phone.set_name("VPHONE-01")
    phone.set_discoverable(True, timeout_s=60)
    phone.music.play(files=[r"D:\\曲库\\a.mp3", r"D:\\曲库\\b.mp3"], loop=True)
    phone.wait_event("hfp.at", predicate=lambda d: d.get("at") == "ATA", timeout=30)
    phone.calls.incoming("13800138000")
"""

from __future__ import annotations

import functools
import os
import threading
import time
from typing import Callable, List, Optional, Union

import numpy as np

from . import codec_adpcm, media, protocol
from .events import EventBus
from .exceptions import BtPhoneError, TransportError, WaitTimeout
from .transport import SerialTransport

A2DP_RATE = 44100
A2DP_CHANNELS = 2
HFP_RATE = 16000
HFP_CHANNELS = 1

_CHUNK_SAMPLES = 2048  # 每帧推送的样本数(每声道)


def _operation(func):
    """公开 API 装饰器:进入时记录事件位置,作为后续 wait_event 的锚点,
    使"一次操作期间发出的事件"都可被等到、且不与更早事件混淆。"""

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        phone = getattr(self, "_phone", self)
        phone._cmd_mark = phone.events.position()
        return func(self, *args, **kwargs)

    return wrapper


class _Namespace:
    def __init__(self, phone: "BtPhone") -> None:
        self._phone = phone

    def _req(self, cmd: str, args: Optional[dict] = None, timeout: float = 10.0) -> dict:
        return self._phone._req(cmd, args, timeout)


class Pairing(_Namespace):
    """配对控制,含配对失败模拟。"""

    @_operation
    def set_mode(self, mode: str) -> dict:
        """mode: auto(自动接受)/ reject(拒绝配对)/ wrong_pin(回错误PIN)/ timeout(不响应)/ manual(等脚本 confirm)。"""
        return self._req("pair.mode", {"mode": mode})

    @_operation
    def confirm(self, accept: bool) -> dict:
        return self._req("pair.confirm", {"accept": bool(accept)})

    @_operation
    def policy(self, auto_accept: Optional[bool] = None, auto_reconnect: Optional[bool] = None) -> dict:
        args = {}
        if auto_accept is not None:
            args["auto_accept"] = bool(auto_accept)
        if auto_reconnect is not None:
            args["auto_reconnect"] = bool(auto_reconnect)
        return self._req("pair.policy", args)

    @_operation
    def list_bonded(self) -> list:
        return self._req("pair.list", {}).get("devices", [])

    @_operation
    def remove_bond(self, mac: str) -> dict:
        return self._req("pair.remove", {"mac": mac.upper()})


class Connection(_Namespace):
    """连接控制(双向)。"""

    @_operation
    def connect(self, mac: str, profiles: Optional[List[str]] = None) -> dict:
        return self._req("conn.connect", {"mac": mac.upper(), "profiles": profiles or ["a2dp", "hfp"]}, timeout=20)

    @_operation
    def disconnect(self, profile: str = "all") -> dict:
        return self._req("conn.disconnect", {"profile": profile})

    @_operation
    def status(self) -> dict:
        return self._req("conn.status", {})


class Music(_Namespace):
    """音乐播放:电脑曲库经 Type-C 实时推流,AVRCP 元数据推送,切歌联动。"""

    @_operation
    def play(
        self,
        file: Optional[str] = None,
        files: Optional[List[str]] = None,
        loop: bool = False,
        metadata: Optional[dict] = None,
    ) -> dict:
        """开始播放。传 file 单曲或 files 播放列表;loop 循环;metadata 覆盖歌曲信息。"""
        if self._phone._worker is not None:
            self.stop()
        playlist = files if files else ([file] if file else None)
        if not playlist:
            raise BtPhoneError("play() 需要 file 或 files 参数")
        worker = _PlaylistWorker(self._phone, [str(p) for p in playlist], loop=loop, metadata=metadata)
        self._phone._worker = worker
        worker.start()
        return {"tracks": len(playlist), "loop": loop}

    @_operation
    def pause(self) -> dict:
        return self._req("music.pause", {})

    @_operation
    def resume(self) -> dict:
        return self._req("music.resume", {})

    @_operation
    def stop(self) -> dict:
        worker = self._phone._worker
        if worker is not None:
            worker.abort()
            self._phone._worker = None
        return self._req("music.stop", {})

    @_operation
    def next(self) -> dict:
        worker = self._phone._worker
        if worker is not None:
            worker.advance(1)
        return {"ok": True}

    @_operation
    def prev(self) -> dict:
        worker = self._phone._worker
        if worker is not None:
            worker.advance(-1)
        return {"ok": True}

    @_operation
    def set_track_info(
        self,
        title: str,
        artist: str = "",
        album: str = "",
        duration_ms: int = 0,
        track_no: int = 0,
        total_tracks: int = 0,
    ) -> dict:
        return self._req(
            "avrcp.metadata",
            {"title": title, "artist": artist, "album": album, "duration_ms": duration_ms,
             "track_no": track_no, "total_tracks": total_tracks},
        )


class Calls(_Namespace):
    """通话模拟:呼入/呼出/接听/挂断/通话语音。"""

    @_operation
    def incoming(self, number: str, ring: bool = True) -> dict:
        """模拟来电,车机应响铃;车机接听后收到 hfp.at{"at":"ATA"} 事件。"""
        result = self._req("hfp.incoming", {"number": number})
        if ring:
            self._req("hfp.ring", {"on": True})
        return result

    @_operation
    def answer(self) -> dict:
        """"手机侧"接听(把通话置为激活,模拟自动接听场景)。"""
        return self._req("hfp.answer", {})

    @_operation
    def hangup(self) -> dict:
        return self._req("hfp.hangup", {})

    @_operation
    def dial(self, number: str) -> dict:
        """模拟手机拨出,车机应显示呼出界面。"""
        return self._req("hfp.dial", {"number": number})

    @_operation
    def set_voice(self, file: Optional[str]) -> None:
        """设置接通后循环播放的语音文件(None 关闭)。"""
        self._phone._voice_file = file

    @_operation
    def voice_start(self) -> dict:
        return self._req("hfp.voice", {"on": True})

    @_operation
    def voice_stop(self) -> dict:
        return self._req("hfp.voice", {"on": False})


class Net(_Namespace):
    """WiFi(STA/AP/扫描/状态)。NAT 网络共享与 BT PAN 在 Phase 2。"""

    @_operation
    def wifi_sta(self, ssid: str, password: str = "") -> dict:
        return self._req("net.wifi_sta", {"ssid": ssid, "password": password}, timeout=20)

    @_operation
    def wifi_scan(self, timeout: float = 15.0) -> list:
        """扫描周围 AP,返回 [{ssid, rssi, auth}],阻塞约 1.5~3s。"""
        return self._req("net.wifi_scan", {}, timeout=timeout).get("aps", [])

    @_operation
    def ap_start(self, ssid: str, password: str = "") -> dict:
        return self._req("net.ap_start", {"ssid": ssid, "password": password}, timeout=20)

    @_operation
    def ap_stop(self) -> dict:
        return self._req("net.ap_stop", {})

    @_operation
    def status(self) -> dict:
        return self._req("net.status", {})

    @_operation
    def tether_pan(self, on: bool) -> dict:
        return self._req("net.tether_pan", {"on": bool(on)}, timeout=20)


class Lyrics(_Namespace):
    """歌词推送(Phase 3;固件 vendor 钩子就绪前固件会返回未实现)。"""

    @_operation
    def push(self, lines: List[dict], title: str = "") -> dict:
        """lines: [{"t": 毫秒, "text": "歌词行"}, ...]"""
        return self._req("lyrics.push", {"title": title, "lines": lines}, timeout=15)


class _PlaylistWorker(threading.Thread):
    """后台推流线程:按播放列表逐曲推流,响应暂停/切歌。"""

    def __init__(self, phone: "BtPhone", files: List[str], loop: bool, metadata: Optional[dict]) -> None:
        super().__init__(name="btphone-music", daemon=True)
        self._phone = phone
        self._files = files
        self._loop = loop
        self._override_metadata = metadata or {}
        self._index = 0
        self._abort_flag = threading.Event()
        self._advance_flag = threading.Event()
        self._advance_step = 1
        self._track_mark = 0  # 本曲开始播放前的事件位置,防止匹配到上一曲的 music.ended

    def abort(self) -> None:
        self._abort_flag.set()

    def advance(self, step: int) -> None:
        self._advance_step = step
        self._advance_flag.set()

    def run(self) -> None:
        phone = self._phone
        events = phone.events
        cancel_car_next = events.on("avrcp.cmd", lambda n, d: self.advance(1) if d.get("cmd") == "next" else None)
        cancel_car_prev = events.on("avrcp.cmd", lambda n, d: self.advance(-1) if d.get("cmd") == "prev" else None)
        try:
            while not self._abort_flag.is_set():
                path = self._files[self._index]
                try:
                    self._play_one(path)
                except BtPhoneError as exc:
                    events.emit("music.error", {"file": path, "error": str(exc)})
                    break
                if self._abort_flag.is_set():
                    break
                ended = self._wait_track_end()
                if self._abort_flag.is_set():
                    break
                step = self._consume_advance()
                if step is None:
                    if ended == "eos":
                        step = 1
                    else:  # 被外部 stop
                        break
                nxt = self._next_index(self._index, step)
                if nxt is None:  # 播放到列表末尾且不循环
                    break
                self._index = nxt
        finally:
            cancel_car_next()
            cancel_car_prev()
            if phone._worker is self:
                phone._worker = None
            phone.events.emit("playlist.done", {"index": self._index})

    def _next_index(self, idx: int, step: int) -> Optional[int]:
        """返回下一曲索引;返回 None 表示播放结束。"""
        n = len(self._files)
        nxt = idx + step
        if nxt >= n:
            return 0 if self._loop else None  # 播完列表且不循环:结束
        if nxt < 0:
            return n - 1 if self._loop else 0  # 首曲向前:循环回末尾,否则重播首曲
        return nxt

    def _consume_advance(self) -> Optional[int]:
        """有挂起的切歌请求则消费,返回步长;否则 None。"""
        if self._advance_flag.is_set():
            self._advance_flag.clear()
            return self._advance_step
        return None

    def _wait_track_end(self) -> str:
        """等待本曲自然结束(music.ended)或被打断(advance/abort)。"""
        while not self._abort_flag.is_set():
            if self._advance_flag.is_set():
                return "advance"
            try:
                self._phone.events.wait_event(
                    "music.ended", timeout=0.2, after=self._track_mark
                )
                if not self._advance_flag.is_set():
                    return "eos"
            except WaitTimeout:
                continue
        return "abort"

    def _play_one(self, path: str) -> None:
        from .client_audio import push_audio_stream

        phone = self._phone
        decoded = media.decode_audio(path, rate=A2DP_RATE, channels=A2DP_CHANNELS)
        adpcm = codec_adpcm.encode(decoded.pcm, decoded.channels)
        meta = {
            "title": os.path.splitext(os.path.basename(path))[0],
            "artist": "", "album": "",
            "duration_ms": decoded.duration_ms,
            "track_no": self._index + 1, "total_tracks": len(self._files),
        }
        meta.update(self._override_metadata)
        phone._req("avrcp.metadata", meta)
        open_result = phone._req("audio.open", {
            "sink": "a2dp", "rate": decoded.rate, "channels": decoded.channels, "codec": "adpcm",
        })
        phone._transport.set_audio_budget(int(open_result.get("buffer_size", 65536)))
        self._track_mark = phone.events.position()  # 本曲事件起点(防串曲)
        # a2dp 掉线重连窗口内 play 会被 fail-fast 拒绝;固件自动重连(3s/6s/12s
        # 退避,加上连接尝试本身最长约 40s)期间等链路回来再试,而不是整曲放弃。
        # 窗口 5 次 × 12s ≈ 60s,完整覆盖退避序列。
        from .exceptions import CommandError, TransportError
        for attempt in range(5):
            try:
                phone._req("music.play", {})
                break
            except CommandError as exc:
                if "a2dp not connected" not in str(exc) or attempt == 4:
                    raise
                deadline = time.monotonic() + 12.0
                while time.monotonic() < deadline:
                    if self._abort_flag and self._abort_flag.is_set():
                        raise
                    try:
                        conn = (phone._req("conn.status", {}, timeout=4).get("connections") or {})
                        if str(conn.get("a2dp")) == "connected":
                            break
                    except Exception:
                        pass
                    time.sleep(0.5)
            except TransportError:
                # 射频上电/USB 瞬断(弱供电台架已知病理,数秒自愈)或恢复进行中:
                # 不放弃整曲,等链路回来继续重试
                if attempt == 4:
                    raise
                time.sleep(1.0)
        phone.events.emit("music.started", {"file": path, "title": meta["title"], "index": self._index})
        push_audio_stream(
            phone._transport, adpcm,
            abort=self._abort_flag, on_progress=phone._on_stream_progress,
        )
        # 注意:推完不发送 music.stop——设备把缓冲播完才发 music.ended;
        # 中途停止由 Music.stop() 负责下发 music.stop 命令。


class BtPhone:
    """虚拟手机客户端。"""

    def __init__(
        self,
        port: str,
        baudrate: int = 2_000_000,
        io: Optional[object] = None,
        connect: bool = True,
        on_disconnect: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._transport = SerialTransport(port, baudrate=baudrate, io=io, on_disconnect=on_disconnect)
        self.events = self._transport.events
        self._worker: Optional[_PlaylistWorker] = None
        self._voice_file: Optional[str] = None
        self._voice_thread: Optional[threading.Thread] = None
        self._voice_stop = threading.Event()
        self._evt_cursor = 0  # wait_event 读取游标:每次成功匹配后推进,避免重复匹配旧事件
        self._cmd_mark = 0    # 最近一次命令发出前的事件位置:wait_event 不会匹配命令之前的旧事件
        self._progress_lock = threading.Lock()
        self._progress_consumed = 0
        self.pairing = Pairing(self)
        self.conn = Connection(self)
        self.music = Music(self)
        self.calls = Calls(self)
        self.net = Net(self)
        self.lyrics = Lyrics(self)
        self.events.on("bt.hfp.state", self._on_hfp_state)
        if connect:
            self.open()

    # ---- 生命周期 -------------------------------------------------

    def open(self, reset: bool = True) -> "BtPhone":
        self._transport.open(reset=reset)
        return self

    def wait_reconnect(self, timeout: float = 60.0, poll: float = 1.0) -> "BtPhone":
        """USB 掉线后等待端口重新枚举,并无复位重连恢复会话。

        场景:net.ap_start / net.wifi_sta 的射频上电瞬间,弱供电链路
        (细 USB 线/弱端口)上 CH340 会从总线掉线并自动恢复(约 1~30s),
        板子本身不重启。此时命令报"串口读取失败",用本方法跨过暂态:

            try:
                phone.net.ap_start("VPHONE-AP", "12345678")
            except BtPhoneError:
                phone.wait_reconnect()      # 等重新枚举,无复位接管
            phone.net.status()              # 会话继续
        """
        from serial.tools import list_ports

        from .exceptions import TransportError
        self._transport.close()
        deadline = time.monotonic() + timeout
        port = self._transport._port_name
        while True:
            if any(p.device == port for p in list_ports.comports()):
                try:
                    return self.open(reset=False)
                except TransportError:
                    pass  # 枚举尚未稳定,继续等
            if time.monotonic() >= deadline:
                raise TransportError(f"等待 {port} 重新枚举超时({timeout}s)")
            time.sleep(poll)

    def close(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.abort()
            self._worker = None
        self._transport.close()

    def __enter__(self) -> "BtPhone":
        if not self._transport.is_open:
            self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 事件 -----------------------------------------------------

    def on_event(self, pattern: str, callback: Callable[[str, dict], None]) -> Callable[..., None]:
        return self.events.on(pattern, callback)

    def wait_event(
        self,
        pattern: str,
        timeout: Optional[float] = 10.0,
        predicate: Optional[Callable[[dict], bool]] = None,
        since: Optional[int] = None,
    ) -> dict:
        """等待事件并返回数据。

        默认锚点 = max(读取游标, 最近一次命令的发出位置):既能等到命令
        执行期间发出的事件,又不会匹配到更早的旧事件;每次成功匹配后游标
        自动推进。复杂并发场景请用 since 显式指定起点(取值来自
        phone.events.position())。
        """
        if since is None:
            since = max(self._evt_cursor, self._cmd_mark)
        data, seq = self.events.wait_event_ext(pattern, timeout=timeout, predicate=predicate, after=since)
        self._evt_cursor = max(self._evt_cursor, seq)
        return data

    # ---- 系统与蓝牙基础 -------------------------------------------

    @_operation
    def info(self) -> dict:
        return self._req("sys.info", {})

    @_operation
    def reset(self) -> dict:
        """重启板子(保留 NVS:名字/绑定/回连对象)。注意:重启瞬间串口会断,
        本命令大概率以 TransportError 收尾,属正常现象。"""
        return self._req("sys.reset", {}, timeout=15)

    @_operation
    def factory_reset(self) -> dict:
        """恢复出厂:擦除 NVS(名字/绑定/回连对象)后重启。调试配对流程用。"""
        return self._req("sys.factory", {}, timeout=15)

    @_operation
    def set_name(self, name: str) -> dict:
        return self._req("bt.set_name", {"name": name})

    @_operation
    def set_discoverable(self, on: bool = True, timeout_s: int = 0) -> dict:
        mode = "disc_conn" if on else "conn"
        return self._req("bt.set_discoverable", {"mode": mode, "timeout_s": int(timeout_s)})

    @_operation
    def status(self) -> dict:
        return self._req("conn.status", {})

    def _req(self, cmd: str, args: Optional[dict] = None, timeout: float = 10.0) -> dict:
        return self._transport.request(cmd, args, timeout=timeout)

    def request(self, cmd: str, args: Optional[dict] = None, timeout: float = 10.0) -> dict:
        """直接发一条命令(诊断/新命令还没包装成方法时用),如 phone.request("bt.rssi")。"""
        return self._req(cmd, args, timeout=timeout)

    # ---- 内部 -----------------------------------------------------

    def _on_stream_progress(self, consumed_bytes: int) -> None:
        with self._progress_lock:
            self._progress_consumed = consumed_bytes

    def _on_hfp_state(self, _name: str, data: dict) -> None:
        state = data.get("state")
        if state == "active" and self._voice_file:
            if not (self._voice_thread and self._voice_thread.is_alive()):
                self._voice_stop.clear()
                self._voice_thread = threading.Thread(
                    target=self._voice_loop, name="btphone-voice", daemon=True
                )
                self._voice_thread.start()
        elif state in ("idle", "ended", "disconnected"):
            self._voice_stop.set()

    def _voice_loop(self) -> None:
        """通话激活后循环推流通话语音(16k 单声道),挂断自动停止。"""
        from .client_audio import push_audio_stream

        try:
            decoded = media.decode_audio(self._voice_file, rate=HFP_RATE, channels=HFP_CHANNELS)
        except BtPhoneError as exc:
            self.events.emit("voice.error", {"error": str(exc)})
            return
        adpcm = codec_adpcm.encode(decoded.pcm, decoded.channels)
        while not self._voice_stop.is_set():
            try:
                result = self._req("audio.open", {
                    "sink": "hfp", "rate": HFP_RATE, "channels": HFP_CHANNELS, "codec": "adpcm",
                })
                self._transport.set_audio_budget(int(result.get("buffer_size", 65536)))
                push_audio_stream(self._transport, adpcm, abort=self._voice_stop)
                self._req("audio.stop", {})
            except BtPhoneError:  # 串口关闭/断连/命令失败:退出语音循环
                break
            if self._voice_stop.is_set():
                break
            self.events.emit("voice.looped", {})
