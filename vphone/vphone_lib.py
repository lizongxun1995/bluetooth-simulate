#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vphone 蓝牙测试库 —— 把"虚拟手机"(vphone APK)的全部能力封装成 Python 接口。

一台 Android 手机装上 vphone APK 后, 即成为 PC 可远程驱动的蓝牙测试终端:
  门B(媒体): set_track/playlist/play/pause/next/prev + 车机按键回流(wait_for)
  门C(电话): incoming/dial/answer/hangup/hold/dtmf/audio_bt + 车机接听挂断回流
  蓝牙:      bt_scan/bt_bond/bt_unpair/bt_reconnect/bt_state/bt_enable (配对不出App)
  部署:      install() 更新APK并自动拉起 + enable_autoconfirm() 配对弹窗自动点

传输: 默认 adb forward(USB, 127.0.0.1:18800 → 手机 8800); 同 WiFi 可 base="http://手机IP:8800"。
事件: start_events() 后台线程收 logcat(TAG=VPhone), wait_for(pattern) 做断言等待。

示例:
    from vphone_lib import VPhone
    vp = VPhone(serial="SN_PHONE_A")
    vp.start_events()
    vp.playlist(file="demo_pl.txt"); vp.play()
    vp.incoming("13800138000")
    assert vp.wait_for("【接听】", timeout=15)      # 车机上有人按了接听
    vp.hangup()
