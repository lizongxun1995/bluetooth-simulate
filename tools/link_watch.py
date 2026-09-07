"""链路节律观测:分两个阶段刻画 USB 通路滞留的形态。

阶段A(纯听 15s,零发送): 记录每帧到达时刻。
  - beacons 平滑 50ms 间隔 → 空闲时链路健康,问题只在收发交互时出现
  - beacons 断流几秒后成批涌到 → 数据在通路里被滞留(挂起/TT 批量转发)
阶段B(5 轮 echo,每轮间隔 3s): 量每轮精确 RTT。
输出: 每秒帧数、最大静默间隔、echo RTT 列表。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import serial  # noqa: E402

from btphone import protocol  # noqa: E402


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM8"
    ser = serial.Serial(port, 2_000_000, timeout=0.05)
    parser = protocol.FrameParser()
    t0 = time.monotonic()

    arrivals: list[float] = []
    n_audio = 0

    def listen(seconds: float) -> list:
        got = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for f in parser.feed(ser.read(4096)):
                got.append(f)
                arrivals.append(time.monotonic() - t0)
                if not f.is_ctrl:
                    n_audio_local[0] += 1
        return got

    n_audio_local = [0]

    print(f"=== 阶段A: 纯听 15s(零发送) ===")
    listen(15.0)
    if arrivals:
        gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
        gaps.sort()
        p50 = gaps[len(gaps) // 2]
        mx = gaps[-1]
        print(f"帧数 {len(arrivals)}, 间隔 p50={p50*1000:.0f}ms 最大={mx*1000:.0f}ms")
        # 每秒帧数分布
        sec = 0
        cnt = 0
        for t in [a for a in arrivals if a <= 15.0]:
            while t > sec + 1:
                print(f"  [{sec:2d}s] {'#'*min(cnt, 60)} {cnt}")
                sec += 1
                cnt = 0
            cnt += 1
        print(f"  [{sec:2d}s] {'#'*min(cnt, 60)} {cnt}")
    else:
        print("一帧都没收到!")

    print(f"\n=== 阶段B: 5 轮 echo RTT(每轮隔 3s) ===")
    seq = 200
    for i in range(1, 6):
        req = {"id": seq, "cmd": "sys.echo", "args": {"msg": f"rtt{i}"}}
        ser.write(protocol.pack_ctrl(req, seq))
        ser.flush()
        t_send = time.monotonic()
        rtt = None
        resp_seq = f'"id":{seq}'.encode()
        while time.monotonic() - t_send < 8.0:
            for f in parser.feed(ser.read(4096)):
                arrivals.append(time.monotonic() - t0)
                if f.is_ctrl and resp_seq in f.payload and b'"ok"' in f.payload:
                    rtt = time.monotonic() - t_send
        print(f"  echo{i}: RTT = {rtt*1000 if rtt else -1:.0f}ms")
        seq += 1
        time.sleep(3.0)

    ser.close()
    print(f"\n总帧数 {len(arrivals)}(含beacons), CTRL音频帧 {n_audio_local[0]}")


if __name__ == "__main__":
    main()
