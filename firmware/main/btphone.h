/* btphone 固件公共声明 */
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

#define BTPHONE_FW_VERSION "0.3.8"

/* WiFi 开关:Phase 2 单独做,当前关闭,把 ~43KB 启动内存让给 BT 协议栈。
 * 恢复:改 1 后重编译(wifi_nat.c/net.* 命令代码保留,无需改动)。 */
#define BTPHONE_WIFI 0
#define BTPHONE_CHIP_DESC  "ESP32 (classic BT, dual-mode)"

/* 蓝牙地址以 6 字节表示 */
typedef uint8_t bda_t[6];

/* -------- 配对策略 -------- */
typedef enum {
    PAIR_AUTO = 0,    /* 自动接受 */
    PAIR_MANUAL,      /* 等 PC confirm */
    PAIR_REJECT,      /* 拒绝 → 配对失败 */
    PAIR_WRONG_PIN,   /* 回错误 PIN → 配对失败(传统 PIN 场景) */
    PAIR_TIMEOUT,     /* 不响应 → 配对超时失败 */
} pair_mode_t;

void bt_gap_set_pair_mode(pair_mode_t mode);
pair_mode_t bt_gap_get_pair_mode(void);
void bt_gap_confirm(bool accept);

/* -------- 连接管理 -------- */
void bt_gap_get_status_json(char *out, size_t len);

/* -------- 音频会话 -------- */
typedef enum { AUDIO_SINK_A2DP = 0, AUDIO_SINK_HFP } audio_sink_t;
typedef enum { AUDIO_CODEC_PCM = 0, AUDIO_CODEC_ADPCM } audio_codec_t;

bool audio_open(audio_sink_t sink, int rate, int channels, audio_codec_t codec);
void audio_close(void);
void audio_set_playing(bool playing);
bool audio_is_playing(void);
bool audio_pipeline_ready(void); /* 音频缓冲已分配(首次 audio.open 成功后为真) */
uint32_t audio_position_ms(void);
size_t audio_stream_buffer_size(void);
size_t audio_stream_free(void);
/* A2DP 音源拉取数据(由 a2dp_source 数据回调调用) */
int audio_pull_a2dp(uint8_t *buf, int32_t len);
/* HFP 语音拉取数据(由 hfp_ag 音频回调调用,8k/16k 单声道) */
int audio_pull_hfp(uint8_t *buf, int32_t len);
/* UART/Flash 数据入口:送入解码管线 */
void audio_consume_stream(const uint8_t *data, size_t len, bool eos);

/* -------- AVRCP -------- */
void avrcp_send_metadata(const char *title, const char *artist, const char *album,
                         long duration_ms, long track_no, long total_tracks);
void avrcp_send_track_change(void);
void avrcp_send_play_status(long pos_ms, bool playing);
bool avrcp_rc_connected(void);

/* -------- HFP -------- */
void hfp_incoming(const char *number);
void hfp_ring(bool on);
void hfp_answer(void);
void hfp_hangup(void);
void hfp_dial(const char *number);
bool hfp_call_active(void);

/* -------- GAP(配对/连接管理) -------- */
const char *bt_gap_get_name(void);
void bt_gap_set_name(const char *name);
void bt_gap_set_scan_mode_str(const char *mode, int timeout_s); /* none|conn|disc|disc_conn */
esp_err_t bt_gap_set_pair_mode_str(const char *mode);           /* auto|manual|reject|wrong_pin|timeout */
void bt_gap_set_auto_accept(bool on);
bool bt_gap_get_auto_accept(void);
void bt_gap_set_auto_reconnect(bool on);
bool bt_gap_get_auto_reconnect(void);
int  bt_gap_get_bonded(bda_t *list, char names[][64], int max);
esp_err_t bt_gap_remove_bond_str(const char *mac);
void bt_gap_get_status_json(char *out, size_t len);
void bt_gap_factory_reset(void);
/* 信号测量:记最近 ACL 对端(配对/A2DP 回调喂入),bt.rssi 查询用 */
void bt_gap_note_peer(const bda_t bda);
void bt_gap_note_peer_connected(const bda_t bda); /* 同时持久化为回连对象 */
bool bt_gap_peer_valid(void);
void bt_gap_query_rssi(void); /* 异步:结果以 bt.rssi 事件上报 */
/* profile 自动连接:配对成功/上电回连时由 GAP 调度(延迟发起,避开回调上下文) */
void bt_gap_schedule_profile_connect(const bda_t bda, int delay_ms);
void bt_gap_boot_reconnect(void); /* 上电:按 NVS last_peer 回连(受 auto_reconnect 策略控制) */

/* -------- A2DP Source -------- */
esp_err_t a2dp_connect(const bda_t bda);
void a2dp_disconnect(void);
void a2dp_start(void);    /* MEDIA_CTRL_START */
void a2dp_suspend(void);  /* MEDIA_CTRL_STOP/SUSPEND */
bool a2dp_is_connected(void);
bool a2dp_is_busy(void);         /* connecting/disconnecting */
const char *a2dp_state_str(void);

/* -------- HFP AG -------- */
void hfp_connect(const bda_t bda);
void hfp_disconnect(void);
void hfp_set_voice_out(bool on);
bool hfp_voice_out(void);
bool hfp_call_active(void);
bool hfp_slc_connected(void);

/* -------- 模块初始化 -------- */
void bt_gap_init(void);
void a2dp_source_init(void);
void avrcp_tg_init(void);
void hfp_ag_init(void);

uint32_t btphone_rtc_boot_count(void);
bool btphone_bt_up(void);
