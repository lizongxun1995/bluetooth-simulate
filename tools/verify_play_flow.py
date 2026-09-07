# -*- coding: utf-8 -*-
"""复现阶段C: 完整 p.music.play() 路径, 全事件监听 + 超时时转储线程栈。"""
import sys, time, threading, traceback
sys.path.insert(0, r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate")
from btphone import BtPhone

TONE = r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate\tools\_probe_tone.wav"
T0 = time.monotonic()

def ts():
    return f"[{time.monotonic()-T0:6.1f}s]"

p = BtPhone("COM8")
def dump(name, data):
    d = dict(data)
    if d.get("peer") == "00:11:22:33:44:55" and name == "bt.conn" and d.get("state") == "connected":
        return  # 保活噪音
    print(f"{ts()} EV {name} {d}", flush=True)

p.events.on("*", dump)
time.sleep(1)

try:
    conn = p.request("conn.status", {}).get("connections") or {}
    print(f"{ts()} a2dp={conn.get('a2dp')}", flush=True)
except Exception as e:
    print(f"{ts()} status err {e}", flush=True)

print(f"{ts()} p.music.play()", flush=True)
p.music.play(files=[TONE])
try:
    d = p.events.wait_event("music.started", timeout=45)
    print(f"{ts()} music.started ✓ {d}", flush=True)
except Exception:
    print(f"{ts()} music.started 45s 未到, 转储线程栈:", flush=True)
    for tid, frame in sys._current_frames().items():
        for t in threading.enumerate():
            if t.ident == tid:
                print(f"--- {t.name} ---", flush=True)
                traceback.print_stack(frame)
    try:
        p.music.stop()
    except Exception:
        pass

# 等缓冲播完看 ended(固件可能不发, 记录现象)
try:
    d = p.events.wait_event("music.ended", timeout=25)
    print(f"{ts()} music.ended ✓ {d}", flush=True)
except Exception:
    print(f"{ts()} music.ended 25s 未到", flush=True)
p.close()
