#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vphone 控制端 —— PC 侧驱动"虚拟手机"(vphone APK)。

默认走 adb forward(本机 127.0.0.1:18800 → 手机 8800, 手机无需入网);
手机与 PC 同一 WiFi 时可用 --base http://<手机IP>:8800 走局域网。
HTTP 不通时自动降级为显式广播(adb am broadcast -p, Android 8+ 隐式广播收不到)。

用法示例:
  python vphone_ctl.py start                        # 一键拉起手机端服务(广播 status)
  python vphone_ctl.py enable-account               # 启用电话账号(等价手动设置, 一次性)
  python vphone_ctl.py status
  python vphone_ctl.py incoming --num 13800138000   # 模拟来电(门C)
  python vphone_ctl.py dial --num 10086             # 模拟拨出
  python vphone_ctl.py answer / hangup / hold / audio-bt
  python vphone_ctl.py track --title 青花瓷 --artist 周杰伦 --dur 229
  python vphone_ctl.py playlist --file demo_pl.txt  # 载入播放列表(每行: 标题|歌手|专辑|秒)
  python vphone_ctl.py play / pause / next / prev / silence / autoadvance
  python vphone_ctl.py events                       # 实时看车机按键/呼叫事件(logcat -s VPhone)

多设备时用 --serial 指定手机序列号(adb devices 查), 或设环境变量 VPHONE_SERIAL。
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

PKG = "com.bt.vphone"
REMOTE_PORT = 8800   # 手机端 ControlServer
LOCAL_PORT = 18800   # PC 侧 forward 端口(避开本机 8800 常见占用)
ADB = "adb"
ACCOUNT = f"{PKG}/.VPhoneConnectionService VPHONE 0"


def adb(serial, *args):
    cmd = [ADB] + (["-s", serial] if serial else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def pick_serial(explicit):
    if explicit:
        return explicit
    out = adb(None, "devices").stdout
    serials = [l.split()[0] for l in out.splitlines()
               if l.strip() and not l.startswith("List") and l.split()[-1] == "device"]
    if len(serials) == 1:
        return serials[0]
    if not serials:
        sys.exit("!! 没有 adb 设备, 先连手机(开 USB 调试)")
    env = os.environ.get("VPHONE_SERIAL")
    if env in serials:
        return env
    sys.exit("!! 多台设备, 用 --serial 指定手机序列号(手机, 不是车机): " + ", ".join(serials))


def http(base, path, params=None):
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.read().decode("utf-8")
    except Exception as e:
        return f"!! 请求失败: {e}"


def broadcast(serial, cmd, params):
    """显式广播兜底通道。注意必须带 -p: Android 8+ 清单 receiver 收不到隐式广播。
    结果从 logcat 尾部取 [adb] 行(广播本身无返回值)。"""
    args = ["shell", "am", "broadcast", "-a", f"{PKG}.CMD", "-p", PKG, "--es", "cmd", cmd]
    for k, v in params.items():
        args += ["--es", k, str(v)]
    adb(serial, *args)
    time.sleep(1.2)
    out = adb(serial, "logcat", "-d", "-s", "VPhone", "-t", "30").stdout
    for line in reversed(out.splitlines()):
        if "[adb]" in line:
            return line.split("[adb]", 1)[1].strip()
    return out.strip().splitlines()[-1] if out.strip() else "(无日志, 服务起了吗?)"


def playlist_text(args):
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            return f.read().replace("\r", "").replace("\n", ";")
    return args.text or ""


def route(args):
    c = args.cmd
    if c in ("status", "help", "h"):
        return "status", "/status", {}
    if c == "incoming":
        return "incoming", "/call/incoming", {"number": args.num or "13800138000"}
    if c == "dial":
        return "dial", "/call/dial", {"number": args.num or "10086"}
    if c == "answer":
        return "answer", "/call/answer", {}
    if c == "hangup":
        return "hangup", "/call/hangup", {}
    if c == "hold":
        return "hold", "/call/hold", {"on": args.on}
    if c == "dtmf":
        return "dtmf", "/call/dtmf", {"key": args.key or ""}
    if c == "audio-bt":
        return "audio_bt", "/call/audio-bt", {}
    if c == "track":
        return "track", "/media/track", {"title": args.title or "", "artist": args.artist or "",
                                         "album": args.album or "", "duration": str(args.dur or 240)}
    if c == "play":
        return "play", "/media/play", {}
    if c == "pause":
        return "pause", "/media/pause", {}
    if c == "next":
        return "next", "/media/next", {}
    if c == "prev":
        return "prev", "/media/prev", {}
    if c == "silence":
        return "silence", "/media/silence", {"on": args.on}
    if c == "autoadvance":
        return "autoadvance", "/media/autoadvance", {"on": args.on}
    if c == "playlist":
        return "playlist", "/media/playlist", {"text": playlist_text(args)}
    sys.exit(f"!! 未知命令 {c}, help 查看")


def follow_events(serial):
    p = subprocess.Popen(
        [ADB] + (["-s", serial] if serial else []) + ["logcat", "-s", "VPhone"],
        stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    print("== 实时事件 (logcat -s VPhone, Ctrl+C 退出) ==")
    try:
        for line in p.stdout:
            print(line, end="")
    except KeyboardInterrupt:
        pass
    finally:
        p.terminate()


def main():
    ap = argparse.ArgumentParser(description="vphone 虚拟手机控制端")
    ap.add_argument("--serial", help="手机序列号(多设备时必填)")
    ap.add_argument("--base", help="直连模式: http://<手机IP>:8800 (默认 adb forward)")
    ap.add_argument("--port", type=int, default=LOCAL_PORT, help="PC 侧本地端口(默认 18800)")
    ap.add_argument("cmd", help="start|enable-account|status|incoming|dial|answer|hangup|hold|"
                                "dtmf|audio-bt|track|play|pause|next|prev|silence|autoadvance|"
                                "playlist|events|help")
    ap.add_argument("--num")
    ap.add_argument("--title")
    ap.add_argument("--artist")
    ap.add_argument("--album")
    ap.add_argument("--dur", type=int)
    ap.add_argument("--on", default="1")
    ap.add_argument("--key")
    ap.add_argument("--file", help="播放列表文件(每行: 标题|歌手|专辑|秒)")
    ap.add_argument("--text", help="播放列表文本(与 --file 二选一, 行或;分隔)")
    args = ap.parse_args()

    serial = pick_serial(args.serial)

    if args.cmd == "events":
        follow_events(serial)
        return
    if args.cmd == "start":
        # 显式广播 status 顺带拉起前台服务(注册账号+HTTP+MediaSession)
        print(broadcast(serial, "status", {}))
        return
    if args.cmd == "enable-account":
        r = adb(serial, "shell", "telecom", "set-phone-account-enabled", *ACCOUNT.split())
        print((r.stdout + r.stderr).strip() or "(空输出)")
        return

    alias, path, params = route(args)

    base = args.base
    if not base:
        r = adb(serial, "forward", f"tcp:{args.port}", f"tcp:{REMOTE_PORT}")
        if r.returncode != 0:
            sys.exit(f"!! adb forward 失败: {r.stderr.strip()}")
        base = f"http://127.0.0.1:{args.port}"

    result = http(base, path, params)
    if result.startswith("!!"):  # HTTP 不通 → 广播兜底
        print(f"{result}\n-- HTTP 不通, 降级为显式广播 --")
        result = broadcast(serial, alias, params)
    print(result)


if __name__ == "__main__":
    main()
