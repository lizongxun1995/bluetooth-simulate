"""真机推流断连复现探针。

连接板子 → 播放一段测试音 → 全程记录:
- transport 每次断链的原因与时刻(包装 _handle_broken)
- 串口是否从系统消失(0.5s 轮询 list_ports,区分"CH340 掉总线"与"仅读写出错")
- 每个音频帧的写入时刻/大小/当时信用点
- 关键事件时间线(过滤 audio.buffer,但统计其数量)
- 推流结束后 sys.info 是否还能通(自动重连耗时)

用法: python tools/stream_probe.py COM7 [时长秒,默认5]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serial.tools import list_ports  # noqa: E402

from btphone import BtPhone  # noqa: E402
from btphone.media import make_tone_wav  # noqa: E402

T0 = time.monotonic()


def ts() -> str:
    return f"[{time.monotonic() - T0:7.2f}s]"


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

    wav = Path(__file__).parent / "_probe_tone.wav"
    make_tone_wav(str(wav), duration_s=dur, freq=440)
    print(f"{ts()} 测试音 {wav} ({dur}s)")

    phone = BtPhone(port)
    tr = phone._transport
    print(f"{ts()} 已连接 {phone.info().get('name')}")

    breaks: list[tuple[float, str]] = []
    orig_broken = tr._handle_broken

    def spy_broken(reason: str) -> None:
        breaks.append((time.monotonic() - T0, reason))
        print(f"{ts()} *** 断链: {reason}")
        orig_broken(reason)

    tr._handle_broken = spy_broken

    # 记录音频帧写入(时刻/大小/当时信用点)
    orig_send = tr.send_audio
    frames: list[tuple[float, int, int]] = []

    def spy_send(codec: int, data: bytes, eos: bool = False) -> None:
        frames.append((time.monotonic() - T0, len(data), tr.audio_credit()))
        if eos or len(frames) % 20 == 1:
            print(f"{ts()} 帧 #{len(frames)} {len(data)}B credit={tr.audio_credit()}{' EOS' if eos else ''}")
        orig_send(codec, data, eos=eos)

    tr.send_audio = spy_send

    # 端口存在性监控(区分 CH340 掉总线 vs 读写出错)
    port_gone: list[tuple[float, float]] = []
    stop_flag = {"stop": False}
    import threading

    def watch_port() -> None:
        present = True
        while not stop_flag["stop"]:
            now = any(p.device == port for p in list_ports.comports())
            if present and not now:
                port_gone.append((time.monotonic() - T0, time.monotonic() - T0))
                print(f"{ts()} *** COM 口从系统消失!")
            if not present and now:
                print(f"{ts()} COM 口重新出现")
            present = now
            time.sleep(0.4)

    threading.Thread(target=watch_port, daemon=True).start()

    buf_events = {"n": 0, "last_free": -1}
    timeline: list[str] = []

    def on_evt(name: str, d: dict) -> None:
        if name == "audio.buffer":
            buf_events["n"] += 1
            buf_events["last_free"] = d.get("free", -1)
            return
        line = f"{ts()} ◀ {name} {d}"
        timeline.append(line)
        print(line)

    phone.on_event("*", on_evt)

    print(f"{ts()} 开始播放...")
    t_play = time.monotonic() - T0
    r = phone.music.play(files=[str(wav)], loop=False)
    print(f"{ts()} play → {r}")

    # 等 playlist.done(推流线程结束)最多 dur+40s
    deadline = time.monotonic() + dur + 40
    while time.monotonic() < deadline:
        if phone._worker is None:
            break
        time.sleep(0.2)
    print(f"{ts()} 推流线程结束(worker=None)")
    time.sleep(3)
    stop_flag["stop"] = True

    print("\n===== 汇总 =====")
    print(f"播放起点: {t_play:.2f}s, 音频帧 {len(frames)} 个")
    if frames:
        ws = [frames[i + 1][0] - frames[i][0] for i in range(len(frames) - 1)]
        print(f"帧间隔: min={min(ws):.3f}s max={max(ws):.3f}s avg={sum(ws)/len(ws):.3f}s")
    print(f"断链次数: {len(breaks)}")
    for t, reason in breaks:
        print(f"  - {t:.2f}s {reason}")
    print(f"COM 口消失次数: {len(port_gone)}")
    print(f"audio.buffer 心跳: {buf_events['n']} 条, 最后 free={buf_events['last_free']}")

    # 断链后探测自动恢复耗时
    if breaks:
        print("\n等待 5s 后测自动重连...")
        time.sleep(5)
        t0 = time.monotonic()
        try:
            info = phone.request("sys.info", timeout=130)
            print(f"{ts()} 自动重连成功,耗时 {time.monotonic()-t0:.1f}s: {info.get('name')}")
        except Exception as exc:  # noqa: BLE001
            print(f"{ts()} 自动重连失败: {exc}")

    phone.close()


if __name__ == "__main__":
    main()
