/* 串口协议层:UART0(Type-C/CH340,2Mbaud)
 * 帧格式与 PC 端 btphone/protocol.py 一致:
 *   A5 5A | type | len(2,LE) | seq(2,LE) | payload | crc16(2,LE,覆盖 type..payload)
 * type: 0x01=CTRL(JSON) 0x02=AUDIO
 * CTRL JSON: {"id":n,"cmd":"...","args":{}} / 响应 / {"evt":"...","data":{...}}
 */
#include "host_link.h"
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/uart.h"
#include "esp_system.h"
#include "cJSON.h"
#include "btphone.h"
#include "audio_pipeline.h"
#include "wifi_nat.h"

#define PROT_UART_NUM UART_NUM_0
#define PROT_BAUD 2000000
#define RX_BUF_SIZE (4096)
#define MAX_PAYLOAD 4096

/* CRC16-CCITT(CCITT-FALSE:init 0xFFFF,无反射) */
static uint16_t crc16_ccitt(const uint8_t *data, size_t len);
static uint16_t crc16_ccitt_update(uint16_t crc, const uint8_t *data, size_t len);

/* ---------------- 发送 ---------------- */
static SemaphoreHandle_t tx_mutex;

/* 同 id 重试的应答缓存(dispatch_command 用);误码计数(sys.info 上报,
 * 2026-09-01 加:量化串口链路质量,空闲态丢帧率 ~8.7% 即由此定位)。 */
static int32_t s_last_req_id = 0;
static char s_last_reply[2048];
static uint16_t s_last_reply_seq = 0;
static volatile uint32_t s_rx_frames = 0, s_rx_crc_err = 0, s_rx_hdr_err = 0;


static void send_frame(uint8_t type, const uint8_t *payload, uint16_t len, uint16_t seq) {
    static uint8_t out[MAX_PAYLOAD + 16];
    size_t p = 0;
    out[p++] = 0xA5; out[p++] = 0x5A;
    out[p++] = type; out[p++] = 0;
    out[p++] = len & 0xFF; out[p++] = (len >> 8) & 0xFF;
    out[p++] = seq & 0xFF; out[p++] = (seq >> 8) & 0xFF;
    memcpy(out + p, payload, len); p += len;
    uint16_t crc = crc16_ccitt(out + 2, p - 2);
    out[p++] = crc & 0xFF; out[p++] = (crc >> 8) & 0xFF;
    xSemaphoreTake(tx_mutex, portMAX_DELAY);
    uart_write_bytes(PROT_UART_NUM, out, p);
    xSemaphoreGive(tx_mutex);
}

static void send_json(const char *json, uint16_t seq) {
    send_frame(0x01, (const uint8_t *)json, (uint16_t)strlen(json), seq);
}

void host_send_event(const char *evt, const char *json_body) {
    static uint16_t evt_seq = 0;
    char buf[512];
    snprintf(buf, sizeof(buf), "{\"evt\":\"%s\",\"data\":%s}", evt, json_body ? json_body : "{}");
    send_json(buf, ++evt_seq);
}

void host_send_event_str(const char *evt, const char *key, const char *value) {
    char body[256];
    snprintf(body, sizeof(body), "{\"%s\":\"%s\"}", key, value ? value : "");
    host_send_event(evt, body);
}

void host_send_event_int(const char *evt, const char *key, long value) {
    char body[64];
    snprintf(body, sizeof(body), "{\"%s\":%ld}", key, value);
    host_send_event(evt, body);
}

/* ---------------- 命令分发 ----------------
 * 每个命令处理器返回 0 并把结果写入 *result(cJSON 对象,所有权转移),
 * 失败时返回 -1 并设置 *err_msg。 */

typedef struct {
    cJSON *result;
    const char *err_msg;
} cmd_reply_t;

