# 排查交接文档 — ESP32 虚拟手机蓝牙搜不到问题

> 交接对象:GLM 5.3(接手排查)
> 日期:2026-09-01
> 一句话现状(2026-09-01 信号弱排查后更新):**搜不到已解决(用户手机实测搜到 VPHONE-01);"换后置口"建议作废——用户约束板子必须连 hub。当前新问题:1m 内仍排列表末位(信号弱/响应慢)。三嫌疑+修复见 NOTES.md「"搜得到但排位垫底/信号弱"三嫌疑」节:①关 BT modem sleep(RC 时钟漂移错拍,已改固件)②无源 hub 弱供电压发射功率(需带供电 hub,A/B 对比)③摆位。固件 v0.2.1 已编译未烧录(累积:discoverable 真值+关 sleep+bt.rssi 信号测量),烧前先关 GUI 释放 COM7。**

---

## 一、项目要实现的内容(目标)

用一块 **ESP32 开发板**充当"脚本全控的虚拟手机",通过 Type-C 串口连电脑,连车机做蓝牙自动化测试:

- **经典蓝牙**(Bluedroid 协议栈):
  - A2DP Source:PC 曲库经串口实时推流到板子,板子解码后播给车机(音频不落板,即推即播)
  - AVRCP Target:推歌名/歌手/专辑元数据,接收车机切歌/暂停并联动
  - HFP AG:模拟来电/接听/挂断/通话中推流语音
  - GAP:蓝牙名 **VPHONE-01**,可见/可连可控,**配对失败可模拟**(拒绝/错 PIN/超时)
- **PC 侧交付**:Python 库 `btphone`(BtPhone API + 事件流)+ CLI + tkinter GUI。**不做** Web UI、**不做**多设备管理
- **串口协议**:2 Mbaud,控制走 JSON 帧,音频走二进制帧,同一串口复用(帧格式见 `docs/PROTOCOL.md`)
- WiFi/网络共享是 Phase 2,当前固件用 `BTPHONE_WIFI=0` 编译关闭
- 详细规划见 `.zcode/plans/plan-sess_877c3e7f-110a-4caf-b1e5-e92cae2f5375.md`

**当前阶段目标(Phase 1 验收第一步)**:板子上电 → 车机/手机能搜到 VPHONE-01 → 能配对。**这一步还没打通。**

## 二、硬件与环境

| 项 | 值 |
|---|---|
| 板子 | ESP32-D0WDQ6 / WROOM-32,rev v1.1,chip id 34:98:7a:a3:e6:64 |
| 串口 | CH340,COM7,2 000 000 baud |
| 板上按钮 | EN=复位;BOOT=GPIO0;**按住 BOOT 再按 EN = ROM 下载模式**(弱供电下最可靠的烧录方式) |
| 环境 | Windows 11,Python 3.11 + pyserial,ESP-IDF v5.x,esptool 烧录 |
| 烧录方式 | `idf.py build flash` 或 esptool 直烧,**流程没问题**(每次 hash 校验通过;Arduino IDE 底层也是 esptool,不需要别的烧录工具) |

## 三、核心故障与排查链(已确认的三层根因)

**用户原始故障**:串口已连接、已设可见,但车机和手机都搜不到蓝牙。

排查结论:不是一个原因,是**三层问题叠着**,逐层剥开:

### 第 1 层(已修复):sdkconfig 是 BLE-only
- 最初 sdkconfig 没开经典蓝牙,当然搜不到。
- 修复:逐项对齐官方示例 `esp-idf/examples/bluetooth/bluedroid/classic_bt/`(a2dp_source / hfp_ag)的配置:`CONFIG_BT_ENABLED=y`、`CONFIG_BT_BLUEDROID_ENABLED=y`、`CONFIG_BT_CLASSIC_ENABLED=y`、`CONFIG_BT_A2DP_ENABLE=y`、`CONFIG_BT_HFP_ENABLE/AG_ENABLE/WBS=y`、`CONFIG_BTDM_CTRL_MODE_BTDM=y`。
- 固件入口 `app_main.c` 用 `esp_bt_controller_enable(ESP_BT_MODE_BTDM)`。**坑**:代码里要传 `ESP_BT_MODE_BTDM` 这个真符号,不要用别名。

