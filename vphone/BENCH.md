# vphone 台架实测操作卡（阶段2：门B' / 门C' 判定）

前置（2026-09-07 已确认）：手机 SN_PHONE_A 蓝牙 ON 无残留配对；车机 0x123456789EF
蓝牙 ON、adb 可达；vphone 已装、账号已启用（`telecom set-phone-account-enabled` 已跑过）。

```bash
cd vphone
export VPHONE_SERIAL=SN_PHONE_A    # 多设备必设(手机,不是车机)
export PYTHONIOENCODING=utf-8
python vphone_ctl.py start               # 拉起手机端服务
```

## 第0步 配对（一次性，人工）
手机蓝牙设置搜车机（或车机搜手机）→ 配对确认。配对后看手机"已连接设备"应出现
媒体音频(A2DP)/通话音频(HFP) 两类。若车机侧有旧记录先删再配。

## 门B' 媒体（看车机屏）
```bash
python vphone_ctl.py playlist --file demo_pl.txt
python vphone_ctl.py play                # 车机应显示: 青花瓷/周杰伦, 总时长 3:49, 进度推进
# 等 10s 观察进度条 → python vphone_ctl.py pause (进度冻结) → play (恢复) → next (晴天)
python vphone_ctl.py silence --on 1      # 若元数据不上屏: 开静音流激活 A2DP 再 play
```
判定：歌名/歌手任意可控、总时长非 0、进度每秒推进、暂停恢复、自动连播。
车机按播放/暂停/切歌 → `python vphone_ctl.py events` 应见 `车机按键: 【下一曲】` 等。

## 门C' 来电（看车机屏）
```bash
python vphone_ctl.py incoming --num 13800138000   # 车机应弹来电 UI + 显示号码
# 车机上按接听 → events 应见 "车机/系统按了【接听】(onAnswer)"
# 车机上挂断    → events 应见 "车机/系统按了【挂断】(onDisconnect)"
python vphone_ctl.py dial --num 10086            # 反向: 车机应显示拨号态
python vphone_ctl.py audio-bt                    # 加分项: SCO 通话音频路由
python vphone_ctl.py hangup
```

## 证据收集
```bash
adb -s SN_PHONE_A logcat -s VPhone            # 手机侧: 按键回调/状态机全事件
adb -s 0x123456789EF logcat | grep -iE "hfp|avrcp|a2dp|sco"   # 车机侧 profile 层
```

## 失败路径
- 元数据不上屏 → silence 开 + play；再不行 MP3+外部播放器对照。
- 门C' 来电不上车机 → 如实记录（厂商 Telecom→HFP 传导差异）→ BlueZ 备选升级。
