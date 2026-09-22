# 项目备忘录（重要事项 / 环境路径 / 踩坑记录）

> 用途：记录环境路径、日常操作方法、以及实现过程中踩过的坑，避免重复犯错。持续更新。

## 1. 环境与路径

| 项 | 值 |
|---|---|
| ESP-IDF | v5.3.2，路径 `C:\Espressif\esp-idf` |
| IDF 安装器 | `C:\Espressif\frameworks\esp-idf-v5.3.2`（framework 目录），工具链在 `C:\Espressif\tools` |
| 目标芯片 | ESP32-D0WDQ6（WROOM-32 模组，**双模蓝牙**），`idf.py set-target esp32` |
| 开发板 | 38pin DevKit，Type-C + CH340 串口，**4MB Flash、无 PSRAM** |
| 串口 | **COM7**，波特率：烧录 921600 / 运行 2000000 |
| 板子资料 | `资料/` 目录（引脚图、WROOM-32 中文手册、Arduino 例程、CH340 驱动） |
| Flash 配置 | DIO 模式，4MB，自定义分区表 `firmware/partitions.csv`（nvs/phy/factory，**无存储分区**） |

### 板卡要点（来自官方资料）
- WROOM-32 = 原版双模（BT Classic + BLE）；S3/C3/C6/H2 是 BLE-only，本项目不可用
- 4MB Flash 上限，分区表不要超；无 PSRAM，堆 ~320KB，BT+WiFi 同开时内存紧张
- 烧录时若进不了下载模式：按住 BOOT 键再复位（教程明确提示）
- CH340 驱动安装包在 `资料/串口驱动/`

## 2. 日常操作（Git Bash 下）

### 编译 + 烧录（在 `firmware/` 目录）
```bash
cmd //c "set MSYSTEM=&& call C:\Espressif\esp-idf\export.bat >nul 2>&1 && idf.py build && idf.py -p COM7 flash"
```
- **必须 `cmd //c`（双斜杠）**：单斜杠会被 MSYS 路径转换吃掉
- **必须先 `set MSYSTEM=`**：Git Bash 的 MSYSTEM 环境变量会让 export.bat 误判为 Unix 环境而中断
- 只改了 Python 侧不需要重编固件；改固件后 flash 完按 `tools/check_board.py` 或直接用库握手验证

### PC 侧 Python 库
```bash
pip install -e .          # 安装 btphone 库 + btphone-cli
python -m pytest tests/ -q   # 27 个测试，全离线（走内置模拟器）
btphone-cli --sim         # 无硬件时用模拟器试命令
btphone-cli               # 真机（默认 COM7，可 --port 指定）
```

### 板子状态速查
```python
from btphone import BtPhone
p = BtPhone("COM7")
print(p.info())   # free_heap / wifi_init / wifi_err 等诊断字段在这里
```

## 3. 架构关键决策

1. **零板上存储**：所有歌曲/语音数据存电脑，播放时经 Type-C 2Mbaud 实时推流。板子不缓存任何媒体数据（clip_store/assets 分区已删除，partitions.csv 只有 nvs/phy/factory）。
2. **串口协议**：`0xA5 0x5A | type(1) | pad(1) | len(2LE) | seq(2LE) | payload | CRC16-CCITT(2LE)`；TYPE_CTRL=0x01(JSON)、TYPE_AUDIO=0x02；CRC 覆盖 type..payload。
3. **音频编码**：PC 端 ffmpeg 任意格式 → IMA-ADPCM(4:1) → 串口 → 板端解码 → SBC → A2DP。ADPCM 块格式特殊：BLOCK_SAMPLES=1023（奇数），块头存第 0 个采样，nibble 编码 1..n-1。
4. **流控**：板端每 **50ms 无条件**发 `audio.buffer` 事件上报空闲字节；PC 端按预算发送。不能改成"变化才上报"——缓冲清空后 PC 会饿死（已踩坑）。
5. **AVRCP 元数据**：IDF v5.3.2 官方 AVRCP TG 没有元数据 API，用 `firmware/patches/apply_patch.py` 给 IDF 打补丁（新增 esp_avrc_tg_set_track_info/set_play_status）。**重装 IDF 后要重新打补丁**。

## 4. 踩坑记录（按时间）

### 串口 / CH340
- **打开串口即触发复位**：pyserial open() 默认拉高 DTR/RTS，CH340 电路会让板子进复位/下载模式。修法：open 后立即 `dtr=False; rts=True; sleep(0.1); rts=False` 做一次确定性复位进运行模式。close 时也要释放 DTR/RTS。
- **ROM boot 窗口期写入会卡死下载模式**：复位后 ~0.5s 内写数据可能把芯片锁在 download-wait（现象：RX 全 0x00）。修法：handshake 先等板子主动发的第一个事件（audio.buffer 心跳），**再**发命令。
- **RX 方向启动后 ~8s 才稳定**：handshake 里 sys.info 重试 15 次 × 1.5s。

### 协议 / Python 库
- FrameParser 一开始把帧头当成 5 字节（len 挪位），seq 跨进 payload → CRC 全挂、帧全丢。帧头固定 **6 字节**，seq 在 offset 4。
- CRC 覆盖范围必须含 pad 字节：`crc16(header+payload)`。
- 事件竞态：命令执行期间发出的事件会在 wait_event 之前被错过 → EventBus 维护历史队列 + `wait_event_ext` 返回 seq；公开 API 用 `_operation` 装饰器打命令位置标记，wait 从 `max(游标, 命令标记)` 起算。
- 歌单播完后 `music.stop` 不能紧跟 push 发——会吞掉 music.ended 事件。

### 固件 / IDF
- **Git Bash 编译**：MSYSTEM 必须清空，`cmd /c` 要写成 `cmd //c`（见上）。
- cJSON.h 找不到：main/CMakeLists 的 REQUIRES 里要加 `json`。
- IRAM 溢出 2.3KB + DRAM 溢出 57KB：静态解码缓冲(72KB)全改堆分配（懒分配，首次 audio.open 才分）；`CONFIG_COMPILER_OPTIMIZATION_SIZE=y`；去掉 `CONFIG_UART_ISR_IN_IRAM`。
- IDF v5.3.2 API 与旧资料差异：`esp_bt_dev_set_device_name` 已废弃 → `esp_bt_gap_set_device_name`；HFP 用 `esp_hf_ag_api.h`（ESP_HF_*_EVT）；A2DP media ctrl 确认走 `ESP_A2D_MEDIA_CTRL_ACK_EVT`。
- **esp_wifi_init NO_MEM(257) —— 已修**：`audio_pipeline_init` 里残留了一份急切分配(~92KB 环缓冲 + 6KB 任务栈)，排在 `wifi_nat_init` 之前，把 `esp_wifi_init`(需 ~33KB)挤死。教训：**懒分配重构要做彻底，别只加 alloc_buffers() 忘了删 init 里的旧分配**。诊断手段：wifi_nat_init 各步堆水印(`wifi_heap` 字段)+ `esp_wifi_init` 前后对比，一次定位。
- **esp_wifi_set_max_tx_power 必须 start 之后调才生效**：放 init 后是无操作。已挪到 STA_START/AP_START 事件回调里（`WIFI_TX_POWER_QDBM`，单位 0.25dBm，8=2dBm）。
- `esp_reset_reason()` / RTC_DATA_ATTR 开机计数（sys.info 的 `reset`/`rtc_boot` 字段）是区分"芯片重启"vs"仅 USB 掉线"的关键证据，别裸猜。

### WiFi 启动会打掉 USB 链路（硬件现象，重要！）
- **现象**：`net.ap_start` / 射频第一次上电瞬间，CH340 从 Windows 总线消失（GetOverlappedResult Access denied → COM 口消失），1~30s 后自动重新枚举。100% 复现（5/5），2dBm 满降功率也拦不住。
- **板子其实没死**：RTC 开机计数不变、堆不变、心跳照发——只有 USB 链路被干掉；但 `do_ap_start` 尾部状态会被暂态打乱（phase 探针=0、mode 读回脏值），**AP 实际没起来**。
- **定性**：BT 满功率跑几小时没事，唯独 WiFi 上电必出事 → 射频初始化的电流尖峰/辐射，固件无法根治。这是这块板 + USB 线 + 端口的供电问题。
- **对策**：
  1. 换**短/粗/带屏蔽的数据线**；换台式机**后置主板 USB 口**；用**带独立供电的 Hub**（DEPLOY.md 排查表已加此条）。
  2. 库侧 `BtPhone.wait_reconnect()`：等端口回来并无复位重连（`open(reset=False)` 跳过 RTS 复位脉冲），脚本跨过暂态继续干（见 examples/04_wifi.py 的 resilient 模式）。
  3. 遗留验证：换好供电后重跑 `examples/04_wifi.py`，并用**手机 WiFi 列表**确认能看到 VPHONE-AP（本机 WLAN 开关是关的且无管理员权限，PC 侧没法自己验）。

### 内存预算（4MB Flash / 无 PSRAM 板）
- 音频环：STREAM 24KB + PCM 32KB + 解码临时缓冲（堆，**首次 audio.open 懒分配**）
- WiFi 静态 RX 10 + 动态 RX 8 / TX 6；esp_wifi_init 本身吃 ~33KB（堆水印实测）
- BT Classic（A2DP+AVRCP+HFP+GAP）本身吃 ~100KB 级堆
- 结论：BT+WiFi+音频同开是这个板子的极限，任何大缓冲都要懒分配

### 图形界面(btphone/gui.py)
- **链路中断后库永久装死（2026-09-01，已修）**：A2DP 推流期间 USB 链路被打断（观察点应验——
  14:10 播歌,14:21 起 GUI 全部报"串口未打开"）。板子其实一直活着(probe bt_up=true),链路自愈了,
  但 `_handle_broken` 收口后 transport 永不再打开,GUI 只有 WiFi 类操作会 wait_reconnect。
  修法：`transport.request()` 收口状态下先 `_auto_recover()`——真机 close()+open(reset=True)
  强重连（blip 熬惩罚态,最长 ~2min）;sim 复用注入对象走轻握手;失败后 5s 静默窗内直接快速报错
  防点击风暴。GUI 状态灯随之中断变橙"●重连中"、恢复变绿并记日志。
- **GUI 按钮"点击无效"bug（2026-09-01，已修）**：`_kick(fn,*args)` 是**工厂**——返回闭包给
  `command=`。带"点击时才取值"参数的按钮曾写成 `command=lambda: self._kick(fn, var.get())`，
  点击时调用了 `_kick` 但**返回的闭包被丢弃，什么都没发生**（无日志无报错）。受害按钮：设置
  蓝牙名、连接、连接模拟器、主动连接、模拟来电/拨出、推送元数据、▶播放、语音开、热点开关、
  连接 STA、配对模式下拉——共 13 处。修法：动态参数一律 `lambda: self._run(fn, var.get())`。
  冒烟：`tools/gui_smoke_buttons.py`（真实 invoke 按钮路径）。注意"库和固件正常、只有 GUI 坏"
  时先用 `tools/debug_set_name.py` 之类绕开 GUI 直打库，能立刻二分定位。
- **测试里反复 `tk.Tk()` 会随机炸（2026-09-01，已修）**：一个 pytest 进程开第 N 个 Tk 根实例
  后，Windows 上随机报 `fonts.tcl: no such file`（文件其实存在，Tcl 状态坏了），挂的用例
  每轮不同。修法：`tests/test_gui.py` 用 `scope="module"` 的 Tk 根，每用例 `Toplevel` 隔离；
  `pump` 吞掉销毁后残留 after 回调的 TclError。
- 改名后的真机回显：`_set_name` 现在会回读 info 刷新左栏。**手机端可能缓存旧蓝牙名**——改名后
  手机上要重新搜索（或先"取消配对"）才显示新名字,不是设置失败。

- tkinter 实现,入口 `btphone-gui` / `python -m btphone.gui`(`--sim` 连内置模拟器)。
- 线程模型:阻塞调用全走 `ThreadPoolExecutor(2)`,事件经 `queue.Queue` 回流主线程(`root.after(80)` 泵),**tkinter 变量只能在主线程读**——按钮回调里读完变量再传参给工作线程。
- `_kick(fn,*args)` 必须返回**闭包**给 `command=`,直接写 `command=self._kick(fn)` 会在建控件时就执行(已踩)。
- `audio.buffer` 心跳(50ms 一条)在订阅处过滤掉,否则日志区刷屏。
- GUI 冒烟测试在 `tests/test_gui.py`:驱动真实 Tk 循环调按钮同路径方法,无头环境可跑(root.withdraw)。

### BT 射频上电掉 USB 复审定案 + 库自续命 bug（2026-09-01，重要！）
此前在 PC 侧连接逻辑里越修越深，复审用四个实验一锤定音：
- **实验1**（`tools/watch_bt_boot.py`，2M 单会话抓全程+离线解码）：复位后链路**精确在 3.68s 掉口，100% 复现**——正好是 `vTaskDelay(3000)` 后 BT 控制器上电瞬间。恢复后心跳照发（CPU 活着）。
- **实验2**（固件加 `bt_up` 标志 + `sys.stage` 打点，v0.2.0）：恢复后安静探针 `tools/probe_bt_up.py` 查 sys.info → **`bt_up=true`**。app_main 每次都走完，`sys.ready`/`bt.scan_mode` 是**发进了 3.7s 断链死窗里丢了**，不是没发。free_heap 95KB。
- **实验3**（`tools/test_close_effect.py`，rtc_boot 计数法）：**开/关句柄不复位板子**（rtc_boot 四轮不变）。
- **实验4**（`tools/debug_open.py` 间谍钩子）：库握手失败真实形态=句柄零字节 3s→收口→**固定 2s 节奏重开→每次 `ClearCommError Access denied` 瞬死**——重开本身把 hub/驱动惩罚态续命成永久死锁（120s 耗尽报"链路反复断开"）。单次打开、从不重开的探针永远成功。
- **USB 拓扑**（`tools/usb_topology.ps1`）：COM7 `LOCATION=1-2.3.4`=**两级 Generic Hub（05E3:0610 无源）后面**（显示器/dock 口），弱供电+过流保护全对上。
- **修复**：① transport 握手 `first_byte_grace_s=8`（句柄活着零字节先宽限——启动静默+射频瞬断窗本来就 ~4s，别急着收口制造新过渡态）；② 重开间隔指数退避 2→4→8→16→20s（只有静默能让链路恢复）；③ 静默升级判断按本轮增量 `rx_bytes==started_bytes`（修掉跨会话累计导致二次连接永不升级的 bug）。33 测试全过，**`verify_bt_boot.py` 真机全绿**。
- **教训**：**"握手成功"≠"蓝牙栈起来"**——audio.buffer 心跳在 BT 初始化**之前**就开始发，`wait_event("*")` 匹配的第一条就是心跳。判断栈死活必须看 `sys.ready`/`bt_up`。另一个教训：症状侧（连接逻辑）打补丁前，先做一次能直接观测问题本体的实验。

