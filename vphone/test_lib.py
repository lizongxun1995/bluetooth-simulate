#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vphone_lib 无设备单测 —— 只验 PC 侧逻辑(事件解析/三层断言/kwargs 约定), 不碰手机不碰 adb。

运行: python test_lib.py   (零依赖, 全绿打印 OK)
"""
import inspect
import json
import time

import vphone_lib
from vphone_lib import VPhone, VEvent, VPhoneError, VPhoneTimeoutError

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ✓ {name}")


def fake_vp(*envelopes, repeat_last=True):
    """构造不连设备的 VPhone: http() 依次返回给定 /events 信封(耗尽后重复最后一个)。
    信封 = {'last': N, 'events': [{'id','ts','type','src','detail'}, ...]}"""
    vp = VPhone.__new__(VPhone)
    vp.wait_timeout = 15
    state = {"i": 0}

    def http(path, *, timeout=6, since=0, **params):
        i = min(state["i"], len(envelopes) - 1) if repeat_last else state["i"]
        state["i"] += 1
        if not repeat_last and i >= len(envelopes):
            return "{}"
        env = envelopes[i]
        # 仿真真机行为: 服务端按 since 过滤, 只回 id>since 的事件
        kept = [e for e in env["events"] if e["id"] > since]
        return json.dumps({"last": env["last"], "events": kept})

    vp.http = http
    return vp


def fake_staged(*envelopes):
    """多路场景回放专用: 只有 /events 拉取推进信封(指令路径直接回 ok 不消耗),
    信封内容 = 服务端累积历史(since 过滤后即"本步新事件"), 命中事件的 id 就是下一步水位。"""
    vp = VPhone.__new__(VPhone)
    vp.wait_timeout = 15
    state = {"i": 0}

    def http(path, *, timeout=6, since=0, **params):
        if path != "/events":
            return "ok(指令模拟)"
        i = min(state["i"], len(envelopes) - 1)
        state["i"] += 1
        env = envelopes[i]
        return json.dumps({"last": env["last"],
                           "events": [e for e in env["events"] if e["id"] > since]})

    vp.http = http
    return vp


def ev(eid, etype, src="car", detail="", ts=1700000000000):
    return {"id": eid, "ts": ts, "type": etype, "src": src, "detail": detail}


def test_vevent():
    e = VEvent._of(ev(7, "CAR_ANSWER", "car", "车机按了【接听】(onAnswer) 号码=138", 1788835224553))
    assert e.id == 7 and e.type == "CAR_ANSWER" and e.src == "car"
    assert "CAR_ANSWER" in str(e) and "车机按了【接听】" in str(e) and "#7" in str(e)
    assert len(e.time.split(":")) == 3          # HH:MM:SS
    assert VEvent._of({}) == VEvent(0, 0, "", "", "")   # 缺字段容错不抛
    ok("VEvent 解析/str/time/容错")


def test_events_snapshot():
    vp = fake_vp({"last": 3, "events": [ev(2, "CAR_PLAY"), ev(3, "CAR_NEXT", "cmd")]})
    es = vp.events(since=0)
    assert isinstance(es[0], VEvent) and len(es) == 2 and es[1].src == "cmd"
    assert vp.event_watermark() == 3
    ok("events() 快照返回 list[VEvent] + event_watermark")


def test_wait_event():
    # since 回放: 命中历史事件
    vp = fake_vp({"last": 5, "events": [ev(5, "CAR_ANSWER")]})
    hit = vp.wait_event(evt_type="CAR_ANSWER", since=0, timeout=1)
    assert hit and hit.id == 5
    # 未来水位: 首拉对齐 last=5 后无新事件 → 超时返回 None
    vp2 = fake_vp({"last": 5, "events": [ev(5, "CAR_ANSWER")]})
    t0 = time.time()
    assert vp2.wait_event(evt_type="CAR_ANSWER", timeout=0.2) is None
    assert time.time() - t0 < 2
    # tuple 任一命中 + src/detail 过滤
    vp3 = fake_vp({"last": 6, "events": [ev(6, "CMD_PLAY", "cmd", "播放")]})
    assert vp3.wait_event(evt_type=("CAR_PLAY", "CMD_PLAY"), src="cmd", detail="播放", since=0)
    ok("wait_event 回放命中/未来超时None/tuple+src+detail 过滤")


def test_expect_event():
    vp = fake_vp({"last": 1, "events": [ev(1, "CMD_PLAY", "app", "App 内部状态")]})
    hit = vp.expect_event(evt_type="CMD_PLAY", since=0)
    assert hit.type == "CMD_PLAY"
    # 超时: 异常消息要带过滤条件 + because + 窗口内实际事件(排查现场)
    vp2 = fake_vp({"last": 1, "events": [ev(1, "CMD_PAUSE", "app", "App 暂停")]})
    try:
        vp2.expect_event(evt_type="CAR_ANSWER", timeout=0.2, since=0, because="等车机接听")
        raise AssertionError("应当抛 VPhoneTimeoutError")
    except VPhoneTimeoutError as e:
        msg = str(e)
        assert "CAR_ANSWER" in msg and "等车机接听" in msg and "CMD_PAUSE" in msg
    assert isinstance(VPhoneTimeoutError("x"), VPhoneError)   # 既有兜底不受影响
    ok("expect_event 命中返回VEvent / 超时抛异常带完整上下文")


def test_expect_no_event():
    quiet = fake_vp({"last": 9, "events": []})
    assert quiet.expect_no_event(evt_type="CAR_PLAY", within=0.2) is None
    noisy = fake_vp({"last": 9, "events": [ev(9, "CAR_PLAY", "car", "车机按了播放")]})
    try:
        noisy.expect_no_event(evt_type="CAR_PLAY", within=1, since=0)
        raise AssertionError("应当抛 VPhoneTimeoutError")
    except VPhoneTimeoutError as e:
        assert "CAR_PLAY" in str(e)
    ok("expect_no_event 平静通过 / 意外命中抛异常")


def test_timeout0_peek():
    vp = fake_vp({"last": 2, "events": [ev(2, "CAR_NEXT")]})
    t0 = time.time()
    assert vp.wait_event(evt_type="CAR_NEXT", since=0, timeout=0) is not None
    assert vp.wait_event(evt_type="CAR_ANSWER", since=0, timeout=0) is None
    assert time.time() - t0 < 0.5          # 只查一次, 立即返回
    ok("timeout=0 = 非阻塞探一眼(单次快照)")


def test_multicall_commands():
    """多路指令面: lib→APK 的路径/参数精确匹配(hangup number 三态 / swap / hold on 映射)。"""
    sent = []

    def http(path, *, timeout=6, since=0, **params):
        sent.append((path, params))
        return json.dumps({"last": 0, "events": []})

    vp = VPhone.__new__(VPhone)
    vp.wait_timeout = 15
    vp.http = http
    vp.incoming(number="13900139000")
    vp.answer()
    vp.hold(on=True)
    vp.hold(on=False)
    vp.swap()
    vp.hangup(number="13900139000")
    vp.hangup()
    vp.hangup(number="all")
    assert sent == [
        ("/call/incoming", {"number": "13900139000"}),
        ("/call/answer", {}),
        ("/call/hold", {"on": "1"}),
        ("/call/hold", {"on": "0"}),
        ("/call/swap", {}),
        ("/call/hangup", {"number": "13900139000"}),
        ("/call/hangup", {}),                 # number=None 不带参数 → APK 挂前景
        ("/call/hangup", {"number": "all"}),
    ]
    ok("多路指令: incoming/answer/hold(1|0)/swap/hangup(None|号码|all) 路径参数精确")


def test_multicall_scenario():
    """多路时序回放(第十一轮语义): 等待→接听自动保持→切换→挂前景→held自动恢复→全挂。
    信封=累积历史(仿真服务端); 命中事件 id 作下一步 since —— 与真机脚本同构。
    事件链对齐真机 GSM: 来电即抢媒体焦点(MEDIA_CALL_PAUSE); 挂 active 后保持路
    自动取回(CALL_ACTIVE/app 自动恢复); 全挂归还焦点(MEDIA_CALL_RESUME)。"""
    A, B = "13800138000", "13900139000"
    hist = {}

    def h(*new):
        hist.update({e["id"]: e for e in new})
        return {"last": max(hist), "events": list(hist.values())}

    vp = fake_staged(
        {"last": 0, "events": []},
        h(ev(1, "RING_IN", "cmd", f"注入来电 {A} (车机应弹来电UI)"),
          ev(2, "MEDIA_CALL_PAUSE", "app", f"来电 {A} → 媒体暂停(音频焦点被通话抢占)")),
        h(ev(3, "CALL_ACTIVE", "cmd", f"指令接听 {A} → active")),
        h(ev(4, "RING_IN", "cmd", f"注入来电(等待路) {B}"),
          ev(5, "CALL_WAITING", "app", f"第二路来电等待中 {B}")),
        h(ev(6, "CALL_HELD", "app", f"接听 {B}, 原通话自动保持 ({A})"),
          ev(7, "CALL_ACTIVE", "cmd", f"指令接听 {B} → active")),
        h(ev(8, "CALL_HELD", "cmd", f"指令切换: 保持 ({B})"),
          ev(9, "CALL_ACTIVE", "cmd", f"指令切换 → {A} → active")),
        h(ev(10, "CALL_ENDED", "cmd", f"指令挂断 ({A})"),
          ev(11, "CALL_ACTIVE", "app", f"active挂断 → 保持路自动恢复(真机CHLD=1) ({B}) → active")),
        h(ev(12, "CALL_ENDED", "cmd", f"指令挂断 ({B}) [全挂]"),
          ev(13, "MEDIA_CALL_RESUME", "app", "通话结束, 音频焦点归还 → 媒体自动恢复播放")),
    )

    def step(label, fn, *expects):
        m = step.m
        fn()
        for evt, detail in expects:
            hit = vp.expect_event(evt_type=evt, detail=detail, since=m,
                                  timeout=2, because=label)
            assert hit.type == evt and detail in hit.detail
            m = hit.id
        step.m = m

    step.m = vp.event_watermark()                     # 对齐水位 0
    step("A 来电(媒体让焦点)", lambda: vp.incoming(number=A),
         ("RING_IN", A), ("MEDIA_CALL_PAUSE", "媒体暂停"))
    step("接听 A", vp.answer, ("CALL_ACTIVE", A))
    step("B 等待", lambda: vp.incoming(number=B), ("CALL_WAITING", B))
    step("接听 B(A 自动保持)", vp.answer, ("CALL_HELD", A), ("CALL_ACTIVE", B))
    step("切换", vp.swap, ("CALL_HELD", B), ("CALL_ACTIVE", A))
    step("挂前景 A(B 自动恢复)", vp.hangup,
         ("CALL_ENDED", A), ("CALL_ACTIVE", "自动恢复"))
    step("全挂 B(媒体恢复)", lambda: vp.hangup(number="all"),
         ("CALL_ENDED", B), ("MEDIA_CALL_RESUME", "媒体自动恢复"))
    ok("多路场景回放: 等待/自动保持/切换/挂前景自动恢复/全挂+媒体焦点 全链路事件断言")


def test_kwargs_only():
    vp = fake_vp({"last": 0, "events": []})
    for bad in (lambda: vp.incoming("123"),
                lambda: vp.hangup("138"),
                lambda: vp.swap("x"),
                lambda: vp.set_track("t", "a"),
                lambda: vp.upload_audio("x.mp3"),
                lambda: vp.bt_bond("CARKIT-1"),
                lambda: vp.contacts_load(100),
                lambda: vp.wait_event("CAR_PLAY")):
        try:
            bad()
            raise AssertionError("位置参数应当被拒绝")
        except TypeError:
            pass
    # 传输原语的主判别参数保留位置传法(http/cmd 的 path/cmd)
    assert vp.http("/events", since=0) == '{"last": 0, "events": []}'
    ok("全公开方法 keyword-only 强制生效; 传输原语主参数仍可位置传")


def test_signature_contract():
    """公开方法(除四个传输原语)的每个参数都必须 KEYWORD_ONLY —— 兼容性契约。"""
    prims = {"http", "http_post", "broadcast", "cmd", "_adb"}
    checked = 0
    for name, fn in inspect.getmembers(VPhone, inspect.isfunction):
        if name.startswith("_") or name in prims or name == "__init__":
            continue
        for p in inspect.signature(fn).parameters.values():
            if p.name == "self":
                continue
            assert p.kind == inspect.Parameter.KEYWORD_ONLY, \
                f"{name}.{p.name} 不是 keyword-only"
            checked += 1
    # __init__ 也要全 keyword-only
    for p in inspect.signature(VPhone.__init__).parameters.values():
        if p.name != "self":
            assert p.kind == inspect.Parameter.KEYWORD_ONLY
    ok(f"签名契约: {checked} 个参数全部 KEYWORD_ONLY")


if __name__ == "__main__":
    print("== vphone_lib 无设备单测 ==")
    for t in (test_vevent, test_events_snapshot, test_wait_event, test_expect_event,
              test_expect_no_event, test_timeout0_peek, test_multicall_commands,
              test_multicall_scenario, test_kwargs_only, test_signature_contract):
        t()
    print(f"== 全部通过 ({PASS} 组) ==")