static int cmd_sys_info(cmd_reply_t *r) {
    r->result = cJSON_Parse("{\"fw\":\"" BTPHONE_FW_VERSION "\",\"profiles\":[\"a2dp\",\"avrcp\",\"hfp_ag\"]}");
    cJSON *chip = cJSON_AddStringToObject(r->result, "chip", BTPHONE_CHIP_DESC);
    (void)chip;
    cJSON *name = cJSON_AddStringToObject(r->result, "name", bt_gap_get_name());
    (void)name;
    cJSON_AddNumberToObject(r->result, "buffer_size", (double)audio_stream_buffer_size());
    cJSON_AddNumberToObject(r->result, "free_heap", (double)esp_get_free_heap_size());
    cJSON_AddStringToObject(r->result, "wifi_init", wifi_init_fail_step() ? wifi_init_fail_step() : "ok");
    cJSON_AddNumberToObject(r->result, "wifi_err", wifi_init_err());
    cJSON_AddStringToObject(r->result, "wifi_heap", wifi_heap_trace());
    cJSON_AddNumberToObject(r->result, "reset", (double)esp_reset_reason());
    cJSON_AddNumberToObject(r->result, "rtc_boot", (double)btphone_rtc_boot_count());
    cJSON_AddBoolToObject(r->result, "bt_up", btphone_bt_up());
    /* 链路质量:好帧/坏帧计数——量化串口误码(见 handle_rx_byte 注释) */
    cJSON_AddNumberToObject(r->result, "rx_frames", (double)s_rx_frames);
    cJSON_AddNumberToObject(r->result, "rx_crc_err", (double)s_rx_crc_err);
    cJSON_AddNumberToObject(r->result, "rx_hdr_err", (double)s_rx_hdr_err);
    return 0;
}

static int cmd_sys_echo(cJSON *args, cmd_reply_t *r) {
    r->result = cJSON_CreateObject();
    cJSON_AddStringToObject(r->result, "msg", cJSON_GetStringValue(cJSON_GetObjectItem(args, "msg")));
    return 0;
}

