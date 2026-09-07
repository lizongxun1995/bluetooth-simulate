"""btphone-cli:交互式/单命令调试工具。

用法::

    btphone-cli --port COM3                  # 进入交互模式
    btphone-cli --port COM3 status           # 单命令
    btphone-cli --port SIM --sim             # 连接内置模拟器
    btphone-cli --port COM3 music play --file D:\\a.mp3
"""

from __future__ import annotations

import argparse
import cmd
import json
import shlex
import sys
from typing import Optional

from .client import BtPhone
from .exceptions import BtPhoneError
from .simulator import SimPhone

BANNER = """btphone-cli 交互控制台(虚拟手机)。
命令: status | info | name <名> | discover on|off
      pair <auto|manual|reject|wrong_pin|timeout> | pairlist | unpair <mac>
      connect <mac> | disconnect [profile]
      music play --file <路径> | music play --files <路径1,路径2> | pause | resume | stop | next | prev
      track <歌名> [歌手] [专辑]
      call incoming <号码> | call dial <号码> | answer | hangup
      sim pair | sim press <cmd> | sim answer | sim hangup   (仅 --sim)
      events <模式> | quit
"""


def _build_phone(args: argparse.Namespace) -> tuple[BtPhone, Optional[SimPhone]]:
    sim = None
    if args.sim:
        sim = SimPhone(name=args.name, speed=args.speed)
        phone = BtPhone("SIM", io=sim.device_io)
    else:
        phone = BtPhone(args.port, baudrate=args.baud)
    return phone, sim


class Console(cmd.Cmd):
    intro = BANNER
    prompt = "btphone> "

    def __init__(self, phone: BtPhone, sim: Optional[SimPhone]) -> None:
        super().__init__()
        self.phone = phone
        self.sim = sim
        self._cancel = self.phone.on_event("*", self._print_event)
        self._quiet_cmds = {"events"}

    def _print_event(self, name: str, data: dict) -> None:
        print(f"  [事件] {name} {json.dumps(data, ensure_ascii=False)}")

    # ---- 基础 ----

    def do_quit(self, arg: str) -> bool:
        self._cancel()
        self.phone.close()
        if self.sim:
            self.sim.close()
        return True

    do_exit = do_quit
    do_EOF = do_quit

    def do_status(self, arg: str) -> None:
        print(json.dumps(self.phone.status(), ensure_ascii=False, indent=2))

    def do_info(self, arg: str) -> None:
        print(json.dumps(self.phone.info(), ensure_ascii=False, indent=2))

    def do_name(self, arg: str) -> None:
        print(self.phone.set_name(arg.strip()))

    def do_discover(self, arg: str) -> None:
        on = arg.strip().lower() in ("on", "1", "true")
        print(self.phone.set_discoverable(on))

    def do_pair(self, arg: str) -> None:
        print(self.phone.pairing.set_mode(arg.strip() or "auto"))

    def do_pairlist(self, arg: str) -> None:
        for dev in self.phone.pairing.list_bonded():
            print(f"  {dev.get('mac')}  {dev.get('name')}")

    def do_unpair(self, arg: str) -> None:
        print(self.phone.pairing.remove_bond(arg.strip()))

    def do_connect(self, arg: str) -> None:
        print(self.phone.conn.connect(arg.strip()))

    def do_disconnect(self, arg: str) -> None:
        print(self.phone.conn.disconnect(arg.strip() or "all"))

    def do_track(self, arg: str) -> None:
        parts = shlex.split(arg)
        title = parts[0] if parts else ""
        artist = parts[1] if len(parts) > 1 else ""
        album = parts[2] if len(parts) > 2 else ""
        print(self.phone.music.set_track_info(title, artist=artist, album=album))

    def do_pause(self, arg: str) -> None:
        print(self.phone.music.pause())

    def do_resume(self, arg: str) -> None:
        print(self.phone.music.resume())

    def do_stop(self, arg: str) -> None:
        print(self.phone.music.stop())

    def do_next(self, arg: str) -> None:
        print(self.phone.music.next())

    def do_prev(self, arg: str) -> None:
        print(self.phone.music.prev())

    def do_answer(self, arg: str) -> None:
        print(self.phone.calls.answer())

    def do_hangup(self, arg: str) -> None:
        print(self.phone.calls.hangup())

    def do_events(self, arg: str) -> None:
        """events <模式> [次数] — 打印匹配事件若干条后返回。"""
        parts = arg.split()
        pattern = parts[0] if parts else "*"
        count = int(parts[1]) if len(parts) > 1 else 1
        for _ in range(count):
            try:
                data = self.phone.wait_event(pattern, timeout=30)
                print(json.dumps(data, ensure_ascii=False))
            except BtPhoneError as exc:
                print(f"  {exc}")
                break

    # ---- 带子命令的 ----

    def do_music(self, arg: str) -> None:
        parts = shlex.split(arg)
        if not parts:
            print("用法: music play --file <路径> | pause | resume | stop | next | prev")
            return
        sub, rest = parts[0], parts[1:]
        if sub == "play":
            files: list[str] = []
            if "--files" in rest:
                i = rest.index("--files")
                files = [p.strip() for p in rest[i + 1].split(",") if p.strip()]
            elif "--file" in rest:
                i = rest.index("--file")
                files = [rest[i + 1]]
            print(self.phone.music.play(files=files, loop="--loop" in rest))
        else:
            getattr(self, f"do_{sub}", lambda a: print("未知子命令"))("")

    def do_call(self, arg: str) -> None:
        parts = shlex.split(arg)
        if len(parts) != 2 or parts[0] not in ("incoming", "dial"):
            print("用法: call incoming <号码> | call dial <号码>")
            return
        if parts[0] == "incoming":
            print(self.phone.calls.incoming(parts[1]))
        else:
            print(self.phone.calls.dial(parts[1]))

    def do_sim(self, arg: str) -> None:
        if self.sim is None:
            print("仅 --sim 模式可用")
            return
        parts = shlex.split(arg)
        if not parts:
            return
        sub = parts[0]
        if sub == "pair":
            self.sim.car_pair()
        elif sub == "press":
            self.sim.car_press(parts[1] if len(parts) > 1 else "next")
        elif sub == "answer":
            self.sim.car_answer()
        elif sub == "hangup":
            self.sim.car_hangup()
        elif sub == "dial":
            self.sim.car_dial(parts[1] if len(parts) > 1 else "10086")
        else:
            print(f"未知模拟动作: {sub}")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="btphone-cli", description="蓝牙车机模拟测试 CLI")
    parser.add_argument("--port", default="SIM", help="串口名,如 COM3;模拟器用 SIM")
    parser.add_argument("--baud", type=int, default=2_000_000)
    parser.add_argument("--sim", action="store_true", help="使用内置固件模拟器")
    parser.add_argument("--name", default="VPHONE-01", help="蓝牙名(--sim 时)")
    parser.add_argument("--speed", type=float, default=100.0, help="模拟器缓冲消耗倍速")
    parser.add_argument("command", nargs="*", help="单命令(留空进交互模式)")
    args = parser.parse_args(argv)

    try:
        phone, sim = _build_phone(args)
    except BtPhoneError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    if args.command:
        console = Console(phone, sim)
        console.onecmd(" ".join(args.command))
        console._cancel()
        phone.close()
        if sim:
            sim.close()
        return 0

    try:
        Console(phone, sim).cmdloop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
