#!/usr/bin/env python
"""ESP32 开发板到货自检。

用途:确认买到的板子是"原版双模 ESP32"(芯片 ESP32-D0WDQ6/D0WD),
而不是 S3/C3/C6/H2 等纯 BLE 型号(经典蓝牙测试工具完全无法使用)。

用法:
    python tools/check_board.py COM7

依赖: pip install esptool
"""

import re
import subprocess
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    port = sys.argv[1]
    print(f"[*] 连接 {port} ...")
    try:
        out = subprocess.run(
            [sys.executable, "-m", "esptool", "--port", port, "--chip", "auto", "flash_id"],
            capture_output=True, text=True, timeout=30,
        ).stdout + subprocess.run(
            [sys.executable, "-m", "esptool", "--port", port, "--chip", "auto", "read_mac"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except FileNotFoundError:
        print("未找到 esptool,请先: pip install esptool")
        return 2

    print(out)
    m = re.search(r"Detecting chip type\.\.\.\s*(\w+)", out)
    chip = m.group(1) if m else "?"
    if chip.lower() == "esp32":
        print("[OK] 芯片为原版 ESP32(双模),可以用于本项目。")
        flash = re.search(r"Device:.*(4MB|8MB|16MB|2MB)", out)
        if flash:
            print(f"[OK] Flash 容量: {flash.group(1)}")
        mac = re.search(r"MAC:\s*([0-9a-f:]+)", out)
        if mac:
            print(f"[OK] MAC: {mac.group(1)}")
        print("[提示] 建议每块板贴标签记录 MAC/编号,方便服务器绑定设备别名。")
        return 0
    if chip == "?":
        print("[!!] 未能识别芯片。确认串口是否被占用(关掉其他串口工具)后重试。")
        return 1
    print(f"[!!] 检测到 {chip}。S3/C3/C6/H2 只有低功耗蓝牙(BLE),"
          "无法实现 A2DP/HFP,请退换为原版 ESP32(WROOM-32)开发板!")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
