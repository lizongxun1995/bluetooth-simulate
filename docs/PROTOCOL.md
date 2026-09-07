# 串口协议参考(PC ↔ ESP32,2Mbaud,8N1)

## 帧格式

```
偏移  长度  内容
0     2     同步字 0xA5 0x5A
2     1     TYPE: 0x01=CTRL(JSON 控制) 0x02=AUDIO(音频)
3     1     保留(恒 0)
4     2     LEN(小端,负载长度)
6     2     SEQ(按 TYPE 独立递增)
8     LEN   负载
8+LEN 2     CRC16-CCITT(小端,覆盖 TYPE..负载,init 0xFFFF)
```

- CTRL 负载为 UTF-8 JSON(单帧 ≤4096B):
  - 请求 `{"id":1,"cmd":"music.play","args":{}}`
  - 响应 `{"id":1,"ok":true,"result":{}}` / `{"id":1,"ok":false,"error":"..."}`
  - 事件 `{"evt":"bt.a2dp.state","data":{"state":"connected"}}`
- AUDIO 负载头 2 字节:`[codec][flags]` + 数据。
  codec:`0`=PCM s16le、`1`=IMA-ADPCM、`0xFF`=沿用 audio.open 会话设置;
  flags bit0 = EOS(本流最后一帧)。

## 音频数据模型

- 曲库/语音文件存电脑,播放时经串口**实时推流**(MP3 由 PC 侧 ffmpeg 先解码
  为 PCM,再 IMA-ADPCM 4:1 压缩后推流,板端解码)。
- 流控(信用点):固件每 50ms 上报 `audio.buffer {"free": N}`(绝对空闲字节);
  PC 以 `audio.open` 返回的 `buffer_size` 为初始额度,每发一帧扣减,
  低于帧长时等待事件。`audio.underrun` 事件表示固件侧缓冲异常。

## 命令清单(v1)

| 命令 | args | result/说明 |
|---|---|---|
| sys.info | | 固件版本/芯片/蓝牙名/缓冲大小 |
| sys.reset | | 恢复出厂并重启 |
| sys.echo | `{msg}` | `{msg}` 回显(联调用) |
| bt.set_name | `{name}` | 设蓝牙名(持久化) |
| bt.set_discoverable | `{mode: none\|conn\|disc\|disc_conn, timeout_s}` | 可见/可连 |
| pair.mode | `{mode: auto\|manual\|reject\|wrong_pin\|timeout}` | 配对策略,后三种=配对失败模拟 |
| pair.confirm | `{accept}` | manual 模式下确认/拒绝 |
| pair.policy | `{auto_accept, auto_reconnect}` | 自动接受/上电回连 |
| pair.list | | 已配对设备列表 |
| pair.remove | `{mac}` | 删除绑定 |
| conn.connect | `{mac, profiles:[a2dp,hfp]}` | PC 主动连接车机 |
| conn.disconnect | `{profile: a2dp\|hfp\|all}` | 断开 |
| conn.status | | 全量状态 |
| audio.open | `{sink: a2dp\|hfp, rate, channels, codec}` | 开音频会话,返回 buffer_size/free |
| audio.stop | | 关会话 |
| music.play / pause / resume / stop | | A2DP 传输控制 |
| avrcp.metadata | `{title, artist, album, duration_ms, track_no, total_tracks}` | 推送曲目信息到车机 |
| hfp.incoming | `{number}` | 模拟来电 |
| hfp.ring | `{on}` | 铃声开/关(来电后 2s 周期自动 RING) |
| hfp.answer | | 手机侧接听 |
| hfp.hangup | | 挂断 |
| hfp.dial | `{number}` | 模拟手机拨出 |
| hfp.voice | `{on}` | 通话中语音推流开关 |
| net.wifi_sta | `{ssid, password}` | STA 接入(非阻塞,结果经 net.sta 事件) |
| net.wifi_scan | | 同步扫描,阻塞 1.5~3s,返回 `{aps:[{ssid,rssi,auth}]}` |
| net.ap_start | `{ssid, password}` | 开热点(APSTA 模式,可同时 STA) |
| net.ap_stop | | 关 WiFi |
| net.status | | `{sta:{ssid,connected,ip}, ap:{ssid}, started}` |
| net.tether_pan / lyrics.push | — | 未实现:`not_implemented:*`(Phase 2/3) |

## 事件清单

| 事件 | data | 触发 |
|---|---|---|
| bt.a2dp.state | `{state: connecting\|connected\|disconnecting\|disconnected}` | A2DP 链路 |
| bt.hfp.state | `{state, number, call_setup, call_active, audio_on}` | HFP/通话状态机 |
| bt.conn | `{profile, state}` | 单协议链路事件 |
| bt.scan_mode | `{mode}` | 可见性变化 |
| pair.request | `{mac, method: ssp_confirm\|pin, passkey}` | 车机发起配对 |
| pair.result | `{mac, ok, reason}` | 配对完成/失败(rejected/wrong_pin/timeout) |
| avrcp.cmd | `{cmd: play\|pause\|next\|prev\|vol_up\|vol_down\|mute\|vol_set, value?}` | 车机媒体按键 |
| hfp.at | `{at: ATA\|AT+CHUP\|ATD\|AT+VTS, arg}` | 车机 AT 命令 |
| hfp.ring | `{}` | 来电铃声周期 |
| audio.buffer | `{free}` | 信用点(50ms 周期,无条件上报——不能"变化才发",否则额度用尽且缓冲清空时死锁) |
| audio.underrun | `{}` | 下溢(流控异常时) |
| music.ended | `{}` | 一曲自然播完(EOS 且缓冲耗尽) |
| sys.ready / sys.error | | 启动/异常 |

## ADPCM 块格式(与固件 ima_adpcm.c / PC codec_adpcm.py 一致)

- 每块 `BLOCK_SAMPLES=1023`(每声道,奇数);
- 块内按声道分段,每段:`[predictor s16 LE(=首样本值)][step_index u8][pad u8]`
  + nibble 数据(2 样本/字节,低 nibble 在前),编码第 1..1022 个样本;
- IMA 标准 step 表(90 级)与 index 调整表。
