"""示例 3:通话全流程模拟(呼入→车机接听→通话语音→挂断)。

无硬件试跑: python examples/03_calls.py --sim
"""

import argparse
import sys
import time

from btphone import BtPhone, SimPhone


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM3")
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--voice", default=None, help="通话语音文件(WAV/MP3)")
    args = ap.parse_args()

    sim = None
    if args.sim:
        sim = SimPhone(name="VPHONE-01", speed=100)
        phone = BtPhone("SIM", io=sim.device_io)
    else:
        phone = BtPhone(args.port)

    try:
        if args.voice:
            phone.calls.set_voice(args.voice)
            print(f"[0] 通话语音已设置: {args.voice}")

        # 1. 模拟来电 → 车机应响铃并显示号码
        phone.calls.incoming("13800138000")
        state = phone.wait_event("bt.hfp.state", predicate=lambda d: d.get("state") == "incoming")
        print(f"[1] 来电已发起,车机应显示: {state['number']}")

        # 2. 车机接听(真机:人在车机上按接听;模拟:car_answer)
        if sim:
            sim.car_answer()
        phone.wait_event("hfp.at", predicate=lambda d: d.get("at") == "ATA")
        phone.wait_event("bt.hfp.state", predicate=lambda d: d.get("state") == "active")
        print("[2] 车机已接听,通话激活" + (",语音推流中" if args.voice else ""))

        # 3. 车机侧 DTMF 按键(如拨打分机)
        if sim:
            sim._emit("hfp.at", {"at": "AT+VTS", "arg": "5"})
            at = phone.wait_event("hfp.at", predicate=lambda d: d.get("at") == "AT+VTS")
            print(f"[3] 车机按了 DTMF: {at['arg']}")

        # 4. 挂断
        time.sleep(3)
        phone.calls.hangup()
        phone.wait_event("bt.hfp.state", predicate=lambda d: d.get("state") == "idle")
        print("[4] 已挂断,通话状态回到空闲")
        return 0
    finally:
        phone.close()
        if sim:
            sim.close()


if __name__ == "__main__":
    sys.exit(main())