### "搜得到但排位垫底/信号弱"三嫌疑 + 修复（2026-09-01，v0.2.1 待烧录）
用户实测：手机 1m 内能搜到 VPHONE-01，但排在整个列表最后面。三个嫌疑：
1. **BT modem sleep 开着**（sdkconfig `CONFIG_BTDM_CTRL_MODEM_SLEEP=y`，original 模式），
   而低速时钟是**内部 150kHz RC**（`CONFIG_RTC_CLK_SRC_INT_RC=y`，无 32K 晶振）——RC 漂移让
   inquiry 扫描窗错拍，设备对搜索**响应迟缓/时断时续**，手机列表里就出现得晚、排位靠后。
   台架设备 USB 常供电，sleep 零收益。**修法：app_main bluedroid 起来后 `esp_bt_sleep_disable()`**。
   参考 esp32.com 同类症状帖（inquiry 响应慢/CoD 迟迟不出）。
2. **两级无源 hub 弱供电压低发射功率**——RF PA 电流冲击时 3.3V 跌落（brownout 已禁用所以不
   复位，但 PA 输出受损），1m 内 RSSI 应该 -45~-60dBm，垫底说明手机收到的可能 < -75dBm。
   这与"射频上电掉 USB"是同一根源（供电）。**修法：换带独立供电的 hub，或临时插后置口做 A/B。**
3. **摆位**：WROOM-32 是 PCB 板载天线，显示器立柱/dock 金属壳/金属桌面边缘都会削信号。
   天线侧朝外、离金属 5cm 以上。
- **量化手段（v0.2.1 新增）**：固件加 `bt.rssi` 命令（`esp_bt_gap_read_rssi_delta`）——配对完成
  自动上报一次 `bt.rssi` 事件（`rssi_delta`: 0=基准区间内很强，负值=低多少 dB）；之后可随时
  `phone.request("bt.rssi")` 再测。判据：**≥ -10 正常；< -30 信号确实差，查供电/摆位**。
- SIM 同步 `bt.rssi`（`_CmdFail` 业务错误类型，错误串直透），40 单测全过。

### "每次播放都断连"真相：USB 高延迟病理态（2026-09-04，当时定案——"hub TT"假设后被推翻，真根因见下一节）
用户报 GUI 播放必失败 + 界面卡。逐层剥洋葱（`tools/stream_probe.py`→`graylink_probe.py`→`raw_probe.py`）：
- **板子完全清白**：裸协议探针（不经 btphone 库）每条 sys.echo 都正确应答，**零丢包、零 CRC 错**，
  rtc_boot 全天 =1 从不重启。固件、ESP32 芯片都没问题。
- **真凶**：USB 链路进入**高延迟病理态**——命令往返 **3~10+ 秒**（正常 <10ms），数据被成批滞留
  （几百条心跳挤在同一毫秒到达）。库的 3~10s 超时在此期间误报"失败"；命令其实都到了、板子也回了，
  只是响应迟到被弃。"播放断连"= avrcp.metadata 超时，音频帧根本没开始发。
- **定位**：CH340 是全速 USB 设备，数据经带供电 HUB 的转发芯片（TT）中转——廉价 HUB 的 TT 在持续
  流量压力下会进入这种批量滞留态（知名问题）。今早 GUI 推流能跑 20 分钟，说明是当天逐渐恶化的
  TT/驱动状态，不是稳定存在的缺陷。
- **库层防御（已做，42 测试过 + 真机验证 3.4s 延迟下命令全通）**：transport 记录 `_last_rx_mono`，
  `link_sick()`=心跳断流 >1.5s；`request()` 等待期间动态判断，病理态自动 +15s 续期（命令迟到但能到，
  不误报）。GUI 状态灯黄色"●高延迟"+ 处置提示。
- **根治在硬件路径（用户操作）**：① 重插板子/换 HUB 口（TT 状态复位，立即见效的验证）；② 诊断性
  直插电脑后置口一次确认 HUB 是元凶；③ 确认后换每口独立 TT 的质量好的带供电 HUB（几十块）。
- **注意**：病理态下推流音质必卡（信用点也 3 秒一批到达），播放要顺滑必须修 USB 路径，软件只保证
  命令不误报。延迟体检随时可做：`python tools/raw_probe.py COM7`（看命令往返耗时）。

### 真根因：ch341ser 大块 ReadFile"攒满才完成"× pyserial read(4096)（2026-09-04 深夜，终审定案）

上节"hub TT"结论被连续三个阴性实验推翻：换 HUB 口 ✗、关系统 USB selective suspend ✗、
**直插主机根口（零 hub 级联）✗——症状分毫不变**（`tools/link_watch.py`：纯听 15s 零发送，
beacons 仍每 3.0~3.5s 一批 65~70 帧、最大静默 3500ms、echo RTT 3391ms）。物理层全部出清后，
两条铁证把责任锁到 PC 侧驱动/读法：
1. **固件不可能"攒批"**：`uart_driver_install(..., tx_buf=0)`（`host_link.c:597`，发送直写硬件
   FIFO）——若下游停止取数，beacon 任务会阻塞在 `uart_write_bytes` 里**停止生成**，每 3s 只会
   到 2~3 帧；实际每批 ~67 帧、序号连续、全天零丢帧 → 数据是**实时穿过 USB 到达电脑**后被扣在
   驱动里才放行。
2. **.NET SerialPort 独立交叉验证**（`BytesToRead` 每 2ms 轮询）：59B（=1 条 beacon）每 50ms
   **完美平滑到达**——线上节律从未坏过，坏的是应用取数这一段。

`tools/read_pattern_probe.py` 一锤定音（同一链路、只换读法）：`read(4096)`+timeout=0.05 → 10s
只到 **3 次**（间隔 3500ms）；`in_waiting` 精确读 → **393 次**（p50=47ms / max=62ms）。
**机制**：pyserial 设 timeout=0.05 时只写 `ReadTotalTimeoutConstant=50`、`ReadIntervalTimeout=0`
（serialwin32.py:116）；ch341ser 的挂起 ReadFile 在此组合下不按总超时"有多少返多少"，而是
**攒满请求字节数才完成**。4096B ÷ 心跳流 1.3KB/s = **3.3s**——"3 秒"不是任何定时器，是缓冲
攒满时间。历史现象全部对上：早间推流 20 分钟正常（音频码率高，4KB 瞬间攒满）；空闲命令 RTT
3.4s；连续 echo 0.4s（离满 4KB 恰差 ~0.4s 心跳）；用户 MCU 串口/ADB/投屏流畅（不同驱动/不同
应用读法，不受此坑）。

**修复（零硬件/零系统改动）**：`transport._rx_loop` 改 `read(io_obj.in_waiting or 1)`——只读
"已到达"的字节数，空闲时 read(1) 等首字节。真机验证：握手 0.08s，sys.info / sys.echo /
avrcp.metadata / conn.status 全部 **47~63ms**（含原"必断连"元凶 avrcp.metadata）。
**衍生结论**：① 板子随时可**放回 hub**（直插主机纯属诊断用，不必保留）；②
`tools/fix_ch340_epm.ps1`（CH340 EPM=0 管理员方案）**不需要了**，留档备用；③ 系统级 USB
selective suspend 保持关闭（对串口台架更安全；恢复则把对应 setacvalueindex 值设回 1）；
④ 库层 link_sick/+15s 续期防御保留（无害，防真实链路劣化）。
**复盘教训**：①"数据成批到达"类症状，先用**独立于嫌疑技术栈**的观测法（.NET/纯 win32）看
驱动层真实到达节律，再谈传输路径责任；② **读缓冲大小本身就是症状面**——同一驱动不同读法
天壤之别；③"TT 假设"输在把"3 秒"当成硬件定时行为，没先做 4096÷1300 这道算术。


### GUI 卡顿三个线程 bug（2026-09-04，已修）
tkinter 本身没问题，**不用换框架**——卡顿全是我们的用法错误：
1. **主线程发阻塞串口请求**："刷新状态"按钮直调 `_refresh_info()`（`phone.info()` 最长 10s+）；
   事件处理器 `_set_conn_text()` 在主线程调 `phone.conn.status()`——链路坏时自动重连握手最长 2 分钟
   冻死界面。→ 拆成"工作线程拉数据 + `_post` 回主线程渲染"（`_refresh_info`/`_refresh_conn`/`_set_conn_text`）。
2. **工作线程直接调 tkinter（含 `root.after`！）**：跨线程调用只有主线程恰好在 mainloop 里才会被处理，
   否则永久挂起该工作线程=偶发卡死。→ 新增 `_ui_q` 线程安全队列，`_post()` 入队，`_drain_events`
   （80ms 主线程泵）消费；`_log` 自动转主线程。
3. **日志无上限**：长推流会话 Text 无限膨胀越跑越慢。→ `MAX_LOG_LINES=2000` 环形裁剪。
另：`_music_play` 的 Listbox 选区改点击时（主线程）取值；`_set_pair_mode` 同理；`lbl_link` 状态改
`_link_lost` 标记（不跨线程 cget）。


### 真机车机联调四连修（2026-09-01，v0.3.0→v0.3.2，固件+PC 双侧）

**v0.3.0 根因1——配对成功但无任何 profile 连接**：CoD 曾是默认值，车机只当"未知设备"。
→ CoD 改智能手机 `0x5A020C`；配对成功即自动连 a2dp/hfp；上电回连（NVS 记 peer）；命令全部
fail-fast（错误带回 err_msg 而非静默）；GUI 加 profile 连接指示。此后车机侧配对/回连正常。

**v0.3.1 根因2——流媒体期 panic（ESP_RST_PANIC=4，整除零）**：裸发 `music.play`（未经
`audio.open`）→ Bluedroid 立即拉 PCM → `ring_read` 对未初始化 ring 取模 `r->size=0` →
IntegerDivideByZero。取证手法：注入 `BtPhone(io=LoggingSerial)` 抓原始 RX 字节（帧解析器会
丢弃 panic 文本！）+ `xtensa-esp32-elf-addr2line` 解回溯。修复五件套：① ring 全操作
size==0 守卫；② `music.play/resume` 校验 `audio_pipeline_ready()`；③ **音频缓冲启动即预分配
61K**（顺序决定生死：媒体通路先起会吃光大块堆，audio.open 永远失败）；④ 缓冲缩减
（STREAM 16K + PCM 24K + 解码 20K）；⑤ 未分配时 free 报 0 而非 SIZE_MAX（防 PC 信用点爆）
+ underrun 事件限频 1/s（BTC 任务上下文）。

**v0.3.2 链路稳定三连修（有官方/社区资料背书）**：
- **HFP 停止主动回连**：实测该车机对 AG 主动发起的 HFP SLC 约 5s 后掐断，且**连坐整条 ACL**
  （a2dp 跟着掉，"reason":"normal"）；车机自己发起的 HFP 反而长期保持。→ `connect_timer_cb`
  只自动连 a2dp；HFP 等车机发起，或 PC 端 `conn.connect(profiles=["hfp"])` 显式连。
  （pschatzmann/ESP32-A2DP#26/#393：车机普遍倾向自己发起 HFP）
- **静音保活**：A2DP 连上即 `audio_set_playing(true)+a2dp_start()`，空环输出静音——车机不再
  掐"从不播媒体"的空闲音频链（esp-idf#6342 同型问题）。music.pause/stop 仍正常挂起。
- **掉线退避重连** 3s/6s/12s（最多 3 次），用户主动断开不回连。
- **`music.ended` 固件端实现**（此前只有模拟器会发！真机播完 worker 永远等不到、列表永不切歌）：
  PC 每曲推完发 EOS 空帧（协议早有），decode_loop 见"EOS + 流环空 + PCM 环空"即上报；
  **EOS 后解不动的尾块必须丢弃**——不完整 ADPCM 块会让 avail 永不归零，事件永不触发。
- PC 侧 `music.play` 重试窗 5×12s≈60s（覆盖固件退避全程），TransportError 也按可重试处理。

**PC transport 三重修复（"恢复永远失败/8 分钟挂死"根因群）**：
1. **僵尸 rx 线程竞态**：`close()` 后重开会清 `_closed` 并换新句柄，速度快于旧 rx 线程复查
   标志 → 旧线程拿着已关句柄 read 抛错 → `_handle_broken` **把刚建好的新会话再杀一次**。
   → rx 循环加句柄身份校验（`self._io is not io_obj` 即安静退场）。
2. **死句柄复用**：`_handle_broken` 只置 `_closed` 不清 `_io`；`close()` 见 `_closed` 已置早退
   也不清；`_start_session` 见 `_io` 非空直接复用死句柄。→ 真串口断链时立刻丢弃并 close 句柄
   （注入后端 sim 除外——它有状态必须复用）。
3. **恢复递归死锁**：恢复中 `_verify_ready` 的 sys.info 再进 `_auto_recover`，在已持有的
   非重入锁上自死锁（表现为之前 catch_panic2 的 8 分钟挂死）。→ `_recovering` 标志让恢复
   路径的请求直接 fail-fast + 锁 `acquire(timeout=15)` 有界等待。

**"神秘重启"真相（排查半天的乌龙）**：`sys.stage ctrl_init` 在启动后 3~7s 才出现**不是重启**
——`app_main` 有意 `vTaskDelay(3000)` 错开射频上电电流冲击（弱供电板）。判据：事件 seq 连续
递增、悬置请求仍被应答。真正断链是 **BT 射频上电瞬态把弱供电的 CH340 砸下 USB**（数秒自愈，
transport 注释早有记载）——修复前恢复逻辑瘫在这，修复后 6~11s 全自动恢复。

**终极验证**（tools/verify_boot_play.py）：esptool 硬复位→握手后**立即** play（抢在 BT 栈起
来前）→ 射频瞬断 USB 掉口→ 自动恢复→ a2dp 回连+静音保活→ `music.started`→ 播完
`music.ended`→`playlist.done`，全程无人工干预、无 panic、无挂死。pytest 44/44。

