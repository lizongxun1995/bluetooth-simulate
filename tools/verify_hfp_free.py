# -*- coding: utf-8 -*-
"""验证: 无 HFP 主动回连干扰下的 a2dp 稳定性 + play 触发点定位。

阶段 A: 等回连后静置 25s —— 链路应当稳定(此前 HFP 周期性被踢+连坐掉线)
阶段 B: 跳过 avrcp.metadata, 手动 audio.open → music.play → 推流
        —— 若不再 play 时掉线, 说明触发点是元数据推送
阶段 C: 完整 p.music.play() (含 metadata) 复验
"""
import sys, time, threading
sys.path.insert(0, r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate")

from btphone import BtPhone, media, codec_adpcm
from btphone.client_audio import push_audio_stream

TONE = r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate\tools\_probe_tone.wav"
CAR = "00:11:22:33:44:55"
T0 = time.monotonic()

drops = []          # (t, reason)
def ts():
    return f"[{time.monotonic()-T0:6.1f}s]"

def on_conn(name, data):
    if data.get("peer") != CAR:
        return
    prof = data.get("profile", "?")
    st = data.get("state", "?")
    line = f"{ts()} conn {prof} -> {st}"
    if prof == "a2dp" and st == "disconnected":
        reason = data.get("reason", "?")
        drops.append((time.monotonic()-T0, reason))
        line += f"  reason={reason}"
    print(line, flush=True)

p = BtPhone("COM8", connect=False)
p.events.on("bt.conn", on_conn)
p._transport.open()

print(f"{ts()} fw=", p.request("sys.info", {}) .get("fw_version"), flush=True)

# ---- 阶段 A: 等回连 + 静置 ----
deadline = time.monotonic() + 60
a2dp = None
while time.monotonic() < deadline:
    try:
        conn = (p.request("conn.status", {}, timeout=4).get("connections") or {})
        a2dp = str(conn.get("a2dp"))
        if a2dp == "connected":
            break
    except Exception as e:
        print(f"{ts()} status err: {e}", flush=True)
    time.sleep(1)
print(f"{ts()} 阶段A: a2dp={a2dp}", flush=True)
if a2dp != "connected":
    print("FAIL: 60s 内 a2dp 未回连", flush=True)
    sys.exit(1)

hold_end = time.monotonic() + 25
while time.monotonic() < hold_end:
    time.sleep(5)
    try:
        conn = (p.request("conn.status", {}, timeout=4).get("connections") or {})
        info = p.request("sys.info", {}, timeout=4)
        print(f"{ts()} hold: a2dp={conn.get('a2dp')} hfp={conn.get('hfp')} "
              f"heap={info.get('free_heap')}", flush=True)
    except Exception as e:
        print(f"{ts()} hold status err: {e}", flush=True)

a_drops = len(drops)
print(f"{ts()} 阶段A 结束: 期间 a2dp 掉线 {a_drops} 次", flush=True)

# ---- 阶段 B: 无 metadata 的播放 ----
decoded = media.decode_audio(TONE, rate=44100, channels=2)
adpcm = codec_adpcm.encode(decoded.pcm, decoded.channels)
t_play = time.monotonic()
print(f"{ts()} 阶段B: audio.open (无 avrcp.metadata)", flush=True)
r = p.request("audio.open", {"sink": "a2dp", "rate": 44100, "channels": 2, "codec": "adpcm"})
p._transport.set_audio_budget(int(r.get("buffer_size", 65536)))
print(f"{ts()} music.play", flush=True)
p.request("music.play", {})
print(f"{ts()} 推流 {len(adpcm)} bytes", flush=True)
push_audio_stream(p._transport, adpcm)
# 等缓冲播完 → music.ended
try:
    p.events.wait_event("music.ended", timeout=15)
    print(f"{ts()} 阶段B: music.ended 到达 ✓ (play后 {time.monotonic()-t_play:.1f}s)", flush=True)
except Exception:
    print(f"{ts()} 阶段B: music.ended 未到(15s)", flush=True)
b_drops = len(drops)
print(f"{ts()} 阶段B 结束: 累计掉线 {b_drops} 次", flush=True)

# ---- 阶段 C: 完整 API 播放 ----
t_play2 = time.monotonic()
print(f"{ts()} 阶段C: p.music.play() 完整路径(含 metadata)", flush=True)
p.music.play(files=[TONE])
try:
    p.events.wait_event("music.started", timeout=70)
    print(f"{ts()} 阶段C: music.started ✓ (play后 {time.monotonic()-t_play2:.1f}s)", flush=True)
except Exception as e:
    print(f"{ts()} 阶段C: music.started 未到: {e}", flush=True)
try:
    p.events.wait_event("music.ended", timeout=30)
    print(f"{ts()} 阶段C: music.ended ✓", flush=True)
except Exception:
    print(f"{ts()} 阶段C: music.ended 未到(30s)", flush=True)
try:
    p.music.stop()
except Exception:
    pass
c_drops = len(drops)

print(f"\n==== 汇总 ====", flush=True)
print(f"阶段A(静置25s): {a_drops} 次掉线; 阶段B(无meta播放): +{b_drops-a_drops}; "
      f"阶段C(完整播放): +{c_drops-b_drops}", flush=True)
for t, reason in drops:
    print(f"  drop @ {t:.1f}s reason={reason}", flush=True)
try:
    info = p.request("sys.info", {}, timeout=8)
    print(f"结束: reset={info.get('boot_reset_reason')} heap={info.get('free_heap')}", flush=True)
except Exception as e:
    print(f"结束: sys.info 失败 {e}", flush=True)
p.close()