static int cmd_bt_set_name(cJSON *args, cmd_reply_t *r) {
    const char *name = cJSON_GetStringValue(cJSON_GetObjectItem(args, "name"));
    if (!name) { r->err_msg = "missing name"; return -1; }
    bt_gap_set_name(name);
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_bt_set_discoverable(cJSON *args, cmd_reply_t *r) {
    const char *mode = cJSON_GetStringValue(cJSON_GetObjectItem(args, "mode"));
    long timeout_s = 0;
    cJSON *t = cJSON_GetObjectItem(args, "timeout_s");
    if (cJSON_IsNumber(t)) timeout_s = (long)t->valuedouble;
    if (!mode) { r->err_msg = "missing mode"; return -1; }
    bt_gap_set_scan_mode_str(mode, (int)timeout_s);
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_pair_mode(cJSON *args, cmd_reply_t *r) {
    const char *mode = cJSON_GetStringValue(cJSON_GetObjectItem(args, "mode"));
    if (!mode) { r->err_msg = "missing mode"; return -1; }
    if (bt_gap_set_pair_mode_str(mode) != ESP_OK) { r->err_msg = "bad mode"; return -1; }
    r->result = cJSON_CreateObject();
    cJSON_AddStringToObject(r->result, "mode", mode);
    return 0;
}

static int cmd_pair_confirm(cJSON *args, cmd_reply_t *r) {
    cJSON *accept = cJSON_GetObjectItem(args, "accept");
    bt_gap_confirm(cJSON_IsTrue(accept));
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_pair_policy(cJSON *args, cmd_reply_t *r) {
    cJSON *aa = cJSON_GetObjectItem(args, "auto_accept");
    cJSON *ar = cJSON_GetObjectItem(args, "auto_reconnect");
    if (aa) bt_gap_set_auto_accept(cJSON_IsTrue(aa));
    if (ar) bt_gap_set_auto_reconnect(cJSON_IsTrue(ar));
    r->result = cJSON_CreateObject();
    cJSON_AddBoolToObject(r->result, "auto_accept", bt_gap_get_auto_accept());
    cJSON_AddBoolToObject(r->result, "auto_reconnect", bt_gap_get_auto_reconnect());
    return 0;
}

static int cmd_pair_list(cmd_reply_t *r) {
    r->result = cJSON_CreateObject();
    cJSON *arr = cJSON_AddArrayToObject(r->result, "devices");
    bda_t list[16];
    char names[16][64];
    int n = bt_gap_get_bonded(list, names, 16);
    for (int i = 0; i < n; i++) {
        cJSON *dev = cJSON_CreateObject();
        char mac[18];
        snprintf(mac, sizeof(mac), "%02X:%02X:%02X:%02X:%02X:%02X",
                 list[i][0], list[i][1], list[i][2], list[i][3], list[i][4], list[i][5]);
        cJSON_AddStringToObject(dev, "mac", mac);
        cJSON_AddStringToObject(dev, "name", names[i]);
        cJSON_AddItemToArray(arr, dev);
    }
    return 0;
}

static int cmd_pair_remove(cJSON *args, cmd_reply_t *r) {
    const char *mac = cJSON_GetStringValue(cJSON_GetObjectItem(args, "mac"));
    if (!mac) { r->err_msg = "missing mac"; return -1; }
    if (bt_gap_remove_bond_str(mac) != ESP_OK) { r->err_msg = "not bonded"; return -1; }
    r->result = cJSON_CreateObject();
    return 0;
}

static bool parse_bda(const char *mac, bda_t out) {
    unsigned int b[6];
    if (sscanf(mac, "%02X:%02X:%02X:%02X:%02X:%02X",
               &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6) return false;
    for (int i = 0; i < 6; i++) out[i] = (uint8_t)b[i];
    return true;
}

static int cmd_conn_connect(cJSON *args, cmd_reply_t *r) {
    const char *mac = cJSON_GetStringValue(cJSON_GetObjectItem(args, "mac"));
    bda_t bda;
    if (!mac || !parse_bda(mac, bda)) { r->err_msg = "bad mac"; return -1; }
    cJSON *profiles = cJSON_GetObjectItem(args, "profiles");
    bool do_a2dp = true, do_hfp = true;
    if (cJSON_IsArray(profiles)) {
        do_a2dp = false; do_hfp = false;
        const cJSON *item;
        cJSON_ArrayForEach(item, profiles) {
            if (strcmp(item->valuestring, "a2dp") == 0) do_a2dp = true;
            if (strcmp(item->valuestring, "hfp") == 0) do_hfp = true;
        }
    }
    if (do_a2dp && a2dp_connect(bda) != ESP_OK) { r->err_msg = "a2dp connect failed"; return -1; }
    if (do_hfp) hfp_connect(bda);
    r->result = cJSON_CreateObject();
    cJSON_AddStringToObject(r->result, "mac", mac);
    return 0;
}

static int cmd_conn_disconnect(cJSON *args, cmd_reply_t *r) {
    const char *profile = cJSON_GetStringValue(cJSON_GetObjectItem(args, "profile"));
    a2dp_disconnect();
    if (!profile || strcmp(profile, "all") == 0 || strcmp(profile, "hfp") == 0)
        hfp_disconnect();
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_conn_status(cmd_reply_t *r) {
    char json[512];
    bt_gap_get_status_json(json, sizeof(json));
    r->result = cJSON_Parse(json);
    if (!r->result) r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_bt_rssi(cmd_reply_t *r) {
    if (!bt_gap_peer_valid()) { r->err_msg = "no acl peer yet(pair/connect first)"; return -1; }
    bt_gap_query_rssi(); /* 异步:结果经 bt.rssi 事件上报 */
    r->result = cJSON_CreateObject();
    cJSON_AddBoolToObject(r->result, "requested", true);
    return 0;
}

static int cmd_audio_open(cJSON *args, cmd_reply_t *r) {
    const char *sink = cJSON_GetStringValue(cJSON_GetObjectItem(args, "sink"));
    const char *codec = cJSON_GetStringValue(cJSON_GetObjectItem(args, "codec"));
    cJSON *rate = cJSON_GetObjectItem(args, "rate");
    cJSON *channels = cJSON_GetObjectItem(args, "channels");
    if (!sink || !codec || !cJSON_IsNumber(rate) || !cJSON_IsNumber(channels)) {
        r->err_msg = "missing fields"; return -1;
    }
    if (!audio_open(strcmp(sink, "hfp") == 0 ? AUDIO_SINK_HFP : AUDIO_SINK_A2DP,
                    (int)rate->valuedouble, (int)channels->valuedouble,
                    strcmp(codec, "adpcm") == 0 ? AUDIO_CODEC_ADPCM : AUDIO_CODEC_PCM)) {
        r->err_msg = "no heap for audio buffers (retry after disconnecting profiles)";
        return -1;
    }
    r->result = cJSON_CreateObject();
    cJSON_AddNumberToObject(r->result, "buffer_size", (double)audio_stream_buffer_size());
    cJSON_AddNumberToObject(r->result, "free_heap", (double)esp_get_free_heap_size());
    cJSON_AddStringToObject(r->result, "wifi_init", wifi_init_fail_step() ? wifi_init_fail_step() : "ok");
    cJSON_AddNumberToObject(r->result, "wifi_err", wifi_init_err());
    cJSON_AddStringToObject(r->result, "wifi_heap", wifi_heap_trace());
    cJSON_AddNumberToObject(r->result, "free", (double)audio_stream_free());
    return 0;
}

static int cmd_audio_stop(cmd_reply_t *r) {
    audio_close();
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_music_play(cmd_reply_t *r) {
    if (!a2dp_is_connected()) {
        r->err_msg = "a2dp not connected (pair/connect car first)";
        return -1;
    }
    if (!audio_pipeline_ready()) {
        /* A2DP 启动后协议栈会立刻拉 PCM,缓冲未分配时必须拒绝
         * (v0.3.0 及之前:拉到未初始化的 ring → 除零 panic 重启) */
        r->err_msg = "audio not opened (audio.open first)";
        return -1;
    }
    audio_set_playing(true);
    a2dp_start();
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_music_pause(cmd_reply_t *r) {
    audio_set_playing(false);
    a2dp_suspend();
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_music_resume(cmd_reply_t *r) {
    if (!a2dp_is_connected()) {
        r->err_msg = "a2dp not connected (pair/connect car first)";
        return -1;
    }
    if (!audio_pipeline_ready()) {
        r->err_msg = "audio not opened (audio.open first)";
        return -1;
    }
    audio_set_playing(true);
    a2dp_start();
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_music_stop(cmd_reply_t *r) {
    audio_set_playing(false);
    a2dp_suspend();
    r->result = cJSON_CreateObject();
    return 0;
}

static long json_long(const cJSON *obj, const char *key, long dflt) {
    const cJSON *item = cJSON_GetObjectItem(obj, key);
    return (item && cJSON_IsNumber(item)) ? (long)item->valuedouble : dflt;
}

static int cmd_avrcp_metadata(cJSON *args, cmd_reply_t *r) {
    const char *title = cJSON_GetStringValue(cJSON_GetObjectItem(args, "title"));
    if (!title) { r->err_msg = "missing title"; return -1; }
    avrcp_send_metadata(
        title,
        cJSON_GetStringValue(cJSON_GetObjectItem(args, "artist")),
        cJSON_GetStringValue(cJSON_GetObjectItem(args, "album")),
        json_long(args, "duration_ms", 0),
        json_long(args, "track_no", 0),
        json_long(args, "total_tracks", 0));
    r->result = cJSON_CreateObject();
    cJSON_AddBoolToObject(r->result, "applied", true);
    if (!avrcp_rc_connected())
        cJSON_AddStringToObject(r->result, "warn",
                                "avrcp not connected; metadata cached only");
    return 0;
}

static int cmd_hfp_incoming(cJSON *args, cmd_reply_t *r) {
    const char *number = cJSON_GetStringValue(cJSON_GetObjectItem(args, "number"));
    if (!number) { r->err_msg = "missing number"; return -1; }
    if (!hfp_slc_connected()) {
        r->err_msg = "hfp not connected (pair/connect car first)";
        return -1;
    }
    hfp_incoming(number);
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_hfp_ring(cJSON *args, cmd_reply_t *r) {
    cJSON *on = cJSON_GetObjectItem(args, "on");
    hfp_ring(!cJSON_IsFalse(on));
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_hfp_answer(cmd_reply_t *r) {
    hfp_answer();
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_hfp_hangup(cmd_reply_t *r) {
    hfp_hangup();
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_hfp_dial(cJSON *args, cmd_reply_t *r) {
    const char *number = cJSON_GetStringValue(cJSON_GetObjectItem(args, "number"));
    if (!number) { r->err_msg = "missing number"; return -1; }
    if (!hfp_slc_connected()) {
        r->err_msg = "hfp not connected (pair/connect car first)";
        return -1;
    }
    hfp_dial(number);
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_hfp_voice(cJSON *args, cmd_reply_t *r) {
    cJSON *on = cJSON_GetObjectItem(args, "on");
    hfp_set_voice_out(cJSON_IsTrue(on));
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_net_wifi_sta(cJSON *args, cmd_reply_t *r) {
    const char *ssid = cJSON_GetStringValue(cJSON_GetObjectItem(args, "ssid"));
    const char *password = cJSON_GetStringValue(cJSON_GetObjectItem(args, "password"));
    if (!ssid) { r->err_msg = "missing ssid"; return -1; }
    if (wifi_sta_join(ssid, password) != ESP_OK) { r->err_msg = "sta join failed"; return -1; }
    r->result = cJSON_CreateObject();
    cJSON_AddBoolToObject(r->result, "connecting", true);
    return 0;
}

static int cmd_net_ap_start(cJSON *args, cmd_reply_t *r) {
    const char *ssid = cJSON_GetStringValue(cJSON_GetObjectItem(args, "ssid"));
    const char *password = cJSON_GetStringValue(cJSON_GetObjectItem(args, "password"));
    if (!ssid) { r->err_msg = "missing ssid"; return -1; }
    if (wifi_ap_start(ssid, password) != ESP_OK) { r->err_msg = "ap start failed"; return -1; }
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_net_ap_stop(cmd_reply_t *r) {
    if (wifi_ap_stop() != ESP_OK) { r->err_msg = "ap stop failed"; return -1; }
    r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_net_status(cmd_reply_t *r) {
    char json[256];
    wifi_status_json(json, sizeof(json));
    r->result = cJSON_Parse(json);
    if (!r->result) r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_net_wifi_scan(cmd_reply_t *r) {
    static char json[1536];
    int count = 0;
    /* 扫描阻塞 1.5~3s;PC 端命令超时默认 10s,可覆盖 */
    if (wifi_scan(json, sizeof(json), &count, 12) != ESP_OK) {
        r->err_msg = "scan failed(wifi started?)";
        return -1;
    }
    r->result = cJSON_Parse(json);
    if (!r->result) r->result = cJSON_CreateObject();
    return 0;
}

static int cmd_not_implemented(cJSON *args, cmd_reply_t *r) {
    (void)args; (void)r;
    return -2; /* 特殊返回:按未实现处理 */
}

typedef int (*cmd_fn1)(cJSON *args, cmd_reply_t *r);
typedef int (*cmd_fn0)(cmd_reply_t *r);

typedef struct {
    const char *name;
    void *fn;
    bool takes_args;
} cmd_entry_t;

static const cmd_entry_t commands[] = {
    { "sys.info",          cmd_sys_info, false },
    { "sys.echo",          cmd_sys_echo, true },
    { "sys.reset",         NULL, false }, /* 特判 */
    { "bt.set_name",       cmd_bt_set_name, true },
    { "bt.set_discoverable", cmd_bt_set_discoverable, true },
    { "pair.mode",         cmd_pair_mode, true },
    { "pair.confirm",      cmd_pair_confirm, true },
    { "pair.policy",       cmd_pair_policy, true },
    { "pair.list",         cmd_pair_list, false },
    { "pair.remove",       cmd_pair_remove, true },
    { "conn.connect",      cmd_conn_connect, true },
    { "conn.disconnect",   cmd_conn_disconnect, true },
    { "conn.status",       cmd_conn_status, false },
    { "bt.rssi",           cmd_bt_rssi, false },
    { "audio.open",        cmd_audio_open, true },
    { "audio.stop",        cmd_audio_stop, false },
    { "music.play",        cmd_music_play, false },
    { "music.pause",       cmd_music_pause, false },
    { "music.resume",      cmd_music_resume, false },
    { "music.stop",        cmd_music_stop, false },
    { "avrcp.metadata",    cmd_avrcp_metadata, true },
    { "hfp.incoming",      cmd_hfp_incoming, true },
    { "hfp.ring",          cmd_hfp_ring, true },
    { "hfp.answer",        cmd_hfp_answer, false },
    { "hfp.hangup",        cmd_hfp_hangup, false },
    { "hfp.dial",          cmd_hfp_dial, true },
    { "hfp.voice",         cmd_hfp_voice, true },
    /* WiFi(v1 已实现:STA/AP/扫描/状态;NAT 共享 Phase 2) */
    { "net.wifi_sta",      cmd_net_wifi_sta, true },
    { "net.wifi_scan",     cmd_net_wifi_scan, false },
    { "net.ap_start",      cmd_net_ap_start, true },
    { "net.ap_stop",       cmd_net_ap_stop, false },
    { "net.status",        cmd_net_status, false },
    { "net.tether_pan",    cmd_not_implemented, true },
    { "lyrics.push",       cmd_not_implemented, true },
};

static void dispatch_command(const cJSON *root) {
    const cJSON *id = cJSON_GetObjectItem(root, "id");
    const cJSON *cmd = cJSON_GetObjectItem(root, "cmd");
    const cJSON *args = cJSON_GetObjectItem(root, "args");
    if (!cJSON_IsNumber(id) || !cJSON_IsString(cmd)) return;

    /* 请求去重:串口链路(2M 波特率 + 供电不稳的 hub)偶发误码,CRC 校验
     * 后静默丢帧,PC 端只能干等超时。PC 超时后用同一 id 重发,这里命中
     * 缓存就重发上次应答——命令不会执行两遍(幂等由固件侧保证,PC 无需
     * 区分命令语义)。2026-09-01 实测空闲态 8.7% 请求超时而信用事件毫秒
     * 级流动,即请求帧单向丢失;PC 端 id 时间种子跨进程单调,不误撞缓存。 */
    int32_t rid = (int32_t)id->valuedouble;
    if (rid != 0 && rid == s_last_req_id) {
        send_json(s_last_reply, s_last_reply_seq);
        return;
    }

    cmd_reply_t reply = { 0 };
    char errbuf[96];
    int rc = -2;
    bool found = false;

    if (strcmp(cmd->valuestring, "sys.reset") == 0) {
        /* 纯重启:保留 NVS(名字/last_peer/策略)。
         * 2026-09-04 前 sys.reset 误调 factory_reset,把 NVS 擦了——
         * 每次重启名字回默认、回连对象丢失。恢复出厂改走 sys.factory。 */
        esp_restart();
        found = true;
        rc = 0;
        reply.result = cJSON_CreateObject();
    } else if (strcmp(cmd->valuestring, "sys.factory") == 0) {
        bt_gap_factory_reset();
        found = true;
        rc = 0;
        reply.result = cJSON_CreateObject();
    } else {
        for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); i++) {
            if (strcmp(commands[i].name, cmd->valuestring) != 0) continue;
            found = true;
            if (commands[i].takes_args)
                rc = ((cmd_fn1)commands[i].fn)((cJSON *)(args && cJSON_IsObject(args) ? args : cJSON_CreateObject()), &reply);
            else
                rc = ((cmd_fn0)commands[i].fn)(&reply);
            break;
        }
        if (!found) rc = -2;
        if (rc == -1 && reply.err_msg == NULL) reply.err_msg = "internal error";
    }

    char out[2048];
    if (rc == 0) {
        char *result_str = reply.result ? cJSON_PrintUnformatted(reply.result) : NULL;
        snprintf(out, sizeof(out), "{\"id\":%d,\"ok\":true,\"result\":%s}",
                 (int)id->valuedouble, result_str ? result_str : "{}");
        if (result_str) cJSON_free(result_str);
    } else {
        const char *err;
        if (rc == -2) { snprintf(errbuf, sizeof(errbuf), "not_implemented:%s", cmd->valuestring); err = errbuf; }
        else err = reply.err_msg ? reply.err_msg : "error";
        snprintf(out, sizeof(out), "{\"id\":%d,\"ok\":false,\"error\":\"%s\"}", (int)id->valuedouble, err);
    }
    if (reply.result) cJSON_Delete(reply.result);
    send_json(out, (uint16_t)id->valuedouble);
    /* 应答落缓存供同 id 重试重发(见 dispatch_command 头部注释) */
    memcpy(s_last_reply, out, sizeof(s_last_reply));
    s_last_reply_seq = (uint16_t)id->valuedouble;
    s_last_req_id = rid;
}

/* ---------------- 接收 ----------------
 * 状态机解析帧:CRC 失败/非法头 → 丢弃并回到找同步字。 */

typedef enum { PS_SYNC1, PS_SYNC2, PS_HEADER, PS_BODY } parse_state_t;

static void handle_rx_byte(uint8_t b);

static parse_state_t ps = PS_SYNC1;
static uint8_t header[6];
static uint8_t body[MAX_PAYLOAD + 2];
static size_t header_len, body_pos, body_need;
static uint8_t frame_type;

static void handle_rx_byte(uint8_t b) {
    switch (ps) {
    case PS_SYNC1:
        if (b == 0xA5) ps = PS_SYNC2;
        break;
    case PS_SYNC2:
        ps = (b == 0x5A) ? PS_HEADER : PS_SYNC1;
        header_len = 0;
        break;
    case PS_HEADER:
        header[header_len++] = b;
        if (header_len == sizeof(header)) {
            frame_type = header[0];
            body_need = header[2] | ((size_t)header[3] << 8);
            if ((frame_type != 0x01 && frame_type != 0x02) || body_need > MAX_PAYLOAD) {
                s_rx_hdr_err++; /* 头非法:多半是误码/失步后的伪帧 */
                ps = PS_SYNC1;
                break;
            }
            body_pos = 0;
            ps = (body_need == 0) ? PS_BODY : PS_BODY; /* 零负载也走 BODY 收 CRC */
            if (body_need == 0) body[0] = body[1] = 0;
        }
        break;
    case PS_BODY: {
        body[body_pos++] = b;
        if (body_pos < body_need + 2) break;
        ps = PS_SYNC1;
        /* 校验:CRC 覆盖 header(6B) + payload(body_need) */
        uint16_t calc = crc16_ccitt_update(crc16_ccitt(header, 6), body, body_need);
        uint16_t rx = (uint16_t)(body[body_need] | (body[body_need + 1] << 8));
        if (calc != rx) { s_rx_crc_err++; break; } /* CRC 错:丢帧(计数上报) */
        s_rx_frames++;

        if (frame_type == 0x02) {
            /* 音频帧:payload[0]=codec(0xFF=沿用会话);payload[1]=flags;bit0=eos */
            bool eos = body_need >= 2 && (body[1] & 0x01);
            audio_consume_stream(body + 2, body_need - 2, eos);
        } else {
            body[body_need] = '\0';
            cJSON *root = cJSON_Parse((const char *)body);
            if (root) {
                dispatch_command(root);
                cJSON_Delete(root);
            }
        }
        break;
    }
    }
}

/* CRC16-CCITT 实现 */
static uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
    return crc16_ccitt_update(0xFFFF, data, len);
}

static uint16_t crc16_ccitt_update(uint16_t crc, const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)((uint16_t)data[i] << 8);
        for (int b = 0; b < 8; b++)
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
    return crc;
}

static void host_rx_task(void *arg) {
    uint8_t chunk[512];
    for (;;) {
        /* 首字节阻塞等,到达后立刻把驱动缓冲里已有的字节一次抽干。
         * 不能"请求大块 + 定时超时"读:uart_read_bytes 读不满请求量就
         * 等满超时,每个请求平白多吃最多 50ms(2026-09-01 实测空闲 RTT
         * 地板 46~63ms 即此因,改后应降到个位数毫秒)。 */
        int n = uart_read_bytes(PROT_UART_NUM, chunk, 1, portMAX_DELAY);
        if (n != 1) continue;
        handle_rx_byte(chunk[0]);
        size_t buffered = 0;
        uart_get_buffered_data_len(PROT_UART_NUM, &buffered);
        if (buffered > 0) {
            if (buffered > sizeof(chunk)) buffered = sizeof(chunk);
            n = uart_read_bytes(PROT_UART_NUM, chunk, buffered, 0);
            for (int i = 0; i < n; i++) handle_rx_byte(chunk[i]);
        }
    }
}

/* 周期上报音频缓冲水位(信用点)。
 * 必须无条件周期上报:若只在变化时上报,PC 用完额度且缓冲恰好清空
 * (free 回到初值)时将永不触发,造成死锁。 */
static void credit_task(void *arg) {
    char body[48];
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(50));
        uint32_t free_bytes = audio_stream_free();
        snprintf(body, sizeof(body), "{\"free\":%lu}", (unsigned long)free_bytes);
        host_send_event("audio.buffer", body);
    }
}

void host_link_init(void) {
    tx_mutex = xSemaphoreCreateMutex();
    uart_config_t cfg = {
        .baud_rate = PROT_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    uart_driver_install(PROT_UART_NUM, RX_BUF_SIZE * 2, 0, 0, NULL, 0);
    uart_param_config(PROT_UART_NUM, &cfg);
    /* UART0 默认引脚(GPIO1/3),即 Type-C 串口 */
    uart_set_pin(PROT_UART_NUM, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE,
                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    xTaskCreate(host_rx_task, "host_rx", 8192, NULL, 10, NULL);
    xTaskCreate(credit_task, "host_credit", 3072, NULL, 5, NULL);
}