**遗留观察点**：① heap 5.9~10K 偏紧（静音保活时振荡），已无崩溃但继续盯；② HFP 通话流程
真机未验（车机发起 SLC 后再测来电；WBS/mSBC 协商疑点，必要时关 WBS）；③ HFP 主动连接仍会
被该车机踢（策略已改为不主动连，显式连接留作测试用）。

**v0.3.3~v0.3.5（2026-09-01 晚）——AVRCP 通了 + 通知协议补丁**：
- **AVRCP 永远 disconnected 的根因 = 初始化顺序**：`a2dp_source_init()` 先于 `avrcp_tg_init()`
  时，btc 层 `g_av_with_rc=false` → `BTA_AvEnable` 不带 RCTG 特性 → 既不主动连 AVRCP
  （bta_av_act.c AVCT_INT 分支条件不成立）也不接受车机连入（bta_av_main.c 不建 AVCTP 接受者）。
  IDF 源码 btc_avrc.c 自己就写着 "AVRC Target is expected to be initialized in advance of
  A2DP !!!"。修复：`bt_stack_ready` 里 avrcp_tg → a2dp 顺序。实测 avrcp 在 a2dp 后 +0.1~10.8s
  连上（此前永远不连）。
- **IDF 库缺陷补丁 v2（patches/apply_patch.py）**：`btc_avrc_tg_send_rn_rsp` 的事件 switch 只
  实现了 VOLUME_CHANGE，其余命中 "// todo" 直接 return——TG 永远发不出 PLAY_STATUS/TRACK/
  PLAY_POS 通知，且 REGISTER_NOTIFICATION 的 INTERIM 也不回（esp_avrc_api.h:193 明说应由应用
  在 1s 内回）。车机注册悬挂→重试风暴→堆 30704→5888+命令超时。补丁补齐三个事件的应答装配；
  固件侧 avrcp_tg.c 维护 `s_ntf_mask`（注册位图：INTERIM 置位、CHANGED 清位、断连清全部）。
  music started→ended 真机全通。
- **WBS 关闭**（sdkconfig 两处）：mSBC 协商引发车机 HFP 掉线（esp-idf#14700 等同类），CVSD-only
  换兼容性，顺带启动堆 +20K。
- **改名后搜不到**：`esp_bt_gap_set_device_name` 不刷新 EIR → `bt_gap_set_name` 改名后重应用
  当前 scan mode（`s_scan_mode` 状态），并上报 `bt.scan_mode` 事件供 PC 确认。手机端按 MAC
  缓存名字，验证需手机开关蓝牙重搜索。
- **平台决策**：Pico 2（RP2350+CYW43439）否决——无 SCO 音频通路（HFP 语音做不了）、实时立体声
  A2DP 不现实；现成替代方案 = 树莓派 + BlueZ + oFono（sipfront 2025 文章，未采用）。**用户拍板
  ESP32 单线走到底**，PBAP/MAP（Phase 4）接受降级/受限。

**v0.3.6~v0.3.8（2026-09-01 深夜）——串口链路丢帧定案 + 卡顿根治**：
- **静默 panic 取证**：v0.3.5 曾在 a2dp 重连后 ~6s（车机初始交互窗）panic（reset=4）但串口零
  输出。根因=`CONFIG_ESP_SYSTEM_PANIC_REBOOT_DELAY_SECONDS=0`，复位把未发出的 UART FIFO 一起
  带走。已改 2s（sdkconfig+defaults），下次 panic 能抓 backtrace。panic 复位→射频上电瞬断→
  USB 掉电冷启动（rtc_boot 清零）的连锁也已从取证数据对齐。
- **"卡"+偶发超时的真根因 = 串口链路整帧丢失**：空闲态压测 300 次请求 26 次超时（8.7%），
  超时瞬间信用事件仍在毫秒级流动（板子活着）；v0.3.7 计数器显示 `rx_crc_err=0`——不是误码，
  是 CH340/USB 链路 wholesale 丢字节（双向）。CRC 帧协议静默丢帧无重传 → 每次丢帧 = 8s 冻结。
- **修复（标准做法：幂等重传）**：固件侧 `dispatch_command` 缓存最近请求 id+应答，重复 id 只重
  发缓存（命令不执行两遍，幂等由固件保证）；PC transport 0.6s 未应答同 id 重发；请求 id 改
  时间种子（跨进程单调，防撞固件缓存）；`pack_frame` seq 掩码 u16（大 id 曾会 struct.error）。
  **实测失败率 8.7%→0/300，最坏 RTT 8s→~2s**。
- **延迟地板 46~63ms 根因**：`host_rx_task` 用 `uart_read_bytes(...,512,50ms)`——读不满就等满
  超时，每个请求平白 +≤50ms。改"首字节阻塞 + 抽干缓冲"两段式，实测 RTT min<1ms。
- **音频环 41K→20K**：a2dp+avrcp 连接后堆 ~3K，HFP 的 L2CAP/RFCOMM 建链缓冲挤不进（sys.info
  字段缺失 = cJSON 低堆分配失败的旁证）。STREAM 16K→8K、PCM 24K→12K（信用流控下自稳），
  v0.3.8 空闲堆 45K。
- **HFP 定性（本轮）**：堆耗尽假设被证伪——27K 空闲时 AG 主动 SLC 依然秒断；确认该车机**只
  接受自己发起的 HFP**（与 v0.3.2 结论一致）。今晚车机被反复连断后进入拒连退避（连完即断、
  ~20min 冷却后恢复过一次）——车机侧蓝牙开关可立即恢复。
- **车机行为记录**：短时间反复 connect/disconnect 会让车机 BT 监督进程进入退避（a2dp 也拒），
  测试脚本要拉长间隔；恢复手段=等 ~20min 或车机关开蓝牙。


### WinRT 原生路线首轮实测（2026-09-01 上午，windemo + 台架反馈）
- **门A ✅（零代码基线）**：Windows 设置手动配对 CARKIT-1 后，网易云音乐播放 → 车屏显示
  歌名/歌手/歌词/播放状态。Windows→车机 A2DP+AVRCP 链路完全通，零硬件成本成立。
- **门B 首测（部分通）**：`track` 静态推送车屏无变化；`play`（PlaybackStatus=Playing）后车屏
  显示假歌名——**windemo 从未发送过歌词，车屏却显示歌词 ⇒ 歌词是车机按歌名云端匹配**
  （AVRCP 无歌词字段的实证；假歌名填真歌名可带出真歌词）。缺口 = 无音频流（无声音）。
  → btwinrt 引擎加 AudioGraph 静音流兜底（向车机端点渲染空帧=激活 A2DP），待车机侧复测。
- **配对 verdict**：32feet `PairRequest` 对该车机不成立（车机侧不认）——**必须 Windows 设置
  手动配对一次**；配对持久，之后 Windows 播音频自动连 A2DP。自动化测试结论：配对=一次性
  人工环境准备，脚本只复用配对态（scan/unpair 脚本化保留）。
- **call 首测 null**：旧 windemo 启动时缓存 CallControl，用户配对发生在启动后——btwinrt 改为
  每次动态 `GetDefault()`，配对后复测（门C 未定案）。
- **工程化**：核心抽成 `btwinrt` 类库（SMTC+AudioGraph 静音流+动态 CallControl+scan/pair/unpair，
  去 WinForms 改原生窗口），`pydemo/gui.py` 用 pythonnet(coreclr)+tkinter 调 DLL。
- **大坑：GetForWindow 拒绝消息专用窗口（2026-09-01 定案）**：类库化首测 InvalidCastException
  "Specified cast is not valid"，堆栈指向 `As<T>()` 内 QI——其实 `As<T>` 成功，真凶是
  `GetForWindow` 返回 **E_INVALIDARG(0x80070057)**：HWND 用 `STATIC + HWND_MESSAGE`（消息专用
  窗口）不合格，兜底 GUID 的 QI 异常把它掩盖了。**必须是顶层窗口**（不可见即可，坐标丢到
  -20000 屏外）。线程套间(STA/MTA/专用线程)全是烟雾弹——python 主线程 MTA 一样成功。
  诊断手法：把 As / GetForWindow / hr 分步打点进日志，别让 try/catch 链吞掉中间 HRESULT。
- **pythonnet 调 .NET8 DLL 配方（已验证）**：`pythonnet.load("coreclr")`（DOTNET_ROOT 指向
  `C:\Program Files\dotnet`）+ 对 lib 目录逐个 `Assembly.LoadFrom(...)`（LoadFrom 上下文自动
  探测同目录依赖：WinRT.Runtime / Microsoft.Windows.SDK.NET / InTheHand.Net.Personal）+
  `clr.AddReference`。引擎事件 `Action<string>` 可直接 `eng.OnLog += lambda`（.NET 线程回调，
  GUI 侧走 queue 切回主线程）。selftest 已确认 `[python.exe] 青花瓷/周杰伦 状态=Playing`
  注册进系统媒体会话。
- **端点观察**：车机配对后渲染端点列表显示为 **"远程音频"**（`GetAudioRenderSelector` 的设备
  名），GUI 静音流就选它。AudioGraph 真实签名：`AudioGraphSettings(AudioRenderCategory.Media)`
  构造、`QuantumSizeSelectionMode` 是**属性**——均已加进 tools/_apidump 留档。
- **扫描失败双层根因（2026-09-01 上午定案）**：① 回归——去掉 WinForms 后丢了 WindowsDesktop
  运行时 TPA 里的 `System.Configuration.ConfigurationManager.dll`（纯 NETCore 运行时没有），
  32feet(net461包) 运行时按需加载它 → FileNotFound。修法：btwinrt 显式引用
  `System.Configuration.ConfigurationManager 8.0.1` + `System.Management 8.0.0`（随包落地
  pydemo/lib，pythonnet 与 exe 宿主通吃，实测无 WinForms 也扫出 55 台）。② 用户把系统蓝牙
  关了——"No supported Bluetooth protocol stack found" 同时也是**射频关闭的症状**（与 SO
  报告一致），别急着怀疑依赖。
- **API 开关蓝牙：可行（实测 Allowed，无需管理员）**：`Windows.Devices.Radios.Radio.GetRadiosAsync()`
  + `SetStateAsync(RadioState.On/Off)` → `RadioAccessStatus.Allowed`，unpackaged 控制台程序在本机
  放行。引擎加 `RadioList()/BtSetPower(bool)`，GUI/windemo(`radio/bton/btoff`) 可用——测试
  自动化拿到"蓝牙软复位"能力（对应 ESP32 时代车机侧开关蓝牙的复位手段）。
- **电话功能(门C)当前卡点 = HFP 服务未连接**：PnP 取证（tools/bt_profiles.ps1）：Windows 给
  CARKIT-1 装了 A2DP SNK(110A)/AUDIO(110B)/AVRCP(110C/110E)/**Handsfree(111E, 名字显示
  "Hands-Free AG")**/CarPlay/AndroidAuto 服务节点，但全部 Status=Unknown（未连接）。
  `CallControl.GetDefault()` 只有"通话音频"(HFP) 真连上才非 null。下一步：Windows 设置→
  蓝牙→CARKIT-1 打开"通话音频"（或车机发起），再点 GUI"模拟来电"；若车机因电脑 COD 不建
  HFP → 门C 定案为车机策略天花板。


### WinRT 路线第二轮实测（2026-09-01 中午：门B 通过、门C 定案、无头认证）
- **门B ✅（台架确认）**：静音流+元数据 → 车机能播放、能推送元数据。三项尾巴与根因：
  - **进度恒 0**：代码从未调 `UpdateTimelineProperties`——车机进度条/时长全来自 SMTC 时间轴。
    已加 `SystemMediaTransportControlsTimelineProperties` + 1s 心跳（Playing 推进、Paused 冻结、
    切曲归零、循环回 0），`SetTrack` 增加时长参数（GUI"时长s"输入，默认 240）。
  - **暂停后无法继续播放**：假设=车机暂停挂起 AVDTP 流 → "远程音频"端点失活 → AudioGraph 停摆
    无人重启。已加失速自检：回到 Playing 时采样 `AudioGraph.CompletedQuantumCount`（间隔 1.2s
    不前进=停摆）自动整链重建静音流。待台架复测。
  - **歌词无法测试**：歌词=云端按歌名匹配，进度恒 0 时可能不出逐行歌词；修完进度条用真歌名复测。
- **配对/连接码弹窗 → 无头化（两层）**：台架反馈"配对不用去设置，但弹连接码要手点"。
  ① `Pair` 改 WinRT `DeviceInformationCustomPairing.PairAsync(kinds)` + `PairingRequested` 里
  `Accept()`/`Accept(pin)`（官方无界面配对通道，PIN 默认 1234）；② 引擎 Init 注册
  **`BluetoothWin32Authentication` 全局认证代答**（32feet 真实 API 面已反射核实：ctor 带事件
  处理器，参数可写 `Pin`/`Confirm`，AuthenticationMethod 枚举 =
  Legacy/OutOfBand/NumericComparison/PasskeyNotification/Passkey）——系统级认证回调（含
  `SetServiceState` 触发的重认证弹窗）全部代码代答，不再弹框。注意：引擎存活期间对本机所有
  蓝牙认证事件生效（测试机可接受）。11:10 实测：注册后 `SetServiceState` 激活 111E 未再弹窗
  （链路密钥已满足，连认证回调都没触发）。
- **门C ✗（Windows 原生定案，证据链三轮）**：
  1. 11:05 裸 `StreamSocket` 连车机 111E **成功**（已配对态不弹窗）→ CallControl=null；
  2. 11:07 `SetServiceState(Handsfree,enable)`（=Windows 设置"连接"按钮底层通道）提交 +
     裸 socket → 弹连接码（用户手点）→ CallControl 仍 null；
  3. 11:10 无头代答就位 + SetServiceState + socket **保活 50s**，期间 PnP 里 111E 服务节点
     Status=OK，最后 call 仍 null。
  - **拓扑解读**：车机 111E 服务名 = "CARKIT-1 Hands-Free **AG**"（AG=网关=RFCOMM 服务端）。
    连它 = Windows 当 HF(免提端)；Windows 栈没有"HF 客户端接远端 AG"的电话设备模型 →
    CallControl 永远 null。真手机通路 = 手机当 AG(服务端)、车机当 HF **主动来连**（用户今日
    确认真手机能打电话）；Windows COD 默认电脑类（后发现有注册表覆盖开关，见第三轮）→
    车机不会把电脑当手机主动发起 HFP——ESP32 时代"只接受自发 HFP"是同一行为。
    **CallControl 路线判死**；残路只剩手写 AT 指令
    over 裸 RFCOMM（= 把 ESP32 方案移植 Windows，但那是 HF 连车机 AG，要求车机自身有通话
    能力，CARKIT-1 无独立蜂窝，可行性极低，不做）。
