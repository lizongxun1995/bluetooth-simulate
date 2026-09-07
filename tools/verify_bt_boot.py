"""真机验证:订阅先行 + 复位启动,抓蓝牙栈初始化事件。

期望:sys.ready / bt.scan_mode 出现且无 sys.error = 协议栈真正起来了。
用法: python tools/verify_bt_boot.py [COM口]
"""
import sys
import time

from btphone import BtPhone

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"

evts: list[tuple[str, dict]] = []
p = BtPhone(PORT, connect=False)
p.on_event("*", lambda n, d: evts.append((n, d)) if n != "audio.buffer" else None)
p.open(reset=True)
print("connected, info =", p.info())
time.sleep(8)  # 给 BT 控制器/Bluedroid 初始化留时间
print("--- boot events ---")
for n, d in evts:
    print(f"{n}: {d}")
p.set_name("VPHONE-01")
p.set_discoverable(True, timeout_s=120)
print("status =", p.status())
print(f"--- keep discoverable for 30s, search 'VPHONE-01' on phone/车机 ---")
time.sleep(30)
p.close()
