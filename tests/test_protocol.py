import json

import pytest

from btphone.protocol import (
    FrameParser,
    audio_payload,
    crc16,
    pack_ctrl,
    pack_frame,
    unpack_ctrl,
    TYPE_AUDIO,
    TYPE_CTRL,
)


def test_crc16_ccitt_false_known_vector():
    # CRC-16/CCITT-FALSE("123456789") = 0x29B1
    assert crc16(b"123456789") == 0x29B1


def test_ctrl_roundtrip():
    obj = {"id": 7, "cmd": "music.play", "args": {"sink": "a2dp"}}
    frame_bytes = pack_ctrl(obj, 7)
    parser = FrameParser()
    frames = list(parser.feed(frame_bytes))
    assert len(frames) == 1
    assert frames[0].type == TYPE_CTRL
    assert unpack_ctrl(frames[0]) == obj


def test_parser_handles_split_feed():
    obj = {"id": 1, "cmd": "sys.info", "args": {}}
    raw = pack_ctrl(obj, 1)
    parser = FrameParser()
    frames = []
    # 逐字节喂入
    for i in range(len(raw)):
        frames += list(parser.feed(raw[i : i + 1]))
    assert len(frames) == 1
    assert unpack_ctrl(frames[0]) == obj


def test_parser_recovers_from_corruption():
    good = pack_ctrl({"id": 2, "cmd": "a", "args": {}}, 2)
    parser = FrameParser()
    # 坏帧:合法头 + 坏 CRC
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    frames = list(parser.feed(bytes(bad) + good))
    assert len(frames) == 1
    assert unpack_ctrl(frames[0])["id"] == 2


def test_audio_payload_flags():
    payload = audio_payload(0x01, b"\x01\x02", eos=True)
    assert payload[0] == 0x01
    assert payload[1] & 0x01
    assert payload[2:] == b"\x01\x02"


def test_payload_too_large_rejected():
    with pytest.raises(ValueError):
        pack_frame(TYPE_CTRL, b"x" * 5000, 1)