- **蓝牙名（本机广播名）不可改 verdict**：无公开 API——广播名=计算机名；`BluetoothGetRadioInfo`
  只读；32feet `BluetoothRadio.Name` 只读；BTHPORT 注册表只存**对端**设备名（官方驱动注册表
  文档无本地名键）。改法=改计算机名(重启生效)或个别适配器驱动在设备管理器 Advanced 提供
  Name 字段（来源：superuser q/1183903、MS Learn Q&A、bluetoothapis.h 文档）。
- **GUI 新增**：时长s 输入（元数据区）、🔗连通话音频(HFP实验) 按钮（选中项或默认 CARKIT-1）、
  配对走新无头通道；windemo 新增 `hfp <目标>`、`pair` 带 PIN、`track` 带时长。


### WinRT 路线第三轮（2026-09-01 下午：配对双侧不同步修复 + COD 注册表开关）

- **"配对没成功也没弹窗"根因 = 双侧配对记录不同步**：11:16/11:17 台架日志
  `PairAsync -> AlreadyPaired`——Windows 侧还留着链路密钥（此前 Windows 设置配对过），
  车机侧已删；`PairAsync` 发现本机有记录直接短路返回，配对流程根本没跑（自然零弹窗、
  车机也没配上）。修复：`Pair(target, pin, force)` force 时先 `BluetoothSecurity.RemoveDevice`
  清 Windows 残留记录 → 1.5s → 全新无头配对。GUI 新按钮 **🔁重新配对(清残留)**；
  windemo `repair <序号|MAC> [PIN]`。注意**反向不同步**（车机有记录、Windows 没有）时
  配对会被车机拒——先在车机屏幕删掉本机记录。
