/* 音频管线:UART/Flash ADPCM 流 → 解码 → PCM 环 → A2DP(SBC 由协议栈编码)/HFP 语音 */
#include "audio_pipeline.h"
#include "btphone.h"
#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_heap_caps.h"
#include "ima_adpcm.h"
#include "host_link.h"
#include "ringbuf.h"

/* 下行缓冲大小:PC 端信用点额度,需能吸收串口突发(约 1s 音频)。
 * 2026-09-04 缩减:原 24K+32K+暂存共 ~78K,与 A2DP/HFP 并存时堆只剩 12K,
 * 协议栈运行期再 malloc 会失衡。信用点流控下小环即可自稳。
 * 2026-09-01 再缩 41K→20K:实测 a2dp+avrcp 连接后堆 ~3K,HFP 的
 * L2CAP/RFCOMM 建链缓冲挤不进去,车机后补发起的 HFP 也建不起来;
 * 8K 流环 ≈0.18s、12K PCM 环 ≈0.07s,信用流控下够用。 */
#define STREAM_RING_SIZE (8 * 1024)               /* ADPCM/原始流(约0.18s @44.1k立体声) */
#define PCM_RING_SIZE    (12 * 1024)              /* 解码后 PCM(约 0.07s 44.1k 立体声) */
#define DECODE_CHUNK   (BLOCK_SAMPLES * 2)        /* 每次解码的流字节数上限 */
#define DEC_PCM_SAMPLES (DECODE_CHUNK * 2)        /* 单次解码最大样本数(每声道):
                                                  * 输入 DECODE_CHUNK*2 字节 = 每 2 字节
                                                  * 4 个 nibble = 2 样本/字节,立体声均摊 */

static ringbuf_t s_stream_ring, s_pcm_ring;
static uint8_t *s_stream_storage, *s_pcm_storage;
static uint8_t *s_dec_in;   /* 解码输入暂存(堆分配,按需) */
static int16_t *s_dec_pcm;  /* 解码输出暂存(堆分配,按需) */
static TaskHandle_t s_decode_task;
static volatile bool s_alloc_done = false;  /* btphone: 首次 audio.open 才分配,不与 WiFi 争启动内存 */

static struct {
    audio_sink_t sink;
    int rate, channels;
    audio_codec_t codec;
    bool playing, eos;
    uint32_t consumed_bytes;
} s_cfg = { AUDIO_SINK_A2DP, 44100, 2, AUDIO_CODEC_ADPCM, false, false, 0 };

static void emit_underrun(void) {
    /* 限频:下潜可能持续存在(如 PC 推流断流),每 1s 报一次即可。
     * 本函数可能从 BTC 任务(A2DP 拉流回调)调到,高频事件会拖慢协议栈 */
    static TickType_t s_last = 0;
    TickType_t now = xTaskGetTickCount();
    if (now - s_last < pdMS_TO_TICKS(1000)) return;
    s_last = now;
    host_send_event("audio.underrun", "{}");
}

static bool alloc_buffers(void) {
    if (s_alloc_done) return true;
    s_stream_storage = heap_caps_malloc(STREAM_RING_SIZE, MALLOC_CAP_8BIT | MALLOC_CAP_INTERNAL);
    s_pcm_storage = heap_caps_malloc(PCM_RING_SIZE, MALLOC_CAP_8BIT | MALLOC_CAP_INTERNAL);
    s_dec_in = heap_caps_malloc(DECODE_CHUNK * 2, MALLOC_CAP_8BIT | MALLOC_CAP_INTERNAL);
    s_dec_pcm = heap_caps_malloc(DEC_PCM_SAMPLES * sizeof(int16_t) * 2, MALLOC_CAP_8BIT | MALLOC_CAP_INTERNAL);
    if (!s_stream_storage || !s_pcm_storage || !s_dec_in || !s_dec_pcm) {
        /* 堆不足:全量回滚,保持未初始化态(size=0 的 ring 全部安全空转) */
        heap_caps_free(s_stream_storage); s_stream_storage = NULL;
        heap_caps_free(s_pcm_storage);    s_pcm_storage = NULL;
        heap_caps_free(s_dec_in);         s_dec_in = NULL;
        heap_caps_free(s_dec_pcm);        s_dec_pcm = NULL;
        return false;
    }
    ring_init(&s_stream_ring, s_stream_storage, STREAM_RING_SIZE);
    ring_init(&s_pcm_ring, s_pcm_storage, PCM_RING_SIZE);
    s_alloc_done = true;
    return true;
}

bool audio_open(audio_sink_t sink, int rate, int channels, audio_codec_t codec) {
    if (!alloc_buffers()) return false; /* 堆不足:open 失败,上层报错 */
    ring_drop(&s_stream_ring, ring_used(&s_stream_ring));
    ring_drop(&s_pcm_ring, ring_used(&s_pcm_ring));
    s_cfg.sink = sink;
    s_cfg.rate = rate ? rate : 44100;
    s_cfg.channels = channels ? channels : 2;
    s_cfg.codec = codec;
    s_cfg.playing = false;
    s_cfg.eos = false;
    s_cfg.consumed_bytes = 0;
    return true;
}

