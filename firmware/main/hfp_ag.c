/* HFP AG(Audio Gateway = 模拟手机端):呼入/呼出/接听/挂断/通话语音。
 * 基于 ESP-IDF v5.3.2 的 esp_hf_ag_api.h(AG 角色官方支持)。
 *
 * 音频路径:CONFIG_BT_HFP_AUDIO_DATA_PATH_HCI 时,SCO 音频经
 * esp_hf_ag_register_data_callback 的双向回调收发——上行(车机麦克风)
 * 丢弃,下行(我们给车机的语音)由 audio_pipeline 提供。
 * ESP_HF_UNAT_RESPONSE_EVT 透传未知 AT 命令(是 iOS persona 的 XAPL 钩子)。
 */
#include "btphone.h"
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/timers.h"
#include "esp_hf_ag_api.h"
#include "host_link.h"
#include "audio_pipeline.h"

typedef enum {
    CALL_IDLE = 0,
    CALL_INCOMING,
    CALL_OUTGOING,
    CALL_ACTIVE,
} call_state_t;

static call_state_t s_call = CALL_IDLE;
static char s_number[32] = "";
static int s_call_setup = 0; /* 0=无 1=呼入 2=呼出拨号 3=呼出振铃 */
static int s_call_active = 0;
static bool s_voice_out = false;
static bool s_audio_on = false;
static bool s_slc_on = false;
static bda_t s_peer = { 0 };
static bool s_peer_valid = false;
static TimerHandle_t s_ring_timer;

static void emit_call_state(const char *state) {
    char body[160];
    snprintf(body, sizeof(body),
             "{\"state\":\"%s\",\"number\":\"%s\",\"call_setup\":%d,\"call_active\":%d}",
             state, s_number, s_call_setup, s_call_active);
    host_send_event("bt.hfp.state", body);
}

static void set_call(call_state_t st) {
    s_call = st;
    s_call_active = (st == CALL_ACTIVE) ? 1 : 0;
    s_call_setup = (st == CALL_INCOMING) ? 1 :
                   (st == CALL_OUTGOING) ? 2 : 0;
    if (st == CALL_IDLE) s_number[0] = '\0';
    emit_call_state(st == CALL_IDLE ? "idle" : st == CALL_INCOMING ? "incoming" :
                    st == CALL_OUTGOING ? "outgoing" : "active");
    if (s_slc_on && s_peer_valid) {
        /* 同步 CIND 指示给车机 */
        esp_hf_ag_devices_status_indchange(
            s_peer,
            s_call_active ? ESP_HF_CALL_STATUS_CALL_IN_PROGRESS : ESP_HF_CALL_STATUS_NO_CALLS,
            (esp_hf_call_setup_status_t)s_call_setup,
            ESP_HF_NETWORK_STATE_AVAILABLE, 5);
    }
}

static void ring_timer_cb(TimerHandle_t t) {
    (void)t;
    if (s_call == CALL_INCOMING)
        host_send_event("hfp.ring", "{}");
}

/* ---------------- 对上接口(由 host_link 调用) ---------------- */

void hfp_incoming(const char *number) {
    if (!s_slc_on) return;
    strncpy(s_number, number, sizeof(s_number) - 1);
    s_number[sizeof(s_number) - 1] = '\0';
    /* 关闭带内振铃:车机收到 callsetup=1 后自己产生铃声 */
    esp_hf_ag_bsir(s_peer, ESP_HF_IN_BAND_RINGTONE_NOT_PROVIDED);
    set_call(CALL_INCOMING);
    xTimerStart(s_ring_timer, 0);
}

void hfp_ring(bool on) {
    if (on && s_call == CALL_INCOMING)
        xTimerStart(s_ring_timer, 0);
    else
        xTimerStop(s_ring_timer, 0);
}

void hfp_answer(void) {
    if (s_call != CALL_INCOMING && s_call != CALL_OUTGOING) return;
    xTimerStop(s_ring_timer, 0);
    if (s_peer_valid) {
        esp_hf_ag_answer_call(s_peer, 1, 0,
                              ESP_HF_CALL_STATUS_CALL_IN_PROGRESS,
                              ESP_HF_CALL_SETUP_STATUS_IDLE,
                              s_number, ESP_HF_CALL_ADDR_TYPE_UNKNOWN);
        esp_hf_ag_audio_connect(s_peer);
    }
    set_call(CALL_ACTIVE);
}

void hfp_hangup(void) {
    xTimerStop(s_ring_timer, 0);
    if (s_peer_valid) {
        esp_hf_ag_end_call(s_peer, 0, 0,
                           ESP_HF_CALL_STATUS_NO_CALLS,
                           ESP_HF_CALL_SETUP_STATUS_IDLE,
                           s_number, ESP_HF_CALL_ADDR_TYPE_UNKNOWN);
        if (s_audio_on)
            esp_hf_ag_audio_disconnect(s_peer);
    }
    set_call(CALL_IDLE);
}

void hfp_dial(const char *number) {
    if (!s_slc_on) return;
    strncpy(s_number, number, sizeof(s_number) - 1);
    s_number[sizeof(s_number) - 1] = '\0';
    set_call(CALL_OUTGOING);
    if (s_peer_valid) {
        esp_hf_ag_out_call(s_peer, 0, 0,
                           ESP_HF_CALL_STATUS_NO_CALLS,
                           ESP_HF_CALL_SETUP_STATUS_OUTGOING_DIALING,
                           s_number, ESP_HF_CALL_ADDR_TYPE_UNKNOWN);
    }
    /* 车机后续应显示呼出界面;接通由 PC 调 hfp.answer 模拟 */
}