- **COD（设备类）并非锁死——官方注册表开关（修正第二轮结论）**：MS Learn
  [Bluetooth Registry Entries](https://learn.microsoft.com/en-us/windows-hardware/drivers/bluetooth/bluetooth-registry-entries)
  ——`HKLM\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters` 下 DWORD 值 **COD Major** /
  **COD Type**（取值按 SIG Assigned Numbers；未设/非法时类安装器默认 电脑/台式机）。
  限制：只覆盖 `COD_MAJOR_XXX`/`COD_XXX_MINOR_XXX` 位，**服务类位不受控**（栈按已启用
  服务自动给）；需管理员 + 重启蓝牙生效；**对端缓存配对时的 COD，必须重新配对**车机才会
  看到"手机"。已写 `tools/set_cod_phone.ps1`（手机/智能机=2/3，`-Revert` 还原，自动重启射频）。
  期望管理：这只换"外衣"——Windows 依旧没有 HFP AG 服务端/电话设备模型，CallControl 不会
  因此复活；实验价值 = 车机是否把本机当手机（图标变化、是否主动发起 HFP、Windows 设置里
  "通话音频"行为变化）。若仍不主动连 → 证实缺口在服务面而非 COD，BlueZ PoC 证据+1。
  对照：BlueZ 可全量控——`/etc/bluetooth/main.conf` `Class = 0x5A020C`（连服务类位一起改，
  [askubuntu q/439088](https://askubuntu.com/questions/439088/how-to-change-bluetooth-device-class)；
  hciconfig 已弃用）。


## 5. 待办
- [x] 修 esp_wifi_init NO_MEM（根因：audio_pipeline_init 残留急切分配）
- [x] 蓝牙栈真机定案：bt_up=true，verify_bt_boot 全绿（2026-09-01，见上节）
- [x] 手机实际搜到 VPHONE-01（2026-09-01，但排位垫底→见"信号弱三嫌疑"节）
- [x] 烧录 v0.2.1 + 排位上升确认（2026-09-04）
- [x] GUI 卡顿修复 + 高延迟防御（2026-09-04，见上两节；42 测试全过）
- [x] USB"高延迟"排查闭环（2026-09-04：真根因 = ch341ser 大块 ReadFile 攒满才完成 × pyserial
      read(4096)，改 in_waiting 读法后命令 47~63ms；板子可回 hub，见"真根因"节）
- [x] 车机配对 + profile 连接 + A2DP 推流真机全通（2026-09-01，v0.3.0~v0.3.2，见上节）
- [x] 流媒体 panic 根因闭环 + transport 恢复三重修复（2026-09-01，见上节；44 测试全过）
- [ ] GUI 交用户做车机侧验证（元数据显示/切歌联动/模拟来电）——v0.3.8 已重启 GUI；若车机
      拒连（退避中），先车机关开蓝牙再验
- [ ] HFP 通话真机验证（只等车机发起 SLC——AG 主动连已被证伪两轮；WBS 已关；环已缩堆已留）
- [ ] 改名链路验证：bt.set_name → 固件回报 scan_mode 重应用 → 手机开关蓝牙重搜索（需用户手机）
- [ ] 小环(20K)真机播放验证 + underrun 观察今晚被车机退避挡住，未跑成
- [ ] 观察点：每个连接周期净吃堆 ~10-24K 只还 ~10K（疑似泄漏，今晚车机状态不稳无法定量；
      sys.info 新增 rx_frames/rx_crc_err/rx_hdr_err 可持续监控）；panic 复发时 2s 延迟可抓 backtrace
- [x] btwinrt 静音流车机侧复测（2026-09-01 中午：能播放+元数据上屏，门B ✅；尾巴=进度/暂停/
      歌词，修复后待复测：进度条推进、暂停→播放恢复、真歌名歌词）
- [x] 配对后 call 复测（动态 GetDefault）——门C ✗ 定案（2026-09-01 中午，三轮证据链+拓扑解读
      见上节；Windows 原生 CallControl 判死）
- [ ] 新配对流程复测：解除配对 → GUI **🔁重新配对(清残留)**（11:16 教训：PairAsync 会拿
      AlreadyPaired 短路，双侧记录不同步必须先清；若车机侧也挂着旧记录先在车机删），验证
      全程零弹窗
- [ ] COD 手机类实验（可选，验证车机设备类型判断）：管理员跑 `tools/set_cod_phone.ps1`
      → 重启蓝牙 → 重新配对 → 看车机图标/是否主动连通话音频（不影响 CallControl 结论，
      为 BlueZ PoC 攒证据）
- [ ] 门B 尾巴复测（新 DLL 已发布，GUI 已重启）：进度条每秒推进/暂停冻结/恢复播放/切歌归零/
      真歌名歌词 ——**已被 vphone 路线取代**（门B' 在真手机 MediaSession 上天然正确，无
      SMTC 时间轴坑），WinRT 修复仍暂缓
- [x] 阶段4 终章：三门结论（A✅ B✅ C✗）已定案；WinRT 三根因存档 + vphone 路线全记录
      见下节（2026-09-07）；分支决策 = vphone 主力 / BlueZ 备选 / WinRT 工程原样保留
- [ ] WiFi 真机冒烟重跑（手机确认 VPHONE-AP 可见）
- [x] vphone 阶段1 构建 + 阶段1.5 装机自测 + 阶段3 Python 控制端（2026-09-07 全过，见下节）
- [ ] vphone 阶段2 台架实测：手机 SN_PHONE_A ↔ 车机配对 → 门B'（元数据/进度/连播/
      按键回流）+ 门C'（来电上车机 UI、车机接听/挂断回流）→ 失败则 BlueZ 备选升级


### vphone 通用 APK 路线（2026-09-07：真机自动化主力，阶段1/1.5/3 全部完成）

> 决策链：WinRT 修复暂缓 → BlueZ 降备选 → **闲置 Android 手机 + 通用 APK = vphone**。
> 思路反转：不再让 PC 模拟蓝牙手机，而是把一部真手机（系统栈自带真 HFP AG + 真 AVRCP）
> 变成 PC 可远程驱动的"虚拟手机"。装到任意 Android 5.0+ 手机即成测试终端，无需 root/SIM。

**工程**：`vphone/`（Kotlin，minSdk 21 / targetSdk 34，零第三方依赖）。四模块：
- `CallEngine` + `VPhoneConnectionService`（门C）：Telecom managed ConnectionService，
  PhoneAccount(CAPABILITY_CALL_PROVIDER)；来电注入 `addNewIncomingCall` → setRinging，
  去电 `placeCall` → setDialing，answer/hold/setAudioRoute(ROUTE_BLUETOOTH) 全 App 内模拟；
  车机接听/拒接/挂断/保持/DTMF 按键回流 VConnection.onAnswer/onReject/onDisconnect/onHold/onPlayDtmfTone。
- `MediaEngine`（门B）：framework MediaSession（非 AndroidX），任意元数据+时长+进度 1s 心跳
  +自动连播；车机播放/暂停/切歌/拖进度回流 Callback.onPlay 等；可选静音 AudioTrack 保活 A2DP。
- `ControlServer`：纯手写 HTTP 服务（ServerSocket :8800）+ `Dispatcher` 路由
  （/call/* /media/* /status /help）；`ControlReceiver`：adb 广播兜底通道（com.bt.vphone.CMD）。
- `VPhoneService` 前台服务拉起全套；`MainActivity` 一次性设置界面（启用账号/自检）。

**构建链**（新装机可复现）：AGP 8.5.2 + Gradle 8.7 + JDK 21（`C:\Program Files\Android\openjdk\jdk-21.0.8`）；
SDK `%LOCALAPPDATA%\Android\Sdk`（platforms;android-34 + build-tools;34.0.0）；gradle 在
`%LOCALAPPDATA%\Android\gradle-8.7`。产物 `vphone/app/build/outputs/apk/debug/app-debug.apk`（828KB）。

**阶段1.5 自测（2026-09-07，HUAWEI TEL-AN00a / Android 10，USB 调试，全过）**：
- 权限：`pm grant` CALL_PHONE/READ_PHONE_STATE/ANSWER_PHONE_CALLS 三连，免手动弹窗。
- **电话账号启用可全自动**：`adb shell telecom set-phone-account-enabled com.bt.vphone/.VPhoneConnectionService VPHONE 0`
  → Success（dumpsys 确认 CallProvider/tel）。原方案"手动去电话设置点启用"整步省掉；
  厂商差异待其他手机验证（华为 EMUI Android 10 可用）。
- 门C 自测：HTTP 注入来电 → `onCreateIncomingConnection 号码=13800138000` → call=ringing
  （手机本机弹系统来电界面）→ hangup；广播 dial 10086 → dialing → answer(setActive) →
  audio-bt(SCO 路由请求) → hangup。logcat TAG=VPhone 事件流完整。
- 门B 自测：playlist 4 首（demo_pl.txt）→ play → 3s 后 pos=3s（心跳推进）→ next 切「晴天」。
  静音流 on/off 正常。UTF-8 载入中文歌名无乱码（PC 终端乱码只是控制台代码页显示问题）。
- Python 控制端 `vphone/vphone_ctl.py`：start/status/enable-account/incoming/hangup/playlist/
  play/next/silence 全命令冒烟通过；多设备守卫（--serial / VPHONE_SERIAL）生效。

**本轮新增四坑（都已修，复装记得）**：
1. **adb reverse 方向搞反**：HTTP 服务器在手机上、PC 是客户端，应该用 **`adb forward tcp:18800 tcp:8800`**
   （reverse 是"设备连回来连 PC 服务器"的场景）。且本机 8800 常被占、双 adb 服务端打架会
   出现 "cannot bind listener"——统一用本地高端口 18800 + 重启 adb server 解决。
2. **Android 8+ 隐式广播禁令**：manifest 静态 receiver 收不到 `am broadcast -a com.bt.vphone.CMD`
   这种隐式广播（发出无报错、receiver 静默不触发）。**必须显式**：`-p com.bt.vphone`
   （或 `-n 包/.ControlReceiver`）。vphone_ctl.py 的广播兜底已带 -p。
3. XML 注释里不能写 `--es`（双连字符非法）——manifest 注释写参数示例时用"extras: cmd=xxx"。
4. Kotlin/Telecom API 细节：framework `PlaybackState` 在 `android.media.session` 包（不是
   `android.media`）；Connection 无出站 `playDtmfTone`，车机侧 DTMF 回调名是
   `onPlayDtmfTone(Char)`/`onStopDtmfTone()`；字符串模板内函数调用必须 `${fn()}`。

**待办 → 阶段2 台架实测（手机 SN_PHONE_A ↔ 车机 0x123456789EF，条件已就绪）**：
手机/车机蓝牙均 ON、手机无残留配对（很干净）。步骤：手机删旧配对（若有）→ 与车机配对
（手动一次或 scrcpy）→ `python vphone/vphone_ctl.py play` 看车机屏门B'（元数据/总时长/进度/
自动连播/车机按键回流 events）→ `incoming` 看车机弹来电 UI+号码、车机接听/挂断回流
（`events` 或 `logcat -s VPhone`）→ 加分项 audio-bt 看 SCO。车机侧可 `adb -s 0x123456789EF
logcat | grep -iE "hfp|avrcp|bluetooth"` 抓 profile 层证据。
失败路径：门C' 不传导 HFP → 如实记录 → BlueZ 备选升级。


### vphone 台架第一轮调试（2026-09-07 下午）：三问题闭环 + 车机电话三流定案

用户 GUI 实测报三问题：①配对仍有弹窗 ②断开后无法重连 ③"播放失效但元数据成功"。
手机 dumpsys 取证（比看现象准）还原了完整时间线：

- **14:45:11-12 配对成功 → A2DP/HFP 全自动连上**（CONNECT→CONNECTED 各 ~1s，stack 自动）；
- **14:47:56-14:48:02 车机发了真实 AT 指令**（EVENT_TYPE_HANGUP_CALL=CHUP ×3、AT+CLCC 轮询；
  Telecom 自动回 CLCC）→ 电话链路+回调全通；
- **14:48:11.753/760 HFP 与 A2DP 同毫秒 STACK_EVENT→DISCONNECTED** = **车机侧掐掉整条 ACL**
  （车机蓝牙重启/车机端主动断开）。此后车机上一切无反应 —— 问题③"播放失效"其实是问题②
  的连带：元数据"推送成功"只是手机侧返回 200，车机根本没收到。media_session dump 证明
  VPhoneMedia active=true 且就是系统 Media button session、state=2/pos=71s —— App 侧无辜。

**修复① 配对零弹窗（AutoPairService 无障碍自动点）**：PIN 模式 setPin 反射已静默；
consent/数字比较模式 setPairingConfirmation 需系统权限（华为 Android 10 实测仍弹）→
新增无障碍服务自动点弹窗肯定键（**精确匹配**"配对/确定/允许/OK"，否定词绝不点）。
一次性启用（保留已有服务）：`vphone_lib.enable_autoconfirm()`（settings put secure
enabled_accessibility_services 追加 + accessibility_enabled=1；Android 13+ 需手动开）。

**修复② 断线重连（实测通过 ✅）**：`/bt/reconnect`（GUI"🔌重连"按钮）三条路：
`device.connect()` 反射被华为挡（不可用）→ **A2DP.connect/HFP.connect profile 代理反射
在 Android 10 可用（BLUETOOTH_ADMIN 即可）** → 兜底蓝牙开关循环。实测 15:07:44 A2DP
CONNECTED、15:07:46 HFP CONNECTED，bt_state 出 `[A2DP已连] [HFP已连]`。另：配对完成
自动 setPriority(100)；A2DP 的 setPriority 签名被华为改了（NoSuchMethod，无碍——
连接时 stack 自己写了 1000）；新增 ACL/HFP/A2DP 状态变化事件上报（掉线瞬间 GUI 可见）。

**修复③ 播放加固**：play 时 `requestAudioFocus(GAIN)`（车机 AVRCP 路由跟随焦点，
dump 确认已持有）+ 每次 play 重抢 `isActive`；静音流默认开；AudioTrack 重写为
`AudioTrack.Builder(USAGE_MEDIA/CONTENT_TYPE_MUSIC)` + ≥2s 缓冲 + 写线程 15s 心跳 +
意外退出 2s 自愈（旧版 STREAM_MUSIC 构造器 + sleep(400) 有断流嫌疑，待车机复测
`dumpsys bluetooth_manager | grep mIsPlaying`）。

**修复④ 车机拨出路由（ATD）**：手机有 SIM telephony 账号（CallProvider）会抢 ATD →
`telecom set-user-selected-outgoing-phone-account com.bt.vphone/.VPhoneConnectionService VPHONE 0`
把 VPHONE 设默认去电账号（enable_account() 已并入此命令）→ 车机拨号 →
onCreateOutgoingConnection → **3s 自动接通**（模拟对端摘机，`/call/auto-outgoing?on=0` 可关，
GUI 有勾选框）。车机电话三流全定案：接听=ATA→onAnswer、拒接→onReject、挂断=CHUP→onDisconnect，
全部有 dumpsys 实证；CLCC 由 Telecom 自动应答，App 无需实现。

**免确认安装调研（放弃 input tap）**：华为 adb install 弹 InstallStaging 确认框
（120s 超时实证）；uiautomator dump 找按钮坐标方案因厂商弹窗差异大被否。`install()` 降级为
adb install -r + 自动 am start + 重 forward + 拉服务（手机弹窗仍需手点一次）。

**jar 化结论（用户问 u2.jar/scrcpy.jar 那样直接推）**：不可行。app_process jar 以 shell
身份跑、无包身份，而 Telecom ConnectionService 绑定/PhoneAccount 注册/MediaSession AVRCP
身份全都要求真实安装的 App 包。门B/门C 必须留在 APK；更新痛点靠 install() 缓解。

**两个追加根因（当晚闭环）**：
1. **车机一连 HFP 就"正在呼叫"且挂不断 = Telecom 死呼叫残留**：用户在设默认账号前从车机
   拨出 → Telecom 不知道派给谁 → 呼叫卡在 `SELECT_PHONE_ACCOUNT`（TC@10）永久悬着；每次
   HFP 连上同步给车机 → 车机显示呼叫中，CHUP 对半初始化呼叫无效，ENDCALL 键也清不掉，
   **只能手机重启清除**（重启后 mCalls 空）。VPHONE 已设默认去电账号后新拨出不会再产生。
2. **EMUI 媒体路由坑（mIsPlaying=false 真凶）**：HFP 一连，AudioFlinger 把媒体主输出切到
   `BLUETOOTH_SCO_CARKIT`（通话通道），A2DP 根本不是输出设备 → 车机收不到流。修 =
   `AudioTrack.setPreferredDevice(TYPE_BLUETOOTH_A2DP)` 把静音流硬绑 A2DP（API 23+，
   play() 每次补绑）。实测：绑定后 `mIsPlaying: true`（真音频流上车机）。
   附带发现：手机重启后车机会**主动回连** A2DP+HFP —— "断开连不回"只发生在手机侧不发起
   时，GUI"🔌重连"按钮/蓝牙开关循环就是兜底路径。


### vphone 第二轮：事件总线/真实音频/联系人/通话音频（台架四需求 + 真机全冒烟通过）

用户四+一需求全部落地（App `f-s轮` 代码: EventLog/ContactsEngine/CallAudioEngine 三新模块）：

1. **车机回流事件接口化（断言用）**：App 内 `EventLog` 环形总线(500条) ——
   `type(英文稳定)/src(car|cmd|app|bt)/detail(中文)`，`GET /events?since=N` 出 JSON。
   电话: CAR_ANSWER/CAR_REJECT/CAR_HANGUP/CAR_DIAL/_CAR_HOLD/CAR_UNHOLD/CAR_DTMF；
   媒体: CAR_PLAY/CAR_PAUSE/CAR_NEXT/CAR_PREV/CAR_STOP/CAR_SEEK；
   链路: BT_ACL_*/BT_A2DP_*/BT_HFP_*/BT_BONDED；状态: RING_IN/CALL_ACTIVE/CALL_ENDED/...。
   Python: `vp.events(since=)` / `vp.wait_event("CAR_HANGUP", timeout=15)`（tuple 可多类型,
   detail 子串过滤, 返回 dict 直接 assert）; CLI `events`(实时流)/`wait-event`; GUI 事件流
   改 /events 轮询(🚗高亮车机回流)。**wait_event 默认只看调用之后的新事件**（水位对齐），
   要翻历史用 `since=0`。`dial()` 与车机 ATD 拨出靠 `dialFromCmd` 标记区分
   （CMD_DIAL vs CAR_DIAL，事件 src 也不同）。
2. **电脑音频 → 手机 → 车机真实播放**：手写 HTTP 支持 POST body（Content-Length 精确读,
   60MB 上限）→ `POST /media/upload?name=xx.mp3` 存 App `files/music/`（乐库）。
   播放列表行格式扩为 `标题|歌手|专辑|秒|文件名`（第5列可选）；有文件走 **MediaPlayer**
   （USAGE_MEDIA+CONTENT_TYPE_MUSIC, API28+ `setPreferredDevice(A2DP)` 同款防 SCO 抢路），
   时长/进度取真实值, 播完 MEDIA_TRACK_END 事件+自动连播; 无文件回退静音流。
   Python: `upload_audio(path)` / `play_audio_files([paths])`(上传+列表+播放一步到位) /
   `list_audio()/del_audio()`; GUI "⇧电脑选曲→上传→车机播放"+"🎵乐库管理"。
   **实测**: 5s 440Hz wav → dur=5s(真实), pos 2s→4s 真实推进, `mIsPlaying: true`
   (A2dpStateMachine Connected, SBC 44.1k stereo) —— 真实音频流上车机 ✅。
3. **联系人自定义/1w 压测**：`ContactsEngine` 独立账号(account_type=com.bt.vphone)写
   ContactsProvider → 车机 PBAP 拉的就是它。`/contacts/load?count=10000&prefix=联系人`
   (后台线程+进度) / `import`(姓名|号码 行) / `clear`(只删本账号) / `count`。
   权限 READ/WRITE_CONTACTS 由 `grant_perms()`(pm grant ×5, install 后自动跑)。
   **实测 1w 个 122s**（EMUI Android 10）。注意: 车机侧一般要重连 HFP 或手动刷新通讯录
   才触发 PBAP 重拉。
4. **通话中自定义音频（模拟对端说话）**：`CallAudioEngine` —— MediaPlayer 用
   USAGE_VOICE_COMMUNICATION + CONTENT_TYPE_SPEECH，系统通话中路由 SCO 下行 → 车机
   扬声器出声；再 `setPreferredDevice(TYPE_BLUETOOTH_SCO)` 双保险。`/call/audio?name=xx
   &loop=1`(stop=1 停)，无通话时返回 ⚠ 提示（SCO 未建立车机听不到）。实测：idle 告警/
   dial→3s 自动接通→循环播放链路全通（车机侧听感待用户复测）。
5. **歌词显示 = 蓝牙通道不存在，定案不做**：AVRCP 1.3~1.6 now-playing 元数据只有
   标题/歌手/专辑/流派/曲号/时长 —— **无歌词字段**；A2DP 传的是压缩音频裸流, 文件里的
   ID3/SYLT 歌词也不随流走; Android MediaMetadata 亦无 LYRICS 键可推。车机上能看到歌词
   的场景全是车机自己联网按歌名拉词(车机音乐App)或 HiCar/CarLife 私有通道。可选实验:
   用真歌名(如「晴天」/周杰伦)试车机 BT 音乐页是否联网拉词; 否则手机侧无任何推送手段。

**本轮三坑（都已修）**：
- **EMUI 来电号码恒为 13800138000**：`addNewIncomingCall` extras 里的
  EXTRA_INCOMING_CALL_ADDRESS 在 EMUI(Android 10) 不进 `ConnectionRequest.address`
  (恒 null, 之前测试恰好都用默认号所以从未暴露) → `pendingIncomingNumber` 兜底传递。
- **联系人批量写报 "Too many content provider operations between yield points
  (max 500/批)"**：250 联系人=750 op 超限 → 每批 ≤150 个(450 op)+批尾
  `withYieldAllowed(true)`。另 `countWhere` 忘传 selectionArgs(`?` 未绑定, EMUI 静默回空
  而非抛错) → 计数恒 0, 已修(vphone=10000 total=11263 实证)。
- **华为安装确认反复"User rejected permissions"**：超时后 InstallStaging 残留框会挡住
  后续安装(立即失败不等待) → `am force-stop com.android.packageinstaller` 清掉再装;
  重启手机后第一次安装也可能因 shell 未就绪超时, 等 30s 重试即可。

**追加：车机拉不到通讯录（PBAP 授权）**：车机提示"需要手机打开同步联系人权限" = 手机侧
未授权该设备 PBAP。三层方案（`/bt/allow-car`，GUI"📇授权车机拉通讯录"）：
① 反射 `setPhonebookAccessPermission(1)/setMessageAccessPermission(1)/setSimAccessPermission(1)`
  （隐藏 API，部分 ROM 直接可写，EMUI 大概率被 BLUETOOTH_PRIVILEGED 挡）；
② AutoPairService 扩词：系统弹窗包名(com.android.bluetooth/settings/systemui)里出现
  "联系人/通讯录/通话记录 + 蓝牙/访问"特征 → 自动点「允许」（重配对/重连触发授权框时无人值守）；
③ 人工一次：设置→蓝牙→设备→「共享联系人/访问通讯录」开关。
授权后车机要**重新触发 PBAP**（车机通讯录刷新/蓝牙重连）才会拉到新数据。

**追加：vphone 第三轮（暂停不掉/杂音滋滋/歌词定论）**

- **"暂停了但车机没停"根因**：假元数据模式下出声靠静音流(A2DP 保活)，旧版 `doPause()`
  只暂停 MediaPlayer 与进度心跳，**静音 AudioTrack 没停** → A2DP 流持续、车机侧一直
  "播放中"（事件流实证：用户连按 4 次 CAR_PAUSE #12-15，手机状态确实 paused，但流没断）。
  修复：doPause 同步 `audioTrack?.pause()`；恢复路径 `resumeOrStartSilence()`（旧版
  startSilence 见 track 已存在直接 return，暂停过的流永远恢复不了，一并修）。
- **事件标签更正**：MediaSession.Callback 无法区分按键来源（车机 AVRCP 与手机通知栏/
  耳机线控走同一回调），detail 从"车机按键【X】"改为"媒体按键【X】(车机或手机通知栏)"，
  断言仍用 type（CAR_PAUSE 等不变）。
- **真实音频"滋滋"杂音取证**：logcat `AudioTrackShared: tallyUnderrunFrames(880)...
  bump mUnderrunCount=8`（1.2s 音频 8 次欠载）= 爆音来源，密了听感即"断断续续"。
  A2DP 本身协商正常（SBC 44.1k/16bit/立体声，mIsPlaying=true）。同时抓到
  `com.android.server.telecom` 反复抢瞬时音频焦点 + A2dpStateMachine STARTED→STOPPED
  循环（通话活动期间属正常：SCO 起 A2DP 停是车机蓝牙标准行为）。欠载爆音两类根因：
  ①台架射频干扰（手机贴着 hub/主机线缆，2.4GHz 重传）②管线缓冲。对照实验定位：
  a) 手机自带音乐 App 连车机放同一首 b) 播放中把手机拿离电脑 1m/拔 USB。若自带播放器
  也滋滋 → 环境问题（换位置/加长 USB 延长线）；若只有 vphone 滋滋 → 备选方案改
  MediaCodec+AudioTrack 自管大缓冲（4-8s）替换 MediaPlayer。
- **防御性修复（无论根因都保留）**：`forceMediaRoute()` 播真实音频前释放挂起的 SCO
  （通话中除外，那时 SCO 属于通话）；`pinPlayerA2dp` 绑定失败后台重试 10×500ms（A2DP
  设备可能晚于播放就绪）；`/media/diag` 一条命令看实际输出设备/SCO 占用/绑定状态；
  VPhoneService 持 PARTIAL_WAKE_LOCK + `adb shell dumpsys deviceidle whitelist
  +com.bt.vphone` 进 doze 白名单（EMUI 后台限流解码线程 → 欠载，白名单后 60s 欠载
  3→1 次/分钟，有改善未绝迹，欠载呈突发成串 1.2s×8 次的形态）。
- **卸载重装会清空 vphone 联系人**：联系人挂在 vphone 账号(account_type=com.bt.vphone)
  下，重装 APK 触发系统清理（实测 vphone=0 total=回原生量）→ 每次重装后需重新
  `contacts-load`。