### 第 2 层(已修复):brownout 复位风暴
- 现象:开经典蓝牙射频的瞬间,电流冲击把弱供电的 3.3V 轨砸穿 brownout 阈值 → 芯片复位 → 再上电再跌 → 无限循环。串口日志表现为大量 `rst:0x1 (POWERON_RESET)`(实测 20 秒内 101 次),ROM 输出被截断。
- **这解释了"板子像死了"的大部分假象。**
- 修复(已烧录为 **固件 v0.2.0**):
  1. `firmware/main/app_main.c`:控制器初始化前 `vTaskDelay(pdMS_TO_TICKS(3000))`,错开启动电流冲击,也给 PC 侧留串口连接窗口;
  2. `firmware/sdkconfig`:`CONFIG_ESP_BROWNOUT_DET` 关闭( workaround,治标;根治是供电)。
- 修复后已验证:单次干净启动,无复位风暴。

### 第 3 层(软件已绕,硬件未解决,当前卡点):CH340 USB 链路摆动
弱供电下 CH340 的行为(实测归纳,不是猜测):

- 复位脉冲后约 0.6s,CH340 会从 USB 总线**消失**再回来;
- 已打开的句柄会报 `GetOverlappedResult/ClearCommError failed (PermissionError(13, Access is denied))` = 设备没了;
- 更隐蔽的形态:**句柄不报错但永远读不到字节**(Windows 重枚举后旧句柄僵死);
- 链路在"存活相位/死亡相位"之间振荡,**死亡相位可持续数十秒**;
- **频繁重开会延长死亡相位——只有静默等待才能恢复**(实测静默 10s 稳,3s 不够)。
- 关键教训:任何 DTR/RTS 电平操作(包括"打开后设为 False")经自动复位电路都会打出 EN 脉冲复位板子,把链路踹进死相位。**打开串口后绝对不要碰 DTR/RTS。**

PC 库对策(`btphone/transport.py`,已实现并测试):
- **温和连接**:默认直接开句柄握手,不做任何复位脉冲(板子自动启动,通常已在运行);
- 握手 `_handshake_tolerant_blip()` 容忍三类断链(句柄抛异常/僵死零字节/端口消失),断链就收口静默 2s 重建;
- 判活依据:窗口期内 `rx_bytes` 有无真实增量(audio.buffer 信用点事件每 50ms 一发,是天然心跳);
- **升级条件**:持续 12s(`silent_escalate_s`)完全零字节且句柄从未报错(= 板子真卡死/被闷在下载模式)才做一次 RTS 脉冲复位;脉冲收尾把 DTR/RTS 恢复成 asserted 态(与下次 open 默认一致,零跳变);
- 总预算 120s(熬死相位);超时错误信息区分两种:反复断链 → 提示换线/换口/供电 HUB;始终静默 → 提示查固件/占用。
- 注意:曾有版本静默判断误用绝对值 `rx_bytes == 0`(跨会话累计导致二次连接永不升级),已改为按本轮握手起点的**增量**判断。

## 四、当前状态(截至交接)

已完成并验证:
- 固件 v0.2.0 已烧录:BTDM 模式 + 3s 延迟 + brownout 关闭,**单次干净启动已确认**
- 裸串口抓包确认:板子活着,2 秒 4096 字节 audio.buffer 心跳,69 个 CTRL 帧全部正确解码(CRC 通过)
- PC 库:`BtPhone` API(media/calls/pairing 命名空间)、事件总线、音频推流链路(解码→ADPCM→流控)都已实现
- **测试:33 个非 GUI 测试全过**(`python -m pytest tests/ -q --ignore=tests/test_gui.py`)
- GUI(tkinter 控制台)可用

未完成/未验证:
- **`tools/verify_bt_boot.py COM7` 的最终真机验证没跑成**(上一轮在旧代码上跑,报 60s 超时;现代码已是 120s+增量判断,还没重跑)
- **蓝牙栈真正起来的最终确认**(事件流里看到 `sys.ready` + `bt.scan_mode`,手机能搜到 VPHONE-01)——这是交接后第一件事

已知小问题(不阻塞):
- `audio.buffer` 事件的 `free` 字段显示 4294967295(实际是 -1,固件 uint32 输出,纯显示问题)
- GUI 测试在**全套联跑**时随机挂一个 `_tkinter.TclError`(每次不是同一个测试,单跑必过)——环境性问题

## 五、交接后建议的排查步骤(按序)

