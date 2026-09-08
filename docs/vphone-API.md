# vphone 接口文档

> 面向写测试脚本的人。三件套：**HTTP 端点**（手机 :8800，经 adb forward 或 WiFi）、
> **Python 库** `vphone/vphone_lib.py`（推荐入口）、**CLI** `vphone/vphone_ctl.py`。
> GUI（vphone_gui.py / 打包 exe）按钮与库方法一一对应，不单独列表。
> 架构与线程模型见 [vphone-TECH.md](vphone-TECH.md)。

## 0. 快速上手

```bash
pip install vphone-0.6.0-py3-none-any.whl    # 或 cd vphone && pip install .
# 装完多了两个命令: vphone(CLI) / vphone-gui(图形控制台); 不装也行, cd vphone 直接用源码
```

```python
from vphone_lib import VPhone
vp = VPhone(serial="SN_PHONE_A", wait_timeout=20)  # 单设备可省 serial; 多设备必填或 VPHONE_SERIAL

vp.incoming(number="13800138000")            # 注入来电(车机+手机同时响)
vp.expect_event(evt_type="CAR_ANSWER")       # 有人在车机上按了接听(超时抛异常, 消息带现场)
vp.hangup()

vp.play_audio_files(paths=["D:/music/a.mp3", "D:/music/b.mp3"])  # 上传+列表+播放(车机真出声)
vp.expect_event(evt_type=("CAR_NEXT", "CAR_PREV"))  # 车机上切了歌

vp.contacts_load(count=10000)                # 1w 联系人 → 车机 PBAP 通讯录压测
```

**API 稳定性约定**（v0.6.0 起）：全部公开方法 **keyword-only**（调用必须写参数名，
后续版本插参数不破坏既有调用）；事件断言返回 **VEvent 固定字段数据对象**（不再是无契约的
dict/字符串）。除查询类，所有方法返回手机端结果**文本（中文）**；失败抛 `VPhoneError`。

**v0.6.0 破坏性变更**（相对仓库旧版）：① 位置参数调用全部改为必须 kwargs；
② `events()` 返回 `list[VEvent]`（原裸 dict 信封改由 `events_raw()` 提供）；
③ `wait_event()` 返回 `VEvent`（原 dict）。

---

## 1. HTTP 端点全表

基地址：USB 模式 `http://127.0.0.1:18800`（`adb forward tcp:18800 tcp:8800`），
WiFi 直连 `http://<手机IP>:8800`。返回纯文本，**例外：`/events` 返回 JSON**。
"通道"列：`双`=HTTP+广播兜底（`cmd()` 自动降级）；`HTTP`=仅 HTTP；`adb`=仅 adb 命令。

### 1.1 系统

| 端点 | 参数 | 说明 | 通道 |
|---|---|---|---|
| `GET /status` | — | 四引擎汇总+最近事件+控制面速查 | 双 |
| `GET /events` | `since=N` | 结构化事件 JSON：`{"last":N,"events":[{id,ts,type,src,detail}]}` | HTTP |

### 1.2 电话（门C）

| 端点 | 参数 | 说明 | 通道 |
|---|---|---|---|
| `GET /call/incoming` | `number` | 注入来电（默认 13800138000） | 双 |
| `GET /call/dial` | `number` | 模拟本机拨号（对端 3s 自动接通，见 auto-outgoing） | 双 |
| `GET /call/answer` | — | PC 侧代接 | 双 |
| `GET /call/hangup` | — | 挂断（任意状态，复位 idle） | 双 |
| `GET /call/hold` | `on=0/1` | 通话保持/恢复 | 双 |
| `GET /call/dtmf` | `key` | DTMF 按键（0-9*#，需 active） | 双 |
| `GET /call/audio-bt` | — | 通话音频切蓝牙 SCO | 双 |
| `GET /call/auto-outgoing` | `on=0/1` | 车机拨出后是否 3s 自动接通（默认开） | 双 |
| `GET /call/audio` | `name`,`loop=0/1` / `stop=1` / `seek`(秒) | 通话中播放乐库音频（SCO 下行模拟对端说话）。参数优先级 `stop`>`seek`>`name`；`seek` 拖进度（钳制 `[0,dur]`），未播放时报错、不误起播 | 双 |
| `GET /call/audio-status` | — | 一行通话音频进度（上位机进度条 1s 轮询）：`callAudio=off` 或 `callAudio=playing name="xx.mp3" pos=12s dur=35s loop=0`。name 为纯文件名（无"(循环)"后缀，循环态用 `loop=1` 表达）；播放态与通话态无关，无通话照实报 playing | 双 |

