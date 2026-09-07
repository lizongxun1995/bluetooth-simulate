"""示例 2:配对失败模拟(测试车机的配对异常处理)。

三种失败模式:
  reject     — 固件拒绝配对确认
  wrong_pin  — 传统 PIN 配对回错误 PIN
  timeout    — 不响应配对请求,等链路超时

无硬件试跑: python examples/02_pairing_failure.py --sim
"""

import argparse
import sys

from btphone import BtPhone, SimPhone


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM3")
    ap.add_argument("--sim", action="store_true")
    args = ap.parse_args()

    sim = None
    if args.sim:
        sim = SimPhone(name="VPHONE-01")
        phone = BtPhone("SIM", io=sim.device_io)
    else:
        phone = BtPhone(args.port)

    try:
        for mode in ("reject", "wrong_pin", "timeout"):
            phone.pairing.set_mode(mode)
            phone.set_discoverable(True, timeout_s=30)
            print(f"== 模式 [{mode}]:请在车机上重新搜索配对 VPHONE-01 ==")
            if sim:
                sim.car_pair()
            result = phone.wait_event("pair.result", timeout=60)
            verdict = "车机提示配对失败 ✓" if not result.get("ok") else "车机仍显示配对成功 ✗(检查车机异常处理!)"
            print(f"   结果: {result} → {verdict}")
            phone.pairing.set_mode("auto")  # 恢复自动接受
        return 0
    finally:
        phone.close()
        if sim:
            sim.close()


if __name__ == "__main__":
    sys.exit(main())
