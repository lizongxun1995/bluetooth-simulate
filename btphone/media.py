"""媒体文件处理:解码为 PCM、ADPCM 编码、测试音生成。

解码依赖:
- WAV 文件用标准库 wave 直接读
- 其他格式(mp3/flac/m4a...)调用系统 ffmpeg(需 PATH 中可用)
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from dataclasses import dataclass

import numpy as np

from .exceptions import AudioFormatError


@dataclass
class DecodedAudio:
    pcm: np.ndarray  # int16 (n, channels)
    rate: int
    channels: int

    @property
    def duration_ms(self) -> int:
        return int(self.pcm.shape[0] * 1000 / self.rate)



def _read_wav(path: str) -> tuple[np.ndarray, int, int]:
    with wave.open(path, "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise AudioFormatError(f"仅支持 16bit WAV(当前 {width*8}bit),请安装 ffmpeg 转码: {path}")
    data = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        data = data.reshape(-1, channels)
    else:
        data = data.reshape(-1, 1)
    return data, rate, channels


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def decode_audio(path: str, rate: int = 44100, channels: int = 2) -> DecodedAudio:
    """解码任意音频文件为 s16le PCM。非 WAV 或参数不符时走 ffmpeg 重采样。"""
    try:
        data, src_rate, src_channels = _read_wav(path)
        if src_rate == rate and src_channels == channels:
            return DecodedAudio(data, rate, channels)
    except (wave.Error, AudioFormatError, FileNotFoundError):
        data = None
    if not _ffmpeg_available():
        raise AudioFormatError(
            f"无法解码 {path}:非 16bit WAV 且未找到 ffmpeg。请安装 ffmpeg 或改用 WAV 文件。"
        )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
        "-ar", str(rate), "-ac", str(channels), "-f", "s16le", "pipe:1",
    ]
    try:
        raw = subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout
    except subprocess.CalledProcessError as exc:
        raise AudioFormatError(f"ffmpeg 解码失败: {exc.stderr.decode(errors='replace')[:200]}") from exc
    data = np.frombuffer(raw, dtype="<i2").reshape(-1, channels)
    return DecodedAudio(data, rate, channels)


def make_tone_wav(
    path: str,
    duration_s: float = 2.0,
    freq: float = 440.0,
    rate: int = 44100,
    channels: int = 2,
    kind: str = "sine",
) -> str:
    """生成测试音 WAV(正弦/扫频/静音),用于无素材时自测。"""
    n = int(duration_s * rate)
    t = np.arange(n, dtype=np.float64) / rate
    if kind == "sine":
        wave_data = np.sin(2 * np.pi * freq * t)
    elif kind == "sweep":
        wave_data = np.sin(2 * np.pi * freq * (t ** 2) * (rate / 2 / duration_s))
    elif kind == "silence":
        wave_data = np.zeros(n)
    else:
        raise ValueError(f"未知音型: {kind}")
    data = (wave_data * 20000).astype("<i2").reshape(-1, 1)
    if channels > 1:
        data = np.repeat(data, channels, axis=1)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(data.tobytes())
    return path
