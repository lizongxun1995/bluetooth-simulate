# 部署指南

## 一、硬件准备

| 物品 | 要求 | 备注 |
|---|---|---|
| ESP32 开发板 | **原版双模**(模组 ESP32-WROOM-32,芯片丝印 ESP32-D0WDQ6/D0WD) | **禁止** ESP32-S3/C3/C6/H2(纯 BLE) |
| Type-C 数据线 | 必须能传数据 | 纯充电线不行 |
| USB Hub | 带独立供电 | 多块板同时跑蓝牙别靠电脑口供电 |

板子到货先自检:`python tools/check_board.py COM7`(需 `pip install esptool`),确认输出芯片为 ESP32。

## 二、固件烧录(一次性)

1. 安装 ESP-IDF v5.x(推荐 5.1+,按乐鑫官方 Windows Installer 安装):
   https://docs.espressif.com/projects/esp-idf/zh_CN/latest/esp32/get-started/
2. 在"ESP-IDF 5.x PowerShell"中:

```powershell
cd firmware
idf.py set-target esp32
idf.py menuconfig     # 确认 Btphone 默认配置即可(或直接跳过)
idf.py build
idf.py -p COM7 flash monitor   # monitor 可省略;注意日志已关闭,串口被协议占用
```

3. 每块板烧同一份固件,贴标签:VPHONE-01、VPHONE-02…(设备名烧录后也可用 `phone.set_name()` 改)。

## 三、PC 侧安装(Windows)

```powershell
cd bluetooth-simulate
pip install -e .
```

- 依赖自动安装:pyserial、numpy。
- 播放 MP3/FLAC 等非 WAV 文件需系统安装 **ffmpeg**(加入 PATH);只用 WAV 可不装。
- 串口驱动:CH340 系列装 [WCH 官方驱动](https://www.wch-ic.com/downloads/CH341SER_ZIP.html)(Win10/11 常会自动装)。

## 四、第一次联调(无车机)

用内置固件模拟器,把整条链路(不含真实蓝牙)先跑通:

```powershell
python examples/01_music_and_meta.py --sim
python examples/02_pairing_failure.py --sim
python examples/03_calls.py --sim
```

或进入交互控制台:

```powershell
btphone-cli --sim
```

## 五、连接真车机

```powershell
btphone-cli --port COM3
btphone> status                       # 固件握手
btphone> discover on                  # 车机搜索 VPHONE-01 并配对(PIN 1234 或 SSP 自动确认)
btphone> pair auto                    # 配对策略:自动接受
btphone> music play --file D:\曲库\a.mp3
```

- 车机主动连"手机":板子上电自动回连(可用 `pair policy --auto_reconnect false` 关闭)。
- PC 主动连车机:`connect <车机MAC>`(车机需处于可被发现/可连接状态)。

## 六、常见问题

| 现象 | 处理 |
|---|---|
| `打开串口失败` | 换 COM 号(设备管理器确认);关掉占用串口的工具 |
| 命令超时 | 确认波特率 2M(默认一致);重插 USB;重新上电板子 |
| 车机搜不到板子 | 确认 `discover on` 已执行;车机搜索时离板 <2m |
| 音频断续 | 检查是不是充电线;换带供电 Hub;降低曲库码率 |
| **WiFi 启动后串口掉线(COM 口消失又出现)** | 射频上电电流尖峰:换**短/粗/带屏蔽的数据线**、换台式机**后置 USB 口**、用**带供电 Hub**;板子本身没死,掉线 1~30s 后恢复,库用 `phone.wait_reconnect()` 跨过(见 examples/04_wifi.py) |
| 车机显示不了歌名 | 先确认配对后车机"媒体"来源选了蓝牙;再查 AVRCP 兼容(docs/BRINGUP.md) |

## 七、hfp_ag 兼容性(重要)

`esp_hfp_ag` 在不同 ESP-IDF 版本中 Kconfig 选项与回调事件命名有差异。
`firmware/main/hfp_ag.c` 集中了全部相关调用,编译若报错,对照你本地
`components/bt/host/bluedroid/api/include/api/esp_hfp_ag.h` 逐个对齐即可,
改动面不超过该文件。这也是计划中 Phase 0 spike 的验证点。
