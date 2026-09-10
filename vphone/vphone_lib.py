#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vphone 蓝牙测试库 —— 把"虚拟手机"(vphone APK)的全部能力封装成 Python 接口。

一台 Android 手机装上 vphone APK 后, 即成为 PC 可远程驱动的蓝牙测试终端:
  门B(媒体): set_track/playlist/play/pause/next/prev + upload_audio(电脑音频→车机播放)
             + 车机按键回流(expect_event CAR_PLAY/CAR_NEXT/...)
  门C(电话): incoming/dial/answer/hangup/hold/swap/dtmf/audio_bt + call_audio(通话中自定义音频)
             多路: 呼叫等待(通话中 incoming)/接听自动保持/切换 swap/选择性挂断 hangup(number=)/
             各路独立"对端音频"(切换跟随, HFP 单 SCO 同真机)
             + 车机接听/挂断回流(expect_event CAR_ANSWER/CAR_HANGUP/CAR_DIAL/...)
  联系人:    contacts_load(批量1w)/contacts_import(自定义)/contacts_clear —— PBAP 测车机通讯录
  蓝牙:      bt_scan/bt_bond/bt_unpair/bt_reconnect/bt_state/bt_enable (配对不出App)
  部署:      install() 更新APK并自动拉起 + grant_perms() 授权 + enable_autoconfirm()

── API 稳定性约定 ──
  · 全部公开方法 keyword-only(签名带 *): 后续版本插入新参数不会破坏既有调用;
  · 事件断言三层: events()(快照,非阻塞) / wait_event()(阻塞原语,超时返回None)
    / expect_event()+expect_no_event()(断言版, 超时/命中抛 VPhoneTimeoutError, 消息带上下文);
  · VEvent 为 frozen dataclass, 字段只增不改名(id/ts/type/src/detail)。

传输: 默认 adb forward(USB, 127.0.0.1:18800 → 手机 8800); 同 WiFi 可 base="http://手机IP:8800"。
兼容: start_events()/wait_for() 收 logcat 文本, 旧脚本保留。

示例:
    from vphone_lib import VPhone
    vp = VPhone(serial="SN_PHONE_A", wait_timeout=20)
    mark = vp.event_watermark()            # 水位: 之后发生的事才算数
    vp.incoming(number="13800138000")
    vp.expect_event(evt_type="CAR_ANSWER", since=mark)   # 有人在车机上按了接听(超时抛异常)
    vp.hangup()
    vp.upload_audio(path="D:/music/demo.mp3"); vp.play_audio_files(paths=["D:/music/demo.mp3"])
    vp.expect_event(evt_type=("CAR_NEXT", "CAR_PREV"))   # 车机上按了上一曲/下一曲
    vp.contacts_load(count=10000)                          # 1w 联系人→车机通讯录压测
