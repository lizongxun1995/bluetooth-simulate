/* SPSC 环形缓冲(单生产者/单消费者,仅用 volatile 读写序号,无需加锁)。
 * 用于:UART 音频帧 -> 解码任务 -> A2DP/HFP 馈送。 */
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include "esp_attr.h"

typedef struct {
    uint8_t *buf;
    size_t size;      /* 容量(字节) */
    volatile size_t head; /* 写位置 */
    volatile size_t tail; /* 读位置 */
} ringbuf_t;

static inline void ring_init(ringbuf_t *r, uint8_t *storage, size_t size) {
    r->buf = storage;
    r->size = size;
    r->head = 0;
    r->tail = 0;
}

static inline size_t ring_used(const ringbuf_t *r) {
    if (!r->size) return 0;
    size_t h = r->head, t = r->tail;
    return (h >= t) ? (h - t) : (r->size - t + h);
}

static inline size_t ring_free(const ringbuf_t *r) {
    /* size==0(未初始化)时报 0:报 SIZE_MAX 会让 PC 信用点爆炸狂推数据 */
    if (!r->size) return 0;
    return r->size - 1 - ring_used(r);
}

/* 尽量写入,返回实际写入字节数 */
static inline size_t ring_write(ringbuf_t *r, const uint8_t *data, size_t len) {
    if (!r->size) return 0;
    size_t free_space = ring_free(r);
    if (len > free_space) len = free_space;
    size_t h = r->head;
    size_t first = r->size - h;
    if (first > len) first = len;
    memcpy(r->buf + h, data, first);
    if (len > first) memcpy(r->buf, data + first, len - first);
    r->head = (h + len) % r->size;
    return len;
}

/* 尽量读出,返回实际读取字节数 */
static inline size_t ring_read(ringbuf_t *r, uint8_t *out, size_t len) {
    if (!r->size) return 0; /* 未初始化:无数据可读(勿碰 % r->size,除零panic) */
    size_t used = ring_used(r);
    if (len > used) len = used;
    size_t t = r->tail;
    size_t first = r->size - t;
    if (first > len) first = len;
    if (out) memcpy(out, r->buf + t, first);
    if (len > first && out) memcpy(out + first, r->buf, len - first);
    r->tail = (t + len) % r->size;
    return len;
}

/* 丢弃 len 字节(不拷贝),用于播放位置估算 */
static inline void ring_drop(ringbuf_t *r, size_t len) {
    if (!r->size) return;
    size_t used = ring_used(r);
    if (len > used) len = used;
    r->tail = (r->tail + len) % r->size;
}

/* 峰值查看(不移动读指针) */
static inline size_t ring_peek(const ringbuf_t *r, uint8_t *out, size_t len) {
    if (!r->size) return 0;
    size_t used = ring_used(r);
    if (len > used) len = used;
    size_t t = r->tail;
    size_t first = r->size - t;
    if (first > len) first = len;
    if (out) memcpy(out, r->buf + t, first);
    if (len > first && out) memcpy(out + first, r->buf, len - first);
    return len;
}
