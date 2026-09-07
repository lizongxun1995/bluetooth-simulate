"""实证:关闭串口句柄是否会复位板子(经 rtc_boot 计数判断)。

rtc_boot = RTC 内存计数,每次 app_main 运行 +1,仅真掉电清零。
开→查→关→静置→再开→查:若 rtc_boot 增加,说明 close 复位了板子。
"""
import json
import sys
import time

sys.path.insert(0, ".")
from tools.probe_bt_up import probe_once  # noqa: E402


def ask(tag: str) -> None:
    resp = probe_once(hash(tag) % 10000)
    if resp is None:
        print(f"{tag}: 无响应")
        return
    r = resp.get("result", {})
    print(f"{tag}: rtc_boot={r.get('rtc_boot')} reset={r.get('reset')} "
          f"bt_up={r.get('bt_up')} free_heap={r.get('free_heap')}")


ask("① 开→查(close 前)")
time.sleep(1.0)
ask("② close 后 1s")
time.sleep(8.0)
ask("③ close 后 9s(越过 3.7s 射频窗)")
time.sleep(8.0)
ask("④ close 后 17s")
