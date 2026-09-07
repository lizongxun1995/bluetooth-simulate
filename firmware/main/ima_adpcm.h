/* IMA-ADPCM 解码(与 PC 端 btphone/codec_adpcm.py 格式互逆) */
#pragma once
#include <stdint.h>
#include <stddef.h>

#define BLOCK_SAMPLES 1023 /* 每块每声道样本数(奇数;首样本存块头) */

/* 解码 ADPCM → 交错 s16le PCM。
 * in/in_len: ADPCM 字节流(按块对齐的完整块序列)
 * channels: 声道数;out: 输出 PCM;out_max_samples: 每声道最大样本数
 * 返回实际写出的每声道样本数。 */
size_t adpcm_decode(const uint8_t *in, size_t in_len, int channels,
                    int16_t *out, size_t out_max_samples);
