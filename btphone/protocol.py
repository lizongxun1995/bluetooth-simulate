"""串口帧协议:PC 与 ESP32 固件共用的线上格式。

帧结构(小端)::

    0xA5 0x5A   同步字(2B)
    TYPE  u8    0x01=CTRL(JSON 控制) 0x02=AUDIO(音频数据)
    LEN   u16   负载长度(不含帧头/帧尾)
    SEQ   u16   按 TYPE 独立递增的序号
    PAYLOAD     LEN 字节
    CRC   u16   CRC16-CCITT(poly=0x1021, init=0xFFFF),覆盖 TYPE..PAYLOAD

CTRL 负载是 UTF-8 JSON,三种形态::

    请求: {"id":1, "cmd":"music.play", "args":{...}}
    响应: {"id":1, "ok":true, "result":{...}} / {"id":1, "ok":false, "error":"..."}
    事件: {"evt":"bt.a2dp.state", "data":{...}}

AUDIO 负载头部 2 字节::

    [codec u8][flags u8] + 数据
    codec: 0=PCM(s16le) 1=IMA-ADPCM(带块头)
    flags: bit0=帧末尾(eos)

会话参数(采样率/声道/位宽)不随帧携带,由控制通道 audio.open 声明。
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Iterator, Optional

SYNC = b"\xA5\x5A"

TYPE_CTRL = 0x01
TYPE_AUDIO = 0x02

CODEC_PCM_S16LE = 0x00
CODEC_ADPCM_IMA = 0x01

FLAG_EOS = 0x01

MAX_CTRL_PAYLOAD = 4096
MAX_AUDIO_PAYLOAD = 4096

HEADER_STRUCT = struct.Struct("<BBH")  # type, _pad, len  (在同步字之后)
HEADER_SIZE = 2 + 3 + 2 + 2  # sync + type + len + seq
CRC_SIZE = 2

# CRC16-CCITT 查表
_CRC_TABLE = []
for _b in range(256):
    _crc = _b << 8
    for _ in range(8):
        _crc = ((_crc << 1) ^ 0x1021) if (_crc & 0x8000) else (_crc << 1)
    _CRC_TABLE.append(_crc & 0xFFFF)


def crc16(data: bytes, init: int = 0xFFFF) -> int:
    crc = init
    for byte in data:
        crc = _CRC_TABLE[((crc >> 8) ^ byte) & 0xFF] ^ ((crc & 0xFF) << 8)
    return crc & 0xFFFF


@dataclass
class Frame:
    type: int
    seq: int
    payload: bytes

    @property
    def is_ctrl(self) -> bool:
        return self.type == TYPE_CTRL


def pack_frame(frame_type: int, payload: bytes, seq: int) -> bytes:
    if len(payload) > MAX_AUDIO_PAYLOAD:
        raise ValueError(f"payload too large: {len(payload)}")
    # seq 线上只有 16 位;控制帧的实际匹配键是 JSON 里的 id(32 位,时间种子),
    # 这里静默掩码——大 id 直接 pack 会抛 struct.error。
    head = HEADER_STRUCT.pack(frame_type, 0, len(payload)) + struct.pack("<H", seq & 0xFFFF)
    body = head + payload
    return SYNC + body + struct.pack("<H", crc16(body))


class FrameParser:
    """增量解析字节流为帧。用法:feed() 喂数据,frames 迭代取完整帧。"""

    STATE_SYNC1, STATE_SYNC2, STATE_HEADER, STATE_BODY = range(4)
    HEADER_LEN = 6  # type(1) + pad(1) + len(2) + seq(2)  (同步字之后、负载之前)

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._state = self.STATE_SYNC1
        self._header = bytearray()
        self._body = bytearray()
        self._need = 0

    def feed(self, data: bytes) -> Iterator[Frame]:
        """喂入任意长度字节流,产出解析出的完整帧(带 CRC 校验,坏帧丢弃)。"""
        for byte in data:
            frame = self._feed_byte(byte)
            if frame is not None:
                yield frame

    def _feed_byte(self, byte: int) -> Optional[Frame]:
        if self._state == self.STATE_SYNC1:
            if byte == 0xA5:
                self._state = self.STATE_SYNC2
        elif self._state == self.STATE_SYNC2:
            self._state = self.STATE_HEADER if byte == 0x5A else self.STATE_SYNC1
            self._header.clear()
        elif self._state == self.STATE_HEADER:
            self._header.append(byte)
            if len(self._header) == self.HEADER_LEN:
                frame_type = self._header[0]
                length = struct.unpack_from("<H", self._header, 2)[0]
                if frame_type not in (TYPE_CTRL, TYPE_AUDIO) or length > MAX_AUDIO_PAYLOAD:
                    # 非法帧头,回到找同步字状态(丢弃已收的半个头)
                    self._state = self.STATE_SYNC1
                    return None
                self._need = length
                self._body.clear()
                self._state = self.STATE_BODY
        elif self._state == self.STATE_BODY:
            self._body.append(byte)
            if len(self._body) == self._need + CRC_SIZE:
                payload = bytes(self._body[: self._need])
                (rx_crc,) = struct.unpack_from("<H", self._body, self._need)
                frame_type, seq = self._header[0], struct.unpack_from("<H", self._header, 4)[0]
                self._state = self.STATE_SYNC1
                calc = crc16(bytes(self._header) + payload)
                if calc == rx_crc:
                    return Frame(frame_type, seq, payload)
                # CRC 错:静默丢帧(计数由外层统计)
        return None


def pack_ctrl(obj: dict, seq: int) -> bytes:
    return pack_frame(TYPE_CTRL, json.dumps(obj, ensure_ascii=False).encode("utf-8"), seq)


def unpack_ctrl(frame: Frame) -> dict:
    return json.loads(frame.payload.decode("utf-8"))


def audio_payload(codec: int, data: bytes, eos: bool = False) -> bytes:
    flags = FLAG_EOS if eos else 0
    return bytes([codec, flags]) + data
