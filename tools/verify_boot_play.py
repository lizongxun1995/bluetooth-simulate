# -*- coding: utf-8 -*-
"""终极压力: esptool 复位 → 立即连接并 play(抢在 BT 栈起来前)。
期望: 挺过启动窗 + 射频瞬断, music.started → music.ended 全程无人工干预。
"""
import sys, time, subprocess
sys.path.insert(0, r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate")
from btphone import BtPhone

TONE = r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate\tools\_probe_tone.wav"
T0 = time.monotonic()
def ts():
    return f"[{time.monotonic()-T0:6.1f}s]"

print(f"{ts()} esptool 硬复位", flush=True)
subprocess.run([sys.executable, "-m", "esptool", "--port", "COM8",
                "--before", "default_reset", "--after", "hard_reset", "chip_id"],
               capture_output=True, timeout=30)

p = BtPhone("COM8")
def dump(name, data):
    if name == "audio.buffer":
        return
    print(f"{ts()} EV {name} {dict(data)}", flush=True)
p.events.on("*", dump)
print(f"{ts()} 握手完成, 立即 play", flush=True)

p.music.play(files=[TONE])
try:
    p.events.wait_event("music.started", timeout=90)
    print(f"{ts()} music.started ✓", flush=True)
    p.events.wait_event("music.ended", timeout=25)
    print(f"{ts()} music.ended ✓ —— 全流程通过", flush=True)
except Exception as e:
    print(f"{ts()} 失败: {e}", flush=True)

try:
    info = p.request("sys.info", {}, timeout=8)
    print(f"{ts()} reset={info.get('boot_reset_reason')} heap={info.get('free_heap')}", flush=True)
except Exception as e:
    print(f"{ts()} sys.info: {e}", flush=True)
p.close()
