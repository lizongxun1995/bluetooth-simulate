#!/usr/bin/env python
"""生成测试音频曲库:正弦/扫频/静音/类音乐(和弦琶音)WAV,用于无素材自测。

用法:
    python tools/gen_test_audio.py D:\\曲库 [--duration 5]
"""

import argparse
import os

import numpy as np
import wave


def write_wav(path: str, pcm: np.ndarray, rate: int = 44100, channels: int = 2) -> None:
    data = (np.clip(pcm, -1, 1) * 20000).astype("<i2")
    if channels > 1:
        data = np.repeat(data, channels, axis=1)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(data.tobytes())


def make(kind: str, dur: float, rate: int = 44100) -> np.ndarray:
    t = np.arange(int(dur * rate)) / rate
    if kind == "sine":
        return {"wave": np.sin(2 * np.pi * 440 * t)}
    if kind == "sweep":
        sweep = np.sin(2 * np.pi * (200 + 1800 * t / dur) * t)
        return {"wave": sweep}
    if kind == "silence":
        return {"wave": np.zeros_like(t)}
    if kind == "music":
        # 类音乐:和弦琶音 + 简单包络
        notes = [261.63, 329.63, 392.0, 523.25]
        wave_data = np.zeros_like(t)
        for i, f in enumerate(notes):
            start = i * dur / len(notes)
            seg = (t >= start) & (t < start + dur / len(notes))
            wave_data[seg] += np.sin(2 * np.pi * f * t[seg]) * np.exp(-3 * (t[seg] - start))
        return {"wave": wave_data / max(1e-9, np.max(np.abs(wave_data)))}
    raise ValueError(kind)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--duration", type=float, default=5.0)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    names = {
        "sine": "01_正弦440",
        "sweep": "02_扫频",
        "music": "03_类音乐和弦",
        "silence": "04_静音",
    }
    for kind, title in names.items():
        path = os.path.join(args.outdir, f"{title}.wav")
        result = make(kind, args.duration)
        write_wav(path, result["wave"].reshape(-1, 1))
        print(f"[OK] {path}")
    print("提示:正式压测请换成真实歌曲(MP3亦可,库会自动经 ffmpeg 转码)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
