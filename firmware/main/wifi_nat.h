/* WiFi:STA/AP/扫描/状态(NAT 共享在 Phase 2 接入) */
#pragma once
#include "esp_err.h"
#include <stddef.h>

esp_err_t wifi_nat_init(void);
void wifi_mark_disabled(void);
esp_err_t wifi_sta_join(const char *ssid, const char *password);
esp_err_t wifi_ap_start(const char *ssid, const char *password);
esp_err_t wifi_ap_stop(void);
const char *wifi_init_fail_step(void);
int wifi_init_err(void);
const char *wifi_heap_trace(void);
esp_err_t wifi_scan(char *json_out, size_t len, int *count, int max_aps);
void wifi_status_json(char *out, size_t len);
