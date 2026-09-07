/* IMA-ADPCM 解码器(与 PC 端 btphone/codec_adpcm.py 块格式一致):
 * 每块按声道分段,每段 [predictor s16 = 首样本][step_index u8][pad u8] + nibble 数据。
 * 4bit/样本,恒定 4:1 压缩。 */
#include "ima_adpcm.h"
#include <string.h>

static const int16_t step_table[90] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
    724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
    3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767
};

static const int8_t index_table[8] = { -1, -1, -1, -1, 2, 4, 6, 8 };

static inline int clamp16(int v) {
    if (v > 32767) return 32767;
    if (v < -32768) return -32768;
    return v;
}

/* 解码一段 ADPCM(必须是完整的块序列)为交错的 s16 PCM。
 * 返回写出的样本数(每声道)。数据不完整时尽量解码并返回已解出的量。 */
size_t adpcm_decode(const uint8_t *in, size_t in_len, int channels,
                    int16_t *out, size_t out_max_samples /*每声道*/) {
    const size_t nibbles_per_block = BLOCK_SAMPLES - 1; /* 首样本在块头 */
    const size_t bytes_per_ch_block = 4 + (nibbles_per_block + 1) / 2; /* 511+4 */
    const size_t block_len = bytes_per_ch_block * channels;
    size_t out_pos = 0;

    while (in_len >= block_len && out_pos < out_max_samples) {
        for (int ch = 0; ch < channels; ch++) {
            const uint8_t *seg = in + ch * bytes_per_ch_block;
            int predictor = (int16_t)(seg[0] | (seg[1] << 8));
            int step_index = seg[2];
            const uint8_t *body = seg + 4;

            if (out_pos < out_max_samples)
                out[out_pos * channels + ch] = (int16_t)predictor;

            int step = step_table[step_index];
            size_t nib_count = (block_len / channels - 4) * 2;
            if (out_pos + nib_count > out_max_samples)
                nib_count = out_max_samples - out_pos;

            for (size_t i = 0; i < nib_count; i++) {
                uint8_t nib = (i & 1) ? (body[i / 2] >> 4) : (body[i / 2] & 0x0F);
                int delta = step >> 3;
                if (nib & 4) delta += step;
                if (nib & 2) delta += step >> 1;
                if (nib & 1) delta += step >> 2;
                predictor += (nib & 8) ? -delta : delta;
                predictor = clamp16(predictor);
                step_index += index_table[nib & 7];
                if (step_index < 0) step_index = 0;
                if (step_index > 88) step_index = 88;
                step = step_table[step_index];
                out[(out_pos + 1 + i) * channels + ch] = (int16_t)predictor;
            }
        }
        out_pos += 1 + nibbles_per_block;
        in += block_len;
        in_len -= block_len;
    }
    return out_pos;
}
