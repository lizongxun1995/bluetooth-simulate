"""诊断 GUI"设置蓝牙名失败":真机上把 ASCII/中文/空名各设一遍,每步回读状态。
用法: python tools/debug_set_name.py [COM7]
"""
import sys

from btphone import BtPhone

port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
p = BtPhone(port)
try:
    info = p.info()
    print(f"before: fw={info.get('fw')} name={info.get('name')!r}")

    for label, name in (("ASCII", "VP-TEST-42"), ("中文", "虚拟手机甲"), ("空名", "")):
        try:
            r = p.set_name(name)
            print(f"set_name {label} {name!r} -> {r}")
        except Exception as e:  # noqa: BLE001
            print(f"set_name {label} {name!r} FAILED: {type(e).__name__}: {e}")
        st = p.request("conn.status")
        print(f"  status.name = {st.get('name')!r}")
finally:
    try:
        p.set_name("VPHONE-01")
        print("restored:", p.request("conn.status").get("name"))
    except Exception as e:  # noqa: BLE001
        print("restore failed:", type(e).__name__, e)
    p.close()