void audio_close(void) {
    s_cfg.playing = false;
    s_cfg.eos = false;
    ring_drop(&s_stream_ring, ring_used(&s_stream_ring));
    ring_drop(&s_pcm_ring, ring_used(&s_pcm_ring));
}

void audio_set_playing(bool playing) {
    s_cfg.playing = playing;
}

bool audio_is_playing(void) { return s_cfg.playing; }

bool audio_pipeline_ready(void) { return s_alloc_done; }

size_t audio_stream_buffer_size(void) { return STREAM_RING_SIZE; }

size_t audio_stream_free(void) {
    /* 信用点:只按流环的空闲量上报(PC 按压缩字节计) */
    return ring_free(&s_stream_ring);
}

uint32_t audio_position_ms(void) {
    /* 近似播放位置:已消耗流字节折算成时长 */
    size_t bytes_per_sec = (size_t)s_cfg.rate * s_cfg.channels *
                           (s_cfg.codec == AUDIO_CODEC_ADPCM ? 1 : 2);
    if (!bytes_per_sec) return 0;
    return (uint32_t)(s_cfg.consumed_bytes / bytes_per_sec * 1000);
}

void audio_consume_stream(const uint8_t *data, size_t len, bool eos) {
    if (len) {
        size_t written = ring_write(&s_stream_ring, data, len);
        if (written < len)
            emit_underrun(); /* 缓冲满被丢弃(PC 端流控下不应发生) */
        s_cfg.consumed_bytes += written;
    }
    if (eos) s_cfg.eos = true;
}

/* 解码任务:流环 → PCM 环 */
static void decode_loop(void *arg) {
    for (;;) {
        if (!s_alloc_done) { vTaskDelay(pdMS_TO_TICKS(50)); continue; }
        uint8_t *in = s_dec_in;
        int16_t *pcm = s_dec_pcm;
        size_t avail = ring_used(&s_stream_ring);
        if (avail == 0) {
            /* 流排空且收到过 EOS 帧(PC 每曲推完发空帧标记):整曲播完。
             * playing 不动——a2dp 拉空环继续输出静音,恰好兼作链路保活 */
            if (s_cfg.eos && s_cfg.sink == AUDIO_SINK_A2DP
                && ring_used(&s_pcm_ring) == 0) {
                s_cfg.eos = false;
                host_send_event("music.ended", "{}");
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        size_t take = avail > (size_t)(DECODE_CHUNK * 2) ? (size_t)(DECODE_CHUNK * 2) : avail;
        /* 只取完整字节流(ADPCM 按块边界在 PC 端已保证 eos 之前字节连续可解) */
        ring_peek(&s_stream_ring, in, take);
        size_t samples = 0;
        if (s_cfg.codec == AUDIO_CODEC_ADPCM) {
            samples = adpcm_decode(in, take, s_cfg.channels, pcm, DEC_PCM_SAMPLES / s_cfg.channels);
        } else {
            /* PCM 直通 */
            samples = take / (2 * s_cfg.channels);
            memcpy(pcm, in, samples * 2 * s_cfg.channels);
        }
        if (samples == 0) { /* 不够一块,等待更多数据;EOS 后的残块解不动,
                              若不丢弃 avail 永不归零,music.ended 永不触发 */
            if (s_cfg.eos) {
                ring_drop(&s_stream_ring, ring_used(&s_stream_ring));
                continue;
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        ring_drop(&s_stream_ring, take);
        size_t bytes = samples * 2 * s_cfg.channels;
        size_t written = ring_write(&s_pcm_ring, (uint8_t *)pcm, bytes);
        (void)written;
    }
}

/* A2DP 馈送:协议栈拉 44.1k 立体声 s16 PCM */
int audio_pull_a2dp(uint8_t *buf, int32_t len) {
    if (!s_cfg.playing) return 0;
    size_t got = ring_read(&s_pcm_ring, buf, (size_t)len);
    if ((int32_t)got < len) {
        /* 下溢:补静音保持链路 */
        memset(buf + got, 0, (size_t)(len - got));
        emit_underrun();
    }
    return len;
}

/* HFP 语音馈送:16k 单声道 s16(经过协议栈 CVSD/mSBC 编码) */
int audio_pull_hfp(uint8_t *buf, int32_t len) {
    if (!hfp_call_active() || !hfp_voice_out()) {
        memset(buf, 0, (size_t)len);
        return len;
    }
    size_t got = ring_read(&s_pcm_ring, buf, (size_t)len);
    if ((int32_t)got < len)
        memset(buf + got, 0, (size_t)(len - got));
    return len;
}

void audio_pipeline_init(void) {
    /* 启动即预留音频缓冲(懒加载已废弃):
     * 1) WiFi 已禁用(BTPHONE_WIFI=0),启动期无内存竞争;
     * 2) 顺序决定生死——先让本管线占住 61K,协议栈/媒体通路在剩余堆上
     *    布局能正常工作(实测媒体先启动会吃掉大块堆,之后 audio.open
     *    永远分配失败,保活与播放互斥);
     * 3) 分配失败(极小概率)时管线保持 size=0 安全空转,audio.open 会报错。 */
    alloc_buffers();
    xTaskCreate(decode_loop, "audio_dec", 6144, NULL, 8, &s_decode_task);
}
