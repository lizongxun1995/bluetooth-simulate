/* GAP 层:设备名/可见性/配对策略(含配对失败模拟)/绑定管理/profile 自动连接 */
#include "btphone.h"
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/timers.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_bt_device.h"
#include "esp_gap_bt_api.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "host_link.h"

static pair_mode_t s_pair_mode = PAIR_AUTO;
static bool s_auto_accept = true;
static bool s_auto_reconnect = true;
static char s_name[64] = "VPHONE-01";
static bool s_discoverable = false;  /* 当前可见性(set_scan_mode 同步维护) */
static char s_scan_mode[16] = "off"; /* 当前模式串:改名后需重应用(见 bt_gap_set_name) */
/* 最近 ACL 对端:配对完成/A2DP 连接回调喂入,bt.rssi 链路信号查询用 */
static esp_bd_addr_t s_peer;
static volatile bool s_peer_valid = false;
/* 待确认的配对请求(PC manual 模式下由 pair.confirm 处理) */
static bda_t s_pending_bda;
static volatile bool s_pending_valid = false;
/* profile 自动连接:配对成功/上电回连后延迟发起(不能在协议栈回调上下文里直接连) */
static TimerHandle_t s_connect_timer;
static bda_t s_connect_target;
static volatile bool s_connect_pending = false;

static void connect_timer_cb(TimerHandle_t t) {
    (void)t;
    s_connect_pending = false;
    if (!s_auto_reconnect) return;
    /* 只自动回连 a2dp:HFP 不主动碰。
     * 实测该车机对 AG 主动发起的 HFP SLC 约 5s 后掐断,且连带整条 ACL
     * (a2dp 跟着掉线,"reason":"normal");车机自己发起的 HFP 反而能长期
     * 保持(车机普遍倾向自己发起 HFP,见 pschatzmann/ESP32-A2DP#393 等)。
     * 需要显式连 HFP 时由 PC 端 conn.connect(profiles=["hfp"]) 下发。 */
    if (!a2dp_is_connected() && !a2dp_is_busy())
        a2dp_connect(s_connect_target);
}

void bt_gap_schedule_profile_connect(const bda_t bda, int delay_ms) {
    if (s_connect_timer == NULL || s_connect_pending) return;
    memcpy(s_connect_target, bda, 6);
    s_connect_pending = true;
    /* xTimerChangePeriod 对休眠定时器同时起"改周期+启动"作用 */
    xTimerChangePeriod(s_connect_timer, pdMS_TO_TICKS(delay_ms > 0 ? delay_ms : 1), 0);
}

