"""抓 ESP32 启动日志(带掉口重连):先 115200 收 ROM/bootloader 输出,再切 2M 收固件输出。

用法: python tools/capture_boot.py [COM口] [boot秒数] [app秒数]
判断标准:
  - "Brownout detector was triggered" 反复出现 = 供电跌压复位循环(硬件供电问题)
  - "Guru Meditation" / "abort()" = 固件崩溃,记下栈回溯
  - 正常 boot 结尾 "entry 0x4..." 后切 2M 有 JSON = 固件活着
"""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"
BOOT_SEC = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
APP_SEC = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0

T0 = time.time()


def ts() -> str:
    return f"[{time.time() - T0:6.2f}s]"


def open_port(baud: int, wait: float = 30.0) -> serial.Serial:
    """打开串口;口不在时等它重新枚举。"""
    deadline = time.time() + wait
    while True:
        try:
            return serial.Serial(PORT, baud, timeout=0.1)
        except Exception as e:
            if time.time() >= deadline:
                raise
            print(f"{ts()} 口不可用({e.__class__.__name__}),1s 后重试…")
            time.sleep(1.0)


def drain(ser: serial.Serial, seconds: float, baud: int) -> bytes:
    """读 seconds 秒;中途掉口就等重开,接着读,直到读满预算。"""
    buf = bytearray()
    end = time.time() + seconds
    while time.time() < end:
        try:
            n = ser.in_waiting
            if n:
                buf += ser.read(n)
            else:
                time.sleep(0.02)
        except Exception:
            print(f"{ts()} *** 串口掉线(共收 {len(buf)} 字节),等重枚举…")
            try:
                ser.close()
            except Exception:
                pass
            ser = open_port(baud)
            print(f"{ts()} *** 串口恢复,继续收")
            end = time.time() + seconds  # 掉线不打断本次相位,重新计时
    return bytes(buf)


def phase(baud: int, seconds: float, reset: bool) -> None:
    ser = open_port(baud)
    if reset:
        ser.dtr = False
        ser.rts = True
        time.sleep(0.1)
        ser.rts = False
    data = drain(ser, seconds, baud)
    try:
        ser.close()
    except Exception:
        pass
    text = data.decode("utf-8", "replace")
    print(f"=== {baud} 波特率收 {len(data)} 字节 ===")
    print(text)
    for ln in text.splitlines():
        low = ln.lower()
        if "brownout" in low or "guru" in low or "abort()" in low:
            print(f"{ts()} !! 关键行: {ln}")


def main() -> None:
    phase(115200, BOOT_SEC, reset=True)
    phase(2_000_000, APP_SEC, reset=False)


if __name__ == "__main__":
    main()
