/* btphone 固件入口:BT 控制器 + Bluedroid + 各模块初始化 */
#include "btphone.h"
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "nvs_flash.h"
#include "esp_system.h"
#include "host_link.h"
#include "audio_pipeline.h"
#include "wifi_nat.h"

/* RTC 内存:跨软复位保持,真掉电清零。用于区分"芯片重启"和"整板掉电"。 */
RTC_DATA_ATTR static uint32_t s_rtc_boot_count;

uint32_t btphone_rtc_boot_count(void) { return s_rtc_boot_count; }

/* BT 初始化完成标志:弱供电链路会在射频上电瞬间(约3.7s)掉口数秒,
 * sys.ready 若恰好落进死窗就永远看不到。此标志让 PC 侧在链路恢复后
 * 用 sys.info 安静查询"app_main 是否走完",绕开死窗盲区。 */
static volatile bool s_bt_up = false;

bool btphone_bt_up(void) { return s_bt_up; }

static void bt_stack_ready(void) {
    /* 协议栈就绪后按默认策略:可连接 + 可见,等待车机配对 */
    bt_gap_init();
    /* avrcp_tg_init 必须先于 a2dp_source_init:btc 层的 g_av_with_rc 开关决定
     * A2DP 启用时的特性位,顺序反了 A2DP 会以"无 AVRCP"形态启用——
     * BTA_AvEnable 不带 RCTG 特性(SDP 发现对端后不主动连 AVRCP,bta_av_act.c
     * 的 AVCT_INT 分支条件不成立),且不建 AVCTP 接受者(车机主动连也被拒)。
     * IDF 源码 btc_avrc.c 初始化 TG 时对此有明确告警:
     * "AVRC Target is expected to be initialized in advance of A2DP !!!"
     * 2026-09-01 实测表现为 avrcp 永远 disconnected:车机不显示歌曲元数据、
     * 切歌按键无联动。 */
    avrcp_tg_init();
    a2dp_source_init();
    hfp_ag_init();
    bt_gap_set_scan_mode_str("disc_conn", 0);
    host_send_event_str("bt.scan_mode", "mode", "disc_conn");
    /* 像真手机一样回连上次的车机(受 auto_reconnect 策略控制) */
    bt_gap_boot_reconnect();
}

void app_main(void) {
    s_rtc_boot_count++;
    /* NVS(BT 与 WiFi 配置都依赖) */
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    host_link_init();      /* 串口协议尽早可用 */
    audio_pipeline_init();
#if BTPHONE_WIFI
    wifi_nat_init();       /* netif/事件循环/WiFi 初始化(不 start,等命令) */
#else
    wifi_mark_disabled();  /* WiFi 暂关,全部启动内存留给 BT */
#endif

    /* 这块板供电弱:经典蓝牙射频上电的电流冲击会砸穿 brownout 阈值,
     * 复位后再上电再跌 = 复位风暴。这里错开启动电流冲击,也给 PC 侧
     * 留出建立串口连接的窗口。 */
    vTaskDelay(pdMS_TO_TICKS(3000));

    /* 控制器:双模(经典蓝牙+BLE),与 WiFi 共存。
     * 每步之间发 sys.stage 事件,定位弱供电下的卡死/断链点。 */
    esp_err_t bterr;
    esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    host_send_event_str("sys.stage", "stage", "ctrl_init");
    bterr = esp_bt_controller_init(&bt_cfg);
    if (bterr != ESP_OK) {
        char msg[80];
        snprintf(msg, sizeof(msg), "bt controller init failed: 0x%x", (int)bterr);
        host_send_event_str("sys.error", "msg", msg);
        return;
    }
    bterr = esp_bt_controller_enable(ESP_BT_MODE_BTDM);
    if (bterr != ESP_OK) {
        char msg[80];
        snprintf(msg, sizeof(msg), "bt controller enable(BTDM) failed: 0x%x", (int)bterr);
        host_send_event_str("sys.error", "msg", msg);
        return;
    }
    host_send_event_str("sys.stage", "stage", "ctrl_on");
    bterr = esp_bluedroid_init();
    if (bterr == ESP_OK) bterr = esp_bluedroid_enable();
    if (bterr != ESP_OK) {
        char msg[80];
        snprintf(msg, sizeof(msg), "bluedroid init/enable failed: 0x%x", (int)bterr);
        host_send_event_str("sys.error", "msg", msg);
        return;
    }
    host_send_event_str("sys.stage", "stage", "bluedroid_on");

    /* 台架设备 USB 常供电,modem sleep 只有坏处:低速时钟是内部 150kHz RC
     * (无 32K 晶振),漂移会让 inquiry 扫描窗错拍——手机搜索列表里出现慢、
     * 排位靠后。关掉,让板子对 inquiry 全速响应。 */
    esp_bt_sleep_disable();

    bt_stack_ready();
    s_bt_up = true;
    host_send_event("sys.ready", "{\"fw\":\"" BTPHONE_FW_VERSION "\"}");
}