**追加：vphone 第四轮（播放列表管理/播放器状态机/通话音频下拉/蓝牙改名）**

- **"放完不播下一曲"根因**：onCompletion 回调里直接 doAdvance → startReal → stopReal →
  **在自己 onCompletion 里 release 自己**（回调由主线程派发，release 嵌套在回调内
  死锁/抛错被吞）→ 修法：`main.post {}` 把推进动作挪到回调返回之后。另单曲结束补
  `stopTicker()`（旧版心跳不停 → 时间乱跳）。
- **切歌瞬间总时长 00:00**：applyIndex 旧版 `durationSec = t.dur`，第4列给 0(未知)时
  直接清零 → 车机显示 0 时长 → 修法：dur>0 才覆盖，否则沿用上次值（真实音频播起后
  getDuration 再校准）。
- **暂停态切歌残留旧播放器**：doAdvance 旧版只在 playing 时处理音频，暂停切歌挂着旧
  MediaPlayer → 恢复播放才切 → 现暂停切歌即 stopReal 释放，恢复从新歌 0s 起。
- **播放列表可回读**：`/media/playlist` 不带 text = 查询当前列表（同"标题|歌手|专辑|秒|
  文件"格式回读）→ 控制端可编辑后整表回写；`/media/jump?idx=N` 跳到第 N 首按当前
  播放/暂停态就位。GUI"乐库管理"升级为**播放列表管理**：上移/下移/移除/双击跳播/
  乐库加入/上传并入列表，整表回写后自动跳回原曲目不跳变；demo 播放列表按钮已删。
- **通话音频改下拉选择**（乐库文件 Combobox，去掉手填名 + "乐库第一个"按钮）；
  **蓝牙改名** `/bt/name?name=X`（BluetoothAdapter.setName，EMUI 回读校验，车机重连
  后显示新名），GUI 蓝牙区有输入框。GUI 媒体区新增 1s 刷新的"当前曲目/时间"行
  （车机切歌后电脑端立即跟上）。
- **歌词定论（重申）**：标准蓝牙通道不存在歌词传输——AVRCP 1.3-1.6 正在播放元数据只有
  标题/歌手/专辑/时长/曲目号，无歌词字段；A2DP 只传压缩音频码流；Android MediaSession
  也无 LYRICS key 可推。车机显示歌词只有两条真路：车机自己联网按歌名匹配歌词（需车机
  在线+曲名精确），或 HiCar/CarLife 私有通道。用真歌名（青花瓷/周杰伦）实测本车机不拉
  网络歌词 → 该车机 BT 音源无歌词功能，此路不通，已定案。

**追加：vphone 第五轮（车机拖进度条不生效）**

- **表象**：车机上拖播放进度条，手机端无反应（进度不跳、无事件）。
- **代码面复盘**：`onSeekTo` 早已实现且 `PlaybackState` 里 `ACTION_SEEK_TO` 一直在——
  但**AVRCP 规范没有"绝对定位"直传命令**，车机拖进度条实际发的是 FAST_FORWARD/REWIND
  透传键（连发或按住），落到 MediaSession 的 `onFastForward()/onRewind()`——此前未重写
  = 默认空操作 → "拖了没反应"。
- **修法（MediaEngine.doSeek 统一核心）**：`onSeekTo`(绝对)/`onFastForward`(+10s)/
  `onRewind`(-10s)/`/media/seek?pos=N` 四路入口全部收敛到一个 doSeek：夹取 [0,时长]；
  真实模式同步 `MediaPlayer.seekTo`；静音流模式只改元数据位置；统一落 CAR_SEEK 事件
  （detail 区分 拖进度/快进/快退/指令）。另修一个隐患：MediaPlayer.seekTo 异步生效，
  1s 心跳若在 ~1.5s 窗口内读到旧位置会把进度"弹回"一拍 → 窗口内心跳优先用命令值
  （实测 seek 120s 后 2.6s 稳定在 121s，无弹回）。
- **真机验证（PC 侧全通）**：真实音频 seek 120s ✓ / 静音流 seek 100s✓(心跳续走 102) /
  越界 9999 夹到时长 ✓ / CAR_SEEK 落账 ✓。新增 `/media/seek` + lib `media_seek(sec)` +
  CLI `seek <秒>` + GUI 媒体区**进度条**（释放即跳转，平时 1s 跟随刷新，拖动中不回写）。
- **EMUI 又一坑（重要）**：`adb shell media dispatch` 实测——`play-pause`/`next` 都能
  到达 VPhoneMedia 会话（CAR_PAUSE/CAR_NEXT 落账），但 **`rewind` 播放态/暂停态都不产
  生任何事件**（FF 键名 `fast-forword` 在 usage 里印着但解析器根本不认，EMUI 自己的
  bug）。即 EMUI 的媒体键注入路径对第三方会话**丢弃 FF/RW 键**。若蓝牙栈的 AVRCP FF/RW
  也走按键注入路径，则车机快进/快退在华为机上到不了第三方 App——与歌词同类的平台级
  限制嫌疑。**待车机实测裁决**：拖动/长按快进，看事件流——出 CAR_SEEK=通；啥都没有=
  EMUI 平台丢弃，如实记录。
- 重装后例行恢复已做：1 万联系人重载(138s) + 乐库 7 首 mp3 重建播放列表（暂停态待命）。

**追加：vphone 第五轮补：断开连接功能（保持配对，车机断连/回连测试场景）**

- **实现** `/bt/disconnect?mac=X&force=0`（mac 空=自动选当前已连那台）+ GUI 蓝牙区
  「⏹断开(保配对)」按钮 + CLI `disconnect [--mac] [--force]` + lib `bt_disconnect()`。
  递降三层：①反射 `BluetoothDevice.disconnect()`；②A2DP/HFP 代理反射
  `disconnect(device)`；③`setPriority(0)` 防车机秒回连；全拒且 force=1 → 兜底关蓝牙。
  断开前自动暂停媒体（否则 A2DP 一断 MediaPlayer 改走手机扬声器外放）。
- **EMUI 实测结论（华为 TEL-AN00a Android 10）**：②**可用** —— `A2dpService.disconnect` /
  `HeadsetService.disconnect` 的代理反射返回 true（与 PBAP setPhonebookAccessPermission
  被拦不同，这条 EMUI 放行了）；①设备级 disconnect 隐藏方法本机不存在(NoSuchMethod)；
  ③setPriority 被 BLUETOOTH_PRIVILEGED 拦（与 PBAP 同类）。**且 ③被拦也不碍事：断开后
  观察 30s 车机未自动回连**（车机回连退避长，断开态足够稳定撑完一轮测试）。
- **真机全闭环验证**：断开 → CMD_PAUSE 自动停播 + BT_DISCONNECT/HFP断/A2DP断/ACL断
  事件齐 + /bt/state 的 [已连] 标记消失（配对保留）→ 30s 无回连 → `/bt/reconnect`
  （先补 setPrioBestEffort 恢复 priority）→ A2DP.connect/HFP.connect 反射均 true，
  4s 内双 profile 回连 + 事件链完整。断连/回连两方向都成了可断言的结构化事件。
- 重连代码补了一刀：reconnect 起手先 `setPrioBestEffort(d)` 恢复 priority=100
  （防 disconnect 关过 priority 后系统拒绝回连）。

**追加：vphone 第五轮补2：APK 打包进仓库 + GUI 安装/启动（换机器部署一步化）**

- **需求**：换台机器 clone 仓库后不装 Android 构建环境，GUI 里点一下就完成部署。
- **实现**：`vphone/apk/vphone.apk`（872KB，随 git 分发）+ lib `default_apk()`（优先
  apk/ 目录，回退构建产物）。GUI 顶部新增「📦安装APK」（install→拉服务→自动重连→
  联系人为 0 时弹窗询问重灌 1 万）和「🚀启动App」（am start + 服务应答校验 —— 装完
  App 处于 stopped 态广播唤不醒，必须 am start 一次）。install() 顺手前置 force-stop
  华为残留安装器；lib 新增 `launch()`；apk/README.md 写明重新构建后要同步覆盖 vphone.apk。
- 换机器前提：那台机器有 adb + 手机 USB 调试开着（`adb devices` 能看到），其余全 GUI。

**追加：vphone 第五轮补3：GUI 打包成单文件 exe（换机器免装 Python/adb）**

- `python vphone/build_exe.py` → `vphone/dist/vphone_gui.exe`（约13MB，onefile+windowed，
  内嵌 `apk/vphone.apk` + 本机 adb 及 AdbWinApi/AdbWinUsbApi/libwinpthread DLL）。
  拷到任意 Windows 机器双击即用，`--serial` 参数照传；唯一外部前提 = 手机 USB 驱动。
- lib 冻结态支持：`_bundle_dirs()`（PyInstaller `sys.frozen` → `_MEIPASS` + exe 所在
  目录），`_find_adb`/`default_apk` 优先从这两处取内嵌 adb/apk（先于 PATH，版本可控）。
- **PyInstaller 坑**：`--specpath build` 会让 **相对** add-data 路径解析到 spec 目录下
  → add-data 必须给绝对路径（build_exe.py 内部即如此，手工跑命令时踩过）。
- 验证：冷启动（先 kill adb server）→ exe 自动连上手机建 forward → `/status` 应答 OK；
  另用探针 exe 确认冻结态 `_MEIPASS/adb.exe`、`_MEIPASS/apk/vphone.apk` 解析正确
  （「📦安装APK」在 exe 里可用）。onefile 解包+Defender 扫描首发约 5-10s，属正常。
- **重装清联系人行为不定（本轮新发现）**：上一轮重装实测清空 vphone 账号联系人，
  本轮重装实测**没清**（旧 1 万还在，又叠一轮 → 1.33 万→清理重灌）。结论：装完
  **先 `/contacts/count` 再决定是否重载**；重复重载会叠加重复联系人（同名两套），
  要精确数量就 clear→load 一次到位。注意 **clear/load 都是异步批任务**：clear 发出后
  立刻 load 会被拒（“批量任务进行中”），脚本要轮询 `/contacts/status` 到 idle 再发下一条
  （本轮实测踩过：clear+3s 后 load 被拒 → vphone=0，补一轮 load 才到 1 万）。



1. **总时长始终 0**：`DisplayUpdater.Update()` 触发的 TRACK_CHANGED 里，车机采样的是**旧
   timeline**。修法 = **PushTimeline 要在 Update() 之前 AND 之后各推一次**（后推那次才会被
   采到）。涉及 `btwinrt/Engine.cs` 的 SetTrack/PushTrack/PushTimeline 时序。
2. **HFP 10048（Only one usage of each socket address）**：OS 级 HFP 连接占着 111E 通道，
   应用层 socket 绑不上（同一 (协议,地址,端口) 只许一个）。修法 = 先
   `SetServiceState(false)` 让系统栈放手，再重试连接。（SO#14534722 同类。）
3. **CallControl.GetDefault()=null**：结构性缺口，Windows 无 HFP AG 服务端/电话设备模型，
   **不可模拟**（COD 注册表只换外衣救不了它）——门C ✗ 已定案，正是转 vphone 路线的主因。

---

## 六、第六轮（稳固轮）—— 真机保真度为准绳的状态机理顺 + 全量注释 + 文档 — 2026-09-08

**准绳**：协议层本来就是 EMUI 真栈，模拟的是内容源（播放器/拨号器 App）→ "接近真机"
= 应用层状态机对齐成熟播放器/拨号器语义。产出 TECH 文档里的「真机行为基准表」（T1-T9
PC 回归全过）和「与真机已知偏差清单」（无 SIM 信号电量 / EMUI 丢 FF/RW 键 / 暂停切歌
待车机裁决 / 静音流模式 / PBAP 手动授权），测试有效性边界从此有据可查。

### Kotlin（跨线程 + 状态机）
- **Dispatcher.onMain{} 主线程同步桥**（post+CountDownLatch 8s 超时）：HTTP worker 直改
  引擎状态是双 ticker（进度双倍速）/双 MediaPlayer（两路同播）/seek-release 竞争的根因。
  BtEngine 不上桥（bond 阻塞数秒会 ANR），其 pause 副作用 post 回主线程。
- **applyTrack() 统一换曲入口**：doAdvance/jump/loadPlaylist/setTrack 共用，重置元数据/进度
  **并清 seekAt/seekPosMs**（1.5s 防回弹窗口不跨曲——seek 后切歌进度闪回旧值的根因）。
- **handleTrackEnd() 统一播完入口**：onCompletion 与心跳兜底 2s 去重（lastEndAt）；
  停尾语义（playing=false/进度钉尾/停心跳/停流）两模式一致——真机基准：播完即停不循环。
- 暂停跳曲释放旧 MediaPlayer；actions 掩码补 FF/RW 宣告；暂停态切歌后 0.6s/1.6s 补推两次
  元数据（对付暂停态不刷新的车机栈）；空列表切歌明确报错；jump/seek 缺参报错不静默跳 0。
- CallEngine：onAbort(系统拆线)归零、注入来电 1.5s 未生效回滚 idle、挂断清
  number/pending/CallAudio。ContactsEngine AtomicBoolean CAS 防并发双写。
- BtEngine 断开未成功如实记 BT_DISCONNECT_FAILED（断言不再误判）；ControlReceiver extras
  全透传（广播通道此前拿不到 mac/pos/count）。

### 重大事故：静音流供流线程忙循环（本轮最有价值发现）
- 现象：VPhone 标签 logcat 16 万行/s 刷「静音流心跳」，进程 100% CPU（烧 30+ 分钟），
  并发第二条 logcat 流 read: unexpected EOF!，广播兜底取结果（-t 30）全被垃圾淹没。
- 根因链：doPause 只 audioTrack.pause() 留着供流线程（阻塞在 write 等排空）→ A2DP 路由
  消失后系统瞬间丢缓冲，write() 退化成"立即返回+丢弃"忙循环（佐证：playState=2 PAUSED
  孤儿轨、posSec 每行 +15）→ 每 15 次写一行心跳刷爆。
- 修复（三重独立退出，不依赖跨线程字段可见性）：暂停/播完彻底 stopSilence()（真机基准：
  暂停的播放器没有活跃流）；供流线程连续 5 次 write<200ms 判路由失效即退；setSilence(true)
  暂停态不起流。验证：播放 CPU 0.0% 心跳 1 条/15s；暂停 silence=off 无刷屏；双并发流 45/45
  行共存；Python 重连场景新旧流交接正常。

