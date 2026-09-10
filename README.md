# bluetooth-simulate — 车载蓝牙自动化测试框架

**当前主力路线是 vphone**：一台普通 Android 手机装上 vphone APK 后，成为 PC 可远程驱动的
蓝牙测试终端——车机看到的 A2DP/HFP/AVRCP/PBAP 对端就是一台真手机（协议层就是手机自带
厂商栈），但播放内容、来电、联系人、配对动作全部由 PC 脚本控制，替代"人手一部手机"。

> 一期 ESP32 路线（btphone/firmware）与 Windows 蓝牙路线（btwinrt/windemo）已定案放弃，
> 代码与文档已从仓库清除（git 历史与全轮次工程日志见 docs/NOTES.md，需要时从老提交找回）。

## vphone 能力一览

| 能力 | 说明 | 状态 |
|---|---|---|
| 配对全流程控制 | 扫描/配对不出 App，配对弹窗自动确认，解绑/断连/回连；GUI 列表模糊过滤(名/MAC) | ✅ 台架全通 |
| 音乐(A2DP/AVRCP) | 元数据任意伪造；电脑音频上传→车机真实出声；静音流保活模式 | ✅ |
| 车机按键回流 | 播放/暂停/切歌/拖进度/接听/挂断/拨号 全部落结构化事件可断言 | ✅ |
| 通话(HFP) | 注入来电/拨号/保持/DTMF/通话中自定义音频(对端说话) | ✅ |
| 通讯录(PBAP) | 批量 1 万/自定义联系人写入 → 车机通讯录压测 | ✅ |
| 断连/回连测试 | 保配对断开、手机侧回连，ACL/A2DP/HFP 链路事件断言 | ✅ |
| 发布 | 单文件 exe（内嵌 APK+adb），换机器双击即用 | ✅ |

## 快速开始（vphone）

```bash
# 换机器免装版: vphone/dist/vphone_gui.exe 双击（唯一前提=手机 USB 驱动）
# 开发机(三种任选):
pip install vphone/dist_py/vphone-0.8.1-py3-none-any.whl   # 装库+CLI+GUI 命令, 零第三方依赖
cd vphone
python vphone_ctl.py --serial <手机序列号> install   # 装机并拉起服务(华为可能要点安装确认)
python vphone_gui.py --serial <手机序列号>            # 图形控制台(或命令 vphone-gui)
```

测试脚本（推荐入口 `vphone_lib`，pip 装完 import 名不变）：

```python
from vphone_lib import VPhone
vp = VPhone(serial="SN_PHONE_A", wait_timeout=20)

vp.incoming(number="13800138000")                     # 注入来电(车机+手机同时响)
vp.expect_event(evt_type="CAR_ANSWER")                # 车机接听断言(超时抛异常带现场)
vp.hangup()

vp.play_audio_files(paths=["D:/music/a.mp3"])         # 上传+播放(车机真出声)
vp.expect_event(evt_type=("CAR_NEXT", "CAR_PREV"))

vp.contacts_load(count=10000)                         # 1w 联系人→车机通讯录压测
```

> v0.6.0 起公开方法全部 keyword-only（调用必须写参数名），事件断言返回 VEvent
> 结构化对象。详见 [vphone 接口文档](docs/vphone-API.md) §0/§2.6。

## 目录结构

```
vphone/          主力工程: APK(Kotlin) + Python(lib/CLI/GUI) + build_exe.py/build_py.py + apk/(发行包)
docs/            vphone-TECH/API/DEV 三文档 + NOTES(全轮次日志)
```

## 文档

- [vphone 技术文档](docs/vphone-TECH.md) — 架构/线程模型/双控制面/事故记录/**真机行为基准表**/**与真机已知偏差清单**
- [vphone 接口文档](docs/vphone-API.md) — HTTP 全端点 / Python API / CLI / 事件总表 / 断言示例
- [vphone 开发文档](docs/vphone-DEV.md) — 任务跟进 / 挂账项 / 车机侧验收清单 / 构建发布流程

## 当前状态与路线

- ✅ vphone 台架全通（配对/音乐/来电/通讯录/断连回连/exe 发布），第六轮稳固轮完成
  （跨线程同步桥、状态机对齐真机语义、静音流忙循环事故修复、全量注释与文档）
- ⏳ 车机侧保真度验收（暂停态切歌裁决 / CAR_SEEK / 杂音对照 / 真手机差分对照）——见 DEV 文档清单
- 挂账轻微项与更新规则见 [vphone-DEV.md](docs/vphone-DEV.md)
