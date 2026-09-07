"""示例 1:配对 + 音乐推流 + 车机联动。

演示:设置蓝牙名/可见性 → 等待车机配对 → 推流播放曲库 → 车机上按
下一首联动换曲 → 校验车机收到的元数据。

无硬件试跑: python examples/01_music_and_meta.py --sim
真机:        python examples/01_music_and_meta.py --port COM3
"""

import argparse
import sys

from btphone import BtPhone, SimPhone


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM3")
    ap.add_argument("--sim", action="store_true", help="用内置固件模拟器")
    ap.add_argument("--files", nargs="+", default=[
        r"C:\temp\btphone_lib\01_正弦440.wav",
        r"C:\temp\btphone_lib\02_扫频.wav",
    ], help="曲库文件(可用 tools/gen_test_audio.py 生成)")
    args = ap.parse_args()

    sim = None
    if args.sim:
        sim = SimPhone(name="VPHONE-01", speed=100)
        phone = BtPhone("SIM", io=sim.device_io)
    else:
        phone = BtPhone(args.port)

    try:
        # 1. 基础设置
        phone.set_name("VPHONE-01")
        phone.set_discoverable(True, timeout_s=60)
        print("[1] 已设名并可见,请在车机上搜索 'VPHONE-01' 并配对")

        # 2. 等配对结果(车机发起;真机=人在车机上操作,模拟器=car_pair)
        if sim:
            sim.car_pair()
        result = phone.wait_event("pair.result", timeout=60)
        if not result.get("ok"):
            print(f"[!] 配对失败: {result}")
            return 1
        print(f"[2] 与 {result['mac']} 配对成功")

        # 3. 播放曲库(推流给车机,元数据同步推送)
        phone.music.play(files=args.files, loop=True)
        started = phone.wait_event("music.started", timeout=30)
        print(f"[3] 正在播放: {started['title']}")

        # 4. 模拟车机上按"下一首"(真机上是人在车机屏操作)
        if sim:
            sim.car_press("next")
        phone.wait_event("music.started", predicate=lambda d: d.get("index") == 1, timeout=30)
        print("[4] 车机按下一首 → 已切到第 2 曲")

        # 5. 停止播放并收尾
        phone.music.stop()
        phone.wait_event("playlist.done", timeout=10)
        print("[5] 完成")
        return 0
    finally:
        phone.close()
        if sim:
            sim.close()


if __name__ == "__main__":
    sys.exit(main())
