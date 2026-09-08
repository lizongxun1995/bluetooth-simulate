# vphone 接口文档

> 面向写测试脚本的人。三件套：**HTTP 端点**（手机 :8800，经 adb forward 或 WiFi）、
> **Python 库** `vphone/vphone_lib.py`（推荐入口）、**CLI** `vphone/vphone_ctl.py`。
> GUI（vphone_gui.py / 打包 exe）按钮与库方法一一对应，不单独列表。
> 架构与线程模型见 [vphone-TECH.md](vphone-TECH.md)。

## 0. 快速上手

```python
from vphone_lib import VPhone          # cd vphone 或把 vphone 目录加进 sys.path
vp = VPhone(serial="SN_PHONE_A") # 单设备时可省 serial; 多设备必填或环境变量 VPHONE_SERIAL

vp.incoming("13800138000")             # 注入来电(车机+手机同时响)
assert vp.wait_event("CAR_ANSWER", timeout=15)   # 有人在车机上按了接听
vp.hangup()

vp.play_audio_files(["D:/music/a.mp3", "D:/music/b.mp3"])  # 上传+列表+播放(车机真出声)
assert vp.wait_event(("CAR_NEXT", "CAR_PREV"), timeout=15) # 车机上切了歌

vp.contacts_load(10000)                # 1w 联系人 → 车机 PBAP 通讯录压测
```

所有方法返回**手机端结果文本（中文）**；失败抛 `VPhoneError`（含原始原因）。

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
| `GET /call/audio` | `name`,`loop=0/1` 或 `stop=1` | 通话中播放乐库音频（SCO 下行模拟对端说话） | 双 |

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

### 2.1 构造与生命周期

| 方法 | 说明 |
|---|---|
| `VPhone(serial=None, base=None, port=18800, adb="adb", autostart=True)` | base 直填 `http://手机IP:8800` 走 WiFi；否则 adb forward。autostart=构造即拉服务。多设备：serial 或环境变量 `VPHONE_SERIAL` |
| `install(apk=None)` | adb install -r + 拉起 + forward + 补授权；apk 缺省取 `vphone/apk/`（exe 内嵌优先） |
| `launch()` | 拉 App 界面+服务（装完 stopped 态必备，幂等） |
| `grant_perms()` / `enable_account()` / `enable_autoconfirm()` | 运行时授权 / 电话账号启用+默认去电 / 配对弹窗自动确认（无障碍） |
| `start()` / `status()` | 拉起服务 / 全量状态 |

### 2.2 门C 电话

`incoming(number)` `dial(number)` `answer()` `hangup()` `hold(on)` `dtmf(key)` `audio_bt()`
`set_auto_outgoing(on)` `call_audio(name, loop=False)` `call_audio_stop()`

### 2.3 门B 媒体

| 方法 | 说明 |
|---|---|
| `set_track(title, artist="", album="", dur=240)` | 单曲元数据（伪造） |
| `playlist(text=None, file=None)` | 写列表（都不给=查询）；`playlist_get()` 返回结构化 list |
| `play() pause() next() prev()` | 基本控制 |
| `media_jump(idx)` / `media_seek(sec)` | 跳曲（0 起）/ 拖进度 |
| `silence(on)` / `autoadvance(on)` | 静音流 / 自动连播开关 |
| `upload_audio(path, name=None)` | 电脑音频→乐库（POST，超时按体积放宽） |
| `list_audio()` → `[{'name','kb'}]`、`del_audio(name)` | 乐库管理 |
| `playlist_audio(names, meta=None, autoplay=True)` | 用乐库文件构造真实音频列表并播放 |
| `play_audio_files(paths, autoplay=True)` | 一步到位：上传→列表→播放 |
| `media_status()` / `media_diag()` | 一行状态 / 音质排查 |

### 2.4 蓝牙

`bt_state()` `bt_scan(timeout=20)`→`[{'name','mac','rssi'}]`（按强度降序）
`bt_bond(target)` `bt_unpair(target)`（target=MAC 或名字片段）
`bt_disconnect(mac=None, force=False)` `bt_reconnect(target=None, fallback=True)`
`bt_allow_car(target=None)` `bt_name(name=None)` `bt_enable(on)`

