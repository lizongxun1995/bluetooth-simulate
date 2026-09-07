import time

import pytest

from btphone import BtPhone, CommandError, SimPhone, WaitTimeout
from btphone.media import make_tone_wav


def test_sys_info_and_echo(phone_sim):
    phone, _ = phone_sim
    info = phone.info()
    assert info["chip"].startswith("ESP32")
    assert phone._req("sys.echo", {"msg": "hello"})["msg"] == "hello"


def test_name_and_discoverable(phone_sim):
    phone, sim = phone_sim
    phone.set_name("VPHONE-88")
    phone.set_discoverable(True, timeout_s=30)
    status = phone.status()
    assert status["name"] == "VPHONE-88"
    assert status["discoverable"] is True


def test_pairing_auto_accept(phone_sim):
    phone, sim = phone_sim
    phone.pairing.set_mode("auto")
    sim.car_pair()
    result = phone.wait_event("pair.result", timeout=5)
    assert result["ok"] is True
    assert len(phone.pairing.list_bonded()) == 1


def test_pairing_failure_modes(phone_sim):
    phone, sim = phone_sim
    for mode, reason in (("reject", "rejected"), ("wrong_pin", "wrong_pin"), ("timeout", "timeout")):
        phone.pairing.set_mode(mode)
        sim.car_pair()
        result = phone.wait_event("pair.result", timeout=5)
        assert result["ok"] is False
        assert result["reason"] == reason


def test_pairing_manual_confirm_reject(phone_sim):
    phone, sim = phone_sim
    phone.pairing.set_mode("manual")
    sim.car_pair()
    request = phone.wait_event("pair.request", timeout=5)
    assert request["method"] == "ssp_confirm"
    phone.pairing.confirm(False)
    result = phone.wait_event("pair.result", timeout=5)
    assert result["ok"] is False
    assert phone.pairing.list_bonded() == []


def test_profiles_down_commands_fail_fast(phone_sim):
    """车机未连 profile 时 music/hfp 命令必须报错,不能静默装成功。

    回归保护:2026-09-04 车机配对后不拉 profile,hfp.incoming /
    music.play 在固件里被静默丢弃,PC 侧显示成功但车机毫无反应。"""
    phone, sim = phone_sim
    phone.conn.disconnect("all")
    time.sleep(0.2)
    with pytest.raises(CommandError, match="hfp not connected"):
        phone.calls.incoming("13800138000")
    with pytest.raises(CommandError, match="a2dp not connected"):
        phone._req("music.play")
    # conn.status 上报真实 profile 状态
    st = phone.conn.status()
    conns = st.get("connections") or {}
    assert conns.get("a2dp") in (None, "disconnected", False)
    # 重连后命令恢复可用
    sim.car_connect()
    time.sleep(0.2)
    phone.calls.incoming("13800138000")
    state = phone.wait_event("bt.hfp.state",
                             predicate=lambda d: d.get("state") == "incoming", timeout=5)
    assert state["number"] == "13800138000"
    phone.calls.hangup()


def test_pair_success_auto_connects_profiles(phone_sim):
    """v0.3.0:配对成功后像真手机一样自动拉起 A2DP+HFP。"""
    phone, sim = phone_sim
    phone.conn.disconnect("all")
    time.sleep(0.2)
    phone.pairing.set_mode("auto")
    sim.car_pair()
    phone.wait_event("pair.result", timeout=5)
    phone.wait_event("bt.conn", predicate=lambda d: d.get("profile") == "hfp", timeout=5)
    conns = phone.conn.status().get("connections") or {}
    assert conns.get("a2dp") == "connected"
    assert conns.get("hfp") == "connected"


def test_connect_disconnect(phone_sim):
    phone, sim = phone_sim
    phone.conn.connect("AA:BB:CC:DD:EE:01")
    event = phone.wait_event("bt.conn", predicate=lambda d: d.get("profile") == "a2dp", timeout=5)
    assert event["state"] == "connected"
    phone.conn.disconnect("all")
    event = phone.wait_event("bt.a2dp.state", timeout=5)
    assert event["state"] == "disconnected"


