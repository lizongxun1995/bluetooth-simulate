"""WiFi 冒烟:热点 + 扫描,含"射频上电 USB 掉线"的自动恢复。

弱供电链路(细 USB 线/弱端口)上,net.ap_start / net.wifi_sta 的射频上电
瞬间 CH340 会从总线掉线并自动恢复(约 1~30s),板子本身不重启。
库的 wait_reconnect() 负责跨过这个暂态;若掉线依旧频繁,先按
docs/DEPLOY.md 的"WiFi 启动后串口掉线"条目解决供电。

用法: python examples/04_wifi.py [COM口]
"""

import sys

from btphone import BtPhone, TransportError


def resilient(fn, *args, **kwargs):
    """net.* 命令遇 USB 掉线时自动等待重连并重试一次。"""
    phone = kwargs.pop("phone")
    try:
        return fn(*args, **kwargs)
    except TransportError:
        print(f"  [blip] {fn.__name__} 触发 USB 掉线,等待板子回来...")
        phone.wait_reconnect(timeout=90)
        print("  [blip] 已无复位重连,继续")
        return fn(*args, **kwargs)


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    phone = BtPhone(port)
    print("板子:", phone.info()["name"])

    print("起热点 VPHONE-AP(WPA2, 密码 12345678)...")
    resilient(phone.net.ap_start, "VPHONE-AP", "12345678", phone=phone)
    print("状态:", phone.net.status())

    print("扫描环境 AP(阻塞 1.5~3s)...")
    aps = resilient(phone.net.wifi_scan, phone=phone).get("aps", [])
    for ap in aps[:8]:
        print(f"  {ap['ssid']!r:30} rssi={ap['rssi']}, {ap['auth']}")
    print(f"共 {len(aps)} 个")

    phone.close()


if __name__ == "__main__":
    main()