void hfp_set_voice_out(bool on) { s_voice_out = on; }
bool hfp_voice_out(void) { return s_voice_out; }
bool hfp_call_active(void) { return s_call == CALL_ACTIVE && s_audio_on; }

void hfp_connect(const bda_t bda) {
    memcpy(s_peer, bda, 6);
    s_peer_valid = true;
    esp_hf_ag_slc_connect(s_peer);
}

bool hfp_slc_connected(void) { return s_slc_on; }

void hfp_disconnect(void) {
    if (s_peer_valid)
        esp_hf_ag_slc_disconnect(s_peer);
    set_call(CALL_IDLE);
}

/* ---------------- 协议栈回调 ---------------- */

static void hfp_ag_cb(esp_hf_cb_event_t event, esp_hf_cb_param_t *param) {
    switch (event) {
    case ESP_HF_CONNECTION_STATE_EVT:
        if (param->conn_stat.state == ESP_HF_CONNECTION_STATE_SLC_CONNECTED ||
            param->conn_stat.state == ESP_HF_CONNECTION_STATE_CONNECTED) {
            memcpy(s_peer, param->conn_stat.remote_bda, 6);
            s_peer_valid = true;
            s_slc_on = true;
            bt_gap_note_peer_connected(param->conn_stat.remote_bda);
            host_send_event_str("bt.hfp.state", "state", "connected");
            {
                char body[96];
                snprintf(body, sizeof(body), "{\"profile\":\"hfp\",\"state\":\"connected\"}");
                host_send_event("bt.conn", body);
            }
        } else if (param->conn_stat.state == ESP_HF_CONNECTION_STATE_DISCONNECTED) {
            s_slc_on = false;
            set_call(CALL_IDLE);
            host_send_event_str("bt.hfp.state", "state", "disconnected");
            {
                char body[96];
                snprintf(body, sizeof(body), "{\"profile\":\"hfp\",\"state\":\"disconnected\"}");
                host_send_event("bt.conn", body);
            }
        }
        break;
    case ESP_HF_AUDIO_STATE_EVT:
        s_audio_on = (param->audio_stat.state == ESP_HF_AUDIO_STATE_CONNECTED ||
                      param->audio_stat.state == ESP_HF_AUDIO_STATE_CONNECTED_MSBC);
        {
            char body[64];
            snprintf(body, sizeof(body), "{\"audio_on\":%s}", s_audio_on ? "true" : "false");
            host_send_event("bt.hfp.state", body);
        }
        break;
    case ESP_HF_ATA_RESPONSE_EVT: {
        /* 车机接听 */
        host_send_event("hfp.at", "{\"at\":\"ATA\",\"arg\":\"\"}");
        xTimerStop(s_ring_timer, 0);
        if (s_peer_valid) esp_hf_ag_audio_connect(param->ata_rep.remote_addr);
        set_call(CALL_ACTIVE);
        break;
    }
    case ESP_HF_CHUP_RESPONSE_EVT: {
        /* 车机挂断 */
        host_send_event("hfp.at", "{\"at\":\"AT+CHUP\",\"arg\":\"\"}");
        set_call(CALL_IDLE);
        if (s_peer_valid && s_audio_on)
            esp_hf_ag_audio_disconnect(param->chup_rep.remote_addr);
        break;
    }
    case ESP_HF_DIAL_EVT: {
        /* 车机拨号 */
        const char *num = param->out_call.num_or_loc ? param->out_call.num_or_loc : "";
        char body[96];
        snprintf(body, sizeof(body), "{\"at\":\"ATD\",\"arg\":\"%s\"}", num);
        host_send_event("hfp.at", body);
        strncpy(s_number, num, sizeof(s_number) - 1);
        set_call(CALL_OUTGOING);
        break;
    }
    case ESP_HF_VTS_RESPONSE_EVT: {
        char body[96];
        snprintf(body, sizeof(body), "{\"at\":\"AT+VTS\",\"arg\":\"%s\"}",
                 param->vts_rep.code ? param->vts_rep.code : "");
        host_send_event("hfp.at", body);
        break;
    }
    case ESP_HF_UNAT_RESPONSE_EVT: {
        /* 未知 AT 命令:透传给 PC(iOS persona 的 XAPL 钩子在这里) */
        char body[128];
        snprintf(body, sizeof(body), "{\"at\":\"%s\",\"arg\":\"\"}",
                 param->unat_rep.unat ? param->unat_rep.unat : "");
        host_send_event("hfp.at", body);
        break;
    }
    default:
        break;
    }
}

/* SCO 音频:上行丢弃,下行由音频管线提供(8k/16k 单声道 s16 PCM) */
static void hfp_recv_data(const uint8_t *buf, uint32_t len) {
    (void)buf; (void)len; /* 车机麦克风上行,MVP 不处理 */
}

static uint32_t hfp_send_data(uint8_t *buf, uint32_t len) {
    return (uint32_t)audio_pull_hfp(buf, (int32_t)len);
}

void hfp_ag_init(void) {
    s_ring_timer = xTimerCreate("hfp_ring", pdMS_TO_TICKS(2000), pdTRUE, NULL, ring_timer_cb);
    esp_hf_ag_register_callback(hfp_ag_cb);
    esp_hf_ag_register_data_callback(hfp_recv_data, hfp_send_data);
    esp_hf_ag_init();
}
