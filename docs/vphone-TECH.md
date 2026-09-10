# vphone 技术文档（虚拟手机）

> 一台普通 Android 手机（台架：HUAWEI TEL-AN00a / EMUI / Android 10）装上 vphone APK 后，
> 即成为 **PC 可远程驱动的蓝牙测试终端**：车机看到的 A2DP/HFP/AVRCP/PBAP 对端行为
> 与真手机一致，但播放内容、来电、联系人、配对动作全部由 PC 脚本控制。
> 本文档讲架构与线程模型；接口见 [vphone-API.md](vphone-API.md)；
> 任务跟进与验收清单见 [vphone-DEV.md](vphone-DEV.md)。

---

## 1. 定位与保真度论证

### 1.1 为什么是 vphone（路线背景）

| 前路线 | 结论 |
|---|---|
| ESP32 模拟手机 | 信令级可控，但协议栈细节与真机差距大，车机兼容性问题不断 |
| Windows 蓝牙（btwinrt） | 系统栈无 HFP AG 服务端（CallControl=null 结构性缺口），门C 不可模拟 |
| **vphone（当前主力）** | **协议层本来就是真厂商栈，只模拟"内容源"App** |

### 1.2 保真度的分层论证（重要）

- **协议层 = 真**：A2DP 编码协商、HFP AT 信令、AVRCP 命令/通知、PBAP/MAP 会话全部由
  EMUI 系统蓝牙栈原样发出，与真手机无差别。vphone **不自研任何蓝牙协议逻辑**。
- **应用层 = 模拟**：vphone 模拟的是"播放器 App"（MediaSession）和"拨号器 App"
  （Telecom ConnectionService）的行为。
- 因此 **"接近真机" = 应用层状态机对齐成熟播放器/拨号器语义**。所有行为决策以
  「真机会怎么做」为基准（见 §6 基准表），不可修的偏差显式列在 §7 清单里。

## 2. 系统架构

```
┌─ PC ──────────────────────────────────────────────┐
│ vphone_gui.py(ttk 界面)  vphone_ctl.py(CLI)  测试脚本 │
│            └──────── vphone_lib.py(VPhone) ────────┘ │
│   HTTP(主) : adb forward tcp:18800 → 手机:8800 / WiFi │
│   广播(兜底): adb shell am broadcast -a com.bt.vphone.CMD │
│   事件    : GET /events?since=N (JSON 水位拉取)        │
│   打包    : build_exe.py → dist/vphone_gui.exe        │
│            (内嵌 vphone.apk + adb, 换机器免装)          │
└──────────────────────┬──────────────────────────────┘
                       │ USB (hub 上挂手机+车机板)
┌─ 手机 APK ───────────▼──────────────────────────────┐
│ VPhoneService(前台常驻 + PARTIAL WakeLock)            │
│ ├ ControlServer   HTTP :8800, 每连接 1 worker 线程    │
│ ├ ControlReceiver adb 广播入口(与 HTTP 共用 Dispatcher)│
│ ├ Dispatcher      路由中枢 + onMain{} 主线程同步桥     │
│ ├ MediaEngine     门B: MediaSession 播放器状态机       │
│ │   ├ 真实音频: MediaPlayer 解码乐库文件(车机真出声)    │
│ │   └ 静音流  : AudioTrack 静音帧保活 A2DP             │
│ ├ CallEngine      门C: Telecom ConnectionService 通话 │
│ ├ CallAudioEngine 通话音频(SCO 下行, 模拟对端说话)      │
│ ├ ContactsEngine  联系人批量写(PBAP 测车机通讯录)       │
│ ├ BtEngine        扫描/配对/断连/回连(反射, 自管线程)   │
│ ├ AutoPairService 无障碍服务: 配对弹窗自动确认          │
│ └ EventLog        结构化事件总线(synchronized, 任意线程)│
└──────────────────────┬──────────────────────────────┘
                       │ EMUI 蓝牙栈(真厂商栈)
                     车机 CARKIT-1
```

## 3. 双控制面契约

| 通道 | 载体 | 用途 | 限制 |
|---|---|---|---|
| HTTP | `:8800`（USB forward `127.0.0.1:18800` 或同 WiFi 直连） | 主通道，同步返回纯文本 | 需服务已启动 |
| adb 广播 | `am broadcast -a com.bt.vphone.CMD --es cmd <短名>` | 兜底（HTTP 不通时 `cmd()` 自动降级） | 无返回值→睡 1.2s 取 logcat `[adb]` 行；结果可能滞后 |

- **双通道**（有广播形态）：电话/媒体/蓝牙/联系人全部触发类命令。
- **仅 HTTP**：`/media/upload`(二进制 POST)、`/events`(JSON 拉取)、`/media/status`、`/contacts/count` 等轮询。
- **仅 adb**：装机期命令（`install`/`launch`/`grant_perms`/`enable_account`/`enable_autoconfirm`）——HTTP 可能未就绪。
- 广播别名表 = `Dispatcher.alias`，HTTP 短名表 = Python `PATHS`，**改路由三处要同步**（见 DEV 文档更新规则）。