def test_bt_rssi(phone_sim):
    phone, sim = phone_sim
    # fixture 默认已连车机;先断开,验证"无对端"时的报错
    phone.conn.disconnect("all")
    time.sleep(0.2)
    with pytest.raises(CommandError, match="no acl peer"):
        phone.request("bt.rssi")
    sim.car_pair()
    phone.wait_event("pair.result", timeout=5)
    result = phone.request("bt.rssi")
    assert result["addr"] == "AA:BB:CC:DD:EE:01"
    assert -128 <= result["rssi_delta"] <= 127
    # 断开后对端清空,再查报错
    phone.conn.disconnect("all")
    phone.wait_event("bt.a2dp.state", predicate=lambda d: d.get("state") == "disconnected", timeout=5)
    with pytest.raises(CommandError, match="no acl peer"):
        phone.request("bt.rssi")


def test_music_play_and_track_metadata(phone_sim, wav_file):
    phone, sim = phone_sim
    phone.music.play(file=wav_file, metadata={"title": "测试曲目", "artist": "测试歌手"})
    started = phone.wait_event("music.started", timeout=15)
    assert started["title"] == "测试曲目"
    # 车机侧可见元数据
    deadline = time.time() + 5
    while time.time() < deadline and not sim.track_info():
        time.sleep(0.05)
    assert sim.track_info()["title"] == "测试曲目"
    assert sim.track_info()["artist"] == "测试歌手"
    phone.wait_event("music.ended", timeout=30)


def test_music_playlist_car_next(phone_sim, tmp_path):
    phone, sim = phone_sim
    files = []
    for i in range(3):
        p = str(tmp_path / f"t{i}.wav")
        make_tone_wav(p, duration_s=0.5, freq=300 + i * 100)
        files.append(p)
    phone.music.play(files=files, loop=True)
    started = phone.wait_event("music.started", timeout=15)
    assert started["index"] == 0
    sim.car_press("next")  # 车机按下一首
    started2 = phone.wait_event("music.started", timeout=30)
    assert started2["index"] == 1
    phone.music.stop()


def test_calls_incoming_answer_hangup(phone_sim):
    phone, sim = phone_sim
    phone.calls.incoming("13800138000")
    state = phone.wait_event("bt.hfp.state", predicate=lambda d: d.get("state") == "incoming", timeout=5)
    assert state["number"] == "13800138000"
    sim.car_answer()
    phone.wait_event("hfp.at", predicate=lambda d: d.get("at") == "ATA", timeout=5)
    phone.wait_event("bt.hfp.state", predicate=lambda d: d.get("state") == "active", timeout=5)
    phone.calls.hangup()
    phone.wait_event("bt.hfp.state", predicate=lambda d: d.get("state") == "idle", timeout=5)


def test_calls_car_dial_and_hangup(phone_sim):
    phone, sim = phone_sim
    sim.car_dial("10086")
    at = phone.wait_event("hfp.at", timeout=5)
    assert at["at"] == "ATD"
    assert at["arg"] == "10086"
    sim.car_hangup()
    phone.wait_event("bt.hfp.state", predicate=lambda d: d.get("state") == "idle", timeout=5)


def test_calls_with_voice_stream(phone_sim, tmp_path):
    phone, sim = phone_sim
    voice = str(tmp_path / "voice.wav")
    from btphone.media import make_tone_wav

    make_tone_wav(voice, duration_s=0.5, freq=800, rate=16000, channels=1)
    phone.calls.set_voice(voice)
    phone.calls.incoming("110")
    sim.car_answer()
    # 通话激活后语音应循环推流
    phone.wait_event("voice.looped", timeout=20)
    phone.calls.hangup()
    phone.wait_event("bt.hfp.state", predicate=lambda d: d.get("state") == "idle", timeout=5)


