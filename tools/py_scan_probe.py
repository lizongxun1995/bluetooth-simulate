# -*- coding: utf-8 -*-
"""pythonnet 扫描链路探针: 加载 pydemo/lib 引擎后跑一次 DiscoverDevices。
用法: python tools/py_scan_probe.py
"""
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "pydemo", "lib")
os.environ.setdefault("DOTNET_ROOT", r"C:\Program Files\dotnet")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pythonnet import load
load("coreclr")
import clr  # noqa: E402
from System.Reflection import Assembly  # noqa: E402

for _f in sorted(os.listdir(LIB)):
    if _f.lower().endswith(".dll"):
        Assembly.LoadFrom(os.path.join(LIB, _f))
from BtWinRT import Engine  # noqa: E402

done = threading.Event()
result = {"lines": []}
eng = Engine()
eng.OnScanDone += lambda s: (result.__setitem__("lines", str(s).splitlines()), done.set())
eng.OnLog += lambda m: print("[log]", m)
eng.Init()
eng.ScanAsync()
if not done.wait(45):
    print("!! 45s 超时未回扫描结果")
    sys.exit(1)
lines = result["lines"]
print(f"\n=== 扫描成功: {len(lines)} 台 ===")
for line in lines:
    if "CARKIT-1" in line or "已配对" in line or "VPHONE" in line:
        print(" *", line)
eng.Dispose()