### 1.3 媒体（门B）

| 端点 | 参数 | 说明 | 通道 |
|---|---|---|---|
| `GET /media/track` | `title,artist,album,duration`(秒) | 推单曲元数据（不真出声，可伪造） | 双 |
| `GET /media/playlist` | `text`(多行/;分隔) | 写播放列表；**空 text=查询当前列表**。行格式：`标题\|歌手\|专辑\|秒\|乐库文件名`（后三列可选，`#` 注释） | 双 |
| `GET /media/play` `/pause` `/next` `/prev` | — | 播放控制（空列表 next/prev 返回明确错误） | 双 |
| `GET /media/jump` | `idx`（0 起，**必填**） | 跳到列表第 idx 首，按当前播放/暂停态就位 | 双 |
| `GET /media/seek` | `pos`（秒，**必填**） | 拖进度；缺参报错不静默跳 0 | 双 |
| `GET /media/status` | — | 一行媒体状态（GUI 进度轮询用） | HTTP |
| `GET /media/silence` | `on=0/1` | 静音流模式开关（暂停态置位不立即起流） | 双 |
| `GET /media/autoadvance` | `on=0/1` | 单曲播完自动连播开关（默认开） | 双 |
| `POST /media/upload` | `name`，body=文件字节 | 上传音频到乐库（超时按体积放宽） | HTTP |
| `GET /media/files` | — | 乐库列表 `名字\|KB` | 双 |
| `GET /media/del` | `name` | 删乐库文件 | 双 |
| `GET /media/diag` | — | 音质排查：实际输出设备+SCO+绑定情况 | 双 |

### 1.4 蓝牙

| 端点 | 参数 | 说明 | 通道 |
|---|---|---|---|
| `GET /bt/state` | — | 开关+已配对列表+`[A2DP已连]/[HFP已连]` 标记 | 双 |
| `GET /bt/scan` `/scan-result` | — | 启动扫描 / 取结果 `名字\|MAC\|rssi` | 双 |
| `GET /bt/bond` `/unpair` | `mac` 或 `name` | 配对 / 解配 | 双 |
| `GET /bt/disconnect` | `mac`(可空=当前已连设备), `force=1` | 断开保持配对；断链/回链落 ACL/A2DP/HFP 事件 | 双 |
| `GET /bt/reconnect` | `mac`,`fallback=0/1` | 手机侧主动回连 | 双 |
| `GET /bt/allow-car` | `mac`(可空=第一台 HFP 已连设备) | 授权车机 PBAP/MAP（拉通讯录前提） | 双 |
| `GET /bt/name` | `name` | 查看/改蓝牙名 | 双 |
| `GET /bt/enable` | `on=0/1` | 开关蓝牙 | 双 |

### 1.5 联系人（PBAP）

| 端点 | 参数 | 说明 | 通道 |
|---|---|---|---|
| `GET /contacts/load` | `count`(默认100), `prefix` | **异步**批量生成（联系人00001/13800000001...） | 双 |
| `GET /contacts/import` | `text`（行格式 `姓名\|号码` 或 `姓名,号码`） | **异步**导入自定义 | 双 |
| `GET /contacts/clear` | — | **异步**清空 vphone 账号联系人（不动手机原有） | 双 |
| `GET /contacts/count` | — | `vphone=N total=N` | HTTP |
| `GET /contacts/status` | — | `idle` 或 `批量任务进行中 done/total` | HTTP |

> **异步批任务纪律**：clear/load/import 发出后要轮询 `/contacts/status` 到 `idle` 才能发下一条，
> 否则被拒"批量任务进行中"。Python 侧 `contacts_load/import/clear(wait=True)` 已内置轮询。

## 2. Python 库（VPhone 方法）

> 本节所有方法均为 **keyword-only**：调用必须写参数名，如 `vp.dial(number="10086")`。
> 查询类返回结构化数据，动作类返回手机端结果文本（中文），失败抛 `VPhoneError`。

### 2.1 构造与生命周期

| 方法 | 说明 |
|---|---|
| `VPhone(serial=None, base=None, port=18800, adb="adb", autostart=True, wait_timeout=15)` | base 直填 `http://手机IP:8800` 走 WiFi；否则 adb forward。autostart=构造即拉服务。多设备：serial 或环境变量 `VPHONE_SERIAL`。**wait_timeout**=三层断言的默认超时（`timeout=None` 时用它），慢台架构造时调一次全局生效 |
| `install(apk=None)` | adb install -r + 拉起 + forward + 补授权；apk 缺省按 指定路径→exe 内嵌→仓库 `apk/`→pip 包 `vphone_data`→gradle 产物 顺序查找 |
| `launch()` | 拉 App 界面+服务（装完 stopped 态必备，幂等） |
| `grant_perms()` / `enable_account()` / `enable_autoconfirm()` | 运行时授权 / 电话账号启用+默认去电 / 配对弹窗自动确认（无障碍） |
| `start()` / `status()` | 拉起服务 / 全量状态 |