### Python
- lib：start_events 不再清 on_event（GUI logcat 通道死代码）；_adb 默认 30s 超时（构造函数
  永挂根因）+ devices/forward 10s；http() raise from 保留原始异常 + timeout 可配；
  broadcast() 查 returncode；bt_scan 超时告警；MAC 改正则抠取（行尾 [HFP已连] 标记会让按
  空格取末段取错）；upload_audio 超时按体积放宽（固定 120s 会掐断大文件）；_setup_forward
  幂等（--list 已有同口跳过，重复 rebind 会让 adb server 短暂拒接新连接）；wait_event 参数
  type→evt_type + since 水位语义写明；全部公开方法补 docstring + cmd() 双通道契约矩阵。
- GUI：连接代际 _gen（重复「连接」曾叠加轮询线程→事件双份）；控件取值全改主线程提交前
  完成（tkinter 非线程安全）；播放列表 push() 的 cur_idx HTTP 移出主线程（按钮回调里做
  HTTP 冻结整个 GUI）；轮询连续 3 次失败自停并提示一次；双击扫描项真正绑配对；模块头
  线程模型 + _drain 消息协议表。注：沙箱会话 Text.see() 假死是环境假象，渲染层待人工回归。
- ctl：install 认位置参数与 --file；help 同步；新增 launch/media-diag/playlist-get。

### 文档与发布
- 新增 docs/vphone-TECH.md（架构/线程模型/事故记录/基准表/偏差清单）、vphone-API.md
  （HTTP/Python/CLI/事件/断言）、vphone-DEV.md（任务跟进/挂账/车机侧验收清单/更新规则）。
  根 README 改 vphone 主力。APK 同步 apk/vphone.apk，exe 重建（13MB 内嵌新 APK）。
- 部署铁律（本轮确认）：华为 adb 安装可能每次都弹确认框，卡住不慌、等人到手机旁手动点，
  不反复重试；设备必须挂 hub；重装后内存态（播放列表等）复位需重建，乐库/联系人通常保留。

**追加：vphone 第六轮补 —— GUI exe 下 adb 子进程弹黑窗修复**

windowed exe 无自己的控制台, Windows 会给每个 adb 子进程新弹一个黑窗(连接/扫描/广播/
事件流全是重灾区)。vphone_lib 所有子进程 spawn 点(_adb 的 run + logcat 事件流 Popen)统一加
`CREATE_NO_WINDOW`(非 Windows 平台置 0), 输出仍走管道不受影响。exe 已重建。

**追加：vphone 第六轮补2 —— 车机拨出改默认手动接通(GUI)**

车机 ATD 拨出后 3s 自动接通(模拟对端摘机)改成默认手动: APK 侧机制本就齐(auto-outgoing
关掉停在拨号态, /call/answer 远程摘机), 但 GUI 连接时硬编码推 set_auto_outgoing(True) 且
勾选框默认勾上。改: 勾选框默认不勾(手动), 连接时按勾选框状态同步, 「接听」按钮更名
「接听/接通」(来电 ringing 和拨号态 dialing 通用)。CLI/脚本行为不变(APK 默认仍自动,
要手动自己 set_auto_outgoing(False))。exe 已重建。

**追加：vphone 第六轮补3 —— 库 v0.6.0: 全 kwargs + 结构化断言 + pip 打包**

用户三点诉求(参数 kwargs 风格方便演进兼容/断言数据结构化/打成可安装 python 包)一次落地:

- **全 kwargs**: 全部公开方法签名加 `*`(签名契约测试验证 76 个参数全 KEYWORD_ONLY, 含
  `__init__`), 位置调用直接 TypeError。唯一例外是传输原语 http/http_post/broadcast/cmd
  的主判别参数(同 open(path) 道理)。ctl/gui/库内部 ~50 处调用点全部同步。
- **三层断言 API**(回答"断言是否阻塞/是否超时"): events() 快照非阻塞 / wait_event() 阻塞
  原语超时返 None / expect_event(..., because="") 超时抛 VPhoneTimeoutError(消息带过滤
  条件+because+窗口内最近 8 条事件, 失败现场直接可查) / expect_no_event(within=N) 反向
  断言命中即抛。不支持无限等待: timeout=None→实例默认 VPhone(wait_timeout=15), timeout=0
  =单次快照探一眼。设计动机: 裸 assert vp.wait_event(...) 有 python -O 剥 assert、忘写
  assert 吞 None 两个坑。
- **VEvent 冻结数据类**(id/ts/type/src/detail, 字段只增不改名)取代裸 dict; .time 属性出
  "HH:MM:SS", __str__ 出 "#12 14:03:22 CAR_ANSWER [car] ..."。原 events() 的裸信封
  {'last','events'} 下沉为 events_raw()(GUI/ctl 轮询器用), 新增 event_watermark()。
  VPhoneTimeoutError(VPhoneError) 子类, 既有 except 不受影响。
- **pip wheel**: pyproject.toml(setuptools, 纯 stdlib 零运行时依赖, 版本单一真源
  vphone_lib.__version__=0.6.0), py-modules 导入名不变(vphone_lib, 既有脚本零改动),
  APK 随包走 vphone_data 包(build_py.py 构建时同步, 产物 gitignore, 真源仍是 apk/),
  入口点 vphone/vphone-gui, default_apk() 查找链加 importlib.resources 一环。
  wheel 已在干净 venv 验证: site-packages 导入/版本/APK 落位/两个入口命令。
- 验证: 新增 vphone/test_lib.py 无设备单测 8 组全绿(monkeypatch http 的 fake 严格模拟
  since 水位过滤——初版 fake 不滤 since 导致假失败, 修正后全过); exe 已重建。
- 挂账: 设备冒烟(dial→expect_event/水位回放/GUI 事件流)待华为手机重新插线后补。

**追加：vphone 第六轮补4 —— 仓库大扫除(一期/废弃路线全清)**

用户定夺: 项目只保留 vphone 主力路线。删除清单(先 grep 全量确认 vphone 现役代码/构建
零引用后再动手):
- ESP32 一期: btphone/ firmware/ tests/ examples/ tools/ + 根 pyproject.toml(btphone 包
  配置) + docs/ 五份一期文档(DEPLOY/PROTOCOL/LYRICS_RESEARCH/BRINGUP/HANDOFF)。
