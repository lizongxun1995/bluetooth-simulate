/* WiFi:STA 接入 + softAP 热点 + 扫描 + 状态(与经典蓝牙共存)。
 * NAT 网络共享(车机走电脑网络)在连通性验证通过后接入 esp_nat 组件。
 * 注意:所有 esp_wifi 调用经由 wifi_cmd 队列转投 wifi_task 执行,
 * 避免在 host_link 的串口任务里做阻塞网络操作。 */
#include "wifi_nat.h"
#include "btphone.h"
#include <stdio.h>
#include <string.h>
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_heap_caps.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "host_link.h"

/* 台架场景距离近,限制发射功率削电流尖峰/辐射(USB 链路会被干扰掉线)。
 * 单位 0.25dBm:8 = 2dBm,78 = 默认满功率 19.5dBm。 */
#define WIFI_TX_POWER_QDBM 8

static char s_sta_ssid[33] = "";
static volatile bool s_sta_connected = false;
static char s_sta_ip[16] = "";
static char s_ap_ssid[33] = "";
static bool s_wifi_started = false;
static const char *s_init_fail = NULL;  /* btphone: 初始化失败步骤(null=成功) */
static int s_wifi_err = 0;
static char s_heap_trace[128] = "";     /* btphone: 各初始化步骤堆水印(诊断 NO_MEM) */
static volatile int s_wifi_phase = 0;   /* btphone: do_ap_start/do_sta_join 执行阶段探针 */
static int s_start_err = 0;             /* btphone: esp_wifi_start 返回码 */
static QueueHandle_t s_cmd_queue;
static SemaphoreHandle_t s_lock;

/* 阶段号: 1=进入 2=set_mode完成 3=set_config完成 4=start完成 5=收尾完成 */
#define WIFI_PHASE_ENTER    1
#define WIFI_PHASE_MODE     2
#define WIFI_PHASE_CONFIG   3
#define WIFI_PHASE_STARTED  4
#define WIFI_PHASE_DONE     5