def test_wifi_scan_and_ap_on_sim(phone_sim):
    phone, sim = phone_sim
    aps = phone.net.wifi_scan()
    assert len(aps) >= 1 and "ssid" in aps[0]
    phone.net.ap_start("VPHONE-AP", "12345678")
    event = phone.wait_event("net.ap", predicate=lambda d: d.get("state") == "started", timeout=5)
    assert event["state"] == "started"
    phone.net.wifi_sta("Lab-Router", "secret")
    phone.wait_event("net.sta", predicate=lambda d: d.get("connected") is True, timeout=5)
    status = phone.net.status()
    assert status["sta"]["connected"] is True
    assert status["ap"]["ssid"] == "VPHONE-AP"
    phone.net.ap_stop()
    phone.wait_event("net.ap", predicate=lambda d: d.get("state") == "stopped", timeout=5)


def test_tether_pan_not_implemented_in_v1(phone_sim):
    phone, _ = phone_sim
    with pytest.raises(CommandError) as exc_info:
        phone.net.tether_pan(True)
    assert "not_implemented" in str(exc_info.value)


def test_lyrics_not_implemented_in_v1(phone_sim):
    phone, _ = phone_sim
    with pytest.raises(CommandError):
        phone.lyrics.push([{"t": 0, "text": "第一句"}], title="歌")


def test_wait_event_timeout(phone_sim):
    phone, _ = phone_sim
    with pytest.raises(WaitTimeout):
        phone.wait_event("never.happens", timeout=0.3)


def test_event_subscription(phone_sim):
    phone, sim = phone_sim
    received = []
    cancel = phone.on_event("avrcp.cmd", lambda n, d: received.append(d))
    sim.car_press("play")
    deadline = time.time() + 5
    while time.time() < deadline and not received:
        time.sleep(0.05)
    cancel()
    assert received and received[0]["cmd"] == "play"


def test_sim_direct_instance(phone_sim):
    """两台模拟器互不干扰(每次 fixture 独立)。"""
    sim2 = SimPhone(name="OTHER", speed=10)
    phone2 = BtPhone("SIM", io=sim2.device_io)
    try:
        assert phone2.info()["name"] == "OTHER"
    finally:
        phone2.close()
        sim2.close()


# ---- wait_reconnect(net 启动引发的 USB 掉线恢复) -----------------------

class _FakePortEntry:
    def __init__(self, device):
        self.device = device


def test_wait_reconnect_after_blip(phone_sim, monkeypatch):
    """端口消失→重新出现→无复位重连(reset=False)。"""
    phone, _ = phone_sim
    calls = []

    def fake_comports():
        # 第一次查询端口不在,之后视为重新枚举完成
        fake_comports.gone = getattr(fake_comports, "gone", True)
        if fake_comports.gone:
            fake_comports.gone = False
            return []
        return [_FakePortEntry("SIM")]

    def fake_open(reset=True):
        # 桩掉真串口重建,只验证编排:端口轮询 + reset=False 透传
        calls.append(reset)
        phone._transport._closed.clear()
        return None

    monkeypatch.setattr("serial.tools.list_ports.comports", fake_comports)
    monkeypatch.setattr(phone._transport, "open", fake_open)
    phone.wait_reconnect(timeout=10, poll=0.05)
    assert calls == [False]  # 必须无复位重连


def test_wait_reconnect_timeout(phone_sim, monkeypatch):
    """端口一直不回来 → TransportError 超时。"""
    from btphone import TransportError

    phone, _ = phone_sim
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [])
    with pytest.raises(TransportError):
        phone.wait_reconnect(timeout=0.5, poll=0.1)


def test_request_auto_recovers_after_broken_link(phone_sim):
    """推流/射频瞬态打断链路(_handle_broken 收口)后,下一条命令自动重连成功,
    不该永久报"串口未打开"。"""
    phone, _ = phone_sim
    tr = phone._transport
    tr._handle_broken("串口读取失败: simulated")
    assert not tr.is_open
    assert phone.request("sys.echo", {"msg": "back"})["msg"] == "back"
    assert tr.is_open


