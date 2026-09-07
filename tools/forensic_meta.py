# -*- coding: utf-8 -*-
"""取证: avrcp.metadata 偶发 10s 无响应(板子活着)。
不重启板子(重启会触发射频上电 USB 瞬断, 注入的 LogIO 会挡住恢复)——
改用 conn.disconnect/conn.connect 重建"a2dp/avrcp 刚连上"的早期窗口,
立即发 avrcp.metadata。双向原始字节带时间戳落盘。
"""
import sys, time
sys.path.insert(0, r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate")

import serial

CAP = r"C:\Users\you\forensic_meta.bin"
T0 = time.monotonic()

def ts():
    return f"[{time.monotonic()-T0:6.2f}s]"

class LogIO:
    """代理串口: 双向原始字节落盘, 每块前打时间戳标记。"""
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
            self._f.write(f"\n<<RX {time.monotonic()-T0:9.3f}>>\n".encode() + bytes(data))
        return data
    def write(self, data):
        self._f.write(f"\n>>TX {time.monotonic()-T0:9.3f}>>\n".encode() + bytes(data) + b"\n")
        return self._inner.write(data)
    def close(self):
        try:
            self._inner.close()
        finally:
            self._f.close()

inner = serial.Serial("COM8", 2_000_000, timeout=0.1)
io = LogIO(inner, CAP)

from btphone import BtPhone
p = BtPhone("COM8", io=io)

def dump(name, data):
    if name in ("audio.buffer",):
        return
    print(f"{ts()} EV {name} {dict(data)}", flush=True)
p.events.on("*", dump)

def state(name):
    try:
        conn = (p.request("conn.status", {}, timeout=10).get("connections") or {})
        return conn.get(name)
    except Exception as e:
        return f"ERR:{e}"

def wait_state(name, want, timeout):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = state(name)
        if last in want:
            return True
        time.sleep(0.3)
    return False

print(f"{ts()} 初始状态: a2dp={state('a2dp')} avrcp={state('avrcp')}", flush=True)

for rnd in range(3):
    print(f"\n{ts()} ===== 第 {rnd+1} 轮: 断开 → 重连 → 立即 metadata =====", flush=True)
    try:
        p.conn.disconnect("all")
    except Exception as e:
        print(f"{ts()} disconnect: {e}", flush=True)
    wait_state("a2dp", ("disconnected", False), 15)
    print(f"{ts()} 已断开, 重新连接 a2dp", flush=True)
    try:
        p.conn.connect("00:11:22:33:44:55", profiles=["a2dp"])
    except Exception as e:
        print(f"{ts()} connect: {e}", flush=True)
    if not wait_state("a2dp", ("connected", True), 40):
        print(f"{ts()} a2dp 未回连: {state('a2dp')}", flush=True)
        continue
    t_a2dp = time.monotonic()
    # avrcp 通常紧随 a2dp;等它(最多 15s),一到就立即发 metadata(复现早期窗口)
    wait_state("avrcp", ("connected", True), 15)
    t_avrcp = time.monotonic()
    print(f"{ts()} a2dp +{t_a2dp and 0:.0f} avrcp={state('avrcp')} (+{t_avrcp-t_a2dp:.1f}s) → 立即 metadata", flush=True)
    try:
        r = p.request("avrcp.metadata", {"title": f"法证曲{rnd}", "artist": "VPHONE",
                                         "album": "forensic", "duration_ms": 5000,
                                         "track_no": 1, "total_tracks": 1}, timeout=15)
        dt = time.monotonic() - t_avrcp
        print(f"{ts()} ✓ 应答(+{dt:.1f}s): {r}", flush=True)
    except Exception as e:
        dt = time.monotonic() - t_avrcp
        print(f"{ts()} ✗ 失败(+{dt:.1f}s): {e}", flush=True)
    try:
        info = p.request("sys.info", {}, timeout=10)
        print(f"{ts()} 板子活着: heap={info.get('free_heap')}", flush=True)
    except Exception as e:
        print(f"{ts()} sys.info 也挂了: {e}", flush=True)
    time.sleep(3)

try:
    p.close()
except Exception:
    pass
print(f"完成, 捕获: {CAP}", flush=True)
