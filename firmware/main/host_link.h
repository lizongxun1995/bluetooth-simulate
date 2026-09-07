/* 串口协议层初始化 */
#pragma once

void host_link_init(void);
void host_send_event(const char *evt, const char *json_body);
void host_send_event_str(const char *evt, const char *key, const char *value);
void host_send_event_int(const char *evt, const char *key, long value);