### 2.2 门C 电话

`incoming(number="13800138000")` `dial(number="10086")` `answer()` `hangup()`
`hold(on=True)` `dtmf(key)` `audio_bt()`
`set_auto_outgoing(on=True)` `call_audio(name, loop=False)` `call_audio_stop()`

通话音频进度不设包装方法（上位机直接解析原文一行）：

```python
vp.http("/call/audio-status")          # 'callAudio=off' 或 'callAudio=playing name="xx.mp3" pos=12s dur=35s loop=0'
vp.http_post("/call/audio", {"seek": 30})   # 拖进度；未播放返回错误文本
```

### 2.3 门B 媒体

| 方法 | 说明 |
|---|---|
| `set_track(title, artist="", album="", dur=240)` | 单曲元数据（伪造） |
| `playlist(text=None, file=None)` | 写列表（都不给=查询）；`playlist_get()` 返回结构化 list |
| `play() pause() next() prev()` | 基本控制 |
| `media_jump(idx=0)` / `media_seek(sec=0)` | 跳曲（0 起）/ 拖进度 |
| `silence(on=True)` / `autoadvance(on=True)` | 静音流 / 自动连播开关 |
| `upload_audio(path, name=None)` | 电脑音频→乐库（POST，超时按体积放宽） |
| `list_audio()` → `[{'name','kb'}]`、`del_audio(name)` | 乐库管理 |
| `playlist_audio(names, meta=None, autoplay=True)` | 用乐库文件构造真实音频列表并播放 |
| `play_audio_files(paths, autoplay=True)` | 一步到位：上传→列表→播放 |
| `media_status()` / `media_diag()` | 一行状态 / 音质排查 |

### 2.4 蓝牙

`bt_state()` `bt_scan(timeout=20)`→`[{'name','mac','rssi'}]`（按强度降序）
`bt_bond(target)` `bt_unpair(target)`（target=MAC 或名字片段）
`bt_disconnect(mac=None, force=False)` `bt_reconnect(target=None, fallback=True)`
`bt_allow_car(target=None)` `bt_name(name=None)` `bt_enable(on=True)`

### 2.5 联系人

`contacts_load(count=10000, prefix="联系人", wait=True, timeout=300)`
`contacts_import(text=None, file=None, wait=True)` `contacts_clear(wait=True)`
`contacts_count()`

### 2.6 事件与断言：三层 API（首选）

**设计回答"断言是否阻塞、是否超时"**：四层语义各司其职，全部必带超时、**不支持无限等待**。

| 层 | API | 阻塞 | 超时 | 行为 |
|---|---|---|---|---|
| 快照查询 | `events(since=0) -> list[VEvent]` | 否 | — | 一次拉取，立即返回 |
| 等待原语 | `wait_event(...) -> VEvent \| None` | 是 | 必有 | 超时返回 `None`（可组合自定义流程） |
| 断言版 | `expect_event(..., because="") -> VEvent` | 是 | 必有 | 超时抛 `VPhoneTimeoutError`，消息带**过滤条件+because+窗口内实际发生的最近 8 条事件**（失败现场直接可查） |
| 反向断言 | `expect_no_event(..., within=3)` | 是（观察窗口） | 必有 | 窗口内**命中即抛**（"不应出现的事出现了"） |

**超时规则**：`timeout=None`（默认）= 用构造时的 `wait_timeout`（默认 15s）；`timeout=0` = 只查一次快照立即返回（非阻塞"探一眼"）。

**为什么 expect 和 wait 分两层**：裸 `assert vp.wait_event(...)` 有两个坑——`python -O` 会剥掉
assert、忘写 assert 时 None 被静默吞掉。`expect_event` 抛异常漏不掉，且异常消息自带现场。
自定义流程（如"等到就取消"）仍可用原语组合。

