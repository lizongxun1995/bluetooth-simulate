# -*- coding: utf-8 -*-
"""取证: 复现"板子在 play 序列后 ~3.5s 重启"。
esptool 硬复位 → 立即连接 → a2dp 未回连窗口发 play → 抓原始字节找 panic/brownout。
"""
import sys, time, subprocess, threading
sys.path.insert(0, r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate")

import serial

TONE = r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate\tools\_probe_tone.wav"
CAP = r"C:\Users\you\forensic_reboot.bin"
T0 = time.monotonic()

def ts():
    return f"[{time.monotonic()-T0:6.1f}s]"

class LogIO:
    """代理串口: 所有 RX 落盘(含 panic 文本), TX 前缀标记。"""
    def __init__(self, inner, path):
        self._inner = inner
        self._f = open(path, "wb", buffering=0)
        self.port = inner.port
        self.in_waiting = 0
    def __getattr__(self, name):
        return getattr(self._inner, name)
    def read(self, n=1):
        data = self._inner.read(n)
        if data:
            self._f.write(data)
        return data
    def write(self, data):
        self._f.write(b"\n>>TX>>" + bytes(data) + b"\n")
        return self._inner.write(data)
    def close(self):
        try:
            self._inner.close()
        finally:
            self._f.close()

print(f"{ts()} esptool 硬复位板子", flush=True)
subprocess.run([sys.executable, "-m", "esptool", "--port", "COM8",
                "--before", "default_reset", "--after", "hard_reset", "chip_id"],
               capture_output=True, timeout=30)

time.sleep(0.5)
inner = serial.Serial("COM8", 2_000_000, timeout=0.1)
io = LogIO(inner, CAP)

from btphone import BtPhone
p = BtPhone("COM8", io=io)

def dump(name, data):
    if name in ("audio.buffer",):
        return
    print(f"{ts()} EV {name} {dict(data)}", flush=True)

p.events.on("*", dump)
print(f"{ts()} 已连接(握手完成)", flush=True)

try:
    conn = p.request("conn.status", {}, timeout=4).get("connections") or {}
    print(f"{ts()} a2dp={conn.get('a2dp')} hfp={conn.get('hfp')}", flush=True)
except Exception as e:
    print(f"{ts()} status: {e}", flush=True)

print(f"{ts()} 立即 p.music.play()", flush=True)
p.music.play(files=[TONE])
try:
    d = p.events.wait_event("music.started", timeout=75)
    print(f"{ts()} music.started ✓", flush=True)
    try:
        p.events.wait_event("music.ended", timeout=20)
        print(f"{ts()} music.ended ✓", flush=True)
    except Exception:
        print(f"{ts()} music.ended 未到", flush=True)
except Exception as e:
    print(f"{ts()} music.started 未到: {e}", flush=True)
    try:
        p.music.stop()
    except Exception as e2:
        print(f"{ts()} music.stop: {e2}", flush=True)

time.sleep(1)
try:
    info = p.request("sys.info", {}, timeout=8)
    print(f"{ts()} reset_reason={info.get('boot_reset_reason')} heap={info.get('free_heap')}", flush=True)
except Exception as e:
    print(f"{ts()} sys.info 失败: {e}", flush=True)
try:
    p.close()
except Exception:
    pass
print("完成", flush=True)