## 4. 线程模型

### 4.1 APK 侧（状态归属的根规矩）

- **MediaEngine / CallEngine / ContactsEngine / CallAudioEngine 的全部状态只在主线程读写**。
  MediaSession 回调、1s 心跳、换曲处理都在主线程。
- HTTP worker 线程必须经 `Dispatcher.onMain{}`（post + CountDownLatch, 8s 超时 → "引擎繁忙"）
  进入上述引擎。**历史事故**：worker 直改引擎状态导致双 ticker（进度双倍速）、
  双 MediaPlayer（两路同播）、seek 与 release 竞争。
- **例外**：`BtEngine` 不上桥（bond/reconnect 阻塞数秒，上主线程会 ANR）——自管后台线程 +
  `@Volatile`；其对 MediaEngine 的副作用（断链前 pause）用 `Handler.post` 回主线程。
- `EventLog` 自带 synchronized，任意线程可用。
- 静音流供流线程（`vphone-silence`）：只写 AudioTrack，不碰引擎状态；退出条件三重
  （见 §5 事故记录）。

### 4.2 PC 侧（GUI）

- 主线程 = tkinter 主循环 + **一切控件读写**（tkinter 非线程安全，后台线程取控件值偶发崩溃
  ——所有取值在提交任务前于主线程完成，闭包只带值）。
- 后台 = ThreadPoolExecutor(按钮动作) + `_evt_poll`/`_stat_poll`(轮询) + lib 内 logcat 线程；
  全部经 `queue` 发消息，主线程 `_drain()` 消费（消息协议见 vphone_gui.py 模块头）。
- **连接代际 `_gen`**：每次「连接」成功 +1，旧轮询线程见代际变化自退——重复点「连接」
  不会叠加轮询线程（否则事件双份、进度双刷）。
- 轮询线程连续 3 次失败（USB 断连）→ 日志提示一次并自停，等用户重连。

## 5. 事故记录：静音流供流线程忙循环（第六轮发现并修复）

**现象**：`VPhone` 标签 logcat 以每秒数万行刷 `静音流心跳`（实测 16 万行/s），进程 100% CPU
（烧掉 30+ 分钟 CPU 时间）；第二条并发 logcat 流连接直接 `read: unexpected EOF!`；
广播兜底通道取结果（`logcat -d -t 30`）全被垃圾行淹没。

**根因链**：
1. `doPause()` 只 `audioTrack.pause()`，供流线程保留（阻塞在 `write()` 等缓冲排空）；
2. A2DP 路由消失（车机断连/关蓝牙）后系统瞬间丢弃缓冲，`write()` 退化成
   **立即返回+丢弃数据** 的忙循环（日志佐证：`playState=2` PAUSED 孤儿轨、posSec 每行 +15）；
3. 每 15 次写打一行心跳 → 刷爆 logcat。

**修复（三重独立退出路径，不依赖跨线程字段可见性）**：
1. `doPause()`/播完停尾 → 彻底 `stopSilence()`（停线程+停轨+置空），真机基准：暂停的播放器
   没有活跃音频流；恢复播放时 `ensureAudioOut()` 按需重建。
2. 供流线程加退化检测：连续 5 次 `write()` 耗时 <200ms（正常应阻塞约 1s/次）判定路由已失效，
   立即退出交给自愈逻辑。
3. `setSilence(true)` 暂停态不再起流（只记开关，播放后生效）。

**验证**：播放态 CPU 0.0%、心跳恰 1 条/15s；暂停态 `silence=off`、logcat 安静、CPU 0.0%；
play↔pause 循环干净；双并发 logcat 流各 45/45 行共存；Python 重连场景新旧流交接正常。

## 6. 真机行为基准表（本轮已验证 ✅ = PC 侧回归通过）

