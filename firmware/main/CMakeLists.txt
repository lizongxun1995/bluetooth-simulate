idf_component_register(
    SRCS
        app_main.c
        host_link.c
        ima_adpcm.c
        audio_pipeline.c
        bt_gap.c
        a2dp_source.c
        avrcp_tg.c
        hfp_ag.c
        wifi_nat.c
    INCLUDE_DIRS "."
    REQUIRES nvs_flash bt driver esp_wifi esp_event esp_netif json
)
