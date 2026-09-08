# vphone 开发文档（任务跟进）

> 本文档跟任务、挂账、验收清单。改代码前先读「更新规则」。
> 架构见 [vphone-TECH.md](vphone-TECH.md)，接口见 [vphone-API.md](vphone-API.md)。

## 0. 台架与环境快照

| 项 | 值 |
|---|---|
| 手机（主力） | HUAWEI TEL-AN00a / EMUI / Android 10，serial `SN_PHONE_A` |
| 副设备 | DEVBOARD_1 车机板 / Android 11 / com.example.launcher，serial `0x123456789EF`（已装 vphone，未深测） |
| 车机 | CARKIT-1，MAC `00:11:22:33:44:55` |
| USB 约束 | **手机与车机板都挂 hub，不单独占主机口（防烧主板）**；多设备时 adb 命令必须 `-s` |
| 构建链 | Gradle 8.7 + JDK21 `C:\Program Files\Android\openjdk\jdk-21.0.8`；命令见 §5 |
| git | Gitea `http://git.internal.example/bluetooth-simulate.git` main；`资料/` 永不入库 |

## 1. 第六轮（稳固轮）任务清单 — 2026-09-08

以「真机保真度」为准绳的状态机理顺 + 全量注释 + 文档。

| # | 任务 | 状态 |
|---|---|---|
| 1 | Kotlin 跨线程同步桥 onMain（双 ticker/双 MediaPlayer/seek 竞争根因） | ✅ |
| 2 | MediaEngine 对齐真机播放器语义（applyTrack 统一换曲+清 seek 窗口、暂停跳曲释放旧播放器、FF/RW 能力宣告、静音流暂停不自起、播完停尾、暂停态延迟补推元数据） | ✅ |
| 3 | CallEngine 对齐真机拨号器语义（onAbort 归零、注入失败回滚、挂断全清理） | ✅ |
| 4 | ContactsEngine CAS 防并发双写；BtEngine 断开如实记失败；广播 extras 全透传；jump/seek 缺参报错 | ✅ |
| 5 | PC 回归 T1-T9 对照基准表（TECH §6） | ✅ 全过 |
| 6 | **静音流供流线程忙循环事故**（16 万行/s 刷爆 logcat + 100% CPU + 并发 logcat 流 EOF）根因修复与验证 | ✅（详见 TECH §5） |
| 7 | vphone_lib.py：start_events 不清回调、_adb 默认超时 30s、http 保留原始异常、broadcast 查 returncode、bt_scan 超时告警、MAC 正则、upload 超时按体积、_setup_forward 幂等、全量 docstring | ✅ |
| 8 | vphone_gui.py：连接代际防重复轮询线程、控件取值全回主线程、push() HTTP 移出主线程、断连 3 次自停提示、双击扫描项绑配对、模块头线程模型 | ✅（无渲染逻辑回归通过；渲染见 §3 首项） |
| 9 | vphone_ctl.py：install 位置参数/--file 对齐 docstring、help 补全、新增 launch/media-diag/playlist-get、分支分组注释 | ✅ |
| 10 | 四份文档（TECH/API/DEV/README）+ NOTES 第六轮 | ✅ 本轮 |
| 11 | APK 同步 `vphone/apk/vphone.apk` + exe 重建（内嵌新 APK） | ✅ |

## 2. 挂账轻微项（暂不修，动到相关代码时顺手清）

| 项 | 位置 | 说明 |
|---|---|---|
| BtEngine receiver 未反注册 | BtEngine | 常驻服务生命周期内无实害 |
| ControlServer 无鉴权 | ControlServer | 测试台架内网可接受；上生产环境必须加 |
| URLDecoder `+` 号语义 | Dispatcher | 查询参数里 `+` 会被解成空格（目前参数无 +） |
| takeFocus 结果过期不重取 | MediaEngine | 焦点被抢后 focused 标志可能失真 |
| focus 监听空实现 | MediaEngine | 丢了焦点不回落状态（真机会暂停） |
| PyInstaller 版本未 pin | build_exe.py | 换机器构建行为漂移风险 |
| 通话音频无实时语音链路 | CallAudioEngine | 设计边界，见 TECH §7 |
| 重装后联系人清空行为不定 | ContactsEngine | 装完先 `/contacts/count` 再决定重灌（GUI 已询问） |

## 3. 待办：车机侧保真度验收清单（需要人/车机在场）

按 TECH §6/§7 逐条在真车机上裁决：

