# -*- coding: utf-8 -*-
"""取证: 复现连接周期后的静默 panic, 抓 backtrace(v0.3.6 panic 延迟 2s)。
直连原始串口(不经 btphone transport, 注入后端会挡住 USB 瞬断自愈),
自建帧收发, 全部原始字节带时间戳落盘, USB 掉口自动重开。
"""
import sys, time, threading
sys.path.insert(0, r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate")
import serial
from btphone.protocol import pack_ctrl, FrameParser, unpack_ctrl, TYPE_CTRL

CAR = "00:11:22:33:44:55"
CAP = r"C:\Users\you\forensic_panic.bin"
T0 = time.monotonic()

def ts():
    return f"[{time.monotonic()-T0:7.2f}s]"

class Raw:
    def __init__(self):
        self.ser = None
        self.buf = bytearray()          # 已收字节(供解析)
        self.parser = FrameParser()     # 持久拆帧器:跨块半帧状态不能丢
        self.log = open(CAP, "wb", buffering=0)
        self.lock = threading.Lock()
        self.events = []                # (t, dict) 控制事件/响应
        self.dead = False
        self._open()
        threading.Thread(target=self._rx, daemon=True).start()

    def _open(self):
        for _ in range(40):
            try:
                self.ser = serial.Serial("COM8", 2_000_000, timeout=0.05)
                return True
            except Exception:
                time.sleep(0.5)
        return False

    def _rx(self):
        while not self.dead:
            if self.ser is None:
                if not self._open():
                    time.sleep(0.5)
                    continue
            try:
                data = self.ser.read(4096)
            except (serial.SerialException, OSError):
                self._note(b"\n<<PORT-DIED>>\n")
                try: self.ser.close()
                except Exception: pass
                self.ser = None
                continue
            if data:
                with self.lock:
                    self.buf += data
                    self.log.write(f"\n<<RX {time.monotonic()-T0:8.2f}>>\n".encode() + data)

    def _note(self, b):
        with self.lock:
            self.log.write(b)

    def send(self, obj):
        if self.ser is None:
            raise IOError("port dead")
        frame = pack_ctrl(obj, self._seq())
        self.log.write(f"\n>>TX {time.monotonic()-T0:8.2f}>>\n".encode() + frame + b"\n")
        self.ser.write(frame)

    _sq = 0
    def _seq(self):
        Raw._sq += 1
        return Raw._sq & 0xFFFF

    def pump(self):
        """把 buf 喂给拆帧器, 收集控制消息"""
        with self.lock:
            chunk = bytes(self.buf)
            self.buf.clear()
        for f in self.parser.feed(chunk):
            if f.type == TYPE_CTRL:
                try:
                    self.events.append((time.monotonic(), unpack_ctrl(f)))
                except Exception:
                    pass

    def drain_new(self):
        old = len(self.events)
        self.pump()
        return self.events[old:]

r = Raw()
time.sleep(1.0)

def req(cmd, args=None, wait=8.0):
    rid = 1000 + req._n
    req._n += 1
    r.send({"id": rid, "cmd": cmd, "args": args or {}})
    t_end = time.monotonic() + wait
    while time.monotonic() < t_end:
        for _, m in r.drain_new():
            if m.get("id") == rid:
                return m
        time.sleep(0.1)
    return None
req._n = 0

def heap():
    m = req("sys.info", wait=6)
    if m and m.get("ok"):
        res = m.get("result") or {}
        return f"heap={res.get('free_heap')} reset={res.get('reset')} rtc={res.get('rtc_boot')}"
    return f"sys.info无响应({m})"

def wait_a2dp(timeout):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        m = req("conn.status", wait=6)
        if m and m.get("ok"):
            c = (m.get("result") or {}).get("connections") or {}
            if c.get("a2dp") in ("connected", True):
                avrcp = c.get("avrcp")
                return True, avrcp
        time.sleep(1.0)
    return False, None

print(f"{ts()} 等待板上电回连(含 USB 瞬断自愈)...", flush=True)
ok, avrcp = wait_a2dp(90)
print(f"{ts()} a2dp={ok} avrcp={avrcp} | {heap()}", flush=True)

for cyc in range(4):
    print(f"\n{ts()} ===== 周期 {cyc+1}: 断开→回连 =====", flush=True)
    try:
        r.send({"id": 2000+cyc, "cmd": "conn.disconnect", "args": {"target": "all"}})
    except Exception as e:
        print(f"{ts()} TX失败: {e}", flush=True)
    time.sleep(16)
    print(f"{ts()} 断开后 | {heap()}", flush=True)
    try:
        r.send({"id": 2100+cyc, "cmd": "conn.connect", "args": {"target": CAR, "profiles": ["a2dp"]}})
    except Exception as e:
        print(f"{ts()} TX失败: {e}", flush=True)
    ok, avrcp = wait_a2dp(60)
    print(f"{ts()} 回连 a2dp={ok} avrcp={avrcp}", flush=True)
    time.sleep(8)   # 给车机初始交互(avrcp/注册/元数据拉取)留时间——panic 多发于此
    print(f"{ts()} +8s | {heap()}", flush=True)
    time.sleep(8)
    print(f"{ts()} +16s | {heap()}", flush=True)

print(f"\n{ts()} 额外观察 20s(捕 panic 文本)...", flush=True)
time.sleep(20)
r.dead = True
r.log.close()

# 扫描捕获里的 panic 痕迹
data = open(CAP, "rb").read()
for kw in (b"Guru", b"panic", b"abort", b"Backtrace", b"assert", b"WDT", b"brownout", b"LoadProhibited", b"StoreProhibited"):
    i = data.find(kw)
    if i >= 0:
        print(f"\n=== 命中 {kw} @byte{i} ===")
        print(data[max(0,i-200):i+1200].decode("utf-8", "replace"))
print(f"\n完成, 捕获: {CAP} ({len(data)}B)", flush=True)
