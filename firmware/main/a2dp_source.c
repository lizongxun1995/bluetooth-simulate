/* A2DP Source:向车机推流。数据由 audio_pipeline 提供(44.1k/立体声/s16) */
#include "btphone.h"
#include <stdio.h>
#include <string.h>
#include "esp_a2dp_api.h"
#include "esp_bt_main.h"
#include "host_link.h"

static esp_a2d_connection_state_t s_a2dp_state = ESP_A2D_CONNECTION_STATE_DISCONNECTED;
static esp_a2d_media_ctrl_t s_media_ctrl = ESP_A2D_MEDIA_CTRL_NONE;
static bool s_media_started = false; /* 媒体已 START:重复 START 违反协议栈状态机 */
static bda_t s_peer = { 0 };
static bool s_peer_valid = false;
static bool s_user_disc = false; /* PC 主动断开:期间不做自动重连 */
static int s_retry = 0;         /* 掉线自动重连计数(连上清零) */

static const char *conn_state_str(esp_a2d_connection_state_t s) {
    switch (s) {
    case ESP_A2D_CONNECTION_STATE_CONNECTING: return "connecting";
    case ESP_A2D_CONNECTION_STATE_CONNECTED: return "connected";
    case ESP_A2D_CONNECTION_STATE_DISCONNECTING: return "disconnecting";
    default: return "disconnected";
    }
}

esp_err_t a2dp_connect(const bda_t bda) {
    memcpy(s_peer, bda, 6);
    s_peer_valid = true;
    s_user_disc = false;
    return esp_a2d_source_connect(s_peer);
}

bool a2dp_is_connected(void) {
    return s_a2dp_state == ESP_A2D_CONNECTION_STATE_CONNECTED;
}

bool a2dp_is_busy(void) {
    return s_a2dp_state == ESP_A2D_CONNECTION_STATE_CONNECTING ||
           s_a2dp_state == ESP_A2D_CONNECTION_STATE_DISCONNECTING;
}

const char *a2dp_state_str(void) { return conn_state_str(s_a2dp_state); }

void a2dp_disconnect(void) {
    s_user_disc = true;
    if (s_peer_valid)
        esp_a2d_source_disconnect(s_peer);
}

void a2dp_start(void) {
    if (s_a2dp_state == ESP_A2D_CONNECTION_STATE_CONNECTED && !s_media_started)
        esp_a2d_media_ctrl(ESP_A2D_MEDIA_CTRL_START);
}

void a2dp_suspend(void) {
    if (s_a2dp_state == ESP_A2D_CONNECTION_STATE_CONNECTED && s_media_started)
        esp_a2d_media_ctrl(ESP_A2D_MEDIA_CTRL_STOP);
    s_media_ctrl = ESP_A2D_MEDIA_CTRL_STOP;
}

/* 媒体数据回调:协议栈 SBC 编码前拉取 PCM */
static int32_t a2dp_data_cb(uint8_t *buf, int32_t len) {
    if (buf == NULL || len < 0) return 0;
    return audio_pull_a2dp(buf, len);
}

static void a2dp_cb(esp_a2d_cb_event_t event, esp_a2d_cb_param_t *param) {
    switch (event) {
    case ESP_A2D_CONNECTION_STATE_EVT: {
        esp_a2d_connection_state_t prev = s_a2dp_state;
        s_a2dp_state = param->conn_stat.state;
        memcpy(s_peer, param->conn_stat.remote_bda, 6);
        s_peer_valid = true;
        if (s_a2dp_state == ESP_A2D_CONNECTION_STATE_CONNECTED) {
            s_retry = 0;
            s_user_disc = false;
            s_media_started = false; /* 新连接媒体状态未知,按未启动处理 */
            bt_gap_note_peer_connected(param->conn_stat.remote_bda);
            /* 保活:连上即开媒体推静音(空缓冲自动补零)。车机会丢弃
             * 连接后迟迟不起流的空闲 A2DP 链路(esp-idf#6342 同类),
             * PC 随后 audio.open+music.play 注入真实数据无缝接替 */
            audio_set_playing(true);
            a2dp_start();
        } else {
            bt_gap_note_peer(param->conn_stat.remote_bda);
        }
        host_send_event_str("bt.a2dp.state", "state", conn_state_str(s_a2dp_state));
        {
            char body[160];
            if (s_a2dp_state == ESP_A2D_CONNECTION_STATE_DISCONNECTED)
                snprintf(body, sizeof(body),
                         "{\"profile\":\"a2dp\",\"state\":\"disconnected\","
                         "\"reason\":\"%s\"}",
                         param->conn_stat.disc_rsn == ESP_A2D_DISC_RSN_ABNORMAL
                             ? "abnormal" : "normal");
            else
                snprintf(body, sizeof(body),
                         "{\"profile\":\"a2dp\",\"state\":\"%s\"}",
                         conn_state_str(s_a2dp_state));
            host_send_event("bt.conn", body);
        }
        /* 掉线/连接失败自动重连:退避 3s/6s/12s,最多 3 次。
         * PC 主动断开(s_user_disc)不重连;断链必须等 DISCONNECTED 落地
         * 再发起下一次 connect(Bluedroid 纪律,立即重连会互相踩) */
        if (s_a2dp_state == ESP_A2D_CONNECTION_STATE_DISCONNECTED && !s_user_disc &&
            (prev == ESP_A2D_CONNECTION_STATE_CONNECTED ||
             prev == ESP_A2D_CONNECTION_STATE_CONNECTING) &&
            s_peer_valid && bt_gap_get_auto_reconnect() && ++s_retry <= 3) {
            bt_gap_schedule_profile_connect(s_peer, 3000 << (s_retry - 1));
        }
        break;
    }
    case ESP_A2D_MEDIA_CTRL_ACK_EVT:
        s_media_ctrl = param->media_ctrl_stat.cmd;
        if (param->media_ctrl_stat.status == ESP_A2D_MEDIA_CTRL_ACK_SUCCESS) {
            if (s_media_ctrl == ESP_A2D_MEDIA_CTRL_START)
                s_media_started = true;
            else if (s_media_ctrl == ESP_A2D_MEDIA_CTRL_STOP ||
                     s_media_ctrl == ESP_A2D_MEDIA_CTRL_SUSPEND)
                s_media_started = false;
        }
        break;
    default:
        break;
    }
}

void a2dp_source_init(void) {
    esp_a2d_source_init();
    esp_a2d_register_callback(a2dp_cb);
    esp_a2d_source_register_data_callback(a2dp_data_cb);
}
