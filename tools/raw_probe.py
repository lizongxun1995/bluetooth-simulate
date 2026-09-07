"""裸协议探针:不经 btphone 库,直接 pyserial 收发帧。

每轮: 发一条 sys.echo 原始帧 → 读 3s → 打印收到的全部帧(事件/响应)。
若裸 echo 也无响应 → 板子/链路吞请求;若裸 echo 正常 → btphone 库的 bug。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import serial  # noqa: E402

from btphone import protocol  # noqa: E402


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    ser = serial.Serial(port, 2_000_000, timeout=0.05)
    parser = protocol.FrameParser()
    t0 = time.monotonic()

    def drain(seconds: float, label: str) -> list:
        got = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            chunk = ser.read(4096)
            if not chunk:
                continue
            for f in parser.feed(chunk):
                got.append(f)
                if f.is_ctrl:
                    txt = f.payload.decode("utf-8", "replace")
                    print(f"[{time.monotonic()-t0:6.2f}s] {label} ◀ type={f.type} seq={f.seq} {txt[:120]}")
                else:
                    print(f"[{time.monotonic()-t0:6.2f}s] {label} ◀ AUDIO {len(f.payload)}B")
        return got

    print(f"[{time.monotonic()-t0:6.2f}s] opened, 先听 2s 心跳...")
    drain(2.0, "idle")

    seq = 100
    for i in range(1, 6):
        req = {"id": seq, "cmd": "sys.echo", "args": {"msg": f"raw{i}"}}
        frame = protocol.pack_ctrl(req, seq)
        print(f"\n[{time.monotonic()-t0:6.2f}s] ▶ 发 echo raw{i} ({len(frame)}B): {frame.hex()}")
        ser.write(frame)
        ser.flush()
        got = drain(3.0, f"echo{i}")
        resp = [f for f in got if f.is_ctrl and b'"ok"' in f.payload]
        print(f"    → 响应帧 {len(resp)} 个")
        seq += 1
        time.sleep(0.5)

    # 对照:sys.info
    req = {"id": seq, "cmd": "sys.info", "args": {}}
    frame = protocol.pack_ctrl(req, seq)
    print(f"\n[{time.monotonic()-t0:6.2f}s] ▶ 发 sys.info")
    ser.write(frame)
    ser.flush()
    got = drain(3.0, "info")
    resp = [f for f in got if f.is_ctrl and b'"ok"' in f.payload]
    print(f"    → 响应帧 {len(resp)} 个")

    ser.close()


if __name__ == "__main__":
    main()