| # | 场景 | 真机行为 | vphone 行为 | 验证 |
|---|---|---|---|---|
| T1 | 空列表切歌 | 播放器无反应/提示无歌 | 明确报错"播放列表为空"，不装作切过 | ✅ |
| T2 | 暂停态切歌 | 换曲显示新元数据，保持暂停，进度新歌 0s | doAdvance/jump 统一 `applyTrack()` 复位+清 seek 窗口；暂停保持；延迟补推 2 次元数据（对付暂停态不刷新的车机栈） | ✅ |
| T3 | seek 后切歌 | 新歌从 0s 起，不闪回旧 seek 位置 | applyTrack 清 `seekAt/seekPosMs`（1.5s 防回弹窗口不跨曲） | ✅ |
| T4 | 单曲播完(静音流) | 播完即停，不原地循环 | `handleTrackEnd()`: playing=false、进度钉在总时长、停心跳、停流；与真实音频模式一致 | ✅ |
| T5 | seek/缺参数 | — | jump 缺 idx / seek 缺 pos 返回用法错误，不静默用 0（危险默认值） | ✅ |
| T6 | 来电→接听→挂断 | 挂断后状态归 idle，号码清空 | hangup 清 number/pendingIncomingNumber/CallAudioEngine；onAbort(系统拆线) 同样归零 | ✅ |
| T7 | 并发联系人写入 | — | AtomicBoolean CAS，第二个批任务被拒"批量任务进行中" | ✅ |
| T8 | 注入来电失败 | — | 1.5s 未生效自动回滚 idle 并记事件（不留 ringing 死状态）；判据带 `pendingIncomingNumber` 未消费，1.5s 内正常挂断的一路不误报 | ✅ |
| T9 | 暂停后路由消失 | 播放器保持暂停，无后台活动 | 供流线程彻底退出（§5 事故修复） | ✅ |
| T10 | 通话中来电 | 呼叫等待：新路响铃、原路不动，最多 2 路 | waiting 注入（RING_IN+CALL_WAITING），第 3 路明确拒绝 | ✅ |
| T11 | 接听等待电话 | 原 active 自动保持、新路上线，恒一 active | `answer()` 编排：先 hold 其它 active 再 setActive；手动 hold 后接听同样支持 | ✅ |
| T12 | 双通话切换 | 互换 active/held | `swap()` / `hold(on=0)` 等价；车机 CHLD 键走 onHold/onUnhold 回调 | ✅ |
| T13 | 挂一路 | 挂 active 后另一路保持 held 不自动恢复；挂 held 不影响 active | hangup 选择性/前景优先级；恢复须显式 `hold(on=0)` | ✅ |
| T14 | 通话中拨出 | 新呼叫接通时原通话自动保持 | dial 第二路 + 3s 摘机时 activate（自动 hold 原路） | ✅ |
| T15 | 双通话各自的对端音频 | HFP 单 SCO：车机永远只听得到 active 路；被保持那路声音由网络侧处理，车机不可闻 | 每路 CallRec 独立绑 audioName/loop/进度；切换 followForeground() 换源续播、无 active 即静音；手动 stop 解绑；两路同时混音刻意不做（真机不存在） | ✅ |
| — | 暂停态车机切歌 | 车机上应能切 | 代码侧状态机已对齐(事件+元数据可查)，**待车机实测裁决**（EMUI 是否丢键存疑，见 DEV 清单） | ⏳ |

## 7. 与真机的已知偏差清单（测试有效性边界）

每条注明：偏差原因 → 影响哪些测试场景 → 是否可修。

| 偏差 | 原因 | 影响的场景 | 可修性 |
|---|---|---|---|
| 无 SIM：HFP 信号强度/电量不上报真实值 | 无运营商网络 | 车机信号格/电量显示类用例 | 不可修（需实体 SIM）；测试时忽略这两项显示 |
| EMUI 对第三方 MediaSession 丢 FF/RW 键（部分场景） | 平台层按键分发 | 车机快进/快退键用例 | 不可修（已证同类：paused 态丢键）；用 CAR_SEEK/长按透传替代验证 |
| 暂停态车机切歌待裁决 | 嫌疑：EMUI 丢键 / 车机暂停不刷新 / 已修的推送竞态 | 暂停态切歌用例 | 修复已上车，**待车机侧事件流裁决**（DEV 清单第 1 项） |
| 静音流模式：无真实解码 | 设计如此（测元数据/按键不测音质） | 音质/杂音类用例 | 用真实音频模式（upload_audio）覆盖；两模式行为已对齐 |
| 通话音频是文件播放，非实时语音链路 | 设计如此 | 语音质量/降噪类用例 | 用高码率语音文件近似；实时链路不在范围 |
| PBAP 授权需手动/反射，可能失败 | EMUI 权限模型 | 车机通讯录拉取用例 | bt_allow_car 反射 + 失败时给手动路径指引 |
| 蓝牙名/地址由手机决定 | 系统层 | 需要特定设备名的用例 | bt_name 改名（车机重连后生效）；MAC 不可改 |

---

## 8. 关键设计备注

- **换曲统一入口 `applyTrack()`**：doAdvance/jump/loadPlaylist/setTrack 共用；重置元数据/进度
  **并清 seek 防回弹窗口**。
- **播完统一入口 `handleTrackEnd()`**：onCompletion 与心跳兜底双路到达，2s 去重
  （`lastEndAt`）；自动连播或停尾。
- **ticker 自复活守卫**：心跳 run() 内部可能 stopTicker（停尾路径），续期前必须
  `if (ticker === this)`。
- **ContactsEngine CAS**：批量写用 AtomicBoolean 占坑，防双写/进度互相覆盖。
- **`_setup_forward()` 幂等**：`adb forward --list` 已有同端口转发就跳过——重复 rebind 会让
  adb server 短暂拒接新连接（实测曾干扰诊断，顺手修掉）。
- 事件 `src` 语义：`car`=车机按键回流 / `cmd`=PC 指令 / `app`=App 自身 / `bt`=蓝牙栈 /
  `sys`=系统 Telecom 侧。