/* 记录一步:free_heap/largest_block,供 sys.info 读回 */
static void heap_mark(char step) {
    char one[24];
    snprintf(one, sizeof(one), "%c:%lu/%lu ",
             step, (unsigned long)esp_get_free_heap_size(),
             (unsigned long)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
    size_t used = strlen(s_heap_trace);
    if (used + strlen(one) < sizeof(s_heap_trace))
        strcpy(s_heap_trace + used, one);
}

typedef struct {
    int type; /* 0=sta join, 1=ap start, 2=ap stop */
    char ssid[33];
    char password[65];
} wifi_cmd_t;

static void notify_ip(void) {
    char body[80];
    snprintf(body, sizeof(body), "{\"connected\":%s,\"ip\":\"%s\"}",
             s_sta_connected ? "true" : "false", s_sta_ip);
    host_send_event("net.sta", body);
}

static void wifi_event_cb(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT) {
        if (id == WIFI_EVENT_STA_START) {
            /* 功率限制必须在 start 之后调用才生效 */
            esp_wifi_set_max_tx_power(WIFI_TX_POWER_QDBM);
            esp_wifi_connect();
        } else if (id == WIFI_EVENT_STA_DISCONNECTED) {
            s_sta_connected = false;
            s_sta_ip[0] = '\0';
            notify_ip();
        } else if (id == WIFI_EVENT_AP_START || id == WIFI_EVENT_STA_CONNECTED) {
            esp_wifi_set_max_tx_power(WIFI_TX_POWER_QDBM);
            if (id == WIFI_EVENT_STA_CONNECTED) s_sta_connected = true;
            notify_ip();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *evt = (ip_event_got_ip_t *)data;
        snprintf(s_sta_ip, sizeof(s_sta_ip), IPSTR, IP2STR(&evt->ip_info.ip));
        s_sta_connected = true;
        notify_ip();
    }
}

/* ---- 命令在 wifi_task 里执行 ---- */

static void do_sta_join(const char *ssid, const char *password) {
    wifi_config_t cfg = { 0 };
    strncpy((char *)cfg.sta.ssid, ssid, sizeof(cfg.sta.ssid) - 1);
    strncpy((char *)cfg.sta.password, password, sizeof(cfg.sta.password) - 1);
    esp_wifi_set_mode(WIFI_MODE_APSTA);
    esp_wifi_set_config(WIFI_IF_STA, &cfg);
    if (!s_wifi_started) {
        esp_wifi_start();
        s_wifi_started = true; /* STA_START 事件里自动 connect */
    } else {
        esp_wifi_disconnect();
        esp_wifi_connect();
    }
    strncpy(s_sta_ssid, ssid, sizeof(s_sta_ssid) - 1);
}

static void do_ap_start(const char *ssid, const char *password) {
    wifi_config_t cfg = { 0 };
    strncpy((char *)cfg.ap.ssid, ssid, sizeof(cfg.ap.ssid) - 1);
    cfg.ap.ssid_len = strlen(ssid);
    cfg.ap.max_connection = 4;
    cfg.ap.channel = 1;
    if (password && password[0]) {
        strncpy((char *)cfg.ap.password, password, sizeof(cfg.ap.password) - 1);
        cfg.ap.authmode = WIFI_AUTH_WPA2_PSK;
    } else {
        cfg.ap.authmode = WIFI_AUTH_OPEN;
    }
    s_wifi_phase = WIFI_PHASE_ENTER;
    esp_wifi_set_mode(WIFI_MODE_APSTA);
    s_wifi_phase = WIFI_PHASE_MODE;
    esp_wifi_set_config(WIFI_IF_AP, &cfg);
    s_wifi_phase = WIFI_PHASE_CONFIG;
    if (!s_wifi_started) {
        s_start_err = esp_wifi_start();
        s_wifi_started = true; /* STA_START 事件里自动 connect */
    }
    s_wifi_phase = WIFI_PHASE_STARTED;
    strncpy(s_ap_ssid, ssid, sizeof(s_ap_ssid) - 1);
    host_send_event_str("net.ap", "state", "started");
    s_wifi_phase = WIFI_PHASE_DONE;
}

static void do_ap_stop(void) {
    esp_wifi_stop();
    s_wifi_started = false;
    s_sta_connected = false;
    s_sta_ip[0] = '\0';
    s_ap_ssid[0] = '\0';
    host_send_event_str("net.ap", "state", "stopped");
    notify_ip();
}

static void wifi_task(void *arg) {
    wifi_cmd_t cmd;
    for (;;) {
        if (xQueueReceive(s_cmd_queue, &cmd, portMAX_DELAY) != pdTRUE) continue;
        switch (cmd.type) {
        case 0: do_sta_join(cmd.ssid, cmd.password); break;
        case 1: do_ap_start(cmd.ssid, cmd.password); break;
        case 2: do_ap_stop(); break;
        }
    }
}

static esp_err_t wifi_enqueue(int type, const char *ssid, const char *password) {
    if (!s_cmd_queue) return ESP_ERR_INVALID_STATE;
    wifi_cmd_t cmd = { 0 };
    cmd.type = type;
    if (ssid) strncpy(cmd.ssid, ssid, sizeof(cmd.ssid) - 1);
    if (password) strncpy(cmd.password, password, sizeof(cmd.password) - 1);
    return xQueueSend(s_cmd_queue, &cmd, pdMS_TO_TICKS(100)) == pdTRUE ? ESP_OK : ESP_ERR_TIMEOUT;
}

/* ---- 对 host_link 的接口(线程安全,仅入队) ---- */

esp_err_t wifi_sta_join(const char *ssid, const char *password) {
    if (!ssid || !ssid[0]) return ESP_ERR_INVALID_ARG;
    return wifi_enqueue(0, ssid, password ? password : "");
}

esp_err_t wifi_ap_start(const char *ssid, const char *password) {
    if (!ssid || !ssid[0]) return ESP_ERR_INVALID_ARG;
    return wifi_enqueue(1, ssid, password ? password : "");
}

esp_err_t wifi_ap_stop(void) {
    return wifi_enqueue(2, NULL, NULL);
}

/* 同步扫描:返回 AP 数量,json_out 输出 [{ssid,rssi,auth}](最多 max_aps 个)。
 * 阻塞约 1.5~3s,调用方(串口命令)需容忍。 */
esp_err_t wifi_scan(char *json_out, size_t len, int *count, int max_aps) {
    wifi_scan_config_t cfg = { .show_hidden = false };
    esp_err_t err = esp_wifi_scan_start(&cfg, true); /* 阻塞模式 */
    if (err != ESP_OK) return err;
    uint16_t n = 0;
    esp_wifi_scan_get_ap_num(&n);
    wifi_ap_record_t *records = calloc(n > 20 ? 20 : (n ? n : 1), sizeof(wifi_ap_record_t));
    if (!records) return ESP_ERR_NO_MEM;
    if (n > 20) n = 20;
    esp_wifi_scan_get_ap_records(&n, records);
    /* 按 RSSI 冒泡取前 max_aps */
    for (int i = 0; i < n - 1; i++)
        for (int j = 0; j < n - 1 - i; j++)
            if (records[j].rssi < records[j + 1].rssi) {
                wifi_ap_record_t t = records[j];
                records[j] = records[j + 1];
                records[j + 1] = t;
            }
    if (n > (uint16_t)max_aps) n = (uint16_t)max_aps;
    size_t pos = 0;
    pos += snprintf(json_out + pos, len - pos, "{\"aps\":[");
    for (int i = 0; i < n && pos < len - 96; i++) {
        const char *auth = records[i].authmode == WIFI_AUTH_OPEN ? "open" : "key";
        pos += snprintf(json_out + pos, len - pos, "%s{\"ssid\":\"%s\",\"rssi\":%d,\"auth\":\"%s\"}",
                        i ? "," : "", (const char *)records[i].ssid, records[i].rssi, auth);
    }
    pos += snprintf(json_out + pos, len - pos, "]}");
    *count = (int)n;
    free(records);
    return ESP_OK;
}

void wifi_status_json(char *out, size_t len) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    wifi_mode_t mode = WIFI_MODE_NULL;
    esp_err_t merr = esp_wifi_get_mode(&mode);
    snprintf(out, len,
             "{\"sta\":{\"ssid\":\"%s\",\"connected\":%s,\"ip\":\"%s\"},"
             "\"ap\":{\"ssid\":\"%s\"},\"started\":%s,"
             "\"phase\":%d,\"start_err\":%d,\"mode_err\":%d,\"mode\":%d}",
             s_sta_ssid, s_sta_connected ? "true" : "false", s_sta_ip,
             s_ap_ssid, s_wifi_started ? "true" : "false",
             s_wifi_phase, s_start_err, (int)merr, (int)mode);
    xSemaphoreGive(s_lock);
}

