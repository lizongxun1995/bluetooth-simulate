# bluetooth-simulate — 蓝牙车机模拟测试工具

用一块**原版 ESP32**(ESP32-D0WDQ6/WROOM-32,双模经典蓝牙)开发板充当
"脚本全控的虚拟手机",连接车机后,从电脑侧用 Python 脚本/CLI 完成车载蓝牙
测试的常用操作,替代"人手一部手机"。

## 能力一览

| 能力 | 说明 | 状态 |
|---|---|---|
| 配对全流程控制 | 自动/手动确认,**配对失败模拟**(拒绝/错PIN/超时),解绑/回连 | v1 |
| 音乐(A2DP) | 电脑曲库经 Type-C 实时推流给车机,MP3/WAV 自动转码 | v1 |
| 曲目信息(AVRCP) | 推送歌名/歌手/专辑;车机上按上下首/暂停联动 | v1 |
| 通话(HFP AG) | 模拟来电/拨号/接听/挂断/DTMF/通话中语音 | v1(信令级) |
| 主动连接 | PC 按 MAC 主动连车机;断开任意协议 | v1 |
| 歌词推送 | 标准蓝牙无此协议;按调研结论实现 | P3(先调研) |
| WiFi 共存/网络共享 | 车机走电脑网络(热点+NAT / BT PAN) | P2 |
| 通讯录/短信(PBAP/MAP) | 车机同步联系人/短信 | P4(可裁剪) |

> 为什么不能直接用电脑蓝牙?Windows 蓝牙栈只开放"消费端"角色(把车机当音箱),
> 不支持模拟手机的 AG/AVRCP 角色。ESP32(原版双模)的 Bluedroid 协议栈全部可控。

## 快速开始

```bash
pip install -e .            # PC 侧库 + CLI

# 无硬件,先跑通流程(内置固件模拟器)
python examples/01_music_and_meta.py --sim
btphone-cli --sim
```

硬件到手后(烧录见 docs/DEPLOY.md):

```python
from btphone import BtPhone

phone = BtPhone("COM3")
phone.set_name("VPHONE-01")
phone.set_discoverable(True, timeout_s=60)

# 车机上搜索配对后…
phone.music.play(files=[r"D:\曲库\a.mp3", r"D:\曲库\b.mp3"], loop=True)
phone.wait_event("hfp.at", predicate=lambda d: d.get("at") == "ATA", timeout=30)
phone.calls.incoming("13800138000")   # 模拟来电
phone.calls.hangup()
```

自动化测试里对车机行为做断言:车机上按"下一首"会收到 `avrcp.cmd {"cmd":"next"}`
事件;车机接听来电会收到 `hfp.at {"at":"ATA"}`;配对失败会收到
`pair.result {"ok":false,"reason":"rejected|wrong_pin|timeout"}`。

## 目录结构

```
btphone/        Python 库(BtPhone API / 串口传输 / 事件总线 / 模拟器 / CLI / 图形控制台)
firmware/       ESP-IDF v5.x 固件工程(A2DP+AVRCP+HFP AG+音频管线+WiFi)
tests/          pytest 套件(35 用例,协议/编解码/全流程/GUI 冒烟,基于模拟器)
examples/       示例脚本(音乐元数据/配对失败/通话全流程)
tools/          板卡自检 / btsnoop 歌词调研分析 / 测试音频生成
docs/           部署 / 协议 / 歌词调研SOP / 硬件联调清单
```

## 文档

- [部署指南](docs/DEPLOY.md) — 硬件选型、固件烧录、PC 安装、常见问题
- [串口协议参考](docs/PROTOCOL.md) — 全部命令/事件/帧格式/ADPCM 格式
- [歌词来源调研 SOP](docs/LYRICS_RESEARCH.md) — Phase 0 抓包与分析方法
- [硬件联调清单](docs/BRINGUP.md) — 首次编译/上电检查与 IDF 兼容性风险点

## 硬件采购要点(唯一但要命的坑)

- ✅ 模组丝印 **ESP32-WROOM-32**(芯片 ESP32-D0WDQ6/D0WD)——原版双模
- ❌ ESP32-**S3/C3/C6/H2** —— 只有低功耗蓝牙,本项目完全不可用
- 板子其他方面随意:Type-C 优先、CH340 串口、排针已焊接、4MB Flash

## 当前状态与路线

- ✅ v1(本仓库):PC 库 + CLI + 模拟器 + 测试套件;固件源码完整,待硬件到手联调
- ⏳ P0:歌词来源调研(docs/LYRICS_RESEARCH.md)+ hfp_ag spike(docs/BRINGUP.md)
- ⏳ P2:WiFi 热点+NAT 网络共享、BT PAN spike
- ⏳ P3:歌词推送(视调研结论)
- ⏳ P4:PBAP/MAP 通讯录短信
