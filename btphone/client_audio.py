"""音频推流辅助:按固件流控事件把 ADPCM/PCM 数据分帧推到串口。"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from . import protocol
from .exceptions import TransportError

_AUDIO_CHUNK = 2048  # 每帧音频字节数(<= MAX_AUDIO_PAYLOAD)


def push_audio(
    transport,
    blob: bytes,
    abort: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int], None]] = None,
    chunk_size: int = _AUDIO_CHUNK,
    timeout: float = 30.0,
) -> int:
    """一次性推送整段音频(短音频上传用)。返回已发送字节数。"""
    return push_audio_stream(transport, blob, abort=abort, on_progress=on_progress,
                             chunk_size=chunk_size, timeout=timeout)


def push_audio_stream(
    transport,
    blob: bytes,
    abort: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int], None]] = None,
    chunk_size: int = _AUDIO_CHUNK,
    timeout: float = 30.0,
) -> int:
    """按设备上报的空闲缓冲分帧推送(音乐流式播放用),直到数据发完。

    设备通过 audio.buffer 事件上报空闲字节数;发送前等待空闲 >= chunk_size,
    形成自然背压。codec 复用 audio.open 会话设置,帧头 codec 字段填 0xFF 表示"沿用会话"。
    """
    sent = 0
    offset = 0
    total = len(blob)
    while offset < total:
        if abort is not None and abort.is_set():
            break
        n = min(chunk_size, total - offset)
        if not transport.wait_audio_buffer(n, timeout=timeout):
            raise TransportError(f"音频流控等待超时:设备 {timeout}s 内未释放 {n} 字节缓冲")
        transport.send_audio(0xFF, blob[offset : offset + n])
        offset += n
        sent = offset
        if on_progress is not None:
            on_progress(sent)
    if abort is None or not abort.is_set():
        transport.send_audio(0xFF, b"", eos=True)
    return sent