- Windows 蓝牙废弃路线: btwinrt/(C# 引擎) windemo/(C# demo) pydemo/(tkinter 前端+DLL)。
- 本地杂物(未入库): 资料/(240MB 板卡手册/驱动/安装包, 物理删除不可找回)、gui_err/out.log、
  .pytest_cache(一期 pytest 的)。
git 跟踪的部分用 git rm, 历史提交里随时可找回; 根目录只剩 vphone/ docs/ README。
README 结构/文档索引与 .gitignore(firmware/windemo/tools 条目)同步收窄; NOTES 全轮次日志
保留未动(历史引用指向已删路径属正常)。教训备查: build_exe.py 的 adb 走 PATH, 与资料/无依赖,
清除安全。

**追加：vphone 第七轮 —— 通话音频进度接口(需求方: 上位机蓝牙调试弹窗)**

需求文档: full-stack-fastapi-template-refactor/docs/vphone-APK需求-通话音频进度接口.md。
APK 侧交付(本轮), 纯新增不改既有端点:

- `GET /call/audio-status` 一行进度(对齐 /media/status 风格): `callAudio=off` 或
  `callAudio=playing name="xx.mp3" pos=12s dur=35s loop=0`。name 取原始文件名(新存
  rawName 字段), 不复用 playingName——循环态它被写成 "xx.mp3(循环)"(装饰名保留给人看);
  在播判据用 playingName 而非 mp 非空: 自然播完回调只置空名字不 release 播放器
  (onCompletion 里 release 自身会死锁, 同 MediaEngine 事故)。播放态与通话态解耦,
  无通话照实报 playing(该不该播由上位机结合事件流判断)。
- `GET /call/audio?seek=N` 拖动: 钳制 [0,dur], 未播放明确报错不误起播; 参数优先级
  stop > seek > name。缺参/坏参报错(对齐 /media/seek 的"危险默认值"纪律)。
- `CALL_AUDIO_END` 事件 APK 本就有(自然播完 src=app, 手动 stop 不发), 本轮补进 API
  文档事件表(需求说的"漏列"是文档漏, 不是代码漏)。
- 版本: versionCode 1→2, versionName 0.1.0→0.6.0(对齐仓库发行线)。vphone_lib 按需求
  零改动(上位机直接 vp.http() 拿原文)。

设备验收(华为 TEL-AN00a, 需求§5 用例全过): ①off ②pos 递增 ③seek 生效+越界钳制
④未播 seek 报错 ⑤播完归 off+CALL_AUDIO_END 恰发 2 次(seek 到末尾也触发, 中间手动
stop 不误发) ⑥循环 name 无后缀/loop=1/过 dur 回绕恒 playing ⑦媒体全家+call/audio
旧用法回归不变。测试用 8s 短音频 _short8s.wav 留在乐库(快速播完用例专用)。

顺带关闭补3挂账: dial→expect_event(CALL_ACTIVE/CALL_ENDED)+水位回放+expect_no_event
设备实测全绿(GUI 事件流渲染仍待人工, 沙箱假死是环境假象不变)。

环境发现(排障半小时的教训): 本机 EMUI 对**裸隐式** shell 广播(am broadcast -a X 不带
-p/-n)一律 "Background execution not allowed", 应用前台+前台服务都拦; lib broadcast()
实际发的 -p 包名限定形式和显式 -n 组件形式均正常送达——lib 无需改, 以后手工调试广播
务必带 -p(与 ControlReceiver/lib 注释的既有结论一致, 本次实测复核)。

产物: APK 878441B 同步 apk/; exe(13MB)/wheel(0.6.0) 重建, wheel 内嵌 APK 字节一致;
test_lib.py 8 组回归全绿。

**追加：vphone 第八轮 —— 多路通话：呼叫等待 + 保持恢复 + 三方接听**

需求原话: "模拟接电话途中还有电话打进来, 可以保持和恢复, 保持时可以接听三方来电"。
方案 = Telecom managed ConnectionService 本就支持一个 PhoneAccount 挂多条 Connection,
呼叫等待就是"通话中再 addNewIncomingCall 一条 ringing 连接"; HFP 侧 +CCWA/双路 CLCC
由 EMUI 系统栈自动发给车机, vphone 不碰协议层(保真度卖点不变)。

语义(全部对齐真机 GSM, 详见 API 文档 §1.2 引言):
- 最多 2 路(GSM 上限), 第 3 路 incoming 明确拒绝且零事件;
- 通话中 incoming = 呼叫等待(RING_IN + 新事件 CALL_WAITING), 原 active 不动;
- answer 接等待路时原 active 自动保持(真机标准行为); 手动 hold(on=1) 后接听同样支持
  (场景3 = 需求原话路径, 设备实测通过);
- swap() / hold(on=0) 双通话时等价(互换 active/held);
- hangup 无参挂前景(active>ringing>dialing>held, 对齐车机红键), number= 挂指定路,
  "all" 全挂; 挂掉 active 后 held 保持不自动恢复(真机), hold(on=0) 手动恢复;
- 通话中 dial = 第二路, 3s 摘机时原 active 自动保持(场景4 实测)。

APK 侧(CallEngine 全重写为 calls 列表 + VConnection 各回调持自己的 CallRec):
- 对外 state/number 变为前景派生只读属性(active>ringing>dialing>held), 既有读点
  (CallAudioEngine/MediaEngine/Dispatcher/GUI)零改动;
- activate() 编排恒一 active: 先 hold 其它 active 再 setActive 本路; 车机接听回流
  沿用旧例只发 CAR_ANSWER 不发 CALL_ACTIVE, 车机 onHold/onUnhold 对应 CAR_HOLD/CAR_UNHOLD;
- 挂完最后一路才全清(通话音频随末路停, 中途挂一路 SCO 跟随剩余通话);
- /status 追加 calls=[active:138…,held:10086] 字段(前缀不变, 兼容既有解析);
- capabilities 补 CAPABILITY_HOLD(单保持, 无会议/无双 active —— GSM 就没有)。
- versionCode 2→3, versionName 0.6.0→0.7.0; lib __version__ 0.7.0。

本轮修掉的两个 bug(都是设备验收逮到的):
1. 注入回滚误报: 1.5s 兜底定时器只查"号码还在不在 calls", 分不清"从未建连"和
   "1.5s 内已被正常挂断"——快速脚本里等待路走完一生被误报"注入未生效已回滚"。
   修: 判据加 pendingIncomingNumber 未消费(建连即消费), 只报真正没落地的注入。
2. hangup(all) 不发 CALL_ENDED: teardown 契约是"事件由调用方先发", all 分支漏发,
   清场后的挂断断言空窗。修: 全挂逐路先发 CALL_ENDED(带 [全挂] 标记)再 teardown。

PC 侧: lib 加 swap()/hangup(number=None)("all"=全挂), PATHS 加 swap;
CLI 加 swap 分支 + hangup --num; GUI 加「⇄切换」按钮 + 通话状态行
(_stat_poll 拉 /status 解析 calls=[…] 刷 Label, 双通话不再靠脑补)。

设备验收(华为 TEL-AN00a, 4 场景全绿): ①等待→接听自动保持→切换→挂前景→held 保留
不自动恢复→手动恢复 ②三路上限拒绝(明确文案+零事件+状态不动) ③手动保持后接听三方
(需求原话路径) ④通话中去电第二路 3s 摘机自动保持; 全程 calls=[…] 状态核对 + 零回滚误报。
车机侧显示(CCWA 等待界面/保持标记/CHLD 切换/剩余通话)入 DEV §3 清单待有车机时裁决。

test_lib.py: 8→10 组(多路指令面 fake: 路径/参数精确匹配含 hangup 三态;
多路场景回放 fake_staged: 累积历史信封+指令不消耗, 全链路 expect_event 断言)。

产物: APK 960742B 同步 apk/; exe(13MB)/wheel(0.7.0) 重建, wheel 新 venv 验证
(版本/内嵌 APK/CLI 入口)通过。

**追加：vphone 第九轮 —— 双通道独立对端音频 + GUI 通话音频进度条**

需求原话: "两个电话通道能否分别播放不同的音乐; 模拟时要显示媒体音乐进度条和电话播放的进度条"。

物理结论(先讲清): HFP 只有一条 SCO 音频链, 真机上两路通话也永远只有 active 那路出声
(被保持那路由运营商网络处理, 车机听不到) —— "两路同时混音"真机不存在, 刻意不做。
**能做且保真的**: 每路通话各自绑定"对端音频"(文件/循环/进度), 车机听到的始终是
active 路的那段, 通话切换时自动换源并续各自进度 —— 像给两个人打电话各说各话。

APK 侧(0.8.0, versionCode 4):
- CallRec 加 audioName/audioLoop/audioPosMs; CallAudioEngine.play 绑定到当前 active 路
  (无 active 明确拒绝, 收紧了第七轮"无通话也报 playing"的松语义 —— 该状态不再可进入);
- followForeground()(activate/holdRec/teardown 结尾调用): 换源续播/无绑定静音/无 active
  静音; 等待路进来(ringing 不顶 active)声音不断; 手动 stop=真解绑(该路再激活不自响);
  自然播完归零进度(再切回从头说);
- 新事件 CALL_AUDIO_FOLLOW(实际发生可闻变化时发, detail 带新 active 号码+文件+续播位置);
- /call/audio-status 与 /status 的 callAudio 行末尾追加 num=<号码>(当前出声的是哪路,
  追加字段兼容既有解析); activate 编排内的 holdRec 不再触发 follow —— 中间态
  "无 active"会误报静音事件(设备验收逮到, 收敛为 activate 结尾统一 follow 一次)。

GUI: 电话区新增对端音频进度条(与媒体进度条同款 Scale, 拖动即 seek) + 状态行
「对端音频: 「xx.mp3」 3s/10s 循环 [号码]」; _stat_poll 同窗口拉 /call/audio-status,
_drain 加 caudio/caudiotxt 消息。媒体进度条本就有(scale_pos + 当前曲目行), 未动。

设备验收(华为 TEL-AN00a, 全绿): 无通话拒播 / 等待期间声音不断 / 接听 B(无绑定)静音
且事件带 B 号码 / swap 双向跟随+各自续播位置(实测 B 2s→3s, A 独立) / 保持→静音 /
恢复通话→对端自动继续说话 / 手动 stop 解绑(切回不自响) / 重绑正常 / 全挂复位逐路事件。
意外收获: 车机(CARKIT-1, 当时实连)在双 held 状态发了 CHLD=0(释放保持通话), APK 正确
回流 CAR_REJECT 并拆掉保持路 —— 真车机对多路通话的首次主动操作回流, 记入证据。

验收脚本三次踩坑(都是脚本错, 非 APK 错, 备忘): ①RING_IN 事件在 incoming() 指令内
同步发, 而连接落地(attach)是异步 —— 等 RING_IN 再 answer 仍会抢跑, 可靠同步点是
/status 出现 ringing:; ②swap 的事件在 HTTP 响应前就落日志, 断言必须先记水位再发命令;
③双 held 时 hold(on=0) 恢复的是列表第一路(calls 顺序), 不是"最后保持的那路"。

产物: APK 同步 apk/; exe(13MB)/wheel(0.8.0) 重建; test_lib 10 组回归全绿。

## 七、第十轮 —— GUI 蓝牙列表模糊过滤(小轮) — 2026-09-10

动机: 车机现场扫出几十台设备, 在列表里肉眼找 CARKIT-1 很费劲。

GUI(vphone_gui.py, 0.8.1):
- 🔍过滤框(蓝牙区, 输入即过滤): 关键词按空格分词, 不区分大小写, 每词都须命中
  「蓝牙名 MAC」(例 "carkit-1 00:11" 直达 CARKIT-1); Esc 清空; 右侧计数「匹配 n/m 台」;
- 关键解耦: 列表显示从全量 bt_devs 改为过滤视图 _bt_shown, 配对/解配/重连/断开/
  授权/双击配对 6 处选中类取值全部改为索引 _bt_shown —— 否则过滤后下标错位会
  操作到别的设备(这正是本轮唯一的隐患点); 过滤变化按 MAC 保持原选中项;
- bt_filter() 做成模块级纯函数(不依赖控件), 脱离 GUI 可单测。

验证: bt_filter 单测(大小写/分词AND/中文/MAC片段/无名设备/空)全绿; 控件级测试
(隐藏窗口真 Listbox+StringVar trace)验证回填/过滤/选中保持/无命中兜底全绿;
test_lib 10 组回归全绿; wheel 0.8.1 fresh venv 冒烟通过。APK 无改动(0.8.0 不动)。
渲染观感按惯例待真实桌面过眼。

## 八、第十一轮 —— 两处真机保真度纠正(挂断语义+媒体焦点) — 2026-09-10

用户台架实测逮到两个"不像真机", 都是多轮通话功能出来后才能暴露的组合态:

**① 挂掉 active 通话后, 另一路保持沉默。** 根因是第八轮定语义时的错误假设
"挂掉 active 后 held 保持 held 不自动恢复(真机行为)" —— 实际真机 GSM 是 CHLD=1
语义: 释放 active 时网络**自动取回保持路**(挂掉 B, A 立刻继续有声)。修复:
teardown 在移除记录后检测"无 active 且有 held" → activate(第一个 held), 对端音频
followForeground 从各自进度续播; hangup("all") 传 autoResume=false 抑制(全挂不诈尸,
否则第一路 teardown 会先激活 held 响一声再被挂, 事件流脏)。
事件链: CALL_ENDED(挂的) → CALL_ACTIVE(app, "自动恢复(真机CHLD=1)") →
CALL_AUDIO_FOLLOW(续 Xs)。车机侧 onDisconnect 挂 active 走同一 teardown, 同样自动恢复。

**② 电话接通时媒体音乐还在"播"(进度在走, 没声音)。** 真机此时媒体 App 因音频焦点
被通话抢占而暂停 —— 进度停走, AVRCP 给车机的也是 paused。修复: MediaEngine 加
pausedByCall 标记; CallEngine.attach(ringing/dialing) 来电/去电落地即 pauseForCall()
(铃声一响就停, 不是等接通), activate() 再兜底一次(响铃中手动放歌的场景);
clearAll(全部通话结束) → resumeAfterCall() 自动续播。手动/车机的播放暂停操作清标记
—— 挂断后只恢复"被通话暂停的", 不抢用户意图。新事件 MEDIA_CALL_PAUSE/RESUME。

设备验收 15 项全绿: S1 用户原场景(媒体播放→来电即冻结→双路双音→挂B→A自动恢复
且 _short10s 从 3s 续播→全程媒体暂停→全挂→媒体自动恢复进度 1→2s);
S2 回归(挂 held, active 那路音频零中断、零切换事件);
S3 焦点接管(响铃中手动放歌=接管但接通仍停/通话中手动暂停=挂断后不自动恢复)。

APK 0.8.2(versionCode 5)/lib 0.8.2; test_lib 多路场景回放按新事件链重写
(挂前景→自动恢复, 两端补媒体焦点事件), 10 组全绿; exe/wheel 重建。
另: 验收脚本曾把 /status 里媒体行 focus=held 误当通话 held 断言 —— /status 是多引擎
汇总行, 断言通话态要切出 calls= 段看, 别整行 substring。

## 九、第十二轮 —— 全量审查 + 拆线竞态(僵尸通话) + 通话音频路由跟随 — 2026-09-08

背景: 用户台架实测逮到两个新 bug, 同时要求审查媒体/电话切换逻辑是否符合真机。
审查挖出的高危项与 bug① 同根因, 合并本轮一次修掉(0.8.3)。

**① 车机连挂两路后出现"挂不掉的僵尸通话"**(车机显示有通话+有声, 手机无通话 UI,
重启才能清)。根因链: teardown 挂掉 active 后【同步】复活 held —— 车机"挂断"时两条
onDisconnect 背靠背到达, 复活恰好落在 Telecom 正在拆第二条连接的窗口里, 把半死连接
setActive 拉起来, Telecom 状态错乱(车机看 HFP 还有通话, 手机侧已无此呼叫, 后续挂断
无法路由)。审查还发现同机制第二引信: 去电 3s 自动摘机定时器只查 r.state=="dialing",
而 teardown 从不改 state → "拨出 3s 内挂断"会幽灵 CALL_ACTIVE; 有其它 active 通话时
会被幽灵保持并卡死(这就是审查报告里的高危项)。修复(一套机制关两个引信):
  - teardown 进门先置终态 rec.state="ended";
  - 复活改延迟 300ms 重验(仍在 calls 且仍 held)再 activate —— 避开拆线窗口, 且更贴
    真机(网络取回本就有时延); 代价: 事件链多一条中间 CALL_AUDIO_FOLLOW(静音);
  - 3s 摘机定时器 owns()+state 双校验。
顺带: 车机 onHold 非活动路只记按键不改状态(真机不存在"保持响铃路"); dtmf 加 active
守卫(对齐 API 文档"需 active"); swap 在"无 held 有等待路"时=保持当前+接答(CHLD=2)。

**② 车机/手机通话界面切声音到手机, 听筒/扬声器无声, 只有蓝牙出声。**
根因: CallAudioEngine.pinSco 把对端音频硬绑 TYPE_BLUETOOTH_SCO, Telecom 路由切换
对它无效。同根因还解释了①前半段"两路各放音乐, 进度在走但没声音": 接通时未显式请求
蓝牙路由, EMUI 偶尔把通话留在听筒 → pinSco 找不到 SCO 设备, 声音去了手机听筒
(车机侧=没声音)。修复:
  - VConnection.onCallAudioStateChanged(CallAudioState) → CallAudioEngine 跟踪路由;
  - 蓝牙才绑 SCO, 且 SCO 设备未出现时后台 0.5s×20 重试(设备可能迟到);
  - 听筒/扬声器/有线清 preferredDevice, 走系统通话路由;
  - activate() 在当前路由为蓝牙时显式 conn.setAudioRoute(ROUTE_BLUETOOTH)(真机接通
    默认; 用户切去听筒后不抢回);
  - 新事件 CALL_AUDIO_ROUTE(sys, detail=目标路由)。

**③ 媒体切换顺带修**: 单曲列表 next/跳回自身 = 从头重播(restartIfSameReal; 原实现
只 start() 续播, 进度闪 0 又弹回、声音不断)。

**审查记录未修(待裁决, 见审查报告)**:
  - 无 active 时第二路来电进"呼叫等待"(两路同时 ringing) —— 真机网络侧一般对新主叫
    回忙, 待拿日常手机验证再定是否收紧;
  - 通话 active 中手动播媒体"进度在走没声"(S3 用户覆盖语义, 协作焦点灰区, 文档已载);
  - 拨号等待期无回铃音(可听通道缺口, 补齐成本高, 缓)。

版本: APK 0.8.3(versionCode 6)/lib 0.8.3; test_lib 新增 test_call_races(①幽灵接通
②连挂两路不诈尸 ③路由事件), 13 组全绿; 签名契约仍 77。设备验收待手机回位, 重点:
连挂两路清干净、拨出 3s 内取消无幽灵、车机/手机切路由声音跟随、挂 active 后 held
约 0.3s 恢复、单曲 next 从头播。若再遇僵尸通话先试 PC 侧全挂(hangup number=all),
能清=我们状态机的锅, 不能清=Telecom 侧(需本轮修复装机后复测)。

## 十、小修 —— 接口规范审查落地: 事件名词典化 — 2026-09-08

审查结论(全量 AST 扫签名 + 断言栈人工审): kwargs 化/断言四件套合规且有测试锁定,
唯一实质薄弱点=事件名双源(Kotlin 常量 ↔ Python 裸字符串)无机器校验, 且已发生一例
漂移: CMD_DIAL 在 VPCS.kt 是裸字面量未进 EventLog 常量表(test_lib 靠重复字符串恰好对上)。

- Kotlin: CMD_DIAL 收进 EventLog 常量表(与 CAR_DIAL 同型不同 src), 引用点改常量;
- test_lib 新增 test_event_names_dict: 提取四个 .py 里带事件前缀(CAR/CMD/CALL/RING/
  MEDIA/BT/CONTACTS)的引号字符串, 断言 ⊆ EventLog.kt 常量表 —— 拼错/未收编/改名漏改
  在无设备单测直接红, 不再等到真机断言静默永不命中。普通常量(CREATE_NO_WINDOW 等)
  无前缀不误入, 零排除表;
- 纯重构零行为变化(发射的字符串原样), 但按纪律 APK 版本对齐: 0.8.4/versionCode 7,
  lib/wheel 0.8.4, 三产物重建内嵌一致。test_lib 14 组全绿; 签名契约仍 77。

## 十一、GitHub 公开镜像机制(公司 Gitea → 个人公开仓) — 2026-09-22

公司 Gitea 保持唯一真源; 个人 GitHub 仓(lizongxun1995/bluetooth-simulate, 公开)作为对外镜像,
由本机外置流水线清洗后**单向**同步(用户裁决: 保持 Public, 做清洗版):

- **流水线**: `..s-public-sync\sync.sh`(在项目仓外, **永不入库** —— replacements.txt 里写着
  真实序列号/MAC/内网地址) : push origin → 本地 `--no-local` 镜像克隆 → git filter-repo
  (占位符化: 手机序列号/车机 MAC/内网 Git 地址/车型 CARKIT-1/公司包名/车机板型号/本机用户目录;
  mailmap: 公司邮箱→GitHub noreply) → 全历史敏感串自检(命中即拒推) → push github;
- **确定性**: 规则不变时旧提交洗后哈希不变, 新提交普通 push 叠加即可; 只有改了规则才需 force;
- **本机两个坑已解**: ①仓外裸访问内网 Gitea URL 会 404(凭据只在项目仓上下文生效) → 流水线
  改从本地仓克隆; ②git 出网走本机代理 127.0.0.1:7897, GitHub 首次 OAuth 已存 GCM, 后续无感;
- **纪律**: 公开仓永不直接提交, 一切改动走 Gitea + sync.sh; 台架换设备后新序列号/MAC
  第一时间进 replacements.txt; APK 二进制已验证(解压全文扫描)不含敏感串, 可随仓发布。

首推内容: 28 个提交全历史清洗, 自检+公网 raw/API 复核双通过(敏感串 0 命中/占位符在位/
作者全部 noreply)。skills 目录快照另建了本地仓(~/.agents/skills), 待单独的 GitHub 仓库 URL。
