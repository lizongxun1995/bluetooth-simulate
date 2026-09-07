#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vphone 控制端 CLI —— vphone_lib.VPhone 的命令行薄封装(库为主, GUI 也基于同一库)。

用法示例:
  python vphone_ctl.py start                        # 一键拉起手机端服务
  python vphone_ctl.py enable-account               # 启用电话账号(一次性)
  python vphone_ctl.py status
  python vphone_ctl.py incoming --num 13800138000   # 模拟来电(门C)
  python vphone_ctl.py dial / answer / hangup / hold / dtmf / audio-bt
  python vphone_ctl.py track --title 青花瓷 --artist 周杰伦 --dur 229
  python vphone_ctl.py playlist --file demo_pl.txt  # 播放列表(每行: 标题|歌手|专辑|秒)
  python vphone_ctl.py play / pause / next / prev / silence / autoadvance
  python vphone_ctl.py bt-state / scan / bond --mac CARKIT-1 / unpair --mac CARKIT-2
  python vphone_ctl.py reconnect --mac CARKIT-1            # 断线重连(A2DP+HFP)
  python vphone_ctl.py enable-account                 # 启用电话账号+设为默认去电账号
  python vphone_ctl.py enable-autoconfirm             # 配对弹窗自动点(无障碍, 一次性)
  python vphone_ctl.py install [apk路径]              # 更新APK并自动拉起服务
  python vphone_ctl.py events                       # 实时事件流(Ctrl+C 退出)

多设备: --serial 指定手机序列号, 或环境变量 VPHONE_SERIAL。
"""
import argparse
import sys

from vphone_lib import VPhone, VPhoneError


def main():
    ap = argparse.ArgumentParser(description="vphone 虚拟手机控制端(基于 vphone_lib)")
    ap.add_argument("--serial", help="手机序列号(多设备时必填)")
    ap.add_argument("--base", help="直连模式: http://<手机IP>:8800 (默认 adb forward)")
    ap.add_argument("--port", type=int, default=18800, help="PC 侧本地端口(默认 18800)")
    ap.add_argument("cmd", help="start|enable-account|status|incoming|dial|answer|hangup|hold|"
                                "dtmf|audio-bt|track|play|pause|next|prev|silence|autoadvance|"
                                "playlist|bt-state|scan|scan-result|bond|unpair|bt-enable|events")
    ap.add_argument("--num")
    ap.add_argument("--mac", help="bond/unpair 目标: MAC 或名字片段")
    ap.add_argument("--title")
    ap.add_argument("--artist")
    ap.add_argument("--album")
    ap.add_argument("--dur", type=int)
    ap.add_argument("--on", default="1")
    ap.add_argument("--key")
    ap.add_argument("--file", help="播放列表文件")
    ap.add_argument("--text", help="播放列表文本")
    args = ap.parse_args()

    try:
        vp = VPhone(serial=args.serial, base=args.base, port=args.port)
    except VPhoneError as e:
        sys.exit(f"!! {e}")

    c = args.cmd
    try:
        if c == "events":
            vp.start_events()
            vp.on_event = print
            print("== 实时事件 (logcat -s VPhone, Ctrl+C 退出) ==", flush=True)
            import time
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                vp.stop_events()
        elif c == "start":
            print(vp.start())
        elif c == "enable-account":
            print(vp.enable_account())
        elif c == "status":
            print(vp.status())
        elif c == "incoming":
            print(vp.incoming(args.num or "13800138000"))
        elif c == "dial":
            print(vp.dial(args.num or "10086"))
        elif c == "answer":
            print(vp.answer())
        elif c == "hangup":
            print(vp.hangup())
        elif c == "hold":
            print(vp.hold(args.on != "0"))
        elif c == "dtmf":
            print(vp.dtmf(args.key or ""))
        elif c == "audio-bt":
            print(vp.audio_bt())
        elif c == "track":
            print(vp.set_track(args.title or "", args.artist or "",
                               args.album or "", args.dur or 240))
        elif c == "play":
            print(vp.play())
        elif c == "pause":
            print(vp.pause())
        elif c == "next":
            print(vp.next())
        elif c == "prev":
            print(vp.prev())
        elif c == "silence":
            print(vp.silence(args.on != "0"))
        elif c == "autoadvance":
            print(vp.autoadvance(args.on != "0"))
        elif c == "playlist":
            print(vp.playlist(text=args.text, file=args.file))
        elif c == "bt-state":
            print(vp.bt_state())
        elif c == "scan":
            for d in vp.bt_scan():
                print(f"{d['name']}|{d['mac']}|{d['rssi']}")
        elif c == "scan-result":
            print(vp.cmd("scan_result"))
        elif c == "bond":
            if not args.mac:
                sys.exit("!! bond 需要 --mac <MAC或名字片段>")
            print(vp.bt_bond(args.mac))
        elif c == "unpair":
            if not args.mac:
                sys.exit("!! unpair 需要 --mac <MAC或名字片段>")
            print(vp.bt_unpair(args.mac))
        elif c == "bt-enable":
            print(vp.bt_enable(args.on != "0"))
        elif c == "reconnect":
            print(vp.bt_reconnect(args.mac))
        elif c == "auto-outgoing":
            print(vp.set_auto_outgoing(args.on != "0"))
        elif c == "enable-autoconfirm":
            print(vp.enable_autoconfirm())
        elif c == "install":
            print(vp.install(args.file))
        else:
            sys.exit(f"!! 未知命令 {c}")
    except VPhoneError as e:
        sys.exit(f"!! {e}")


if __name__ == "__main__":
    main()