"""
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

PKG = "com.bt.vphone"
REMOTE_PORT = 8800
DEFAULT_LOCAL_PORT = 18800   # PC 侧 forward 端口(避开本机 8800 常见占用)
ACCOUNT_ARGS = f"{PKG}/.VPhoneConnectionService VPHONE 0".split()


class VPhoneError(RuntimeError):
    pass


# 短名 → HTTP 路径(与 APK Dispatcher 路由对齐; 短名同时用于广播别名)
PATHS = {
    "status": "/status",
    "incoming": "/call/incoming",
    "dial": "/call/dial",
    "answer": "/call/answer",
    "hangup": "/call/hangup",
    "hold": "/call/hold",
    "dtmf": "/call/dtmf",
    "audio_bt": "/call/audio-bt",
    "auto_outgoing": "/call/auto-outgoing",
    "track": "/media/track",
    "play": "/media/play",
    "pause": "/media/pause",
    "next": "/media/next",
    "prev": "/media/prev",
    "silence": "/media/silence",
    "autoadvance": "/media/autoadvance",
    "playlist": "/media/playlist",
    "bt_state": "/bt/state",
    "scan": "/bt/scan",
    "scan_result": "/bt/scan-result",
    "bond": "/bt/bond",
    "unpair": "/bt/unpair",
    "reconnect": "/bt/reconnect",
    "bt_enable": "/bt/enable",
}


def _find_adb(adb="adb"):
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

    def __init__(self, serial=None, base=None, port=DEFAULT_LOCAL_PORT,
                 adb="adb", autostart=True):
        self.adb_path = _find_adb(adb)
        self.serial = serial  # None → _adb 不带 -s; 多设备时在 _pick_serial 里选定
        if not self.serial:
            self.serial = self._pick_serial()
        self._base = base
        self._port = port
        self._forward_ok = False
        if not base:
            self._setup_forward()
        if autostart:
            self.start()

    # ================= 底层 =================

    def _adb(self, *args, timeout=None):
        cmd = [self.adb_path] + (["-s", self.serial] if self.serial else []) + list(args)
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)

    def _pick_serial(self):
        out = self._adb("devices").stdout
        serials = [l.split()[0] for l in out.splitlines()
                   if l.strip() and not l.startswith("List") and l.split()[-1] == "device"]
        if len(serials) == 1:
            return serials[0]
        env = os.environ.get("VPHONE_SERIAL")
        if env in serials:
            return env
        raise VPhoneError(
            f"需要指定手机序列号 --serial(手机, 不是车机): {', '.join(serials) or '无设备'}")

    def _setup_forward(self):
        r = self._adb("forward", f"tcp:{self._port}", f"tcp:{REMOTE_PORT}")
        if r.returncode != 0:
            raise VPhoneError(f"adb forward 失败: {r.stderr.strip()}")
        self._forward_ok = True

    @property
    def base(self):
        return self._base or f"http://127.0.0.1:{self._port}"

    def http(self, path, **params) -> str:
        """HTTP 直连手机控制面; 失败抛 VPhoneError。"""
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=6) as r:
                return r.read().decode("utf-8")
        except Exception as e:
            raise VPhoneError(f"HTTP 不通: {e} (服务起了吗? adb forward 通吗?)")

    def broadcast(self, cmd, **params) -> str:
        """显式广播兜底通道(必须 -p 指定包: Android 8+ 隐式广播收不到)。
        广播无返回值, 结果取 logcat 最新 [adb] 行。"""
        args = ["shell", "am", "broadcast", "-a", f"{PKG}.CMD", "-p", PKG,
                "--es", "cmd", cmd]
        for k, v in params.items():
            args += ["--es", k, str(v)]
        self._adb(*args, timeout=15)
        time.sleep(1.2)
        out = self._adb("logcat", "-d", "-s", "VPhone", "-t", "30", timeout=15).stdout
        for line in reversed(out.splitlines()):
            if "[adb]" in line:
                return line.split("[adb]", 1)[1].strip()
        return "(广播已发但无 [adb] 日志: App 可能处于 stopped 态, 先 am start 启动一次)"

    def cmd(self, cmd, **params) -> str:
        """HTTP 优先, 失败自动降级显式广播。cmd=短名或完整路径。"""
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
        return self.cmd("/status")

    # ================= 门C: 电话 =================

    def incoming(self, number="13800138000") -> str:
        return self.cmd("incoming", number=number)

    def dial(self, number="10086") -> str:
        return self.cmd("dial", number=number)

    def answer(self) -> str:
        return self.cmd("answer")

    def hangup(self) -> str:
        return self.cmd("hangup")

    def hold(self, on=True) -> str:
        return self.cmd("hold", on="1" if on else "0")

    def dtmf(self, key) -> str:
        return self.cmd("dtmf", key=key)

    def audio_bt(self) -> str:
        return self.cmd("audio_bt")

    def set_auto_outgoing(self, on=True) -> str:
        """车机拨出(ATD)后是否 3s 自动接通(模拟对端摘机)。默认开。"""
        return self.cmd("auto_outgoing", on="1" if on else "0")

    # ================= 门B: 媒体 =================

    def set_track(self, title, artist="", album="", dur=240) -> str:
        return self.cmd("track", title=title, artist=artist, album=album, duration=dur)

    def playlist(self, text=None, file=None) -> str:
        """播放列表: text 多行/分号分隔 '标题|歌手|专辑|秒'; file 读文件。"""
        if file:
            with open(file, "r", encoding="utf-8") as f:
                text = f.read().replace("\r", "").replace("\n", ";")
        return self.cmd("playlist", text=text or "")

    def play(self) -> str:
        return self.cmd("play")

    def pause(self) -> str:
        return self.cmd("pause")

    def next(self) -> str:
        return self.cmd("next")

    def prev(self) -> str:
        return self.cmd("prev")

    def silence(self, on=True) -> str:
        return self.cmd("silence", on="1" if on else "0")

    def autoadvance(self, on=True) -> str:
        return self.cmd("autoadvance", on="1" if on else "0")

    # ================= 蓝牙: 扫描/配对 =================

    def bt_state(self) -> str:
        """蓝牙开关 + 已配对列表 + [A2DP已连]/[HFP已连] 标记(台架判定证据)。"""
        return self.cmd("bt_state")

    def bt_scan(self, timeout=20) -> list:
        """触发扫描并等结束, 返回 [{'name','mac','rssi'}] 按信号强度降序。"""
        r = self.cmd("scan")
        if "扫描已启动" not in r:
            raise VPhoneError(r)
        self.wait_for("扫描结束", timeout=timeout)
        out = self.cmd("scan_result")
        devs = []
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) == 3:
                devs.append({"name": parts[0], "mac": parts[1], "rssi": int(parts[2])})
        return devs

    def bt_bond(self, target) -> str:
        """发起配对: target=MAC 或名字片段(如 'CARKIT-1' / '00:11:22:33:44:55')。"""
        return self.cmd("bond", mac=target)

    def bt_unpair(self, target) -> str:
        return self.cmd("unpair", mac=target)

    def bt_reconnect(self, target=None, fallback=True) -> str:
        """断线重连(手机侧主动发起)。target=None 时自动选第一台已配对设备。
        返回文本含各条路径结果; 连上与否用 wait_profile(target) 断言。"""
        if target is None:
            for line in self.bt_state().splitlines():
                if line.startswith("已配对:") or "已配对:" in line:
                    target = line.split("已配对:", 1)[1].strip().split()[-1]  # MAC
                    break
            if not target:
                return "(无已配对设备, 先 bt_bond)"
        return self.cmd("reconnect", mac=target, fallback="1" if fallback else "0")

    def bt_enable(self, on=True) -> str:
        return self.cmd("bt_enable", on="1" if on else "0")

    # ================= 部署 =================

    def install(self, apk=None, timeout=120) -> str:
        """adb install -r + 装完自动拉起服务/重建 forward。
        注意: 部分厂商手机仍会弹一次安装确认(厂商差异大, 不做自动点屏, 需手动点一下)。
        apk 默认取 vphone 工程构建产物 app-debug.apk。"""
        if apk is None:
            apk = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "app", "build", "outputs", "apk", "debug", "app-debug.apk")
        if not os.path.isfile(apk):
            raise VPhoneError(f"APK 不存在: {apk} (先 gradle assembleDebug)")
        try:
            r = self._adb("install", "-r", apk, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise VPhoneError(
                "安装超时: 手机上大概率弹了厂商安装确认框(华为/小米等), 请手动点一下后重试")
        out = (r.stdout + r.stderr).strip()
        if "Success" not in out:
            raise VPhoneError(f"安装失败: {out[-300:]}")
        # 安装后应用处于 stopped 态: 启动一次 + 重新拉服务 + 重 forward
        self._adb("shell", "am", "start", "-n", f"{PKG}/.MainActivity", timeout=15)
        time.sleep(1)
        if not self._base:
            self._setup_forward()
        head = self.start()
        return f"安装成功; 服务已拉起: {head}"

    # ================= 事件流/断言辅助 =================

    def start_events(self):
        """后台线程持续收 logcat(TAG=VPhone)。重复调用幂等。"""
        if getattr(self, "_evt_thread", None) and self._evt_thread.is_alive():
            return
        self._evt_lines = deque(maxlen=2000)
        self._evt_lock = threading.Lock()
        self._evt_seq = 0
        self.on_event = None
        self._evt_stop = threading.Event()
        self._evt_thread = threading.Thread(target=self._evt_loop, daemon=True)
        self._evt_thread.start()

    def stop_events(self):
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
            text=True, encoding="utf-8", errors="replace")
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

    def events_tail(self, n=30) -> list:
        """最近 n 行事件(未开事件流时自动拉一次 logcat 快照)。"""
        if not getattr(self, "_evt_thread", None) or not self._evt_thread.is_alive():
            out = self._adb("logcat", "-d", "-s", "VPhone", "-t", str(n), timeout=15).stdout
            return out.splitlines()
        with self._evt_lock:
            return [l for _, l in list(self._evt_lines)[-n:]]

    def wait_for(self, pattern, timeout=10, regex=False, after_seq=None) -> str:
        """阻塞等待事件流出现匹配行, 返回该行; 超时返回 None。
        pattern 默认子串匹配, regex=True 时按正则。配合车机操作断言:
            vp.incoming(); vp.wait_for("【接听】", 15)   # 有人在车机上按了接听
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

    def wait_bonded(self, target, timeout=25) -> bool:
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

    def wait_profile(self, target, profile="A2DP", timeout=25) -> bool:
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
