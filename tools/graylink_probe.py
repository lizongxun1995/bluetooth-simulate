"""灰链路判别探针:区分"链路单向半死"与"固件命令处理卡死"。

阶段1: 每 0.5s 发一条 sys.echo,持续 15s —— 若期间心跳正常但 echo 开始
       超时,且从未调用过任何复杂命令 → 链路问题。
阶段2: 发一条 avrcp.metadata,再继续 echo 15s —— 若 echo 恰好在 metadata
       之后开始超时 → 固件命令处理卡死。
全程统计 audio.buffer 心跳条数与 CRC 错误数。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from btphone import BtPhone  # noqa: E402

T0 = time.monotonic()


def ts() -> str:
    return f"[{time.monotonic() - T0:7.2f}s]"


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    phone = BtPhone(port)
    tr = phone._transport
    print(f"{ts()} 已连接 {phone.info().get('name')}")

    beacon = {"n": 0}

    def on_evt(name: str, d: dict) -> None:
        if name == "audio.buffer":
            beacon["n"] += 1

    phone.on_event("*", on_evt)

    def echo_round(tag: str, seconds: float) -> int:
        fails = 0
        deadline = time.monotonic() + seconds
        i = 0
        while time.monotonic() < deadline:
            i += 1
            b0 = beacon["n"]
            try:
                r = phone.request("sys.echo", {"msg": f"p{i}"}, timeout=3)
                ok = r.get("msg") == f"p{i}"
                if i % 10 == 1 or not ok:
                    print(f"{ts()} [{tag}] echo#{i} {'OK' if ok else '内容错!'} "
                          f"(心跳+{beacon['n'] - b0}, crc_err={tr.crc_errors})")
                if not ok:
                    fails += 1
            except Exception as exc:  # noqa: BLE001
                fails += 1
                print(f"{ts()} [{tag}] echo#{i} 超时/失败: {exc} "
                      f"(心跳+{beacon['n'] - b0}, crc_err={tr.crc_errors})")
            time.sleep(0.5)
        return fails

    print(f"{ts()} === 阶段1: 纯 echo 15s ===")
    f1 = echo_round("纯echo", 15)

    print(f"{ts()} === 阶段2: avrcp.metadata 后继续 echo 15s ===")
    b0 = beacon["n"]
    try:
        phone.request("avrcp.metadata", {
            "title": "probe", "artist": "", "album": "",
            "duration_ms": 5000, "track_no": 1, "total_tracks": 1,
        }, timeout=5)
        print(f"{ts()} metadata OK (心跳+{beacon['n'] - b0})")
    except Exception as exc:  # noqa: BLE001
        print(f"{ts()} metadata 失败: {exc} (心跳+{beacon['n'] - b0})")
    f2 = echo_round("meta后", 15)

    print(f"\n===== 结论 =====")
    print(f"阶段1 失败 {f1} 次 / 阶段2 失败 {f2} 次 / 心跳共 {beacon['n']} 条 / crc_err={tr.crc_errors}")
    if f1 > 0:
        print("→ 纯 echo 也超时:链路灰死(PC→板方向断),与固件命令无关")
    elif f2 > 0:
        print("→ metadata 之后才失败:固件 avrcp.metadata 卡死接收任务")
    else:
        print("→ 本轮全部正常(间歇性问题)")
    phone.close()


if __name__ == "__main__":
    main()
