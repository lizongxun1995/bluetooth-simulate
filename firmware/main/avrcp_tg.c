/* AVRCP Target:向车机推送元数据,接收车机按键,处理通知(曲目变更/播放状态/播放位置)。
 *
 * 元数据推送依赖 IDF 补丁(firmware/patches/):ESP-IDF v5.3.2 的协议栈对
 * 车机的 GetElementAttributes/GetPlayStatus 请求直接回 BAD_CMD,补丁增加了
 * esp_avrc_tg_set_track_info / esp_avrc_tg_set_play_status 两个接口。
 */
#include "btphone.h"
#include <stdio.h>
#include <string.h>
#include "esp_avrc_api.h"
#include "esp_bt_main.h"
#include "host_link.h"

static volatile bool s_rc_connected = false;
/* 车机当前有效注册的通知事件位图:REGISTER_NOTIFICATION 时置位,
 * CHANGED 发出即清位(AVRCP 规范:CHANGED 消费一次注册,车机随后重注册)。
 * 没有这个状态机就会违规:对未注册的事件发 CHANGED、或对已消费的再发。 */
static volatile uint8_t s_ntf_mask = 0;
static char s_title[96] = "", s_artist[96] = "", s_album[96] = "";
static uint16_t s_track_no = 0, s_total_tracks = 0;
static uint32_t s_duration_ms = 0, s_pos_ms = 0;
static uint8_t s_play_state = ESP_AVRC_PLAYBACK_STOPPED;

static const char *pt_key_name(uint8_t key) {
    switch (key) {
    case ESP_AVRC_PT_CMD_PLAY:     return "play";
    case ESP_AVRC_PT_CMD_PAUSE:    return "pause";
    case ESP_AVRC_PT_CMD_STOP:     return "stop";
    case ESP_AVRC_PT_CMD_FORWARD:  return "next";
    case ESP_AVRC_PT_CMD_BACKWARD: return "prev";
    case ESP_AVRC_PT_CMD_VOL_UP:   return "vol_up";
    case ESP_AVRC_PT_CMD_VOL_DOWN: return "vol_down";
    case ESP_AVRC_PT_CMD_MUTE:     return "mute";
    default:                       return "unknown";
    }
}

bool avrcp_rc_connected(void) { return s_rc_connected; }

/* 通知应答(CHANGED):把状态变化推给车机。
 * 只对车机已注册的事件发,发完清位——注册被本次 CHANGED 消费,车机收到后
 * 会重新注册(走下面的 INTERIM 分支)。这样构成规范的通知循环。 */
static void send_rn(uint8_t event_id) {
    if (!s_rc_connected) return;
    uint8_t bit = (uint8_t)(1u << (event_id & 7));
    if (!(s_ntf_mask & bit)) return;
    s_ntf_mask &= (uint8_t)~bit;
    esp_avrc_rn_param_t rn_param;
    memset(&rn_param, 0, sizeof(rn_param));
    switch (event_id) {
    case ESP_AVRC_RN_PLAY_STATUS_CHANGE:
        rn_param.playback = (esp_avrc_playback_stat_t)s_play_state;
        break;
    case ESP_AVRC_RN_TRACK_CHANGE:
        break; /* elm_id 全 0 即可,车机会重新拉 GetElementAttributes */
    case ESP_AVRC_RN_PLAY_POS_CHANGED:
        rn_param.play_pos = s_pos_ms;
        break;
    default:
        return;
    }
    esp_avrc_tg_send_rn_rsp((esp_avrc_rn_event_ids_t)event_id, ESP_AVRC_RN_RSP_CHANGED, &rn_param);
}

void avrcp_send_metadata(const char *title, const char *artist, const char *album,
                         long duration_ms, long track_no, long total_tracks) {
    strncpy(s_title, title ? title : "", sizeof(s_title) - 1);
    strncpy(s_artist, artist ? artist : "", sizeof(s_artist) - 1);
    strncpy(s_album, album ? album : "", sizeof(s_album) - 1);
    s_duration_ms = (uint32_t)duration_ms;
    s_track_no = (uint16_t)track_no;
    s_total_tracks = (uint16_t)total_tracks;
    /* 补丁接口:写入协议栈元数据缓存,车机 GetElementAttributes 时应答 */
    esp_avrc_tg_set_track_info(s_title, s_artist, s_album,
                               s_track_no, s_total_tracks, s_duration_ms);
    send_rn(ESP_AVRC_RN_TRACK_CHANGE);
}

