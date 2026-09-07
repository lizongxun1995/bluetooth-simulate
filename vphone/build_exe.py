#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 GUI demo 打包成单文件 exe(PyInstaller): 内嵌 vphone.apk + adb(含DLL),
换机器拷一个 exe 就能跑, 免装 Python/adb/构建环境 —— 前提仅: 手机 USB 驱动正常。

用法:  python build_exe.py          → vphone/dist/vphone_gui.exe (约20MB)
exe 用法: vphone_gui.exe [--serial 手机序列号]
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def collect_files():
    """要内嵌进 exe 的资源: apk/ 打包安装包 + 本机 adb 及其伴生 DLL。"""
    sys.path.insert(0, HERE)
    from vphone_lib import _find_adb
    adb = _find_adb()
    if not os.path.isfile(adb):
        sys.exit("!! 本机找不到 adb.exe (PATH/LOCALAPPDATA SDK 均无), 没法内嵌")
    d = os.path.dirname(adb)
    files = [adb]
    for dll in ("AdbWinApi.dll", "AdbWinUsbApi.dll", "libwinpthread-1.dll"):
        p = os.path.join(d, dll)
        if os.path.isfile(p):
            files.append(p)
    return adb, files


def main():
    adb, adb_files = collect_files()
    apk = os.path.join(HERE, "apk", "vphone.apk")
    if not os.path.isfile(apk):
        sys.exit("!! 缺 vphone/apk/vphone.apk (先构建 APK 并同步到 apk/ 目录)")

    sep = ";" if os.name == "nt" else ":"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onefile", "--windowed", "--name", "vphone_gui",
           # spec/中间产物丢进 build/(已 gitignore), 干净
           "--specpath", os.path.join(HERE, "build"),
           os.path.join(HERE, "vphone_gui.py"),
           "--add-data", os.path.join(HERE, "apk", "vphone.apk") + sep + os.path.join("apk", "")]
    for f in adb_files:
        cmd += ["--add-data", f + sep + "."]
    print("[打包]", " ".join(cmd))
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode == 0:
        out = os.path.join(HERE, "dist", "vphone_gui.exe")
        print(f"\n✓ 完成: {out}  ({os.path.getsize(out) // 1024 // 1024}MB)")
        print("  内嵌: vphone.apk + adb; 拷到任何 Windows 机器双击即用(需手机USB驱动)")
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
