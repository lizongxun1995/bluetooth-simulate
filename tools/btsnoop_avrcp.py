#!/usr/bin/env python
"""btsnoop_hci.log 分析器:用于歌词来源调研(Phase 0)。

背景:真手机开"开发者选项 → 蓝牙 HCI 信息收集日志"后连接车机播放带歌词
音乐,导出的 btsnoop_hci.log 里包含手机发出的全部蓝牙包。如果歌词是
手机经蓝牙推送的,一定能在 AVRCP(或自定义 RFCOMM/L2CAP 通道)里看到。

本工具解析 btsnoop 格式,提取 L2CAP 层数据:
  1. 跟踪 L2CAP 建链(cid↔PSM 映射),标出 AVCTP(PSM 23=0x17)、
     RFCOMM(PSM 3)、以及"未知 PSM"(私有协议的重点嫌疑对象);
  2. 对 AVCTP 通道解析 AVRCP 事务头,重点列出 vendor-dependent PDU
     (私有歌词协议最常见的藏身处)。

用法:
    python tools/btsnoop_avrcp.py btsnoop_hci.log [--dump-all]
"""

import argparse
import struct
import sys

AVCTP_PSM = 0x17
RFCOMM_PSM = 0x03

BTSNOOP_HEADER = 16
RECORD_HEADER = 24


def parse_records(data: bytes):
    if data[:8] != b"btsnoop\x00":
        raise ValueError("不是 btsnoop 文件(缺少 btsnoop\\0 头),确认导出的是 Android HCI 日志")
    pos = BTSNOOP_HEADER
    while pos + RECORD_HEADER <= len(data):
        orig_len, inc_len, flags, drops, ts = struct.unpack_from(">IIIIq", data, pos)
        pos += RECORD_HEADER
        if inc_len > len(data) - pos or inc_len == 0:
            break
        yield ts, flags, data[pos : pos + inc_len]
        pos += inc_len


def hci_acl_payloads(records):
    """产出 (方向, handle, payload)。方向: 'tx'=手机→车机。"""
    for _ts, flags, pkt in records:
        if len(pkt) < 9:
            continue
        pkt_type = pkt[0]
        if pkt_type == 0x02:  # ACL
            handle_and_flags, dlen = struct.unpack_from("<HH", pkt, 1)
            handle = handle_and_flags & 0x0FFF
            pb = (handle_and_flags >> 12) & 0x3
            direction = "tx" if (flags & 1) else "rx"
            if pb == 0 and dlen == 0:  # 空包/续包(简化:只处理首包)
                continue
            payload = pkt[5 : 5 + dlen]
            yield direction, handle, payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile")
    ap.add_argument("--dump-all", action="store_true", help="打印所有 L2CAP 载荷(十六进制)")
    args = ap.parse_args()

    data = open(args.logfile, "rb").read()
    cid_psm = {}  # (direction, handle, local_cid) -> psm
    stats = {"avctp_tx": 0, "avctp_vendor": 0, "rfcomm_tx": 0, "unknown_tx": 0}
    findings = []

    for direction, handle, payload in hci_acl_payloads(parse_records(data)):
        if len(payload) < 4:
            continue
        l2len, cid = struct.unpack_from("<HH", payload, 0)
        body = payload[4 : 4 + l2len]

        if cid == 0x0001:  # L2CAP 信令
            if len(body) >= 8 and body[0] == 0x03:  # CONNECTION RESPONSE
                dst_cid, psm_src = struct.unpack_from("<HH", body, 4)
                # response 里没有 psm,真正带 psm 的是 REQUEST(0x02)
                pass
            if len(body) >= 8 and body[0] == 0x02:  # CONNECTION REQUEST
                psm, src_cid = struct.unpack_from("<HH", body, 4)
                cid_psm[(direction, handle, src_cid)] = psm
            continue

        key = (direction, handle, cid)
        psm = cid_psm.get(key)

        if psm == AVCTP_PSM or cid_psm.get((direction, handle, cid)) == AVCTP_PSM:
            # AVCTP: transaction(1) | packet_type(1) | ...(profile id) | AVRCP PDU
            if len(body) >= 4:
                ipid = body[1] & 0x02
                pdu = body[2] if len(body) > 2 else 0
                if ipid:
                    continue  # 连接断开
                if direction == "tx":
                    stats["avctp_tx"] += 1
                    # AVRCP 单元 PDU: 0x10=GetElementAttributes? (AV/C opcode 0x10=VENDOR...)
                    if body[2] == 0x00 and len(body) >= 6 and body[3] == 0x00:
                        # vendor-dependent: company_id 3B + pdu_id
                        company = (body[4] << 16) | (body[5] << 8) | body[6] if len(body) >= 7 else 0
                        if len(body) >= 8:
                            stats["avctp_vendor"] += 1
                            findings.append(("AVRCP VENDOR", direction, pdu if False else body[7], body))
            if args.dump_all:
                findings.append(("AVCTP RAW", direction, 0, body))
        elif psm == RFCOMM_PSM:
            if direction == "tx":
                stats["rfcomm_tx"] += 1
                findings.append(("RFCOMM(自定义/SPP)", direction, 0, body))
        else:
            if direction == "tx" and len(body) > 8:
                stats["unknown_tx"] += 1
                findings.append(("未知通道(重点!)", direction, psm or 0, body))

    print("=== 通道统计(手机→车机) ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print("\n=== 疑似歌词/私有协议载荷(前 80 条) ===")
    shown = 0
    for tag, direction, info, body in findings:
        if shown >= 80:
            break
        shown += 1
        hexdump = body.hex()
        printable = "".join(chr(c) if 32 <= c < 127 else "·" for c in body[:48])
        print(f"[{tag}] {direction} info={info:#x} len={len(body)}")
        print(f"    ASCII: {printable}")
        print(f"    HEX:   {hexdump[:160]}")
    if not findings:
        print("  (未发现 vendor/RFCOMM/未知通道载荷)")
    print("""
=== 结论判断指南 ===
- 若 vendor-dependent 或未知通道里出现大段带时间戳的文本 → 歌词经蓝牙私有协议推送,
  把本工具输出的 HEX 交给协议分析,进入 Phase 3 实现;
- 若一切通道都干净 → 歌词大概率是车机自己联网拉取,蓝牙只需保证元数据正确;
- 配合对照实验(手机断网+本地音乐)双重确认,见 docs/LYRICS_RESEARCH.md。""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