void avrcp_send_track_change(void) {
    send_rn(ESP_AVRC_RN_TRACK_CHANGE);
}

void avrcp_send_play_status(long pos_ms, bool playing) {
    s_pos_ms = (uint32_t)pos_ms;
    s_play_state = playing ? (uint8_t)ESP_AVRC_PLAYBACK_PLAYING : (uint8_t)ESP_AVRC_PLAYBACK_PAUSED;
    esp_avrc_tg_set_play_status(s_pos_ms, s_play_state);
    send_rn(ESP_AVRC_RN_PLAY_STATUS_CHANGE);
    send_rn(ESP_AVRC_RN_PLAY_POS_CHANGED);
}

static void avrcp_tg_cb(esp_avrc_tg_cb_event_t event, esp_avrc_tg_cb_param_t *param) {
    switch (event) {
    case ESP_AVRC_TG_CONNECTION_STATE_EVT:
        s_rc_connected = param->conn_stat.connected;
        if (s_rc_connected)
            bt_gap_note_peer_connected(param->conn_stat.remote_bda);
        else {
            bt_gap_note_peer(param->conn_stat.remote_bda);
            s_ntf_mask = 0; /* 断开:注册全部失效 */
        }
        {
            char body[96];
            snprintf(body, sizeof(body), "{\"profile\":\"avrcp\",\"state\":\"%s\"}",
                     s_rc_connected ? "connected" : "disconnected");
            host_send_event("bt.conn", body);
        }
        host_send_event_str("bt.avrcp.state", "state",
                            s_rc_connected ? "connected" : "disconnected");
        break;
    case ESP_AVRC_TG_PASSTHROUGH_CMD_EVT: {
        /* key_state: 0=按下 1=释放;只上报按下 */
        if (param->psth_cmd.key_state == 0) {
            char body[96];
            snprintf(body, sizeof(body), "{\"cmd\":\"%s\"}",
                     pt_key_name(param->psth_cmd.key_code));
            host_send_event("avrcp.cmd", body);
        }
        break;
    }
    case ESP_AVRC_TG_SET_ABSOLUTE_VOLUME_CMD_EVT: {
        char body[96];
        snprintf(body, sizeof(body), "{\"cmd\":\"vol_set\",\"value\":%u}",
                 param->set_abs_vol.volume);
        host_send_event("avrcp.cmd", body);
        break;
    }
    case ESP_AVRC_TG_REGISTER_NOTIFICATION_EVT: {
        /* 车机注册通知:必须立刻回 INTERIM(esp_avrc_api.h 官方注释要求
         * 1000ms 内),协议栈不代答。之前没人回 INTERIM,车机的注册永远
         * 悬空后不断重试,实测把协议栈刷到命令超时、空闲堆 30704→5888。 */
        uint8_t id = (uint8_t)param->reg_ntf.event_id;
        esp_avrc_rn_param_t rn_param;
        memset(&rn_param, 0, sizeof(rn_param));
        switch (id) {
        case ESP_AVRC_RN_PLAY_STATUS_CHANGE:
            rn_param.playback = (esp_avrc_playback_stat_t)s_play_state;
            break;
        case ESP_AVRC_RN_PLAY_POS_CHANGED:
            rn_param.play_pos = s_pos_ms;
            break;
        default:
            break; /* TRACK_CHANGE 无参数 */
        }
        s_ntf_mask |= (uint8_t)(1u << (id & 7));
        esp_avrc_tg_send_rn_rsp((esp_avrc_rn_event_ids_t)id, ESP_AVRC_RN_RSP_INTERIM, &rn_param);
        break;
    }
    default:
        break;
    }
}

void avrcp_tg_init(void) {
    esp_avrc_tg_init();
    esp_avrc_tg_register_callback(avrcp_tg_cb);
    /* 声明支持的通知事件,否则车机注册会被拒 */
    esp_avrc_rn_evt_cap_mask_t evt_set = { 0 };
    evt_set.bits |= (1 << (ESP_AVRC_RN_PLAY_STATUS_CHANGE - 1));
    evt_set.bits |= (1 << (ESP_AVRC_RN_TRACK_CHANGE - 1));
    evt_set.bits |= (1 << (ESP_AVRC_RN_PLAY_POS_CHANGED - 1));
    esp_avrc_tg_set_rn_evt_cap(&evt_set);
}
