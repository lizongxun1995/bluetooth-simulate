"""IMA-ADPCM 编解码(电脑侧)。

与固件 btphone/ima_adpcm.c 保持一致的块格式:
- 4bit/采样,恒定 4:1 压缩(相对 s16le)。
- 块大小 BLOCK_SAMPLES(奇数)。每块按声道分段,每段:
  [4字节头: predictor s16(=本声道首样本值)][step_index u8][_pad u8]]
  + nibble 数据(2样本/字节,低 nibble 在前),编码第 1..n-1 个样本。
- 首样本原值存于块头,其后样本从 (predictor=首样本, step_index=0) 起编码。
- 块独立解码,坏块只影响本块。
- 每声道 nibble 数恒为偶数(BLOCK_SAMPLES 为奇数 ⇒ n-1 为偶数),无填充歧义;
  末尾不足一块时编码端补零样本至奇数长度。
"""

from __future__ import annotations

import numpy as np

STEP_TABLE = np.array(
    [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
     34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
     157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
     724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
     3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
     15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767], dtype=np.int32)

INDEX_TABLE = np.array([-1, -1, -1, -1, 2, 4, 6, 8], dtype=np.int32)

BLOCK_SAMPLES = 1023  # 每块每声道样本数(奇数,保证 nibble 数为偶)


def samples_per_block(channels: int) -> int:
    return BLOCK_SAMPLES


def bytes_per_block(channels: int) -> int:
    return channels * (4 + (BLOCK_SAMPLES - 1) // 2)


def _reconstruct(nib: int, predictor: int, step: int) -> tuple[int, int]:
    """由 nibble 重建样本。返回 (新predictor, 新step_index 所需 nib)。"""
    delta = step >> 3
    if nib & 4:
        delta += step
    if nib & 2:
        delta += step >> 1
    if nib & 1:
        delta += step >> 2
    predictor += -delta if nib & 8 else delta
    predictor = max(-32768, min(32767, predictor))
    return predictor, nib


def _encode_block(block: np.ndarray, channels: int) -> bytes:
    n = block.shape[0]
    out = bytearray()
    for ch in range(channels):
        predictor = int(block[0, ch])
        step_index = 0
        out += predictor.to_bytes(2, "little", signed=True) + bytes([0, 0])
        nibbles: list[int] = []
        for i in range(1, n):
            sample = int(block[i, ch])
            diff = sample - predictor
            step = int(STEP_TABLE[step_index])
            nib = 0
            if diff < 0:
                nib = 8
                diff = -diff
            if diff >= step:
                nib |= 4
                diff -= step
            if diff >= step >> 1:
                nib |= 2
                diff -= step >> 1
            if diff >= step >> 2:
                nib |= 1
            predictor, _ = _reconstruct(nib, predictor, step)
            step_index = max(0, min(88, step_index + int(INDEX_TABLE[nib & 7])))
            nibbles.append(nib)
        for i in range(0, len(nibbles) - 1, 2):
            out.append(nibbles[i] | (nibbles[i + 1] << 4))
        if len(nibbles) % 2:  # BLOCK_SAMPLES 为奇数时不会走到;保险
            out.append(nibbles[-1])
    return bytes(out)


def encode(pcm: np.ndarray, channels: int) -> bytes:
    """pcm: int16 (n,) 或 (n, channels) → ADPCM 字节流。"""
    if pcm.ndim == 1:
        pcm = pcm.reshape(-1, 1)
    if pcm.shape[1] != channels:
        pcm = np.repeat(pcm[:, :1], channels, axis=1)
    pcm = pcm.astype(np.int16, copy=False)
    out = bytearray()
    for start in range(0, pcm.shape[0], BLOCK_SAMPLES):
        block = pcm[start : start + BLOCK_SAMPLES]
        if block.shape[0] % 2 == 0:  # 补零至奇数,保证 nibble 偶数且解码长度对齐
            block = np.concatenate([block, np.zeros((1, channels), dtype=np.int16)], axis=0)
        out += _encode_block(block, channels)
    return bytes(out)


def _decode_block(data: bytes, channels: int) -> np.ndarray:
    per_ch_bytes = (len(data) - 4 * channels) // channels
    if per_ch_bytes <= 0:
        return np.zeros((0, channels), dtype=np.int16)
    segment = 4 + per_ch_bytes
    samples = 1 + per_ch_bytes * 2
    result = np.empty((samples, channels), dtype=np.int32)
    for ch in range(channels):
        off = ch * segment
        predictor = int(np.frombuffer(data[off : off + 2], dtype="<i2")[0])
        step_index = data[off + 2]
        body = data[off + 4 : off + segment]
        arr = np.frombuffer(body, dtype=np.uint8)
        lo = (arr & 0x0F).astype(np.int8)
        hi = (arr >> 4).astype(np.int8)
        nibbles = np.empty(arr.size * 2, dtype=np.int8)
        nibbles[0::2] = lo
        nibbles[1::2] = hi
        result[0, ch] = predictor
        step = int(STEP_TABLE[step_index])
        for i, nib in enumerate(nibbles):
            step_here = step
            predictor, _ = _reconstruct(int(nib), predictor, step_here)
            step_index = max(0, min(88, step_index + int(INDEX_TABLE[int(nib) & 7])))
            step = int(STEP_TABLE[step_index])
            result[i + 1, ch] = predictor
    return result.astype(np.int16)


def decode(data: bytes, channels: int) -> np.ndarray:
    """ADPCM 字节流 → int16 (n, channels)。"""
    block_len = bytes_per_block(channels)
    chunks = []
    for off in range(0, len(data), block_len):
        chunks.append(_decode_block(data[off : off + block_len], channels))
    if not chunks:
        return np.zeros((0, channels), dtype=np.int16)
    return np.concatenate(chunks, axis=0)
