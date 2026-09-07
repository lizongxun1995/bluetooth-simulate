#!/usr/bin/env python
"""给 ESP-IDF v5.3.x 打 AVRCP TG 补丁(btphone 项目专用)。

背景:IDF 的 Bluedroid AVRCP Target 有两处缺陷,导致车机(CT)无法显示
歌名/歌手,甚至被车机的通知重试刷爆:
  v1. 对车机发来的 GetElementAttributes / GetPlayStatus /
      InformDisplayedCharset 请求直接回 BAD_CMD;
  v2. btc_avrc_tg_send_rn_rsp 的事件 switch 只实现了 VOLUME_CHANGE,
      其余事件(播放状态/曲目变更/播放位置)全部 "todo" 直接 return——
      TG 永远发不出这些通知,REGISTER_NOTIFICATION 的 INTERIM 应答
      也发不出去(官方头文件注释要求 1000ms 内回),车机注册悬空后
      不断重试,实测把协议栈刷到命令超时、空闲堆 30704→5888。

v1 内容:元数据/播放状态缓存 + esp_avrc_tg_set_track_info /
        esp_avrc_tg_set_play_status 两个新 API + 正确应答上述 PDU。
v2 内容:send_rn_rsp 的事件 switch 补齐 PLAY_STATUS_CHANGE /
        TRACK_CHANGE / PLAY_POS_CHANGED 三种通知的参数装配。
可重复运行(每段独立幂等,按各自标记跳过)。

用法: python apply_patch.py [IDF路径]
      默认 IDF 路径 = %IDF_PATH% 或 C:\\Espressif\\esp-idf
回滚: 在 IDF 目录执行 git checkout -- <两个文件>
"""

import os
import sys

MARK_V1 = "btphone patch"
MARK_V2 = "btphone patch v2"

BTC_FILE = os.path.join("components", "bt", "host", "bluedroid", "btc",
                        "profile", "std", "avrc", "btc_avrc.c")
API_FILE = os.path.join("components", "bt", "host", "bluedroid", "api",
                        "include", "api", "esp_avrc_api.h")

# ---------- btc_avrc.c 修改(v1) ----------

BTC_ANCHOR_STORAGE = """#if AVRC_DYNAMIC_MEMORY == FALSE
static btc_rc_cb_t btc_rc_cb;
#else
btc_rc_cb_t *btc_rc_cb_ptr;
#endif ///AVRC_DYNAMIC_MEMORY == FALSE"""

BTC_STORAGE_INSERT = BTC_ANCHOR_STORAGE + """

/* ---- btphone patch: TG metadata storage (added by bluetooth-simulate) ---- */
static char     s_tg_title[96];
static char     s_tg_artist[96];
static char     s_tg_album[96];
static uint16_t s_tg_track_num;
static uint16_t s_tg_total_tracks;
static uint32_t s_tg_song_len;
static uint32_t s_tg_song_pos;
static uint8_t  s_tg_play_state = AVRC_PLAYSTATE_PLAYING;
/* ---- end btphone patch ---- */"""

BTC_ANCHOR_REJECT = """    case AVRC_PDU_GET_PLAY_STATUS:
    case AVRC_PDU_GET_ELEMENT_ATTR:
    case AVRC_PDU_INFORM_DISPLAY_CHARSET:
    //todo: check the valid response for these PDUs
    case AVRC_PDU_LIST_PLAYER_APP_ATTR:"""

