"""vphone 随包资源 —— APK 安装包随 wheel 分发。

apk/vphone.apk 由 build_py.py 在构建时从仓库 apk/ 同步进来(不入 git, 单一真源),
default_apk() 经 importlib.resources 在此定位 —— pip install 的机器不克隆仓库也能装机。
"""
