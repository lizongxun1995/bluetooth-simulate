"""安静探针:不复位、不碰 DTR/RTS,直接问 sys.info,读 bt_up 标志。

用途:弱供电链路在射频上电瞬间(~3.7s)掉口数秒,sys.ready 恰好落进死窗
永远看不到。板子跑稳后用本探针查询"app_main 是否走完、蓝牙栈是否起来",
绕开死窗盲区。

用法: python tools/probe_bt_up.py [COM口]
"""
import json
import os
import sys
import time

import serial

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btphone.protocol import FrameParser, pack_ctrl  # noqa: E402

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"


def probe_once(req_id: int) -> dict | None:
    ser = serial.Serial(PORT, 2_000_000, timeout=0.1)
    try:
        ser.write(pack_ctrl({"id": req_id, "cmd": "sys.info", "args": {}}, req_id))
        ser.flush()
        parser = FrameParser()
        end = time.time() + 3.0
        while time.time() < end:
            n = ser.in_waiting
            if not n:
                time.sleep(0.02)
                continue
            for frame in parser.feed(ser.read(n)):
                if not frame.is_ctrl:
                    continue
                try:
                    obj = json.loads(frame.payload.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if obj.get("id") == req_id and "ok" in obj:
                    return obj
        return None
    finally:
        try:
            ser.close()
        except Exception:
            pass


def main() -> int:
    for attempt in range(1, 6):
        try:
            resp = probe_once(attempt)
        except Exception as exc:
            print(f"[尝试{attempt}] 打不开串口: {exc.__class__.__name__},静默 3s 重试")
            time.sleep(3.0)
            continue
        if resp is None:
            print(f"[尝试{attempt}] 3s 内无响应(句柄僵死?),静默 3s 重试")
            time.sleep(3.0)
            continue
        print("sys.info 响应:")
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        result = resp.get("result", {})
        if resp.get("ok") and result.get("bt_up"):
            print("\n>>> bt_up = true:app_main 走完,蓝牙栈已起来!"
                  "问题纯在 USB 链路/供电,不在固件。")
        else:
            print("\n>>> bt_up = false 或请求失败:app_main 没走完,"
                  "蓝牙栈初始化挂死(射频上电电流冲击),需要固件侧缓解或换供电。")
        return 0
    print("5 次尝试均无响应,链路处于死相位")
    return 1


if __name__ == "__main__":
    sys.exit(main())
