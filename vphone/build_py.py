#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建 vphone 的 pip wheel(库+CLI+GUI+APK 资源)。

流程: 同步 apk/vphone.apk → vphone_data/ → 清理 setuptools 旧产物 → 构建 wheel 到 dist_py/。
产物: dist_py/vphone-<版本>-py3-none-any.whl
安装: pip install dist_py/vphone-*.whl  (装完: from vphone_lib import VPhone / vphone --help / vphone-gui)

注意:
  · 版本号单一真源 = vphone_lib.__version__(pyproject dynamic 引用), 发版只改那一处;
  · vphone_data/apk/ 是构建同步产物, 不入 git(仓库真源是 apk/vphone.apk);
  · Android APK 重建是另一条链(gradle assembleDebug → 同步 apk/), 见 build 流程文档。
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()
OUT = HERE / "dist_py"


def main():
    # 1) 同步 APK 进资源包(单一真源: 仓库 apk/)
    src = HERE / "apk"
    dst = HERE / "vphone_data" / "apk"
    dst.mkdir(parents=True, exist_ok=True)
    apks = sorted(src.glob("*.apk"))
    if not apks:
        sys.exit("!! apk/ 下没有 APK —— 先构建(gradle assembleDebug)或放入打包产物")
    for old in dst.glob("*.apk"):
        old.unlink()
    shutil.copy2(apks[0], dst / apks[0].name)
    print(f"[同步] {apks[0].name} → vphone_data/apk/ ({apks[0].stat().st_size // 1024}KB)")

    # 2) 清理旧产物(脏 build/ 会把陈旧文件带进 wheel)
    for d in (OUT, HERE / "build" / "lib", HERE / "vphone.egg-info"):
        shutil.rmtree(d, ignore_errors=True)
    OUT.mkdir(exist_ok=True)

    # 3) 构建(优先 python -m build, 没有则退 pip wheel —— 两者都走 pyproject 元数据)
    try:
        import build  # noqa: F401
        cmd = [sys.executable, "-m", "build", "--wheel", "--outdir", str(OUT)]
    except ImportError:
        cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(OUT), "."]
    print(f"[构建] {' '.join(cmd[1:])}")
    subprocess.run(cmd, cwd=HERE, check=True)

    # 4) 验证: 列 wheel 内容(模块/资源包/APK 都要在)
    wheels = sorted(OUT.glob("vphone-*.whl"))
    if not wheels:
        sys.exit("!! 未生成 wheel")
    w = wheels[-1]
    print(f"\n[产物] {w}")
    subprocess.run([sys.executable, "-m", "zipfile", "--list", str(w)])
    print("\n✓ 完成。安装: pip install --force-reinstall " + str(w))


if __name__ == "__main__":
    main()