```bash
# 0. 基线:确认测试全过
python -m pytest tests/ -q --ignore=tests/test_gui.py

# 1. 真机验证(核心):连上、握手、看蓝牙栈事件
python tools/verify_bt_boot.py COM7
#    预期:打印 sys.info(fw=0.2.0) + sys.ready/bt.scan_mode 事件,
#    设名 VPHONE-01 + 可见 120s → 这 120s 内用手机搜

# 2. 若上面握手失败:抓启动日志看复位/brownout/guru
python tools/capture_boot.py COM7 10 30
#    (参数:COM口 boot捕获秒数 app捕获秒数;容忍链路闪断自动重连)
```

判读:
- verify 成功 → 让用户手机(装 `资料/ESP32开发板资料/esp32蓝牙使用教程/HC蓝牙助手app安装包/HCbluetooth.apk`,或系统蓝牙设置)搜 **VPHONE-01**;车机同理。
- verify 报"USB 链路反复断开(疑似板卡供电不足)" → 是硬件供电问题,软件已尽力,按第六节清单处理供电后重试。
- verify 报"设备未就绪:Ns 内无任何事件" → 先用 capture_boot 看板子是否真的在启动;若 ROM 日志正常但无应用事件,查固件是否损坏/串口被别的进程占用。
- 若蓝牙栈起来了但手机仍搜不到 → 再查 GAP:capture 日志确认 `bt.scan_mode` 事件里 mode 是否 `disc_conn`;确认没有 `sys.error`(bt controller init failed 之类);必要时对照官方 a2dp_source 示例最小化复现。

## 六、硬件建议(转告用户,这是根因层)

供电疑似不足,软件只能绕,建议按成本从低到高:
1. **换一根粗短的数据线**(很多线材压降大)
2. **插电脑后置 USB 口**(主板直出,供电足于前面板/扩展口)
3. **带独立供电的 USB HUB**
4. **USB 功率计**(¥15-30,夹在线和板之间):直接看 5V 侧电压/电流——注意 ESP32 板上没有能测供电轨的 ADC,想用软件监控 3.3V 轨需要 2×10kΩ 分压接 GPIO34,库侧可以加 `sys.power` 命令 + GUI 电压条(用户此前问过电压监控,这就是可选方案)
5. 板上 EN/BOOT 按钮用户不需要做任何操作;只有烧录不稳时才用"按住 BOOT 按一下 EN"进下载模式

## 七、关键文件地图

| 文件 | 作用 |
|---|---|
| `btphone/transport.py` | **连接策略核心**:温和连接/闪断容忍/静默升级脉冲/120s 预算。改连接逻辑前先读注释,每个"为什么"都是实测教训 |
| `btphone/client.py` | BtPhone API:media/calls/pairing 命名空间 + 播放工作线程 |
| `btphone/protocol.py` | 帧协议:0xA5 0x5A 同步 + TYPE/LEN/SEQ + CRC16-CCITT |
| `firmware/main/app_main.c` | 固件入口(BTDM 使能 + 3s 延迟) |
| `firmware/sdkconfig` | BTDM 配置(对齐官方示例)+ brownout 关闭 |
| `tools/verify_bt_boot.py` | **真机蓝牙栈验证脚本**(交接后第一件事就是跑它) |
| `tools/capture_boot.py` | 启动日志抓取(115200 抓 ROM/复位日志 → 切 2M 抓应用) |
| `tests/test_client.py` | 33 个测试,含 4 个 transport 重连编排测试(全 mock,不动真串口) |
| `docs/PROTOCOL.md` / `docs/DEPLOY.md` / `docs/NOTES.md` | 协议/部署/笔记 |
| `资料/ESP32开发板资料/esp32蓝牙使用教程/` | 厂商教程:Arduino BluetoothSerial SPP 例程(**证明这块板经典蓝牙硬件是好的**)+ HCbluetooth.apk(手机端验证工具) |

## 八、重要背景(避免重新踩坑)

1. **不要"自己乱试"**:用户明确要求先查官方资料再动手。官方参考:esp-idf `examples/bluetooth/bluedroid/classic_bt/`(a2dp_source、hfp_ag);本地 sdkconfig 已逐项对齐过,不要轻动。
2. **验证过的失败不是固件问题**:每次烧录 hash 都校验通过,别往"烧录流程不对"方向查。
3. **假象识别**:"板子没反应"大概率是 USB 链路死相位,不是板子死了。先用 `capture_boot.py` 或裸 pyserial 静默读几秒,看到字节再下结论。
4. 串口打开后碰 DTR/RTS = 复位板子 = 弱供电下拖掉 CH340,这是实测规律,代码注释里也写了。
5. 用户是车载自动化测试工程师,中文沟通;工具的使用方式是脚本/GUI 控制单块板子。
