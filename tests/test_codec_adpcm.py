import numpy as np

from btphone import codec_adpcm


def _sine(n: int, channels: int, freq: float = 440.0, rate: int = 16000) -> np.ndarray:
    t = np.arange(n) / rate
    wave_data = np.sin(2 * np.pi * freq * t) * 12000
    data = wave_data.astype(np.int16).reshape(-1, 1)
    if channels > 1:
        data = np.repeat(data, channels, axis=1)
    return data


def _snr(original: np.ndarray, decoded: np.ndarray) -> float:
    n = min(len(original), len(decoded))
    sig = original[:n].astype(np.float64)
    err = sig - decoded[:n].astype(np.float64)
    power = np.mean(sig**2)
    noise = np.mean(err**2)
    if noise == 0:
        return float("inf")
    return 10 * np.log10(power / noise)


def test_roundtrip_mono():
    pcm = _sine(4000, 1)
    blob = codec_adpcm.encode(pcm, 1)
    # 压缩率约 4:1
    assert len(blob) < len(pcm.tobytes()) / 3
    out = codec_adpcm.decode(blob, 1)
    assert _snr(pcm, out) > 20  # IMA-ADPCM 典型 >25dB


def test_roundtrip_stereo_independent_channels():
    pcm = _sine(3000, 2)
    pcm[:, 1] //= 2  # 两声道不同内容,验证不串声道
    blob = codec_adpcm.encode(pcm, 2)
    out = codec_adpcm.decode(blob, 2)
    assert out.shape[1] == 2
    assert _snr(pcm[:, 0], out[:, 0]) > 20
    assert _snr(pcm[:, 1], out[:, 1]) > 20


def test_block_independence():
    """坏块只影响自身:破坏首块数据,第二块仍可正常解码。"""
    pcm = _sine(2 * codec_adpcm.BLOCK_SAMPLES, 1)
    blob = codec_adpcm.encode(pcm, 1)
    corrupted = bytearray(blob)
    block_bytes = codec_adpcm.bytes_per_block(1)
    for i in range(4, block_bytes):
        corrupted[i] ^= 0xFF
    out = codec_adpcm.decode(bytes(corrupted), 1)
    # 第二块样本起点 = BLOCK_SAMPLES(块解码长度精确对齐)
    seg = slice(codec_adpcm.BLOCK_SAMPLES + 32, codec_adpcm.BLOCK_SAMPLES + 900)
    assert _snr(pcm[seg], out[seg]) > 20


def test_empty():
    out = codec_adpcm.decode(b"", 2)
    assert len(out) == 0