| 方法 | 说明 |
|---|---|
| `events(since=0)` | 快照：`list[VEvent]`（结构化字段，见 2.7） |
| `events_raw(since=0)` | 原始 JSON 信封 `{'last':N,'events':[...]}`（GUI/轮询器用） |
| `event_watermark()` | 取当前水位 N（= 已发生的最大事件 id） |
| `wait_event(evt_type=None, detail=None, src=None, timeout=None, since=None)` | 等待原语，命中返回 `VEvent`，超时 `None` |
| `expect_event(evt_type=None, detail=None, src=None, timeout=None, since=None, because="")` | 断言版，超时抛 `VPhoneTimeoutError` |
| `expect_no_event(evt_type=None, detail=None, src=None, within=None)` | 反向断言：观察窗口内没出现才算过 |
| `start_events(on_event=None)` / `stop_events()` | logcat 文本流兜底通道（GUI 用）；on_event 不传则保留已有回调 |
| `wait_for(pattern, timeout=10, regex=False)` | logcat 文本匹配等待（兼容保留） |
| `wait_bonded(target, timeout=25)` / `wait_profile(target, profile, timeout=25)` | 等配对 / 等 profile 连上（`[A2DP已连]`） |

**匹配规则**：`evt_type` 精确匹配（str，或 tuple 任一命中）；`src` 精确；`detail` 子串。三者与运算。

**`since` 水位语义（重要）**：不传 since = 先取当前水位只等"今后"的事件（推荐，不被历史
误命中）；传 N = 从 id>N 回放（先发命令再断言、不想漏命令瞬间事件时用，
`mark = vp.event_watermark()` → 命令 → `expect_event(..., since=mark)`）。

### 2.7 VEvent 事件对象（字段契约）

```python
@dataclasses.dataclass(frozen=True)
class VEvent:
    id: int          # 事件序号（水位/回放用）
    ts: int          # 毫秒时间戳
    type: str        # 事件类型，见 §4 总表
    src: str         # car/cmd/app/bt/sys
    detail: str      # 人读明细（中文）
    # .time  -> "14:03:22"（本地时间）
    # str(e) -> "#12 14:03:22 CAR_ANSWER [car] 车机按了【接听】..."
```

**契约：字段只增不改名。** 测试脚本按属性访问（`e.type` / `e.src` / `e.detail`），不再
`e["detail"]` 取 dict——将来加字段不破坏既有脚本。异常类：`VPhoneTimeoutError(VPhoneError)`，
既有 `except VPhoneError` 不受影响。

## 3. CLI（vphone_ctl.py）

```
python vphone_ctl.py [--serial S] [--base URL] [--port 18800] <cmd> [参数]
vphone <cmd> ...                        # pip 安装后等价命令(装到 PATH)
```

| 分组 | 命令 |
|---|---|
| 部署 | `start` `grant-perms` `enable-account` `enable-autoconfirm` `install [apk路径|--file]` `launch` |
| 事件 | `events`（实时流 Ctrl+C 退出） `wait-event TYPE [TYPE...] [--detail 子串] [--timeout 30]` |
| 电话 | `incoming [--num]` `dial` `answer` `hangup` `hold [--on]` `dtmf --key` `audio-bt` `call-audio --name [--loop]` `call-audio-stop` `auto-outgoing [--on]` |
| 媒体 | `track --title --artist --album --dur` `play` `pause` `next` `prev` `jump <idx>` `seek <秒>` `silence [--on]` `autoadvance [--on]` `playlist [--text|--file]` `playlist-get` `upload 文件...` `files` `del --name` `play-audio 文件...` `media-diag` |
| 联系人 | `contacts-load [--count] [--prefix]` `contacts-file --file` `contacts-clear` `contacts-count` |
| 蓝牙 | `bt-state` `bt-name [--name]` `bt-enable [--on]` `scan` `scan-result` `bond --mac` `unpair --mac` `disconnect [--mac] [--force]` `reconnect [--mac]` `allow-car [--mac]` |

## 4. 事件类型总表（EventLog → /events）

`src`: `car`=车机按键回流 · `cmd`=PC 指令 · `app`=App 自身 · `bt`=蓝牙栈 · `sys`=系统 Telecom 侧