BTC_REJECT_REPLACE = """    case AVRC_PDU_GET_PLAY_STATUS: {
        /* btphone patch: report stored play status */
        tAVRC_RESPONSE avrc_rsp;
        memset(&avrc_rsp, 0, sizeof(avrc_rsp));
        avrc_rsp.get_play_status.pdu = AVRC_PDU_GET_PLAY_STATUS;
        avrc_rsp.get_play_status.opcode = opcode_from_pdu(AVRC_PDU_GET_PLAY_STATUS);
        avrc_rsp.get_play_status.status = AVRC_STS_NO_ERROR;
        avrc_rsp.get_play_status.song_len = s_tg_song_len;
        avrc_rsp.get_play_status.song_pos = s_tg_song_pos;
        avrc_rsp.get_play_status.play_status = s_tg_play_state;
        send_metamsg_rsp(btc_rc_cb.rc_handle, label, ctype, &avrc_rsp);
    }
    break;
    case AVRC_PDU_GET_ELEMENT_ATTR: {
        /* btphone patch: report stored track metadata */
        tAVRC_RESPONSE avrc_rsp;
        tAVRC_ATTR_ENTRY attrs[6];
        char num_buf1[12];
        char num_buf2[12];
        char num_buf3[12];
        int n = 0;
        memset(&avrc_rsp, 0, sizeof(avrc_rsp));
        memset(attrs, 0, sizeof(attrs));
        if (s_tg_title[0]) {
            attrs[n].attr_id = AVRC_MEDIA_ATTR_ID_TITLE;
            attrs[n].name.str_len = strlen(s_tg_title);
            attrs[n].name.p_str = (uint8_t *)s_tg_title;
            n++;
        }
        if (s_tg_artist[0]) {
            attrs[n].attr_id = AVRC_MEDIA_ATTR_ID_ARTIST;
            attrs[n].name.str_len = strlen(s_tg_artist);
            attrs[n].name.p_str = (uint8_t *)s_tg_artist;
            n++;
        }
        if (s_tg_album[0]) {
            attrs[n].attr_id = AVRC_MEDIA_ATTR_ID_ALBUM;
            attrs[n].name.str_len = strlen(s_tg_album);
            attrs[n].name.p_str = (uint8_t *)s_tg_album;
            n++;
        }
        snprintf(num_buf1, sizeof(num_buf1), "%u", s_tg_track_num);
        attrs[n].attr_id = AVRC_MEDIA_ATTR_ID_TRACK_NUM;
        attrs[n].name.str_len = strlen(num_buf1);
        attrs[n].name.p_str = (uint8_t *)num_buf1;
        n++;
        snprintf(num_buf2, sizeof(num_buf2), "%u", s_tg_total_tracks);
        attrs[n].attr_id = AVRC_MEDIA_ATTR_ID_NUM_TRACKS;
        attrs[n].name.str_len = strlen(num_buf2);
        attrs[n].name.p_str = (uint8_t *)num_buf2;
        n++;
        snprintf(num_buf3, sizeof(num_buf3), "%u", s_tg_song_len);
        attrs[n].attr_id = AVRC_MEDIA_ATTR_ID_PLAYING_TIME;
        attrs[n].name.str_len = strlen(num_buf3);
        attrs[n].name.p_str = (uint8_t *)num_buf3;
        n++;
        avrc_rsp.get_elem_attrs.pdu = AVRC_PDU_GET_ELEMENT_ATTR;
        avrc_rsp.get_elem_attrs.opcode = opcode_from_pdu(AVRC_PDU_GET_ELEMENT_ATTR);
        avrc_rsp.get_elem_attrs.status = AVRC_STS_NO_ERROR;
        avrc_rsp.get_elem_attrs.num_attr = (uint8_t)n;
        avrc_rsp.get_elem_attrs.p_attrs = attrs;
        send_metamsg_rsp(btc_rc_cb.rc_handle, label, ctype, &avrc_rsp);
    }
    break;
    case AVRC_PDU_INFORM_DISPLAY_CHARSET: {
        /* btphone patch: accept charset notification instead of rejecting */
        tAVRC_RESPONSE avrc_rsp;
        memset(&avrc_rsp, 0, sizeof(avrc_rsp));
        avrc_rsp.rsp.pdu = AVRC_PDU_INFORM_DISPLAY_CHARSET;
        avrc_rsp.rsp.opcode = opcode_from_pdu(AVRC_PDU_INFORM_DISPLAY_CHARSET);
        avrc_rsp.rsp.status = AVRC_STS_NO_ERROR;
        send_metamsg_rsp(btc_rc_cb.rc_handle, label, ctype, &avrc_rsp);
    }
    break;
    //todo: check the valid response for these PDUs
    case AVRC_PDU_LIST_PLAYER_APP_ATTR:"""

BTC_SETTERS = """

/* ---- btphone patch: public TG metadata/play-status setters ---- */
esp_err_t esp_avrc_tg_set_track_info(const char *title, const char *artist, const char *album,
                                     uint16_t track_num, uint16_t total_tracks, uint32_t duration_ms)
{
    if (!title || !artist || !album) {
        return ESP_ERR_INVALID_ARG;
    }
    strncpy(s_tg_title, title, sizeof(s_tg_title) - 1);
    s_tg_title[sizeof(s_tg_title) - 1] = '\\0';
    strncpy(s_tg_artist, artist, sizeof(s_tg_artist) - 1);
    s_tg_artist[sizeof(s_tg_artist) - 1] = '\\0';
    strncpy(s_tg_album, album, sizeof(s_tg_album) - 1);
    s_tg_album[sizeof(s_tg_album) - 1] = '\\0';
    s_tg_track_num = track_num;
    s_tg_total_tracks = total_tracks;
    s_tg_song_len = duration_ms;
    return ESP_OK;
}

esp_err_t esp_avrc_tg_set_play_status(uint32_t position_ms, uint8_t playback_state)
{
    s_tg_song_pos = position_ms;
    s_tg_play_state = playback_state;
    return ESP_OK;
}
/* ---- end btphone patch ---- */
"""

# ---------- btc_avrc.c 修改(v2:通知事件参数装配) ----------

BTC_ANCHOR_RN_SWITCH = """    switch (event_id) {
    case ESP_AVRC_RN_VOLUME_CHANGE:
        avrc_rsp.reg_notif.param.volume = param->volume;
        break;
    // todo: implement other event notifications
    default:
        BTC_TRACE_WARNING("%s : Unhandled event ID : 0x%x", __FUNCTION__, event_id);
        return;
    }"""