def test_request_recovery_throttled_on_failure(phone_sim, monkeypatch):
    """自救失败(重连打不开)后 5s 内的后续命令快速报错,不反复叠长阻塞。"""
    from btphone import TransportError

    phone, _ = phone_sim
    tr = phone._transport
    tr._handle_broken("串口读取失败: simulated")

    def fail_open(reset=True):
        raise TransportError("simulated: port gone")

    monkeypatch.setattr(tr, "open", fail_open)
    t0 = time.monotonic()
    with pytest.raises(TransportError, match="自动重连未成功"):
        phone.request("sys.echo", {})
    assert time.monotonic() - t0 < 2.0  # 首次尝试失败后快速返回
    with pytest.raises(TransportError, match="自动重连未成功"):
        phone.request("sys.echo", {})   # 静默窗内:直接快速报错,不再撞
    assert time.monotonic() - t0 < 2.0


# ---- 冷启动握手容忍复位瞬间 USB 闪断 -------------------------------------

def test_open_recovers_from_reset_blip(phone_sim, monkeypatch):
    """复位脉冲后 CH340 从总线消失(固件其实正常启动):收口→等重枚举→
    无复位接管→握手完成。端口一直在却无事件才报"设备未就绪"。"""
    from btphone.exceptions import WaitTimeout

    phone, _ = phone_sim
    tr = phone._transport
    tr.close()  # 从冷启动 open 开始

    calls = {"close": 0, "start": []}
    real_close = tr.close

    def fake_close():
        calls["close"] += 1
        real_close()

    def fake_start():
        calls["start"].append(False)
        tr._closed.clear()

    wait_calls = {"n": 0}

    def fake_wait(pattern, timeout=None, **kw):
        wait_calls["n"] += 1
        if wait_calls["n"] <= 2:
            raise WaitTimeout(f"等待 {pattern} 超时({timeout}s)")
        return ("audio.buffer", {})

    polls = {"n": 0}

    def fake_comports():
        # 第一次轮询时端口尚未回来,之后视为重新枚举完成
        polls["n"] += 1
        if polls["n"] == 1:
            return []
        return [_FakePortEntry(tr._port_name)]

    monkeypatch.setattr(tr, "close", fake_close)
    monkeypatch.setattr(tr, "_pulse_reset", lambda: None)
    monkeypatch.setattr(tr, "_start_session", fake_start)
    monkeypatch.setattr(tr.events, "wait_event", fake_wait)
    monkeypatch.setattr(tr, "request", lambda cmd, args=None, timeout=10.0: {})
    monkeypatch.setattr("serial.tools.list_ports.comports", fake_comports)

    tr.open(reset=True)

    assert calls["start"] == [False, False]  # 静默后接管 + 端口回来后无复位接管
    assert calls["close"] == 2
    assert wait_calls["n"] == 3  # 端口回来后才等到事件


def test_open_recovers_when_blip_keeps_port_name(phone_sim, monkeypatch):
    """闪断的另一种形态:句柄已被弄死,但端口以同名快速重新枚举,
    检查点时枚举里"看得到"端口——也必须重启会话,而不是傻等事件。"""
    from btphone.exceptions import WaitTimeout

    phone, _ = phone_sim
    tr = phone._transport
    tr.close()

    calls = {"close": 0, "start": []}
    real_close = tr.close

    def fake_close():
        calls["close"] += 1
        real_close()

    def fake_start():
        calls["start"].append(False)
        tr._closed.clear()

    wait_calls = {"n": 0}

    def fake_wait(pattern, timeout=None, **kw):
        wait_calls["n"] += 1
        if wait_calls["n"] == 1:
            tr._closed.set()  # 模拟闪断把 RX 句柄弄死
            raise WaitTimeout(f"等待 {pattern} 超时({timeout}s)")
        return ("audio.buffer", {})

    monkeypatch.setattr(tr, "close", fake_close)
    monkeypatch.setattr(tr, "_pulse_reset", lambda: None)
    monkeypatch.setattr(tr, "_start_session", fake_start)
    monkeypatch.setattr(tr.events, "wait_event", fake_wait)
    monkeypatch.setattr(tr, "request", lambda cmd, args=None, timeout=10.0: {})
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [_FakePortEntry(tr._port_name)],  # 端口全程可见(同名秒回)
    )

    tr.open(reset=True)

    assert calls["start"] == [False, False]
    assert calls["close"] == 1
    assert wait_calls["n"] == 2


