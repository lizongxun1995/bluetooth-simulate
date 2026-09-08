#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vphone 控制端 CLI —— vphone_lib.VPhone 的命令行薄封装(库为主, GUI 也基于同一库)。

用法示例:
  python vphone_ctl.py start                        # 一键拉起手机端服务
  python vphone_ctl.py grant-perms                  # 运行时授权(电话+联系人, 一次)
  python vphone_ctl.py enable-account               # 启用电话账号+设默认去电账号(一次性)
  python vphone_ctl.py status
  python vphone_ctl.py incoming --num 13800138000   # 模拟来电(门C)
  python vphone_ctl.py dial / answer / hangup / hold / dtmf / audio-bt
  python vphone_ctl.py call-audio --name demo.mp3 --loop   # 通话中自定义音频(SCO)
  python vphone_ctl.py track --title 青花瓷 --artist 周杰伦 --dur 229
  python vphone_ctl.py playlist --file demo_pl.txt  # 播放列表(每行: 标题|歌手|专辑|秒|文件名)
  python vphone_ctl.py upload a.mp3 b.flac          # 电脑音频 → 手机乐库
  python vphone_ctl.py play-audio a.mp3 b.flac      # 上传+列表+播放(车机真实出声)
  python vphone_ctl.py files / del --name a.mp3     # 乐库管理
  python vphone_ctl.py contacts-load --count 10000  # 批量1w联系人(车机通讯录压测)
  python vphone_ctl.py contacts-file --file 联系人.txt   # 自定义联系人(姓名|号码/行)
  python vphone_ctl.py contacts-count / contacts-clear
  python vphone_ctl.py bt-state / scan / scan-result / bond --mac CARKIT-1 / unpair --mac CARKIT-2
  python vphone_ctl.py reconnect --mac CARKIT-1            # 断线重连(A2DP+HFP)
  python vphone_ctl.py allow-car                      # 授权车机拉通讯录(PBAP)
  python vphone_ctl.py enable-autoconfirm             # 配对弹窗自动点(无障碍, 一次性)
  python vphone_ctl.py install [apk路径]              # 更新APK并自动拉起服务(缺省取 vphone/apk/)
  python vphone_ctl.py launch                         # 拉起App界面+服务(装完 stopped 态必备)
  python vphone_ctl.py media-diag                     # 音质/断续排查(实际输出设备+SCO占用)
  python vphone_ctl.py playlist-get                   # 查询当前播放列表
  python vphone_ctl.py events                       # 实时结构化事件流(Ctrl+C 退出)
  python vphone_ctl.py wait-event CAR_HANGUP --timeout 30   # 断言等待车机回流事件

