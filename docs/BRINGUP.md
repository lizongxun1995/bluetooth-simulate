# 硬件联调清单(Bring-up Checklist)

固件源码已按 ESP-IDF v5.x API 编写,但**尚未在真板上编译验证**(本仓库开发阶段无硬件)。
首次烧录前按本清单逐项确认;以下列出已知需要对照本地 IDF 版本核对的风险点。

## 编译前

- [ ] `idf.py set-target esp32` 后确认 `sdkconfig` 中:
  - `CONFIG_BT_CLASSIC_ENABLED=y`、`CONFIG_BT_A2DP_ENABLE=y`
  - HFP:不同版本选项名有差异(`CONFIG_BT_HFP_ENABLE` / `CONFIG_BT_HFP_AG_ENABLE` /
    `CONFIG_BT_HFP_CLIENT_ENABLE`),**必须启用 AG(Audio Gateway)角色**
- [ ] 分区表 `partitions.csv` 已生效(assets 分区 ~2.3MB FAT)
- [ ] 运行日志已关(`CONFIG_LOG_DEFAULT_LEVEL_NONE`),否则日志字节会污染协议通道

## 已知需对齐的 API(全部集中在单文件,改动面可控)

| 文件 | 风险点 | 处理 |
|---|---|---|
| `hfp_ag.c` | `esp_hfp_ag_*` 事件枚举/AT 事件结构在不同版本不同 | 对照本地 `esp_hfp_ag.h` 调整;AG 回调事件以 `ESP_HFP_AG_*` 前缀搜索 |
| `avrcp_tg.c` | 元数据属性应答(`esp_avrc_tg_send_rsp` 结构)与通知注册细节 | 对照本地 `esp_avrc_api.h`;先跑通 passthrough 按键,再补元数据 |
| `a2dp_source.c` | 数据回调签名(`esp_a2d_source_register_data_callback`)基本稳定 | 若返回值约定不同,按 `examples/bluetooth/bluedroid/classic_bt/a2dp_source` 示例对齐 |
| `clip_store.c` | `esp_vfs_fat_spiflash_mount_rw_wl` 函数名在 v5 各小版本有更名 | v5.1+:同右;旧版:`esp_vfs_fat_spiflash_mount` |
| `sdkconfig.defaults` | `CONFIG_BTPHONE_*` 为本项目自定义项,需在 menuconfig 里建项或删除 | 简单做法:删除该行,波特率在 `host_link.c` 宏中改 |

## 上电顺序检查

1. 烧录后上电,PC 打开串口(2Mbaud)应能 `sys.info` 握手;
2. `discover on` → 手机搜索应能看到 "VPHONE-01"(此时手机可当临时"车机"做冒烟测试!);
3. 手机配对后手机蓝牙音频输出选这块板 → `music play` 应能从手机听到推流内容;
4. 以上都通过后再接真实车机。

## 通话语音通道说明

HFP 的 SCO/eSCO 音频数据路径需要 `sdkconfig` 中配置 PCM/I2S 数据路径
(不同版本选项如 `CONFIG_BT_HFP_AUDIO_DATA_PATH` / `CONFIG_BT_PCM_*`)。
MVP 阶段验证通话状态机与信令即可;要把"通话语音"真实送进车机,需按开发板
接一个 I2S DAC(或使用带编解码的底板),在 `audio_pull_hfp()` 与 I2S 之间接线。
这是 Phase 1 尾部/Phase 2 的工作,不影响信令级测试(呼入/接听/挂断/状态上报)。