| type | src 通常 | 含义 |
|---|---|---|
| `CAR_PLAY` / `CAR_PAUSE` / `CAR_STOP` | car | 车机/手机通知栏媒体键 |
| `CAR_NEXT` / `CAR_PREV` | car | 车机切歌键 |
| `CAR_SEEK` | car/cmd | 拖进度（含 FF/RW ±10s 翻译） |
| `CAR_ANSWER` / `CAR_REJECT` / `CAR_HANGUP` | car | 车机电话键 |
| `CAR_DIAL` | car | 车机发起拨号（ATD） |
| `CAR_HOLD` / `CAR_DTMF` | car | 车机保持 / DTMF |
| `RING_IN` / `CALL_ACTIVE` / `CALL_HELD` / `CALL_ENDED` | app/sys | 通话状态机迁移 |
| `CMD_PLAY/PAUSE/NEXT/PREV/...` | cmd | PC 指令回显（断言"指令已生效"用） |
| `MEDIA_TRACK_END` / `MEDIA_UPLOAD` / `MEDIA_SCO_RELEASED` | app/cmd | 播完 / 上传 / SCO 释放 |
| `CALL_AUDIO_END` | app | 通话音频自然播完（loop=0；detail 带文件名，手动 stop 不发） |
| `BT_ACL_CONNECTED/DISCONNECTED` `BT_A2DP_CONNECTED/DISCONNECTED` `BT_HFP_CONNECTED/DISCONNECTED` | bt | 链路迁移（断连/回连测试主力） |
| `BT_DISCONNECT_FAILED` | app | 断开指令未成功（设备仍连接，断言别误判） |
| `BT_BONDED` / `BT_BOND_FAILED` / `BT_SCAN_*` | bt/app | 配对/扫描 |

## 5. 断言示例（expect_event 三层用法）

```python
# ① expect_event: 车机接听(默认超时=wait_timeout)。失败直接抛, 消息带现场, 漏不掉
vp.incoming(number="13800138000")
e = vp.expect_event(evt_type="CAR_ANSWER", because="车机应能接听来电")
print(f"{e.time} 车机接了: {e.detail}")
vp.hangup()

# ② 多类型任一命中(tuple) + 只认车机来源 + detail 落点校验
e = vp.expect_event(evt_type=("CAR_NEXT", "CAR_PREV"), src="car", timeout=20)
e = vp.expect_event(evt_type="CAR_SEEK", src="car", detail="120s", timeout=20)

# ③ 水位回放: 先记水位→发命令→从水位断言, 命令瞬间的事件不漏
mark = vp.event_watermark()
vp.next()
vp.expect_event(evt_type=("CMD_NEXT", "CAR_NEXT"), since=mark, timeout=5)

# ④ 反向断言: 静音流开启后 5s 内车机不应出现播完事件
vp.silence(on=True); vp.play()
vp.expect_no_event(evt_type="MEDIA_TRACK_END", within=5)

# ⑤ 断连→回连全链路(链路事件)
vp.bt_disconnect()                                              # 保配对断开
vp.expect_event(evt_type="BT_A2DP_DISCONNECTED", timeout=15)
vp.bt_reconnect()
assert vp.wait_profile("CARKIT-1", "A2DP", timeout=25)             # 轮询 /bt_state 标记

# ⑥ timeout=0 探一眼(非阻塞) / wait_event 组合自定义流程
if vp.wait_event(evt_type="CAR_PLAY", timeout=0): ...
r = vp.wait_event(evt_type="RING_IN", timeout=3)                # None 时不抛, 自己决定
```

超时异常长这样（直接指出等的是什么、实际发生了什么）：

```
vphone_lib.VPhoneTimeoutError: 等事件超时(20s): evt_type=('CAR_ANSWER',) 因为: 车机应能接听来电
  等待窗口内最近 8 条事件:
    #41 14:03:19 CMD_INCOMING [cmd] PC注入来电 13800138000
    #42 14:03:19 RING_IN [sys] 来电振铃 ...
```

## 6. 典型错误与排查

| 现象 | 原因/处理 |
|---|---|
| `TypeError: ... takes 1 positional argument but ...` | v0.6.0 起全 kwargs：改成 `vp.dial(number="10086")` 写参数名 |
| `VPhoneTimeoutError: 等事件超时` | 断言没等到——看异常里的最近事件定位（指令到底发没发、车机有没有反应） |
| `HTTP 不通[...]` | 服务没起(`launch()`)、forward 没建(`_setup_forward` 自动)、USB 断 |
| `adb ... 超时(30s)` | 设备掉线/USB 异常；`_adb` 全链路有超时不会挂死 |
| `需要指定手机序列号` | 多设备在线 → `--serial` 或 `VPHONE_SERIAL`（是手机不是车机板） |
| `设备在线但不可用(unauthorized)` | 看手机 USB 调试授权弹窗 |
| `批量任务进行中` | 联系人异步批任务没跑完 → 等 `/contacts/status` idle（lib 的 wait=True 已内置） |
| `广播已发但无 [adb] 日志` | App 处于 stopped 态 → `launch()` 一次 |
| 扫描无结果 | 手机定位服务没开 / 权限没授（bt_scan 报错已提示） |