多设备: --serial 指定手机序列号, 或环境变量 VPHONE_SERIAL。
"""
import argparse
import sys
import time

from vphone_lib import VPhone, VPhoneError, VEvent


def main():
    ap = argparse.ArgumentParser(description="vphone 虚拟手机控制端(基于 vphone_lib)")
    ap.add_argument("--serial", help="手机序列号(多设备时必填)")
    ap.add_argument("--base", help="直连模式: http://<手机IP>:8800 (默认 adb forward)")
    ap.add_argument("--port", type=int, default=18800, help="PC 侧本地端口(默认 18800)")
    ap.add_argument("cmd", help="start|grant-perms|enable-account|status|launch|install|events|"
                                "wait-event|"
                                "incoming|dial|answer|hangup|hold|dtmf|audio-bt|call-audio|"
                                "call-audio-stop|auto-outgoing|"
                                "track|play|pause|next|prev|jump|seek|silence|autoadvance|"
                                "playlist|playlist-get|upload|files|del|play-audio|media-diag|"
                                "contacts-load|contacts-file|contacts-clear|contacts-count|"
                                "bt-state|bt-name|bt-enable|scan|scan-result|bond|unpair|"
                                "disconnect|reconnect|allow-car|enable-autoconfirm")
    ap.add_argument("rest", nargs="*", help="位置参数(如 upload/play-audio 的文件列表)")
    ap.add_argument("--num")
    ap.add_argument("--mac", help="bond/unpair 目标: MAC 或名字片段")
    ap.add_argument("--title")
    ap.add_argument("--artist")
    ap.add_argument("--album")
    ap.add_argument("--dur", type=int)
    ap.add_argument("--on", default="1")
    ap.add_argument("--key")
    ap.add_argument("--file", help="播放列表/联系人文件")
    ap.add_argument("--text", help="播放列表/联系人文本")
    ap.add_argument("--name", help="乐库文件名(del/call-audio)")
    ap.add_argument("--count", type=int, default=10000, help="contacts-load 联系人数量")
    ap.add_argument("--prefix", default="联系人", help="contacts-load 名字前缀")
    ap.add_argument("--loop", action="store_true", help="call-audio 循环播放")
    ap.add_argument("--force", action="store_true", help="disconnect 反射全被拒时兜底关蓝牙")
    ap.add_argument("--detail", help="wait-event 事件 detail 子串过滤")
    ap.add_argument("--timeout", type=float, default=30, help="wait-event 超时秒")
    args = ap.parse_args()

    try:
        vp = VPhone(serial=args.serial, base=args.base, port=args.port)
    except VPhoneError as e:
        sys.exit(f"!! {e}")

    c = args.cmd
    try:
        if c == "events":
            last = 0
            first = True
            print("== 实时结构化事件 (/events, Ctrl+C 退出) ==", flush=True)
            try:
                while True:
                    r = vp.events_raw(since=last)
                    last = r["last"]
                    if first:
                        first = False  # 首拉只对齐水位, 不倒灌历史
                        continue
                    for d in r["events"]:
                        e = VEvent._of(d)
                        mark = "🚗" if e.src == "car" else ("⌨" if e.src == "cmd" else " ")
                        print(f"{mark} {e}", flush=True)
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
        elif c == "wait-event":
            types = tuple(args.rest) or None
            e = vp.wait_event(evt_type=types, detail=args.detail, timeout=args.timeout)
            if e:
                print(f"✓ {e}")
            else:
                sys.exit(f"!! 超时 {args.timeout}s 未等到事件 {types or '(任意)'}")
        # ---------- 部署/生命周期 ----------
        elif c == "start":
            print(vp.start())
        elif c == "grant-perms":
            print(vp.grant_perms())
        elif c == "enable-account":
            print(vp.enable_account())
        elif c == "status":
            print(vp.status())
        # ---------- 电话(门C) ----------
        elif c == "incoming":
            print(vp.incoming(number=args.num or "13800138000"))
        elif c == "dial":
            print(vp.dial(number=args.num or "10086"))
        elif c == "answer":
            print(vp.answer())
        elif c == "hangup":
            print(vp.hangup())
        elif c == "hold":
            print(vp.hold(on=args.on != "0"))
        elif c == "dtmf":
            print(vp.dtmf(key=args.key or ""))
        elif c == "audio-bt":
            print(vp.audio_bt())
        elif c == "call-audio":
            if not args.name:
                sys.exit("!! call-audio 需要 --name <乐库文件名> (先 upload)")
            print(vp.call_audio(name=args.name, loop=args.loop))
        elif c == "call-audio-stop":
            print(vp.call_audio_stop())
        # ---------- 媒体(门B) ----------
        elif c == "track":
            print(vp.set_track(title=args.title or "", artist=args.artist or "",
                               album=args.album or "", dur=args.dur or 240))
        elif c == "play":
            print(vp.play())
        elif c == "pause":
            print(vp.pause())
        elif c == "next":
            print(vp.next())
        elif c == "prev":
            print(vp.prev())
        elif c == "silence":
            print(vp.silence(on=args.on != "0"))
        elif c == "autoadvance":
            print(vp.autoadvance(on=args.on != "0"))
        elif c == "playlist":
            print(vp.playlist(text=args.text, file=args.file))   # 无参=查询当前列表
        elif c == "playlist-get":
            for i, it in enumerate(vp.playlist_get()):
                f = f"  📄{it['path']}" if it["path"] else ""
                print(f"{i}. {it['title']} | {it['artist']} | {it['dur']}s{f}")
        elif c == "media-diag":
            print(vp.media_diag())
        elif c == "jump":
            if not args.rest:
                sys.exit("!! 用法: jump <序号从0起>")
            print(vp.media_jump(idx=int(args.rest[0])))
        elif c == "seek":
            if not args.rest:
                sys.exit("!! 用法: seek <目标秒>")
            print(vp.media_seek(sec=int(args.rest[0])))
        elif c == "bt-name":
            print(vp.bt_name(name=args.name) if args.name else vp.bt_name())
        elif c == "upload":
            if not args.rest and not args.file:
                sys.exit("!! upload 需要文件路径(位置参数或 --file)")
            for p in ([args.file] if args.file else args.rest):
                print(f"[上传] {p}")
                print(vp.upload_audio(path=p))
        elif c == "files":
            for f in vp.list_audio():
                print(f'{f["name"]}\t{f["kb"]}KB')
        elif c == "del":
            if not args.name:
                sys.exit("!! del 需要 --name <乐库文件名>")
            print(vp.del_audio(name=args.name))
        elif c == "play-audio":
            if not args.rest and not args.file:
                sys.exit("!! play-audio 需要音频文件路径(位置参数或 --file)")
            print(vp.play_audio_files(paths=[args.file] if args.file else args.rest))
        # ---------- 联系人(PBAP) ----------
        elif c == "contacts-load":
            print(vp.contacts_load(count=args.count, prefix=args.prefix))
        elif c == "contacts-file":
            if not args.file:
                sys.exit("!! contacts-file 需要 --file (每行: 姓名|号码)")
            print(vp.contacts_import(file=args.file))
        elif c == "contacts-clear":
            print(vp.contacts_clear())
        elif c == "contacts-count":
            print(vp.contacts_count())
        # ---------- 蓝牙 ----------
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
            print(vp.bt_bond(target=args.mac))
        elif c == "unpair":
            if not args.mac:
                sys.exit("!! unpair 需要 --mac <MAC或名字片段>")
            print(vp.bt_unpair(target=args.mac))
        elif c == "bt-enable":
            print(vp.bt_enable(on=args.on != "0"))
        elif c == "disconnect":
            print(vp.bt_disconnect(mac=args.mac, force=args.force))
        elif c == "reconnect":
            print(vp.bt_reconnect(target=args.mac))
        elif c == "allow-car":
            print(vp.bt_allow_car(target=args.mac))
        elif c == "auto-outgoing":
            print(vp.set_auto_outgoing(on=args.on != "0"))
        # ---------- 部署 ----------
        elif c == "enable-autoconfirm":
            print(vp.enable_autoconfirm())
        elif c == "launch":
            print(vp.launch())
        elif c == "install":
            # docstring 写"install [apk路径]": 位置参数与 --file 都认, 都不给=自动找默认包
            print(vp.install(apk=args.rest[0] if args.rest else args.file))
        else:
            sys.exit(f"!! 未知命令 {c}")
    except VPhoneError as e:
        sys.exit(f"!! {e}")


if __name__ == "__main__":
    main()