esp_err_t wifi_nat_init(void) {
    s_heap_trace[0] = '\0';
    heap_mark('0'); /* 进入时 */
    s_lock = xSemaphoreCreateMutex();
    if (esp_netif_init() != ESP_OK) { s_init_fail = "netif"; return ESP_FAIL; }
    heap_mark('1'); /* netif 完成 */
    if (esp_event_loop_create_default() != ESP_OK) { s_init_fail = "event_loop"; return ESP_FAIL; }
    heap_mark('2'); /* 事件循环完成 */
    esp_netif_create_default_wifi_sta();
    esp_netif_create_default_wifi_ap();
    heap_mark('3'); /* 默认 netif 完成 */
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_err_t werr = esp_wifi_init(&cfg);
    heap_mark('4'); /* esp_wifi_init 之后(成败都记) */
    if (werr != ESP_OK) {
        s_init_fail = esp_err_to_name(werr);
        s_wifi_err = (int)werr;
        return ESP_FAIL;
    }
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_cb, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_cb, NULL);
    esp_wifi_set_storage(WIFI_STORAGE_RAM);
    esp_wifi_set_ps(WIFI_PS_NONE); /* 与蓝牙共存时降低时隙抖动 */
    /* 注意:发射功率限制必须在 start 之后设置才生效,
     * 真正生效点在 wifi_event_cb 的 STA_START/AP_START 里(WIFI_TX_POWER_QDBM)。 */

    s_cmd_queue = xQueueCreate(4, sizeof(wifi_cmd_t));
    if (!s_cmd_queue) { s_init_fail = "queue(NO MEM)"; return ESP_ERR_NO_MEM; }
    if (xTaskCreate(wifi_task, "wifi_cmd", 4096, NULL, 7, NULL) != pdPASS) { s_init_fail = "task(NO MEM)"; return ESP_ERR_NO_MEM; }
    return ESP_OK;
}

void wifi_mark_disabled(void) { s_init_fail = "disabled"; }
const char *wifi_init_fail_step(void) { return s_init_fail; }
int wifi_init_err(void) { return s_wifi_err; }
const char *wifi_heap_trace(void) { return s_heap_trace; }
