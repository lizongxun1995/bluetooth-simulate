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



1. **总时长始终 0**：`DisplayUpdater.Update()` 触发的 TRACK_CHANGED 里，车机采样的是**旧
   timeline**。修法 = **PushTimeline 要在 Update() 之前 AND 之后各推一次**（后推那次才会被
   采到）。涉及 `btwinrt/Engine.cs` 的 SetTrack/PushTrack/PushTimeline 时序。
2. **HFP 10048（Only one usage of each socket address）**：OS 级 HFP 连接占着 111E 通道，
   应用层 socket 绑不上（同一 (协议,地址,端口) 只许一个）。修法 = 先
   `SetServiceState(false)` 让系统栈放手，再重试连接。（SO#14534722 同类。）
3. **CallControl.GetDefault()=null**：结构性缺口，Windows 无 HFP AG 服务端/电话设备模型，
   **不可模拟**（COD 注册表只换外衣救不了它）——门C ✗ 已定案，正是转 vphone 路线的主因。