BTC_RN_SWITCH_REPLACE = """    switch (event_id) {
    case ESP_AVRC_RN_VOLUME_CHANGE:
        avrc_rsp.reg_notif.param.volume = param->volume;
        break;
    /* ---- btphone patch v2: play status / track / position notifications ---- */
    case ESP_AVRC_RN_PLAY_STATUS_CHANGE:
        avrc_rsp.reg_notif.param.play_status = (tAVRC_PLAYSTATE)param->playback;
        break;
    case ESP_AVRC_RN_TRACK_CHANGE:
        memset(avrc_rsp.reg_notif.param.track, 0, AVRC_UID_SIZE);
        break;
    case ESP_AVRC_RN_PLAY_POS_CHANGED:
        avrc_rsp.reg_notif.param.play_pos = param->play_pos;
        break;
    /* ---- end btphone patch v2 ---- */
    default:
        BTC_TRACE_WARNING("%s : Unhandled event ID : 0x%x", __FUNCTION__, event_id);
        return;
    }"""

# ---------- esp_avrc_api.h 修改(v1 声明) ----------

API_ANCHOR = "esp_err_t esp_avrc_tg_set_rn_evt_cap(const esp_avrc_rn_evt_cap_mask_t *evt_set);"

API_INSERT = API_ANCHOR + """

/**
 * @brief           [btphone patch] Set track metadata reported in AVRCP
 *                  GetElementAttributes response (e.g. by a car kit display).
 *
 * @param[in]       title/artist/album: UTF-8 strings (copied internally)
 * @param[in]       track_num/total_tracks: position in album / album size
 * @param[in]       duration_ms: track duration in milliseconds
 *
 * @return
 *                  - ESP_OK: success
 *                  - ESP_ERR_INVALID_ARG: if any string pointer is NULL
 */
esp_err_t esp_avrc_tg_set_track_info(const char *title, const char *artist, const char *album,
                                     uint16_t track_num, uint16_t total_tracks, uint32_t duration_ms);

/**
 * @brief           [btphone patch] Set play position/status reported in AVRCP
 *                  GetPlayStatus response and PLAY_STATUS_CHANGED notifications.
 *
 * @param[in]       position_ms: current playback position in milliseconds
 * @param[in]       playback_state: AVRC_PLAYSTATE_STOPPED/PLAYING/PAUSED...
 *
 * @return
 *                  - ESP_OK: success
 */
esp_err_t esp_avrc_tg_set_play_status(uint32_t position_ms, uint8_t playback_state);"""


def _crlf(text: str, use_crlf: bool) -> str:
    return text.replace("\n", "\r\n") if use_crlf else text


def patch_section(path: str, mark: str, replacements) -> str:
    """对单文件应用一组替换;文件已含 mark 则整段跳过(独立幂等)。"""
    with open(path, encoding="utf-8", newline="") as f:
        src = f.read()
    if mark in src:
        return "already-patched"
    use_crlf = "\r\n" in src
    for anchor, replace in replacements:
        anchor, replace = _crlf(anchor, use_crlf), _crlf(replace, use_crlf)
        if anchor not in src:
            raise SystemExit(f"[!!] 锚点未找到(请确认 IDF 版本为 v5.3.x): {path}\n锚点片段:\n{anchor[:120]}")
        src = src.replace(anchor, replace, 1)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(src)
    return "patched"


def append_setters(path: str) -> None:
    """v1 的 setter 实现追加在文件末尾(同样以标记幂等)。"""
    with open(path, "r", encoding="utf-8", newline="") as f:
        cur = f.read()
    if "esp_avrc_tg_set_track_info(const char *title" in cur:
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(cur + _crlf(BTC_SETTERS, "\r\n" in cur))


def main() -> None:
    idf = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IDF_PATH", r"C:\Espressif\esp-idf")
    if not os.path.isdir(idf):
        raise SystemExit(f"IDF 目录不存在: {idf}")
    btc = os.path.join(idf, BTC_FILE)
    api = os.path.join(idf, API_FILE)
    for f in (btc, api):
        if not os.path.isfile(f):
            raise SystemExit(f"文件不存在: {f}")

    r1 = patch_section(btc, MARK_V1, [
        (BTC_ANCHOR_STORAGE, BTC_STORAGE_INSERT),
        (BTC_ANCHOR_REJECT, BTC_REJECT_REPLACE),
    ])
    if r1 == "patched":
        append_setters(btc)
    r2 = patch_section(btc, MARK_V2, [
        (BTC_ANCHOR_RN_SWITCH, BTC_RN_SWITCH_REPLACE),
    ])
    r3 = patch_section(api, MARK_V1, [(API_ANCHOR, API_INSERT)])

    print(f"btc_avrc.c v1(元数据应答): {r1}")
    print(f"btc_avrc.c v2(通知事件装配): {r2}")
    print(f"esp_avrc_api.h v1(新 API 声明): {r3}")
    print("[OK] 补丁完成。回滚: 在 IDF 目录 git checkout -- 上述两个文件")


if __name__ == "__main__":
    main()
