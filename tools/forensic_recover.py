# -*- coding: utf-8 -*-
"""插桩复现: 射频瞬断后 _auto_recover 为何 90s 都救不回来。"""
import sys, time, subprocess, threading
sys.path.insert(0, r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate")

from serial.tools import list_ports
from btphone import transport as T
from btphone.transport import SerialTransport
from btphone import BtPhone

TONE = r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate\tools\_probe_tone.wav"
T0 = time.monotonic()
def ts():
    return f"[{time.monotonic()-T0:6.1f}s]"

# ---- 插桩 ----
_o_open = SerialTransport.open
def open_(self, reset=True):
    print(f"{ts()} TRANSPORT.open(reset={reset}) 开始", flush=True)
    try:
        r = _o_open(self, reset)
        print(f"{ts()} TRANSPORT.open 成功", flush=True)
        return r
    except Exception as e:
        print(f"{ts()} TRANSPORT.open 抛出: {type(e).__name__}: {e}", flush=True)
        raise
SerialTransport.open = open_

_o_start = SerialTransport._start_session
def start_(self):
    print(f"{ts()}   _start_session: io={'有' if self._io else '无'} closed={self._closed.is_set()}", flush=True)
    try:
        r = _o_start(self)
        print(f"{ts()}   _start_session 成功", flush=True)
        return r
    except Exception as e:
        print(f"{ts()}   _start_session 抛出: {e}", flush=True)
        raise
SerialTransport._start_session = start_

_o_rec = SerialTransport._auto_recover
def rec_(self, quiet_s=5.0):
    print(f"{ts()} _auto_recover 触发(stamp {time.monotonic()-self._recover_stamp:.1f}s 前)", flush=True)
    try:
        r = _o_rec(self, quiet_s)
        print(f"{ts()} _auto_recover 返回, is_open={self.is_open}", flush=True)
        return r
    except Exception as e:
        print(f"{ts()} _auto_recover 抛出: {type(e).__name__}: {e}", flush=True)
        raise
SerialTransport._auto_recover = rec_

_o_close = SerialTransport.close
def close_(self):
    print(f"{ts()} close() 调用", flush=True)
    return _o_close(self)
SerialTransport.close = close_

stop_flag = threading.Event()
def port_watch():
    present = True
    while not stop_flag.is_set():
        now = any(p.device == "COM8" for p in list_ports.comports())
        if now != present:
            print(f"{ts()} COM8 {'出现' if now else '消失'}", flush=True)
            present = now
        time.sleep(0.2)
threading.Thread(target=port_watch, daemon=True).start()

# ---- 场景 ----
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
    p.events.wait_event("music.started", timeout=100)
    print(f"{ts()} music.started ✓", flush=True)
    p.events.wait_event("music.ended", timeout=25)
    print(f"{ts()} music.ended ✓", flush=True)
except Exception as e:
    print(f"{ts()} 结果: {e}", flush=True)
stop_flag.set()
try:
    p.close()
except Exception:
    pass