### 2.5 联系人

`contacts_load(count=10000, prefix="联系人", wait=True, timeout=300)`
`contacts_import(text=None, file=None, wait=True)` `contacts_clear(wait=True)`
`contacts_count()`

### 2.6 事件与断言（首选）

| 方法 | 说明 |
|---|---|
| `events(since=0)` | 拉 JSON：`{'last':N,'events':[...]}` |
| `wait_event(evt_type=None, detail=None, src=None, timeout=15, since=None)` | 阻塞等事件；命中返回 dict，超时 None（直接 assert）。evt_type 可传 str 或 tuple |
| `start_events(on_event=None)` / `stop_events()` | logcat 文本流兜底通道（GUI 用）；on_event 不传则保留已有回调 |
| `wait_for(pattern, timeout=10, regex=False)` | logcat 文本匹配等待（兼容保留） |
| `wait_bonded(target, timeout=25)` / `wait_profile(target, profile, timeout=25)` | 等配对 / 等 profile 连上（`[A2DP已连]`） |

**`since` 水位语义（重要）**：`wait_event` 不传 since = 先取当前水位只等"今后"的事件
（推荐，不被历史误命中）；传 N = 从 id>N 回放（先发命令再断言、不想漏命令瞬间事件时用）。

## 3. CLI（vphone_ctl.py）

```
python vphone_ctl.py [--serial S] [--base URL] [--port 18800] <cmd> [参数]
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
| `BT_ACL_CONNECTED/DISCONNECTED` `BT_A2DP_CONNECTED/DISCONNECTED` `BT_HFP_CONNECTED/DISCONNECTED` | bt | 链路迁移（断连/回连测试主力） |
| `BT_DISCONNECT_FAILED` | app | 断开指令未成功（设备仍连接，断言别误判） |
| `BT_BONDED` / `BT_BOND_FAILED` / `BT_SCAN_*` | bt/app | 配对/扫描 |

## 5. 断言示例

```python
# 车机接听(15s 内)
vp.incoming("13800138000")
assert vp.wait_event("CAR_ANSWER", timeout=15), "车机未接听"

# 车机切歌(下一曲或上一曲都算)
assert vp.wait_event(("CAR_NEXT", "CAR_PREV"), timeout=15)

# 只认车机侧来源的拖进度, 且落到 120s
e = vp.wait_event("CAR_SEEK", src="car", timeout=20)
assert e and "120s" in e["detail"]

# 断连→回连全链路(链路事件)
vp.bt_disconnect()                                    # 保配对断开
assert vp.wait_event("BT_A2DP_DISCONNECTED", timeout=15)
vp.bt_reconnect()
assert vp.wait_profile("CARKIT-1", "A2DP", timeout=25)   # 轮询 /bt/state 标记

# 先发命令再断言(不漏命令瞬间事件): 传 since 水位
wm = vp.events()["last"]
vp.next()
assert vp.wait_event(("CMD_NEXT", "CAR_NEXT"), since=wm, timeout=5)
```

## 6. 典型错误与排查

| 现象 | 原因/处理 |
|---|---|
| `HTTP 不通[...]` | 服务没起(`launch()`)、forward 没建(`_setup_forward` 自动)、USB 断 |
| `adb ... 超时(30s)` | 设备掉线/USB 异常；`_adb` 全链路有超时不会挂死 |
| `需要指定手机序列号` | 多设备在线 → `--serial` 或 `VPHONE_SERIAL`（是手机不是车机板） |
| `设备在线但不可用(unauthorized)` | 看手机 USB 调试授权弹窗 |
| `批量任务进行中` | 联系人异步批任务没跑完 → 等 `/contacts/status` idle（lib 的 wait=True 已内置） |
| `广播已发但无 [adb] 日志` | App 处于 stopped 态 → `launch()` 一次 |
| 扫描无结果 | 手机定位服务没开 / 权限没授（bt_scan 报错已提示） |
