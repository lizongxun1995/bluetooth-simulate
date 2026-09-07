"""读法对比探针:同一条链路、同一个驱动,只改应用程序的读法。

阶段A: ser.read(4096) + timeout —— 现行读法,预期 ~3.3s 批量(4096B/1.3KBps)
阶段B: in_waiting 驱动的精确读 —— 经典 Win32 串口模式,预期 ~50ms 平滑

若 A 批、B 平滑 → 根因 = 大块 ReadFile 在 ch341ser 上"攒满才完成",
修复在 btphone 传输层,零硬件/系统改动。
"""

from __future__ import annotations

import sys
import time

import serial


def histogram(events: list[float], dur: float, label: str) -> None:
    gaps = sorted(b - a for a, b in zip(events, events[1:]))
    print(f"{label}: 到达 {len(events)} 次, 间隔 p50={gaps[len(gaps)//2]*1000:.0f}ms "
          f"max={gaps[-1]*1000:.0f}ms" if gaps else f"{label}: 无数据")
    sec = 0
    cnt = 0
    for t in events:
        while t > sec + 1:
            print(f"  [{sec:2d}s] {'#'*min(cnt,60)} {cnt}")
            sec += 1
            cnt = 0
        cnt += 1
    print(f"  [{sec:2d}s] {'#'*min(cnt,60)} {cnt}")


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM9"
    ser = serial.Serial(port, 2_000_000, timeout=0.05)
    time.sleep(1.0)  # 让开板瞬间积压
    ser.reset_input_buffer()

    print("=== 阶段A: read(4096) ===")
    events = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10.0:
        if ser.read(4096):
            events.append(time.monotonic() - t0)
    histogram(events, 10.0, "A")

    time.sleep(1.0)
    ser.reset_input_buffer()
    print("\n=== 阶段B: in_waiting 精确读 ===")
    events = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10.0:
        n = ser.in_waiting
        if n:
            ser.read(n)
            events.append(time.monotonic() - t0)
        else:
            time.sleep(0.001)
    histogram(events, 10.0, "B")

    ser.close()


if __name__ == "__main__":
    main()
