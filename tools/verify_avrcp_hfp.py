# -*- coding: utf-8 -*-
"""v0.3.3 双修复验证:
1. avrcp_tg_init 先于 a2dp_source_init → a2dp 连上后 avrcp 应自动 connected
   (之前永远 disconnected: 车机无元数据/无切歌联动)
2. CONFIG_BT_HFP_WBS_ENABLE=n → 显式连 HFP 不再被车机 5s 掐(嫌疑: mSBC 协商)
流程: 连板 → 等 a2dp → 等 avrcp → 播放(带元数据) → 连 HFP 监控 90s → 模拟来电。
"""
import sys, time
sys.path.insert(0, r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate")
from btphone import BtPhone, CommandError

CAR = "00:11:22:33:44:55"
TONE = r"C:\Users\you\Documents\AutoTestFramework\bluetooth-simulate\tools\_probe_tone.wav"
T0 = time.monotonic()
def ts():
    return f"[{time.monotonic()-T0:6.1f}s]"

def profile_state(p, name):
    """conn.status 里 a2dp 是字符串状态, avrcp/hfp 是布尔, 统一成 connected/disconnected"""
    conn = (p.request("conn.status", {}, timeout=10).get("connections") or {})
    v = conn.get(name)
    if v is True:
        return "connected"
    if v is False:
        return "disconnected"
    return str(v)

results = []
def check(label, ok):
    results.append((label, ok))
    print(f"{ts()} {'✓' if ok else '✗✗✗'} {label}", flush=True)

p = BtPhone("COM8")
def dump(name, data):
    if name in ("audio.buffer",):
        return
    if name.startswith(("bt.conn", "bt.hfp", "hfp.", "music.", "avrcp.", "sys.")):
        print(f"{ts()} EV {name} {dict(data)}", flush=True)
p.events.on("*", dump)
info = p.request("sys.info", {}, timeout=8)
print(f"{ts()} 固件 {info.get('fw')} heap={info.get('free_heap')}", flush=True)
check(f"固件版本 0.3.8 (实际 {info.get('fw')})", str(info.get("fw", "")).startswith("0.3.8"))

# --- 阶段1: 等 a2dp(boot_reconnect 只回连 a2dp) ---
print(f"{ts()} 等 a2dp 回连...", flush=True)
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        if profile_state(p, "a2dp") == "connected":
            break
    except Exception:
        pass  # 射频瞬断自愈窗口
    time.sleep(0.5)
def safe_state(p, name):
    try:
        return profile_state(p, name)
    except Exception:
        return "查询失败(瞬断自愈中)"

check("a2dp connected", safe_state(p, "a2dp") == "connected")
t_a2dp = time.monotonic()

# --- 阶段2: 等 avrcp(顺序修复的核心断言) ---
print(f"{ts()} 等 avrcp(修复前: 永远不连)...", flush=True)
deadline = time.monotonic() + 45
while time.monotonic() < deadline:
    try:
        if profile_state(p, "avrcp") == "connected":
            break
    except Exception:
        pass
    time.sleep(0.5)
avrcp_up = safe_state(p, "avrcp") == "connected"
check(f"avrcp connected (+{time.monotonic()-t_a2dp:.1f}s)", avrcp_up)

# --- 阶段3: 播放 + 元数据推送 ---
try:
    p.music.set_track_info(title="AVRCP验证曲", artist="VPHONE", album="v0.3.3")
    print(f"{ts()} 元数据已下发", flush=True)
except Exception as e:
    print(f"{ts()} set_track_info: {e}", flush=True)
p.music.play(files=[TONE])
try:
    p.events.wait_event("music.started", timeout=60)
    p.events.wait_event("music.ended", timeout=25)
    check("音乐播放完整(started→ended)", True)
except Exception as e:
    check(f"音乐播放完整: {e}", False)

# --- 阶段4: 显式连 HFP, 监控 90s(修复前 5s 被掐+连坐掉 ACL) ---
print(f"{ts()} 显式连接 HFP(WBS 已关)...", flush=True)
try:
    r = p.conn.connect(CAR, profiles=["hfp"])
    print(f"{ts()} conn.connect → {r}", flush=True)
except CommandError as e:
    print(f"{ts()} conn.connect: {e}", flush=True)
deadline = time.monotonic() + 20
while time.monotonic() < deadline and profile_state(p, "hfp") != "connected":
    time.sleep(0.5)
hfp_up = profile_state(p, "hfp") == "connected"
check("HFP SLC connected", hfp_up)
if hfp_up:
    t_hfp = time.monotonic()
    print(f"{ts()} 监控 90s(修复前 ~5s 即被掐)...", flush=True)
    kicked_at = None
    while time.monotonic() - t_hfp < 90:
        try:
            if profile_state(p, "hfp") != "connected":
                kicked_at = time.monotonic() - t_hfp
                break
        except Exception:
            pass
        time.sleep(1.0)
    check(f"HFP 存活 90s" + (f"(实际 {kicked_at:.1f}s 掉线!)" if kicked_at else ""),
          kicked_at is None)

    # --- 阶段5: 模拟来电(用户 GUI 报障的直接场景) ---
    if kicked_at is None:
        try:
            p.calls.incoming("13800138000")
            print(f"{ts()} hfp.incoming 已下发, 等响铃事件...", flush=True)
            p.events.wait_event("hfp.ring", timeout=15)
            check("模拟来电: hfp.ring 事件到达", True)
            p.calls.hangup()
            time.sleep(1.0)
            check("挂断后回 idle", profile_state(p, "hfp") == "connected")
        except Exception as e:
            check(f"模拟来电: {e}", False)

# --- 收尾: 留下 a2dp+avrcp(+hfp) 全连接状态给 GUI 验证 ---
try:
    conn = (p.request("conn.status", {}, timeout=6).get("connections") or {})
    print(f"{ts()} 最终状态: {conn}", flush=True)
    info = p.request("sys.info", {}, timeout=8)
    print(f"{ts()} heap={info.get('free_heap')}", flush=True)
except Exception as e:
    print(f"{ts()} 收尾查询: {e}", flush=True)
p.close()

print("\n===== 结果 =====", flush=True)
for label, ok in results:
    print(f"{'PASS' if ok else 'FAIL'}  {label}", flush=True)
