"""2M 单会话抓完整启动窗口,离线解码事件:回答"蓝牙栈到底起来没有"。

为什么单开 2M 而不先抓 115200:sys.ready 只在启动时发一次,若在文本相位
期间发出就永远错过了。这里开门即发 RTS 复位脉冲,从 0s 起在同一 2M 会话
里收全程:ROM 输出在 2M 下是乱码,FrameParser 会自动丢弃,CRC 足以滤掉
假同步。心跳(audio.buffer)不计入事件表,单独计数当"活着"信号。

判定:
  - sys.ready + bt.scan_mode(disc_conn) 出现      → 蓝牙栈 OK,问题在 USB/供电
  - sys.error 出现                                 → 固件报错,按 msg 修
  - 心跳在 ~4s(射频上电)附近断流/掉口             → 电流冲击砸掉 USB 链路
  - 心跳持续但始终无 sys.ready                     → app_main 卡死在 BT 初始化

用法: python tools/watch_bt_boot.py [COM口] [监听秒数,默认35]
"""
import json
import os
import sys
import time

import serial

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btphone.protocol import FrameParser  # noqa: E402

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 35.0

T0 = time.time()


def ts() -> str:
    return f"[{time.time() - T0:6.2f}s]"


events = []       # (t, name, data) 非 heartbeat 事件
beacons = 0       # audio.buffer 心跳计数
rx_total = 0
drops = []        # (t, 已收字节) 掉口时刻

ser = serial.Serial(PORT, 2_000_000, timeout=0.1)
try:
    # 开门后立即 RTS 脉冲复位:让板子从 0s 重新走完整启动流程
    ser.dtr = False
    ser.rts = True
    time.sleep(0.1)
    ser.rts = False
    print(f"{ts()} 复位脉冲已发,开始监听 {DUR:.0f}s")
except Exception as exc:
    print(f"{ts()} 脉冲期间掉口({exc.__class__.__name__}),静默后无脉冲重开")
    try:
        ser.close()
    except Exception:
        pass
    time.sleep(3.0)
    ser = serial.Serial(PORT, 2_000_000, timeout=0.1)

parser = FrameParser()
end = time.time() + DUR
while time.time() < end:
    try:
        n = ser.in_waiting
        if n:
            chunk = ser.read(n)
        else:
            time.sleep(0.02)
            continue
    except Exception as exc:
        print(f"{ts()} *** 掉口({exc.__class__.__name__},已收 {rx_total}B),静默 3s 等重枚举")
        drops.append((round(time.time() - T0, 2), rx_total))
        try:
            ser.close()
        except Exception:
            pass
        time.sleep(3.0)
        try:
            ser = serial.Serial(PORT, 2_000_000, timeout=0.1)  # 无脉冲,板子继续跑
        except Exception:
            continue
        continue
    t = time.time() - T0
    rx_total += len(chunk)
    for frame in parser.feed(chunk):
        if not frame.is_ctrl:
            continue
        try:
            obj = json.loads(frame.payload.decode("utf-8", "replace"))
        except ValueError:
            continue
        if "evt" in obj:
            name = str(obj["evt"])
            if name == "audio.buffer":
                beacons += 1
            else:
                events.append((t, name, obj.get("data", {})))

try:
    ser.close()
except Exception:
    pass

print(f"\n=== {DUR:.0f}s 共收 {rx_total} 字节,掉口 {len(drops)} 次 @ {drops} ===")
print(f"=== audio.buffer 心跳 {beacons} 次(全程健康约 {int((DUR - 1) * 20)} 次) ===")
print("=== 非 heartbeat 事件 ===")
if not events:
    print("(无 —— 既没有 sys.ready,也没有 sys.error)")
for t, name, data in events:
    print(f"[{t:6.2f}s] {name}: {data}")