def test_open_recovers_from_silent_dead_handle(phone_sim, monkeypatch):
    """最隐蔽的闪断形态:句柄不抛异常、端口同名在枚举里,但永远读不到
    字节(Windows 重枚举后旧句柄僵死)。零字节流入即判定僵死,重建会话。"""
    from btphone.exceptions import WaitTimeout

    phone, _ = phone_sim
    tr = phone._transport
    tr.close()

    calls = {"close": 0, "start": []}
    real_close = tr.close

    def fake_close():
        calls["close"] += 1
        real_close()

    def fake_start():
        calls["start"].append(False)
        tr._closed.clear()

    wait_calls = {"n": 0}

    def fake_wait(pattern, timeout=None, **kw):
        wait_calls["n"] += 1
        if wait_calls["n"] == 1:
            raise WaitTimeout(f"等待 {pattern} 超时({timeout}s)")
        return ("audio.buffer", {})

    monkeypatch.setattr(tr, "close", fake_close)
    monkeypatch.setattr(tr, "_pulse_reset", lambda: None)
    monkeypatch.setattr(tr, "_start_session", fake_start)
    monkeypatch.setattr(tr.events, "wait_event", fake_wait)
    monkeypatch.setattr(tr, "request", lambda cmd, args=None, timeout=10.0: {})
    # rx_bytes 在本测试中永不增长(模拟读不到数据),端口全程可见
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [_FakePortEntry(tr._port_name)],
    )

    tr.open(reset=True)

    assert calls["start"] == [False, False]
    assert calls["close"] == 1
    assert wait_calls["n"] == 2


def test_open_escalates_to_pulse_after_total_silence(phone_sim, monkeypatch):
    """链路健康但板子全程零字节(卡死/被闷在下载模式):先温和握手,
    持续静默才升级为脉冲复位,复位后握手成功。"""
    from btphone.exceptions import WaitTimeout

    phone, _ = phone_sim
    tr = phone._transport
    tr.close()
    tr.reset_quiet_s = 0.01  # 测试不等真实静默期
    tr.silent_escalate_s = 0.5  # 阈值调小:fake 的 wait 不耗真实时间,只有 sleep 计时
    tr.first_byte_grace_s = 0.0  # 本测试模拟"链路健康但板子零字节",须关闭宽限才会升级

    calls = {"close": 0, "start": 0, "pulse": 0}
    real_close = tr.close

    def fake_close():
        calls["close"] += 1
        real_close()

    def fake_start():
        calls["start"] += 1
        tr._closed.clear()
        tr._io = object()  # 让 is_open 为真:链路健康、纯粹没数据

    def fake_pulse():
        calls["pulse"] += 1

    wait_calls = {"n": 0}

    def fake_wait(pattern, timeout=None, **kw):
        wait_calls["n"] += 1
        # 升级前:窗口1(elapsed≈0<0.5,不升级)→重试→窗口2(elapsed≈2s≥0.5,
        # 零字节且从未断链)→判 silent 升级脉冲;复位后窗口3直接来事件
        if wait_calls["n"] <= 2:
            raise WaitTimeout(f"等待 {pattern} 超时({timeout}s)")
        return ("audio.buffer", {})

    monkeypatch.setattr(tr, "close", fake_close)
    monkeypatch.setattr(tr, "_pulse_reset", fake_pulse)
    monkeypatch.setattr(tr, "_start_session", fake_start)
    monkeypatch.setattr(tr.events, "wait_event", fake_wait)
    monkeypatch.setattr(tr, "request", lambda cmd, args=None, timeout=10.0: {})
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [_FakePortEntry(tr._port_name)],
    )

    tr.open(reset=True)

    assert calls["pulse"] == 1  # 温和握手失败后,恰好升级一次脉冲复位
    assert wait_calls["n"] == 3
    assert calls["start"] == 3  # 初始1 + 静默重试1 + 升级后1
    assert calls["close"] == 2
