# 打包安装包(换机器克隆即装)

`vphone.apk` = 随仓库分发的 vphone 安装包。GUI 顶部「📦安装APK」/ CLI `install`
默认装这里的文件 —— 换台机器 clone 下来就能装, 不需要 Android 构建环境。

重新构建后同步覆盖(否则 GUI 装的还是旧版):

```bash
gradle -p vphone assembleDebug
cp vphone/app/build/outputs/apk/debug/app-debug.apk vphone/apk/vphone.apk
git add vphone/apk/vphone.apk && git commit -m "apk: 同步 <改动摘要>"
```

注意:
- 装完 App 处于 stopped 态, GUI 点「🚀启动App」(或 `launch()`)拉起一次;
- 重装可能清空 vphone 账号联系人(行为不定), GUI 检测到 0 会询问重灌;
- 华为等厂商可能弹一次安装确认框, 需在手机上手动点一下(不做自动点屏)。