static void persist_last_peer(const bda_t bda) {
    char mac[18];
    snprintf(mac, sizeof(mac), "%02X:%02X:%02X:%02X:%02X:%02X",
             bda[0], bda[1], bda[2], bda[3], bda[4], bda[5]);
    nvs_handle_t nvs;
    if (nvs_open("btphone", NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_set_str(nvs, "last_peer", mac);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
}

void bt_gap_note_peer(const bda_t bda) {
    memcpy(s_peer, bda, 6);
    s_peer_valid = true;
}

void bt_gap_note_peer_connected(const bda_t bda) {
    memcpy(s_peer, bda, 6);
    s_peer_valid = true;
    /* 只在"确认用上"的时刻持久化(配对成功/profile 连上):
     * 连接尝试/断开的回调里对端可能是台不在线的设备,写进去会让
     * 上电回连去呼一台不存在的设备。 */
    persist_last_peer(bda);
}

bool bt_gap_peer_valid(void) { return s_peer_valid; }

void bt_gap_query_rssi(void) {
    if (s_peer_valid)
        esp_bt_gap_read_rssi_delta(s_peer);
}

static void emit_pair_result(const bda_t bda, bool ok, const char *reason) {
    char body[160];
    snprintf(body, sizeof(body),
             "{\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\",\"ok\":%s,\"reason\":\"%s\"}",
             bda[0], bda[1], bda[2], bda[3], bda[4], bda[5],
             ok ? "true" : "false", reason ? reason : "");
    host_send_event("pair.result", body);
}

/* -------- 名称 / 可见性 -------- */

const char *bt_gap_get_name(void) { return s_name; }

void bt_gap_set_name(const char *name) {
    strncpy(s_name, name, sizeof(s_name) - 1);
    s_name[sizeof(s_name) - 1] = '\0';
    esp_bt_gap_set_device_name(s_name);
    /* 改名后必须重应用 scan mode:Bluedroid 的 EIR(inquiry 应答里的设备名)
     * 不会跟着 set_device_name 自动刷新,不重刷手机端 inquiry 拿不到新名字,
     * 实测表现即"改名后搜索不到设备"。bt_scan_mode_str 会同步上报事件,
     * PC 端可据此确认广播已带新名。 */
    bt_gap_set_scan_mode_str(s_scan_mode, 0);
    nvs_handle_t nvs;
    if (nvs_open("btphone", NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_set_str(nvs, "name", s_name);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
}

void bt_gap_set_scan_mode_str(const char *mode, int timeout_s) {
    (void)timeout_s; /* TODO: 定时回收可见性 */
    strncpy(s_scan_mode, mode, sizeof(s_scan_mode) - 1);
    s_scan_mode[sizeof(s_scan_mode) - 1] = '\0';
    esp_bt_connection_mode_t conn = ESP_BT_NON_CONNECTABLE;
    esp_bt_discovery_mode_t disc = ESP_BT_NON_DISCOVERABLE;
    if (strcmp(mode, "conn") == 0) { conn = ESP_BT_CONNECTABLE; }
    else if (strcmp(mode, "disc") == 0) { conn = ESP_BT_CONNECTABLE; disc = ESP_BT_GENERAL_DISCOVERABLE; }
    else if (strcmp(mode, "disc_conn") == 0) { conn = ESP_BT_CONNECTABLE; disc = ESP_BT_GENERAL_DISCOVERABLE; }
    esp_bt_gap_set_scan_mode(conn, disc);
    s_discoverable = (disc == ESP_BT_GENERAL_DISCOVERABLE);
    host_send_event_str("bt.scan_mode", "mode", mode);
}

/* -------- 配对策略 -------- */

void bt_gap_set_pair_mode(pair_mode_t mode) { s_pair_mode = mode; }
pair_mode_t bt_gap_get_pair_mode(void) { return s_pair_mode; }

esp_err_t bt_gap_set_pair_mode_str(const char *mode) {
    if (strcmp(mode, "auto") == 0) s_pair_mode = PAIR_AUTO;
    else if (strcmp(mode, "manual") == 0) s_pair_mode = PAIR_MANUAL;
    else if (strcmp(mode, "reject") == 0) s_pair_mode = PAIR_REJECT;
    else if (strcmp(mode, "wrong_pin") == 0) s_pair_mode = PAIR_WRONG_PIN;
    else if (strcmp(mode, "timeout") == 0) s_pair_mode = PAIR_TIMEOUT;
    else return ESP_ERR_INVALID_ARG;
    return ESP_OK;
}

void bt_gap_set_auto_accept(bool on) { s_auto_accept = on; }
bool bt_gap_get_auto_accept(void) { return s_auto_accept; }
void bt_gap_set_auto_reconnect(bool on) { s_auto_reconnect = on; }
bool bt_gap_get_auto_reconnect(void) { return s_auto_reconnect; }

void bt_gap_confirm(bool accept) {
    if (!s_pending_valid) return;
    esp_bt_gap_ssp_confirm_reply(s_pending_bda, accept);
    s_pending_valid = false;
    if (!accept) emit_pair_result(s_pending_bda, false, "rejected");
}

/* -------- 绑定管理 -------- */

int bt_gap_get_bonded(bda_t *list, char names[][64], int max) {
    int num = esp_bt_gap_get_bond_device_num();
    if (num > max) num = max;
    esp_bd_addr_t *raw = (esp_bd_addr_t *)list;
    esp_bt_gap_get_bond_device_list(&num, raw);
    /* 远端名称需要 EIR/查询,这里留空,PC 侧用自己维护的车机别名即可 */
    for (int i = 0; i < num; i++) {
        if (names) names[i][0] = '\0';
    }
    return num;
}

esp_err_t bt_gap_remove_bond_str(const char *mac) {
    unsigned int b[6];
    if (sscanf(mac, "%02X:%02X:%02X:%02X:%02X:%02X",
               &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6)
        return ESP_ERR_INVALID_ARG;
    esp_bd_addr_t addr;
    for (int i = 0; i < 6; i++) addr[i] = (uint8_t)b[i];
    return esp_bt_gap_remove_bond_device(addr);
}

void bt_gap_get_status_json(char *out, size_t len) {
    char conn[192];
    if (s_peer_valid)
        snprintf(conn, sizeof(conn),
                 "{\"peer\":\"%02X:%02X:%02X:%02X:%02X:%02X\",\"a2dp\":\"%s\","
                 "\"avrcp\":%s,\"hfp\":%s}",
                 s_peer[0], s_peer[1], s_peer[2], s_peer[3], s_peer[4], s_peer[5],
                 a2dp_state_str(),
                 avrcp_rc_connected() ? "true" : "false",
                 hfp_slc_connected() ? "true" : "false");
    else
        snprintf(conn, sizeof(conn),
                 "{\"a2dp\":\"%s\",\"avrcp\":false,\"hfp\":false}", a2dp_state_str());
    snprintf(out, len,
             "{\"name\":\"%s\",\"discoverable\":%s,"
             "\"pair_mode\":\"%s\",\"connections\":%s}",
             s_name, s_discoverable ? "true" : "false",
             s_pair_mode == PAIR_AUTO ? "auto" : s_pair_mode == PAIR_MANUAL ? "manual" :
             s_pair_mode == PAIR_REJECT ? "reject" : s_pair_mode == PAIR_WRONG_PIN ? "wrong_pin" : "timeout",
             conn);
}

void bt_gap_boot_reconnect(void) {
    if (!s_auto_reconnect) return;
    nvs_handle_t nvs;
    char mac[24];
    size_t mlen = sizeof(mac);
    if (nvs_open("btphone", NVS_READONLY, &nvs) != ESP_OK) return;
    bool ok = nvs_get_str(nvs, "last_peer", mac, &mlen) == ESP_OK;
    nvs_close(nvs);
    unsigned int b[6];
    bda_t bda;
    if (!ok || sscanf(mac, "%02X:%02X:%02X:%02X:%02X:%02X",
                      &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6)
        return;
    for (int i = 0; i < 6; i++) bda[i] = (uint8_t)b[i];
    memcpy(s_peer, bda, 6);
    s_peer_valid = true;
    /* 3s:避开启动期射频电流冲击,也给 PC 侧留出串口建连窗口 */
    bt_gap_schedule_profile_connect(bda, 3000);
}

void bt_gap_factory_reset(void) {
    nvs_handle_t nvs;
    if (nvs_open("btphone", NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_erase_all(nvs);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
    esp_restart();
}

/* -------- GAP 回调 -------- */

static void gap_cb(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *param) {
    switch (event) {
    case ESP_BT_GAP_AUTH_CMPL_EVT: {
        char body[160];
        snprintf(body, sizeof(body),
                 "{\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\",\"ok\":%s,\"status\":%d}",
                 param->auth_cmpl.bda[0], param->auth_cmpl.bda[1], param->auth_cmpl.bda[2],
                 param->auth_cmpl.bda[3], param->auth_cmpl.bda[4], param->auth_cmpl.bda[5],
                 param->auth_cmpl.stat == ESP_BT_STATUS_SUCCESS ? "true" : "false",
                 (int)param->auth_cmpl.stat);
        host_send_event("pair.result", body);
        if (param->auth_cmpl.stat == ESP_BT_STATUS_SUCCESS) {
            /* 配对完成 ACL 已起:记为回连对象,并主动报一次链路信号 */
            bt_gap_note_peer_connected(param->auth_cmpl.bda);
            esp_bt_gap_read_rssi_delta(param->auth_cmpl.bda);
            /* 配对成功后像真手机一样主动拉起 A2DP+HFP。
             * 2026-09-04 实测:车机配对后不会主动连任何 profile
             * (默认 CoD 是"音箱"),没有 profile 连接,音乐/通话全部无效。 */
            bt_gap_schedule_profile_connect(param->auth_cmpl.bda, 1500);
        }
        break;
    }
    case ESP_BT_GAP_READ_RSSI_DELTA_EVT: {
        /* rssi_delta:0=在基准接收区间内(很强),负值=低多少 dB */
        char body[96];
        snprintf(body, sizeof(body),
                 "{\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\",\"rssi_delta\":%d}",
                 param->read_rssi_delta.bda[0], param->read_rssi_delta.bda[1],
                 param->read_rssi_delta.bda[2], param->read_rssi_delta.bda[3],
                 param->read_rssi_delta.bda[4], param->read_rssi_delta.bda[5],
                 (int)param->read_rssi_delta.rssi_delta);
        host_send_event("bt.rssi", body);
        break;
    }
    case ESP_BT_GAP_CFM_REQ_EVT: {
        /* SSP 数字确认:按策略自动/拒绝/不响应 */
        char body[160];
        snprintf(body, sizeof(body),
                 "{\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\",\"method\":\"ssp_confirm\",\"passkey\":%lu}",
                 param->cfm_req.bda[0], param->cfm_req.bda[1], param->cfm_req.bda[2],
                 param->cfm_req.bda[3], param->cfm_req.bda[4], param->cfm_req.bda[5],
                 (unsigned long)param->cfm_req.num_val);
        host_send_event("pair.request", body);
        memcpy(s_pending_bda, param->cfm_req.bda, 6);
        s_pending_valid = true;
        if (s_pair_mode == PAIR_AUTO && s_auto_accept) {
            esp_bt_gap_ssp_confirm_reply(param->cfm_req.bda, true);
            s_pending_valid = false;
        } else if (s_pair_mode == PAIR_REJECT) {
            esp_bt_gap_ssp_confirm_reply(param->cfm_req.bda, false);
            s_pending_valid = false;
        } else if (s_pair_mode == PAIR_TIMEOUT) {
            /* 不响应 → 链路超时,配对失败 */
        } else {
            /* manual/wrong_pin:等 PC 指令 */
        }
        break;
    }
    case ESP_BT_GAP_PIN_REQ_EVT:
        /* 传统 PIN:wrong_pin 模式回错误 PIN */
        {
            esp_bt_pin_code_t pin;
            int len;
            if (s_pair_mode == PAIR_WRONG_PIN) {
                memcpy(pin, "9999", 4);
                len = 4;
            } else {
                memcpy(pin, "1234", 4);
                len = 4;
            }
            esp_bt_gap_pin_reply(param->pin_req.bda, true, len, pin);
        }
        break;
    case ESP_BT_GAP_DISC_STATE_CHANGED_EVT:
    case ESP_BT_GAP_MODE_CHG_EVT:
    default:
        break;
    }
}

void bt_gap_init(void) {
    esp_bt_gap_register_callback(gap_cb);
    /* IO 能力:DisplayOnly;车机侧通常发起 SSP 数字确认 */
    esp_bt_io_cap_t iocap = ESP_BT_IO_CAP_OUT;
    esp_bt_gap_set_security_param(ESP_BT_SP_IOCAP_MODE, &iocap, sizeof(iocap));
    /* 传统 PIN 兜底 */
    esp_bt_pin_code_t pin;
    memcpy(pin, "1234", 4);
    esp_bt_gap_set_pin(ESP_BT_PIN_TYPE_VARIABLE, 4, pin);

    /* CoD:智能手机(0x5A020C)。协议栈默认 CoD 是"HiFi 音频设备(音箱)",
     * 车机不把音箱当手机——配对后不拉 HFP/A2DP,音乐/通话全无响应
     * (2026-09-04 实测定案,见 docs/NOTES.md)。 */
    esp_bt_cod_t cod = { 0 };
    cod.minor = 0x03;    /* Smartphone */
    cod.major = 0x02;    /* Phone */
    cod.service = 0x02D0; /* Audio|Telephony|Information|Capturing */
    esp_bt_gap_set_cod(cod, ESP_BT_INIT_COD);

    s_connect_timer = xTimerCreate("prof_conn", pdMS_TO_TICKS(1500),
                                   pdFALSE, NULL, connect_timer_cb);

    /* 恢复名称 */
    nvs_handle_t nvs;
    char name[64];
    size_t len = sizeof(name);
    if (nvs_open("btphone", NVS_READONLY, &nvs) == ESP_OK) {
        if (nvs_get_str(nvs, "name", name, &len) == ESP_OK) {
            memcpy(s_name, name, sizeof(s_name));
        }
        nvs_close(nvs);
    }
    esp_bt_gap_set_device_name(s_name);
}