"""
import dataclasses
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

PKG = "com.bt.vphone"
REMOTE_PORT = 8800
DEFAULT_LOCAL_PORT = 18800   # PC 侧 forward 端口(避开本机 8800 常见占用)
ACCOUNT_ARGS = f"{PKG}/.VPhoneConnectionService VPHONE 0".split()

__version__ = "0.8.0"   # 单一版本真源: pyproject.toml 动态引用此处(dynamic attr)


# Windows: GUI(windowed exe)无控制台时, 每个 adb 子进程都会新弹一个黑窗 ——
# 所有子进程统一带 CREATE_NO_WINDOW(仅 Windows 有此标志; 输出仍走管道不受影响)。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class VPhoneError(RuntimeError):
    pass


class VPhoneTimeoutError(VPhoneError):
    """断言类 API(expect_event/expect_no_event)超时/意外命中时抛出 —— VPhoneError 子类,
    既有 `except VPhoneError` 兜底逻辑不受影响。"""


@dataclasses.dataclass(frozen=True)
class VEvent:
    """结构化事件(手机端 EventLog → /events 拉取)的固定形态。
    字段契约: 只增不改名 —— 断言代码可长期依赖。"""
    id: int        # 递增序号, 兼作水位(events(since=id) 取之后的事件)
    ts: int        # 手机时钟 epoch 毫秒
    type: str      # 事件类型(英文稳定): CAR_ANSWER/CAR_NEXT/BT_A2DP_CONNECTED/... 全表见 docs/vphone-API.md
    src: str       # 来源: car=车机按键  cmd=PC指令  app=App内部  bt=蓝牙栈  sys=系统Telecom侧
    detail: str    # 人读中文描述

    @property
    def time(self) -> str:
        """本地时间 HH:MM:SS(按 PC 时区换算, 人读用)。"""
        return time.strftime("%H:%M:%S", time.localtime(self.ts / 1000))

    def __str__(self):
        return f"#{self.id} {self.time} {self.type} [{self.src}] {self.detail}"

    @classmethod
    def _of(cls, d: dict) -> "VEvent":
        """从 /events 的 JSON dict 构造(缺字段容错为空值, 不抛)。"""
        return cls(id=int(d.get("id") or 0), ts=int(d.get("ts") or 0),
                   type=str(d.get("type") or ""), src=str(d.get("src") or ""),
                   detail=str(d.get("detail") or ""))


def default_apk():
    """默认安装包查找链: exe/脚本自带 → 源码仓 apk/ → pip 安装的 vphone_data 包 → gradle 产物。"""
    base = os.path.dirname(os.path.abspath(__file__))
    for d in _bundle_dirs() + [base]:
        hits = sorted(
            glob.glob(os.path.join(d, "apk", "*.apk")),
            key=os.path.getmtime, reverse=True)
        if hits:
            return hits[0]
    # pip install 装的 wheel: APK 在 vphone_data 包里(importlib.resources 定位, 免猜 site-packages)
    try:
        import importlib.resources
        with importlib.resources.files("vphone_data").joinpath("apk") as d:
            hits = sorted(p for p in d.iterdir() if p.name.endswith(".apk"))
            if hits:
                return str(hits[-1])
    except Exception:
        pass
    b = os.path.join(base, "app", "build", "outputs", "apk", "debug", "app-debug.apk")
    return b if os.path.isfile(b) else None


# 短名 → HTTP 路径(与 APK Dispatcher 路由对齐; 短名同时用于广播别名)。
# 分组: 电话(门C) / 媒体(门B) / 蓝牙 / 联系人 / 系统。改路由要同时改 APK Dispatcher。
PATHS = {
    "status": "/status",
    # ---- 电话(门C) ----
    "incoming": "/call/incoming",
    "dial": "/call/dial",
    "answer": "/call/answer",
    "hangup": "/call/hangup",
    "hold": "/call/hold",
    "swap": "/call/swap",
    "dtmf": "/call/dtmf",
    "audio_bt": "/call/audio-bt",
    "auto_outgoing": "/call/auto-outgoing",
    "call_audio": "/call/audio",
    # ---- 媒体(门B) ----
    "track": "/media/track",
    "play": "/media/play",
    "pause": "/media/pause",
    "next": "/media/next",
    "prev": "/media/prev",
    "media_status": "/media/status",   # GUI 进度轮询用(输出一行 key=value 文本)
    "silence": "/media/silence",
    "autoadvance": "/media/autoadvance",
    "playlist": "/media/playlist",
    "jump": "/media/jump",
    "seek": "/media/seek",
    "media_upload": "/media/upload",   # 仅 http_post 直达(二进制 POST, 无广播形态)
    "files": "/media/files",
    "diag": "/media/diag",
    "del": "/media/del",
    # ---- 蓝牙 ----
    "bt_state": "/bt/state",
    "scan": "/bt/scan",
    "scan_result": "/bt/scan-result",
    "bond": "/bt/bond",
    "unpair": "/bt/unpair",
    "bt_disconnect": "/bt/disconnect",
    "reconnect": "/bt/reconnect",
    "allow_car": "/bt/allow-car",
    "bt_name": "/bt/name",
    "bt_enable": "/bt/enable",
    # ---- 联系人 ----
    "contacts_load": "/contacts/load",
    "contacts_import": "/contacts/import",
    "contacts_clear": "/contacts/clear",
    "contacts_count": "/contacts/count",
    "contacts_status": "/contacts/status",
    # ---- 系统 ----
    "events": "/events",              # 仅 HTTP(JSON 拉取, 无广播形态)
}


def _bundle_dirs():
    """exe 自带资源的候选目录: PyInstaller onefile 解包目录(_MEIPASS) + exe 所在目录。
    (打成 exe 后 adb/apk 都内嵌在这两处, 换机器免装 adb)"""
    dirs = []
    if getattr(sys, "frozen", False):
        m = getattr(sys, "_MEIPASS", "")
        if m:
            dirs.append(m)
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    return dirs


def _find_adb(adb="adb"):
    """定位 adb.exe: 调用方显式传的可执行路径 → exe 内嵌(版本可控, 换机器免装)
    → LOCALAPPDATA SDK → 交给 PATH。"""
    if adb != "adb" and os.path.isfile(adb):
        return adb
    for d in _bundle_dirs():
        cand = os.path.join(d, "adb.exe")
        if os.path.isfile(cand):
            return cand
    if os.path.isfile(adb):
        return adb
    for cand in (
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk",
                     "platform-tools", "adb.exe"),
    ):
        if os.path.isfile(cand):
            return cand
    return adb


class VPhone:
    """一台运行 vphone APK 的手机。所有方法返回手机端的结果文本(中文)。"""

    def __init__(self, *, serial=None, base=None, port=DEFAULT_LOCAL_PORT,
                 adb="adb", autostart=True, wait_timeout=15):
        """serial=手机序列号(多设备必填; base 直连 WiFi 时不需要)。
        wait_timeout=断言类方法(wait_event/expect_event/expect_no_event)的默认超时秒数
        —— 慢台架构造时调一次全局生效, 调用处仍可用 timeout=/within= 单点覆盖。
        注意: 不支持无限等待; timeout=0 语义是"只查一次快照立即返回"(非阻塞探一眼)。"""
        self.adb_path = _find_adb(adb)
        self.serial = serial  # None → _adb 不带 -s; 多设备时在 _pick_serial 里选定
        if not self.serial:
            self.serial = self._pick_serial()
        self._base = base
        self._port = port
        self._forward_ok = False
        self.wait_timeout = wait_timeout
        if not base:
            self._setup_forward()
        if autostart:
            self.start()

    # ================= 底层 =================

    def _adb(self, *args, timeout=30):
        """执行一条 adb 命令, 返回 CompletedProcess。
        默认 30s 超时 —— 无超时的话 adb/USB 卡死会把构造函数、乃至整个 GUI 永久挂起。
        超时抛 VPhoneError(install 等需要区分超时的调用方自行捕获)。"""
        cmd = [self.adb_path] + (["-s", self.serial] if self.serial else []) + list(args)
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout,
                                  creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            raise VPhoneError(
                f"adb {' '.join(a for a in args[:4])} 超时({timeout}s): 设备掉线/USB异常?") from e

    def _pick_serial(self):
        """自动选目标手机: 恰好一台在线 → 直接用; 多台 → 读环境变量 VPHONE_SERIAL。
        注意: 有设备但 unauthorized/offline 时不在此列 —— 报错里提示看手机授权弹窗。"""
        out = self._adb("devices", timeout=10).stdout
        all_lines = [l for l in out.splitlines()
                     if l.strip() and not l.startswith("List") and l.split()]
        serials = [l.split()[0] for l in all_lines if l.split()[-1] == "device"]
        if not serials and all_lines:
            states = "; ".join(f"{l.split()[0]}={l.split()[-1]}" for l in all_lines)
            raise VPhoneError(f"设备在线但不可用({states}): 手机上确认 USB 调试授权弹窗")
        if len(serials) == 1:
            return serials[0]
        env = os.environ.get("VPHONE_SERIAL")
        if env in serials:
            return env
        raise VPhoneError(
            f"需要指定手机序列号 --serial(手机, 不是车机): {', '.join(serials) or '无设备'}")

    def _setup_forward(self):
        """建 adb forward(本机 port → 手机 8800), USB 控制通道的前提。
        幂等: 本设备该端口已转发就跳过 —— 重复 rebind 会让 adb server 短暂拒接新连接,
        紧随其后创建的 logcat 流直接 "read: unexpected EOF!" 死掉(GUI 重连场景实测),
        表现为重连后 logcat 事件通道假死。"""
        listed = self._adb("forward", "--list", timeout=10).stdout
        for line in listed.splitlines():
            parts = line.split()
            if (len(parts) >= 3 and parts[0] == self.serial
                    and parts[1] == f"tcp:{self._port}"):
                self._forward_ok = True
                return
        r = self._adb("forward", f"tcp:{self._port}", f"tcp:{REMOTE_PORT}", timeout=10)
        if r.returncode != 0:
            raise VPhoneError(f"adb forward 失败: {r.stderr.strip()}")
        self._forward_ok = True

    @property
    def base(self):
        """控制面基地址: 直连 WiFi 时为传入的 base, 否则走 adb forward 本机端口。"""
        return self._base or f"http://127.0.0.1:{self._port}"

    def http(self, path, *, timeout=6, **params) -> str:
        """HTTP 直连手机控制面, 返回纯文本结果; 失败抛 VPhoneError(保留原始异常)。
        path 保留位置传法(单一稳定判别参数, 同 open(path)); 其余全部 keyword-only。
        仅此方法+http_post 走 HTTP —— 无广播兜底(见 cmd() 的双通道契约)。"""
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except Exception as e:
            raise VPhoneError(f"HTTP 不通[{path}]: {e} (服务起了吗? adb forward 通吗?)") from e

    def http_post(self, path, data: bytes, *, timeout=120, **params) -> str:
        """POST 二进制体(/media/upload 推音频用); 失败抛 VPhoneError。"""
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except Exception as e:
            raise VPhoneError(f"HTTP POST 不通[{path}]: {e}") from e

    def broadcast(self, cmd, **params) -> str:
        """显式广播兜底通道(必须 -p 指定包: Android 8+ 隐式广播收不到)。
        广播无返回值 → 发出后睡 1.2s, 从 logcat 最新 [adb] 行取结果。
        已知竞态: 若 1.2s 内 App 未写日志, 可能取到上一条命令的 [adb] 行
        (主线 HTTP 同步返回无此问题, 此通道仅为兜底)。adb 层失败直接抛 VPhoneError。"""
        args = ["shell", "am", "broadcast", "-a", f"{PKG}.CMD", "-p", PKG,
                "--es", "cmd", cmd]
        for k, v in params.items():
            args += ["--es", k, str(v)]
        r = self._adb(*args, timeout=15)
        if r.returncode != 0:
            raise VPhoneError(
                f"广播发送失败[{cmd}]: {(r.stderr or r.stdout).strip()[:200]}")
        time.sleep(1.2)
        out = self._adb("logcat", "-d", "-s", "VPhone", "-t", "30", timeout=15).stdout
        for line in reversed(out.splitlines()):
            if "[adb]" in line:
                return line.split("[adb]", 1)[1].strip()
        return "(广播已发但无 [adb] 日志: App 可能处于 stopped 态, 先 am start 启动一次)"

    def cmd(self, cmd, **params) -> str:
        """控制命令统一入口: HTTP 优先, 失败自动降级 adb 广播。cmd=短名或完整路径。
        ── 双通道契约(哪些方法有广播兜底) ──
        · 双通道(经本方法): 电话/媒体/蓝牙/联系人全部触发类方法;
        · 仅 HTTP: http()/http_post()/upload_audio/media_status/contacts_count/
          events()/wait_event() 及各轮询 —— 二进制 POST 和 JSON 拉取没有广播形态;
        · 仅 adb: start()/enable_account()/enable_autoconfirm()/grant_perms()/launch()
          (装机期 HTTP 可能未就绪)。
        降级代价: HTTP 6s 超时 + 广播 sleep(1.2)+logcat ≈ 8~10s/次。"""
        path = cmd if cmd.startswith("/") else PATHS.get(cmd, "/" + cmd)
        try:
            return self.http(path, **params)
        except VPhoneError:
            return self.broadcast(cmd, **params)

    # ================= 生命周期/账号 =================

    def start(self) -> str:
        """拉起手机端常驻服务(HTTP+MediaSession+蓝牙引擎)。幂等。"""
        return self.broadcast("status")

    def enable_account(self) -> str:
        """启用电话账号 + 设为默认去电账号(车机 ATD 拨出才会进 VPhone 而非 SIM)。"""
        r1 = self._adb("shell", "telecom", "set-phone-account-enabled", *ACCOUNT_ARGS, timeout=15)
        r2 = self._adb("shell", "telecom", "set-user-selected-outgoing-phone-account",
                       *ACCOUNT_ARGS, timeout=15)
        return "set-phone-account-enabled: {}\nset-user-selected-outgoing: {}".format(
            (r1.stdout + r1.stderr).strip() or "(空)",
            (r2.stdout + r2.stderr).strip() or "(空)")

    A11Y_COMP = f"{PKG}/.AutoPairService"

    def enable_autoconfirm(self) -> str:
        """启用配对弹窗自动确认(无障碍服务; 一次性, 保留已有无障碍服务)。
        之后车机配对全程零人工 —— PIN 模式 App 静默应答, 弹窗模式自动点「配对」。"""
        cur = self._adb("shell", "settings", "get", "secure",
                        "enabled_accessibility_services", timeout=15).stdout.strip()
        comp = self.A11Y_COMP
        if comp in cur:
            return "配对自动确认已是启用状态"
        new = comp if not cur else cur + ":" + comp
        self._adb("shell", "settings", "put", "secure",
                  "enabled_accessibility_services", new, timeout=15)
        self._adb("shell", "settings", "put", "secure", "accessibility_enabled", "1", timeout=15)
        chk = self._adb("shell", "settings", "get", "secure",
                        "enabled_accessibility_services", timeout=15).stdout
        return ("配对自动确认已启用 ✓" if comp in chk
                else f"启用失败(现在={chk.strip()}; Android13+ 需手动: 设置→无障碍)")

    def status(self) -> str:
        """四引擎汇总状态+最近事件+控制面速查(对应 GET /status)。"""
        return self.cmd("/status")

    PERMS = ("android.permission.CALL_PHONE", "android.permission.READ_PHONE_STATE",
             "android.permission.ANSWER_PHONE_CALLS", "android.permission.READ_CONTACTS",
             "android.permission.WRITE_CONTACTS")

    def grant_perms(self) -> str:
        """pm grant 批量授权(电话三件套+联系人两件)。debug 包可授, 幂等, 装新 APK 后跑一次。"""
        out = []
        for p in self.PERMS:
            r = self._adb("shell", "pm", "grant", PKG, p, timeout=15)
            out.append(f"{p.split('.')[-1]}: {'OK' if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200]}")
        return "运行时授权: " + "; ".join(out)

    # ================= 门C: 电话 =================

    def incoming(self, *, number="13800138000") -> str:
        """注入来电(Telecom 注入连接, 手机+已连车机同时响铃)。可控可重复,
        替代真机"打电话过来"。失败(账号未启用等)自动回滚 idle 并在返回文本里说明原因。
        通话中注入 = 呼叫等待(RING_IN+CALL_WAITING 事件, 当前通话不受影响);
        已有两路(GSM 上限)时明确拒绝。"""
        return self.cmd("incoming", number=number)

    def dial(self, *, number="10086") -> str:
        """模拟本机拨号(手机侧发起, 车机上表现为去电界面)。
        对端默认 3s 自动接通 —— 见 set_auto_outgoing()。通话中拨出 = 第二路去电,
        接通时当前通话自动保持。"""
        return self.cmd("dial", number=number)

    def answer(self) -> str:
        """PC 侧代接当前来电(等价手机上划接听)。优先接 ringing(含呼叫等待)，
        其次接通 dialing。接等待来电时当前 active 通话自动保持(真机 GSM 行为)。
        车机侧按接听回流为 CAR_ANSWER 事件。"""
        return self.cmd("answer")

    def hangup(self, *, number=None) -> str:
        """挂断。number=None 挂前景通话(active>ringing>dialing>held, 对齐车机红键)；
        number="号码" 选择性挂某一路；number="all" 全挂复位。
        挂掉 active 后剩余 held 保持 held 不自动恢复(真机行为), hold(on=False) 手动恢复。"""
        return self.cmd("hangup", **({"number": number} if number else {}))

    def hold(self, *, on=True) -> str:
        """通话保持/恢复。on=True 保持当前 active；on=False 恢复 held ——
        双通话时即"切换"语义(挂起 active、激活 held)。"""
        return self.cmd("hold", on="1" if on else "0")

    def swap(self) -> str:
        """双通话切换：挂起当前 active、激活 held(与 hold(on=False) 等价, 语义显式)。
        需要一路 active + 一路 held, 否则返回明确错误。"""
        return self.cmd("swap")

    def dtmf(self, *, key) -> str:
        """发送 DTMF 按键(key='0'-'9','*','#'; 需 active 通话)。
        车机侧按键回流为 CAR_DTMF 事件。"""
        return self.cmd("dtmf", key=key)

    def audio_bt(self) -> str:
        """通话音频切到蓝牙 SCO(等价用户在手机上选"蓝牙接听")。"""
        return self.cmd("audio_bt")

    def set_auto_outgoing(self, *, on=True) -> str:
        """车机拨出(ATD)后是否 3s 自动接通(模拟对端摘机)。默认开。"""
        return self.cmd("auto_outgoing", on="1" if on else "0")

    def call_audio(self, *, name, loop=False) -> str:
        """通话中向车机播放自定义音频(模拟"对端说话", SCO 下行)。
        name=乐库文件名(先 upload_audio); 需先有 active 通话(车机接听/answer)。
        多路(0.8.0+): 音频绑定到当前 active 那一路, 每路可各绑不同文件;
        swap/answer/hold 切换时车机听到的"对端"自动跟随并续各自进度
        (HFP 单 SCO, 真机同样只有 active 路出声; 事件 CALL_AUDIO_FOLLOW 可断言)。"""
        return self.cmd("call_audio", name=name, loop="1" if loop else "0")

    def call_audio_stop(self) -> str:
        """停止通话音频并解除当前路的绑定(对端"闭嘴"; 再激活不再自响起播)。"""
        return self.cmd("call_audio", stop="1")

    # ================= 联系人: 自定义/批量(PBAP 测车机通讯录) =================

    def _contacts_status(self) -> str:
        """轮询联系人批量任务状态(idle/进行中 N/M)。
        单独包一层: 轮询中 HTTP 断连要报清"USB 掉了", 不能裸抛被误读成任务失败。"""
        try:
            return self.http("/contacts/status")
        except VPhoneError as e:
            raise VPhoneError(f"等待联系人任务时 HTTP 中断(USB掉线?): {e}") from e

    def contacts_load(self, *, count=10000, prefix="联系人", wait=True, timeout=300) -> str:
        """批量生成联系人(联系人00001/13800000001...)写入系统通讯录 → 车机 PBAP 可拉取。
        1w 个约 10~30s; wait=True 轮询到完成并返回最终计数。
        注意: clear/load 是异步批量任务 —— 上一任务没跑完就发下一任务是"批量任务进行中"报错。"""
        r = self.cmd("contacts_load", count=count, prefix=prefix)
        if not wait or "已启动" not in r:
            return r
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self._contacts_status()
            if "idle" in s:
                return r + "\n" + s
            time.sleep(1.5)
        return r + "\n(等待完成超时, 最新: " + self._contacts_status() + ")"

    def contacts_import(self, *, text=None, file=None, wait=True, timeout=300) -> str:
        """导入自定义联系人: 每行 '姓名|号码' 或 '姓名,号码'。text 直传或 file 读文件。
        wait=True 轮询到写入完成(异步批量任务, 语义同 contacts_load)。"""
        if file:
            with open(file, "r", encoding="utf-8") as f:
                text = f.read().replace("\r", "").replace("\n", ";")
        r = self.cmd("contacts_import", text=text or "")
        if not wait or "已启动" not in r:
            return r
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self._contacts_status()
            if "idle" in s:
                return r + "\n" + s
            time.sleep(1.5)
        return r + "\n(等待完成超时, 最新: " + self._contacts_status() + ")"

    def contacts_clear(self, *, wait=True, timeout=120) -> str:
        """清空 vphone 写入的联系人(只删本账号, 不动手机原有)。同样要等 idle。"""
        r = self.cmd("contacts_clear")
        if not wait:
            return r
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "idle" in self._contacts_status():
                return r + "\n" + self.http("/contacts/count")
            time.sleep(1.0)
        return r + "\n(等待清空超时, 最新: " + self._contacts_status() + ")"

    def contacts_count(self) -> str:
        """当前通讯录计数(本账号 vphone=N + 系统 total=N)。仅 HTTP 通道。"""
        return self.http("/contacts/count")

    # ================= 门B: 媒体 =================

    def set_track(self, *, title, artist="", album="", dur=240) -> str:
        """设置单曲元数据(不真出声 —— 标题/歌手/时长皆可伪造, 测车机显示与进度条)。
        真实出声请用 upload_audio()+playlist_audio()。dur 单位秒。"""
        return self.cmd("track", title=title, artist=artist, album=album, duration=dur)

    def playlist(self, *, text=None, file=None) -> str:
        """播放列表: text 多行/分号分隔 '标题|歌手|专辑|秒'; file 读文件。
        都不给 = 查询当前播放列表(同格式回读)。"""
        if file:
            with open(file, "r", encoding="utf-8") as f:
                text = f.read().replace("\r", "").replace("\n", ";")
        return self.cmd("playlist", text=text or "")

    def playlist_get(self) -> list:
        """当前播放列表: [{'title','artist','album','dur','path'}, ...]。"""
        out = self.cmd("playlist")
        items = []
        for line in out.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            p = line.split("|")
            items.append({
                "title": p[0] if len(p) > 0 else "",
                "artist": p[1] if len(p) > 1 else "",
                "album": p[2] if len(p) > 2 else "",
                "dur": int(p[3]) if len(p) > 3 and p[3].isdigit() else 0,
                "path": (p[4].strip() if len(p) > 4 else "") or None,
            })
        return items

    def media_jump(self, *, idx) -> str:
        """跳到播放列表第 idx 首(0 起)并按当前播放/暂停态就位。"""
        return self.cmd("jump", idx=int(idx))

    def media_seek(self, *, sec) -> str:
        """拖动播放进度到第 sec 秒(车机进度条跟随; 车机侧拖动回流为 CAR_SEEK 事件)。"""
        return self.cmd("seek", pos=int(sec))

    def play(self) -> str:
        """继续/开始播放(MediaSession 置 playing, 车机界面同步"播放中")。"""
        return self.cmd("play")

    def pause(self) -> str:
        """暂停(保持进度; 车机界面同步"已暂停")。"""
        return self.cmd("pause")

    def next(self) -> str:
        """下一曲。空列表返回明确错误; 暂停态切歌=换曲但保持暂停(对齐真机播放器语义)。"""
        return self.cmd("next")

    def prev(self) -> str:
        """上一曲(语义同 next)。"""
        return self.cmd("prev")

    def media_status(self) -> str:
        """媒体引擎一行状态(media=playing/paused, track=, pos=/dur=, playlist=N首)。
        GUI 进度条轮询专用(轻量, 不带 /status 的四引擎汇总)。仅 HTTP 通道。"""
        return self.http("/media/status")

    def silence(self, *, on=True) -> str:
        """静音流模式开关。on=True 不真实解码, 用静音帧维持 A2DP 链路(测元数据/按键,
        不测音质); off=用上传的音频文件真实解码。静音模式下进度由秒表模拟,
        单曲播完即停(与真机播放器一致, 不原地循环)。"""
        return self.cmd("silence", on="1" if on else "0")

    def autoadvance(self, *, on=True) -> str:
        """单曲播完是否自动进下一曲(默认开; 关掉可测"播完即停"的车机表现)。"""
        return self.cmd("autoadvance", on="1" if on else "0")

    # ---------- 乐库: 电脑音频 → 手机 → 车机真实播放 ----------

    def upload_audio(self, *, path, name=None) -> str:
        """把电脑上的音频文件(mp3/wav/flac/m4a/aac/ogg)推到手机乐库(POST /media/upload)。
        上传后播放列表第 5 列引用文件名即真实出声(元数据仍可伪造)。"""
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise VPhoneError(f"文件不存在: {path}")
        name = name or os.path.basename(path)
        with open(path, "rb") as f:
            data = f.read()
        # 超时按体积放宽: 每 MB 约 8s(USB forward 实测), 下限 120s ——
        # 固定 120s 会把大文件(无损几十章)中途掐断报"HTTP 不通"
        tmo = max(120, int(len(data) / 1024 / 1024 * 8) + 15)
        return self.http_post("/media/upload", data, name=name, timeout=tmo)

    def list_audio(self) -> list:
        """手机乐库文件列表: [{'name','kb'}]。"""
        out = self.cmd("files")
        devs = []
        for line in out.splitlines():
            if "|" in line:
                n, kb = line.split("|", 1)
                devs.append({"name": n.strip(), "kb": kb.replace("KB", "").strip()})
        return devs

    def del_audio(self, *, name) -> str:
        """删除乐库音频文件(name 见 list_audio())。"""
        return self.cmd("del", name=name)

    def media_diag(self) -> str:
        """音质/断续排查: 实际输出设备(A2DP/SCO)+绑定情况+SCO占用一目了然。"""
        return self.cmd("diag")

    def playlist_audio(self, *, names, meta=None, autoplay=True) -> str:
        """用乐库文件名构造真实音频播放列表并(可选)播放。
        names: 乐库文件名列表; meta: 可选 [(标题,歌手,专辑), ...] 与 names 对齐, 缺省用文件名。"""
        lines = []
        for i, n in enumerate(names):
            t = (meta[i] if meta and i < len(meta) and meta[i] else
                 (os.path.splitext(n)[0], "", "vphone乐库"))
            title, artist, album = (t + ("", "", ""))[:3]
            lines.append(f"{title}|{artist or ''}|{album or ''}|0|{n}")
        r = self.cmd("playlist", text=";".join(lines))
        return r + (("\n" + self.play()) if autoplay else "")

    def play_audio_files(self, *, paths, autoplay=True) -> str:
        """一步到位: 电脑选音频文件 → 逐个上传 → 生成播放列表 → 播放(车机真放出声)。"""
        outs = []
        names = []
        for p in paths:
            outs.append(self.upload_audio(path=p))
            names.append(os.path.basename(p))
        outs.append(self.playlist_audio(names=names, autoplay=autoplay))
        return "\n".join(outs)

    # ================= 蓝牙: 扫描/配对 =================

    def bt_state(self) -> str:
        """蓝牙开关 + 已配对列表 + [A2DP已连]/[HFP已连] 标记(台架判定证据)。"""
        return self.cmd("bt_state")

    def bt_scan(self, *, timeout=20) -> list:
        """触发扫描并等结束, 返回 [{'name','mac','rssi'}] 按信号强度降序。
        扫描不出结果多为: 手机定位服务未开(Android 权限模型)或权限未授 —— 报错里提示。"""
        r = self.cmd("scan")
        if "扫描已启动" not in r:
            raise VPhoneError(r)
        ended = self.wait_for(pattern="扫描结束", timeout=timeout)
        out = self.cmd("scan_result")
        if not ended and "|" not in out:
            raise VPhoneError(
                f"扫描 {timeout}s 未结束且无结果: 定位服务开着吗? bt_state 里看线索")
        devs = []
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) == 3:
                devs.append({"name": parts[0], "mac": parts[1], "rssi": int(parts[2])})
        return devs

    def bt_bond(self, *, target) -> str:
        """发起配对: target=MAC 或名字片段(如 'CARKIT-1' / '00:11:22:33:44:55')。"""
        return self.cmd("bond", mac=target)

    def bt_unpair(self, *, target) -> str:
        """解除配对(车机端设备消失, 模拟用户删除配对)。target=MAC 或名字片段。"""
        return self.cmd("unpair", mac=target)

    def bt_disconnect(self, *, mac=None, force=False) -> str:
        """断开与设备的蓝牙连接(保持配对) —— 车机断连/回连测试场景。
        mac=None 时自动选当前已连接(A2DP/HFP)那台; force=反射全被系统权限拒时
        兜底直接关蓝牙。断链/回链都会落 ACL/A2DP/HFP 结构化事件可断言。"""
        p = {"force": "1" if force else "0"}
        if mac:
            p["mac"] = mac
        return self.cmd("bt_disconnect", **p)

    # 已配对行里抠 MAC 用正则(行尾的 [A2DP已连] 等标记会让"按空格取最后一段"取错)
    _MAC_RE = re.compile(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")

    def _pick_bonded_mac(self, marker=""):
        """从 /bt/state 已配对行正则抠第一个 MAC。marker=额外命中条件(如 '[HFP已连]')。"""
        for line in self.bt_state().splitlines():
            if "已配对:" in line and (not marker or marker in line):
                m = self._MAC_RE.search(line)
                if m:
                    return m.group(0)
        return None

    def bt_reconnect(self, *, target=None, fallback=True) -> str:
        """断线重连(手机侧主动发起)。target=None 时自动选第一台已配对设备(正则抠 MAC)。
        返回文本含各条路径结果; 连上与否用 wait_profile(target) 断言。"""
        if target is None:
            target = self._pick_bonded_mac()
            if not target:
                return "(无已配对设备, 先 bt_bond)"
        return self.cmd("reconnect", mac=target, fallback="1" if fallback else "0")

    def bt_name(self, *, name=None) -> str:
        """查看/修改本机蓝牙名(车机上显示的手机名)。name=None 只查询。"""
        return self.cmd("bt_name", name=name) if name else self.cmd("bt_name")

    def bt_allow_car(self, *, target=None) -> str:
        """授权车机访问联系人/通话记录(PBAP/MAP) —— 车机能拉通讯录的前提。
        target=None 时自动选第一台带 [HFP已连] 的设备。反射失败时返回手动路径指引。"""
        if target is None:
            target = self._pick_bonded_mac("[HFP已连]")
            if not target:
                return "(没找到已连 HFP 的设备, 传 target=MAC 或先连接)"
        return self.cmd("allow_car", mac=target)

    def bt_enable(self, *, on=True) -> str:
        """开/关手机蓝牙。on=False 关蓝牙 —— 模拟用户关蓝牙时车机的断连表现。"""
        return self.cmd("bt_enable", on="1" if on else "0")

    # ================= 部署 =================

    def install(self, *, apk=None, timeout=120) -> str:
        """adb install -r + 装完自动拉起服务/重建 forward。
        apk 默认取 vphone/apk/ 打包产物(仓库自带, 换机器克隆即装), 无则取构建产物。
        注意: 部分厂商手机仍会弹一次安装确认(厂商差异大, 不做自动点屏, 需手动点一下)。"""
        if apk is None:
            apk = default_apk()
            if apk is None:
                raise VPhoneError(
                    "未找到 APK: vphone/apk/ 无打包产物, 也无构建产物 (先 gradle assembleDebug)")
        if not os.path.isfile(apk):
            raise VPhoneError(f"APK 不存在: {apk}")
        # 华为: 上一次安装残留的安装器进程会拦住新安装 → 先顺手清掉(其他机型无此包, 无害)
        try:
            self._adb("shell", "am", "force-stop", "com.android.packageinstaller", timeout=10)
        except Exception:
            pass
        try:
            r = self._adb("install", "-r", apk, timeout=timeout)
        except VPhoneError as e:
            # _adb 已把 TimeoutExpired 包装成 VPhoneError(超时含设备掉线两种可能)
            raise VPhoneError(
                "安装超时: 手机上大概率弹了厂商安装确认框(华为/小米等), 手动点一下后重试"
                f" (原始: {e})") from e
        out = (r.stdout + r.stderr).strip()
        if "Success" not in out:
            raise VPhoneError(f"安装失败: {out[-300:]}")
        # 安装后应用处于 stopped 态: 启动一次 + 重新拉服务 + 重 forward + 顺手补授权
        self._adb("shell", "am", "start", "-n", f"{PKG}/.MainActivity", timeout=15)
        time.sleep(1)
        if not self._base:
            self._setup_forward()
        try:
            self.grant_perms()
        except Exception:
            pass
        head = self.start()
        return f"安装成功; 服务已拉起: {head}"

    def launch(self) -> str:
        """拉起 App 界面 + 常驻服务。装完 APK 处于 stopped 态时广播唤不醒服务,
        必须先 am start 一次 —— GUI「🚀启动App」按钮, 幂等可反复点。"""
        self._adb("shell", "am", "start", "-n", f"{PKG}/.MainActivity", timeout=15)
        time.sleep(1)
        head = self.start()
        try:
            st = self.http("/status").splitlines()[0]
            return f"App 已拉起; 服务应答: {st}"
        except VPhoneError:
            return f"App 已拉起, 但服务未应答 HTTP({head}) → 查手机上 vphone 是否被限制自启"

    # ================= 事件流/断言辅助 =================

    # ---------- 首选: 结构化事件总线(App 内 EventLog, /events JSON) ----------
    # 三层断言设计:
    #   events()         快照查询 —— 非阻塞, 立即返回 list[VEvent];
    #   wait_event()     阻塞原语 —— 超时返回 None(可组合自定义流程);
    #   expect_event()   断言版   —— 超时抛 VPhoneTimeoutError, 消息带"等待窗口内实际事件";
    #   expect_no_event() 反向断言 —— 观察窗口内命中即抛(测"不该发生的事")。
    # 超时规则: 不支持无限等待。timeout=None(默认)=用实例默认 VPhone(wait_timeout=15);
    #           timeout=0 = 只查一次快照立即返回(非阻塞"探一眼")。

    def events_raw(self, *, since=0) -> dict:
        """原始事件信封: {'last':N, 'events':[{'id','ts','type','src','detail'}, ...]}。
        id>N 的事件; last=当前水位。GUI 轮询等需要水位+列表一起拿的场景用这个,
        断言/业务代码请用 events()/wait_event()/expect_event()。"""
        return json.loads(self.http("/events", since=since))

    def events(self, *, since=0) -> list:
        """拉取手机端结构化事件快照(非阻塞): [VEvent(id,ts,type,src,detail), ...]。
        常用 type: CAR_ANSWER/CAR_REJECT/CAR_HANGUP/CAR_DIAL/CAR_HOLD/CAR_DTMF(电话),
                   CAR_PLAY/CAR_PAUSE/CAR_NEXT/CAR_PREV/CAR_SEEK(媒体),
                   BT_A2DP_CONNECTED/BT_HFP_CONNECTED/...(链路), RING_IN/CALL_ACTIVE/..."""
        return [VEvent._of(d) for d in self.events_raw(since=since)["events"]]

    def event_watermark(self) -> int:
        """当前事件水位(最新事件 id)。配合"先命令后断言":
            mark = vp.event_watermark(); vp.next()
            vp.expect_event(evt_type="CAR_NEXT", since=mark)   # 命令瞬间的事件不漏"""
        return self.events_raw(since=1 << 30)["last"]

    @staticmethod
    def _match_event(e: VEvent, types, src, detail) -> bool:
        """三层断言共用的匹配规则: type 精确(str 或 tuple 任一), src 精确, detail 子串。"""
        if types and e.type not in types:
            return False
        if src and e.src != src:
            return False
        if detail and detail not in e.detail:
            return False
        return True

    def _wait_scan(self, *, evt_type=None, detail=None, src=None,
                   timeout=None, since=None):
        """阻塞轮询 /events 直到命中, 返回 (VEvent|None, 观察窗口内全部事件)。
        供 wait_event/expect_event/expect_no_event 共用 —— 后者需要窗口内事件做上下文。"""
        timeout = self.wait_timeout if timeout is None else timeout
        last = since if since is not None else self.event_watermark()
        types = (evt_type,) if isinstance(evt_type, str) else evt_type
        seen = []
        deadline = time.time() + timeout
        while True:
            for e in self.events(since=last):
                last = max(last, e.id)
                seen.append(e)
                if self._match_event(e, types, src, detail):
                    return e, seen
            if time.time() >= deadline:
                return None, seen
            time.sleep(0.3)

    def wait_event(self, *, evt_type=None, detail=None, src=None,
                   timeout=None, since=None):
        """阻塞等待结构化事件, 命中返回 VEvent, 超时返回 None(可组合, 漏判请用 expect_event)。
        evt_type 传 str(精确匹配)或 tuple/list(任一命中); detail=子串过滤;
        src=来源过滤(car=车机按键/cmd=PC命令/app=App自身/bt=蓝牙栈/sys=系统Telecom侧)。
        timeout: None=实例默认(VPhone(wait_timeout=)); 0=只查一次立即返回(非阻塞探一眼)。
        ── since 水位语义(重要) ──
        不传: 先取当前水位, 只等"今后发生"的事件(推荐 —— 不会被历史事件误命中);
        传 N: 从事件 id>N 起回放 —— 用于"先发命令再断言"且不想漏掉命令瞬间产生的事件。
        例: vp.wait_event(evt_type=("CAR_NEXT", "CAR_PREV"), timeout=15)"""
        hit, _ = self._wait_scan(evt_type=evt_type, detail=detail, src=src,
                                 timeout=timeout, since=since)
        return hit

    def expect_event(self, *, evt_type=None, detail=None, src=None,
                     timeout=None, since=None, because=""):
        """断言版 wait_event: 命中返回 VEvent; 超时抛 VPhoneTimeoutError,
        消息带过滤条件 + 等待窗口内实际发生的最近 8 条事件(失败现场直接可排查)。
        because=本次断言的用途说明(如 "等车机接听注入来电"), 会出现在异常消息里。
        测试脚本首选 —— 裸 assert vp.wait_event(...) 有 python -O 剥 assert、
        忘写 assert 吞 None 两个坑, 抛异常的 expect_event 漏不掉。"""
        hit, seen = self._wait_scan(evt_type=evt_type, detail=detail, src=src,
                                    timeout=timeout, since=since)
        if hit is not None:
            return hit
        want = f"type∈{tuple(evt_type) if isinstance(evt_type, (tuple, list)) else evt_type or '任意'}"
        if src:
            want += f" src={src}"
        if detail:
            want += f" detail~'{detail}'"
        ctx = "\n".join(f"  {e}" for e in seen[-8:]) or "  (窗口内无任何事件)"
        why = f"; {because}" if because else ""
        raise VPhoneTimeoutError(
            f"expect_event 超时{self.wait_timeout if timeout is None else timeout}s: "
            f"{want}{why}\n等待窗口内实际事件(最近8条):\n{ctx}")

    def expect_no_event(self, *, evt_type=None, detail=None, src=None,
                        within=None, since=None):
        """反向断言: 观察窗口 within 秒内【不该】出现匹配事件 —— 平静度过返回 None,
        命中即抛 VPhoneTimeoutError(消息带命中的那条)。测"车机不该有反应"的场景:
            vp.pause(); vp.expect_no_event(evt_type="CAR_PLAY", within=3)"""
        within = self.wait_timeout if within is None else within
        hit, _ = self._wait_scan(evt_type=evt_type, detail=detail, src=src,
                                 timeout=within, since=since)
        if hit is not None:
            raise VPhoneTimeoutError(f"expect_no_event 命中(不应出现的事件): {hit}")
        return None

    # ---------- 兼容保留: logcat 文本事件流 ----------

    def start_events(self, *, on_event=None):
        """后台线程持续收 logcat(TAG=VPhone)。重复调用幂等。
        on_event: 本次要设的回调 fn(line); 不传则保留已有回调 ——
        (此处曾无条件 on_event=None, 把 GUI 预设的回调清掉, logcat 事件通道成死代码)。"""
        if getattr(self, "_evt_thread", None) and self._evt_thread.is_alive():
            if on_event is not None:
                self.on_event = on_event
            return
        self._evt_lines = deque(maxlen=2000)
        self._evt_lock = threading.Lock()
        self._evt_seq = 0
        self._evt_stop = threading.Event()
        if on_event is not None or not hasattr(self, "on_event"):
            self.on_event = on_event
        self._evt_thread = threading.Thread(target=self._evt_loop, daemon=True)
        self._evt_thread.start()

    def stop_events(self):
        """停掉 logcat 事件线程并杀掉其 adb 子进程。GUI 关闭/重连时必须调, 否则进程残留。"""
        if getattr(self, "_evt_stop", None):
            self._evt_stop.set()
        p = getattr(self, "_evt_proc", None)
        if p:
            try:
                p.terminate()
            except Exception:
                pass

    def _evt_loop(self):
        cmd = [self.adb_path] + (["-s", self.serial] if self.serial else []) + \
            ["logcat", "-s", "VPhone", "-T", "1"]
        self._evt_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW)
        for line in self._evt_proc.stdout:
            if self._evt_stop.is_set():
                break
            line = line.rstrip("\n")
            with self._evt_lock:
                self._evt_seq += 1
                self._evt_lines.append((self._evt_seq, line))
            if self.on_event:
                try:
                    self.on_event(line)
                except Exception:
                    pass
        self._evt_proc.wait()

    def events_tail(self, *, n=30) -> list:
        """最近 n 行事件(未开事件流时自动拉一次 logcat 快照)。"""
        if not getattr(self, "_evt_thread", None) or not self._evt_thread.is_alive():
            out = self._adb("logcat", "-d", "-s", "VPhone", "-t", str(n), timeout=15).stdout
            return out.splitlines()
        with self._evt_lock:
            return [l for _, l in list(self._evt_lines)[-n:]]

    def wait_for(self, *, pattern, timeout=10, regex=False, after_seq=None) -> str:
        """[兼容保留·logcat 文本匹配] 阻塞等待事件流出现匹配行, 返回该行; 超时返回 None。
        pattern 默认子串匹配, regex=True 时按正则。新代码请用结构化断言 expect_event()。
        配合车机操作断言: vp.wait_for(pattern="【接听】", timeout=15)
        """
        if not getattr(self, "_evt_thread", None) or not self._evt_thread.is_alive():
            self.start_events()
        with self._evt_lock:
            start = after_seq if after_seq is not None else self._evt_seq
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._evt_lock:
                hits = [l for s, l in self._evt_lines if s > start and
                        ((re.search(pattern, l) if regex else pattern in l))]
            if hits:
                return hits[0]
            time.sleep(0.2)
        return None

    def wait_bonded(self, *, target, timeout=25) -> bool:
        """等待目标出现在已配对列表(name 或 MAC 包含 target)。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if target.lower() in self.bt_state().lower():
                    return True
            except VPhoneError:
                pass
            time.sleep(1.5)
        return False

    def wait_profile(self, *, target, profile="A2DP", timeout=25) -> bool:
        """等待目标设备连上指定 profile('A2DP'/'HFP') —— /bt_state 里出 [X已连] 标记。"""
        deadline = time.time() + timeout
        want = f"[{profile}已连]"
        while time.time() < deadline:
            try:
                for line in self.bt_state().splitlines():
                    if target.lower() in line.lower() and want in line:
                        return True
            except VPhoneError:
                pass
            time.sleep(1.5)
        return False