- [ ] **暂停态切歌裁决**（本轮核心疑案）：播放→暂停→车机按下一曲。看事件流：有 `CAR_NEXT` 无切曲 = EMUI 丢键（平台偏差，记录进 §7）；有 `CAR_NEXT` 有 `CMD` 无显示刷新 = 车机显示问题；切曲成功 = 本轮推送竞态修复已解决。三种结果都有结论价值。
- [ ] **车机拖进度裁决**：车机进度条拖动 → 期望 `CAR_SEEK(src=car)` 且手机进度跟随（EMUI 对第三方 session 可能只给 FF/RW 键，已实现 ±10s 翻译）。
- [ ] **杂音对照**：真实音频模式长时间播放 vs 静音流模式，车机侧听感（WakeLock 已加，观察 EMUI 后台限流是否仍致 A2DP 欠载爆音）。
- [ ] **HFP 信号/电量显示**：无 SIM 偏差确认（预期不上报，记录车机实际显示）。
- [ ] **真手机差分对照**：同场景真手机（装 SIM 卡+华为音乐）vs vphone，车机表现逐项对比——保真度的最终度量。
- [ ] **GUI 渲染回归**：本轮 GUI 改动在真实桌面会话点一遍（连接→重复连接→事件流无双份→播放列表管理→断 USB 看自停提示）。沙箱会话 `Text.see()` 等重绘调用会假死，逻辑层已回归通过，渲染层需人工过一遍。

## 4. 部署注意事项（血泪规则）

1. **华为安装确认弹窗**：`adb install -r` 到 TEL-AN00a **每次都可能弹厂商确认框，必须人手点**
   ——安装卡住不表示坏了，**挂着等人在手机旁处理即可，不要反复重试轰炸**。
   重试前先 `am force-stop com.android.packageinstaller` 清掉卡住的安装器。
2. **设备必须挂 USB hub**，不单独占主机口（烧主板风险）；两个设备在线时所有 adb 命令带 `-s`。
3. 联系人异步纪律：clear/load/import 之间轮询 `/contacts/status` 到 idle。
4. 重装 APK 后内存态全复位（播放列表/通话状态），乐库文件与联系人（通常）保留——装完重建列表。
5. 厂商弹窗一律不自动化（input tap 不做：这代是华为，下代未知）。

## 5. 构建/发布流程

```bash
# APK（Git Bash）
cmd //c "set JAVA_HOME=C:\Program Files\Android\openjdk\jdk-21.0.8&& C:\Users\you\AppData\Local\Android\gradle-8.7\bin\gradle.bat -p C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate\vphone assembleDebug --no-daemon --console=plain"
cp vphone/app/build/outputs/apk/debug/app-debug.apk vphone/apk/vphone.apk   # 同步打包产物

# exe（内嵌 apk + adb）
cd vphone && python build_exe.py    # → dist/vphone_gui.exe (~13MB)

# pip wheel（APK 随包分发; 导入名 vphone_lib 不变, 零第三方依赖）
cd vphone && python build_py.py     # → dist_py/vphone-<版本>-py3-none-any.whl
# 版本单一真源 = vphone_lib.__version__（pyproject dynamic attr 引用）; 改版本只改这一处。
# APK 在构建时同步进 vphone_data/（产物已 gitignore, 真源仍是 apk/vphone.apk）。
# 验证(新 venv): pip install 装出的 wheel → from vphone_lib import VPhone;
#   VPhone.default_apk() 指向 site-packages 的 vphone_data/apk/; vphone --help / vphone-gui --help。

# 装机（记得华为确认框）
adb -s SN_PHONE_A shell am force-stop com.android.packageinstaller
adb -s SN_PHONE_A install -r vphone/apk/vphone.apk
python vphone/vphone_ctl.py --serial SN_PHONE_A launch
```

## 6. 更新规则（加端点/事件/命令的三端同步）

1. **HTTP 路由**：`Dispatcher.http()` 的 when 分支（引擎类命令包 `onMain{}`；BtEngine 直调）。
2. **广播别名**：`Dispatcher.alias` 加 `短名 → 路径`。
3. **Python**：`vphone_lib.PATHS` 加短名（分组注释）；需要时加方法+docstring。
4. **CLI**：`vphone_ctl.py` 加分支 + cmd help 字符串 + 顶部用法示例。
5. **事件**：`EventLog` 加常量 + 本文档 §事件表（API 文档）登记 src 语义。
6. **文档**：API 文档对应表格同步；行为类改动补 TECH §6 基准表行。

## 7. 历史轮次索引

详见 `docs/NOTES.md`：一~三轮 vphone 诞生（门B/门C/PBAP/双控制面）、第四轮台架全通
（配对/音乐/来电/通讯录/断连回连）、第五轮 exe 打包与发布、第六轮稳固轮（本轮，含静音流
忙循环事故——TECH §5）。
