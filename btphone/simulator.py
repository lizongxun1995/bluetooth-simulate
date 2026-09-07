"""ESP32 固件协议模拟器:没有硬件也能开发/测试 btphone 库与测试脚本。

用法::

    from btphone import BtPhone, SimPhone

    sim = SimPhone(speed=100)      # speed: 缓冲消耗倍速(测试时调快)
    phone = BtPhone("SIM", io=sim.device_io)
    sim.car_press("next")          # 模拟车机上按下一首
    sim.car_answer()               # 模拟车机接听来电

模拟范围:配对(含失败)、连接/断开、A2DP+AVRCP、HFP 通话、媒体上传、
audio.buffer 流控。WiFi/NAT/歌词 返回 not_implemented(对应 Phase 2/3)。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Dict, Optional

from . import protocol
from .protocol import FrameParser

SESSION_BUFFER = 65536


class SimSerial:
    """模拟串口对象(给 SerialTransport 用的 read/write/close 接口)。"""

    def __init__(self, sim: "SimPhone") -> None:
        self._sim = sim
        self._to_device = bytearray()      # PC→设备
        self._to_device_ev = threading.Event()
        self._to_pc = bytearray()          # 设备→PC
        self._to_pc_cond = threading.Condition()
        self._closed = False
        self._lock = threading.Lock()

    # ---- transport 调用 -------------------------------------------

    def write(self, data: bytes) -> int:
        with self._lock:
            self._to_device += data
        self._to_device_ev.set()
        return len(data)

    def read(self, n: int = 4096) -> bytes:
        with self._to_pc_cond:
            while not self._to_pc and not self._closed:
                self._to_pc_cond.wait(0.05)
            out = bytes(self._to_pc[:n])
            del self._to_pc[:n]
        return out

    @property
    def in_waiting(self) -> int:
        """接收缓冲中已到达的字节数(与 pyserial 的 in_waiting 属性语义一致)。"""
        with self._to_pc_cond:
            return len(self._to_pc)

    def close(self) -> None:
        self._closed = True
        with self._to_pc_cond:
            self._to_pc_cond.notify_all()

    def flush(self) -> None:
        pass

    # ---- simulator 调用 -------------------------------------------

    def device_read(self, n: int = 4096) -> bytes:
        self._to_device_ev.wait(0.05)
        with self._lock:
            self._to_device_ev.clear()
            out = bytes(self._to_device[:n])
            del self._to_device[:n]
        return out

    def device_write(self, data: bytes) -> None:
        with self._to_pc_cond:
            self._to_pc += data
            self._to_pc_cond.notify_all()


class _Session:
    """一条音频会话(向车机放歌或通话语音)。"""

    def __init__(self, sink: str, rate: int, channels: int, codec: str) -> None:
        self.sink = sink
        self.rate = rate
        self.channels = channels
        self.codec = codec
        self.buffered = bytearray()
        self.playing = False
        self.eos = False

    @property
    def bytes_per_second(self) -> float:
        if self.codec == "adpcm":
            return self.rate * self.channels  # 4bit → 半字节每样本
        return self.rate * self.channels * 2


class _NotImplemented(Exception):
    """命令在当前版本固件未实现。"""


class _CmdFail(Exception):
    """命令级业务错误:错误串直透给 PC 端(不加 internal: 前缀)。"""


class SimPhone:
    """固件模拟器。device_io 属性交给 BtPhone(io=...)。"""

    def __init__(self, name: str = "VPHONE-01", speed: float = 1.0) -> None:
        self.name = name
        self.speed = max(1e-6, float(speed))
        self.device_io = SimSerial(self)
        self.events_log: list[tuple[str, dict]] = []

        self._parser = FrameParser()
        self._parser_thread: Optional[threading.Thread] = None
        self._consume_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._req_id = 0

        # 模拟状态
        self.discoverable = False
        self.pair_mode = "auto"
        self.auto_accept = True
        self.auto_reconnect = True
        self.bonded: Dict[str, str] = {}
        self.connections: Dict[str, str] = {}     # profile -> "connected"/"disconnected"
        self.peer_mac: Optional[str] = None       # 最近 ACL 对端(bt.rssi 用)
        self.track: Dict[str, object] = {}
        self.play_state = "stopped"               # stopped/playing/paused
        self.call = {"state": "idle", "number": "", "call_setup": 0, "call_active": 0}
        self.voice_on = False
        self.net_state = {"sta_ssid": "", "sta_connected": False, "sta_ip": "",
                          "ap_ssid": "", "started": False}
        self._session: Optional[_Session] = None

        self._start_threads()

    # ---- 测试辅助 API(模拟车机行为) ------------------------------

    def car_press(self, cmd: str) -> None:
        """模拟车机媒体按键: play/pause/next/prev/ff/rew/vol_up/vol_down。"""
        self._emit("avrcp.cmd", {"cmd": cmd})

    def car_pair(self, mac: str = "AA:BB:CC:DD:EE:01", name: str = "CAR-HU") -> None:
        """模拟车机发起配对。"""
        self.peer_mac = mac
        self._emit("pair.request", {"mac": mac, "name": name, "method": "ssp_confirm"})
        if self.pair_mode == "auto" and self.auto_accept:
            self.bonded[mac] = name
            self._emit("pair.result", {"mac": mac, "ok": True, "reason": ""})
            # 固件 v0.3.0:配对成功后自动拉起 profiles(像真手机)
            if self.auto_reconnect:
                self.car_connect(mac)
        elif self.pair_mode == "reject":
            self._emit("pair.result", {"mac": mac, "ok": False, "reason": "rejected"})
        elif self.pair_mode == "wrong_pin":
            self._emit("pair.result", {"mac": mac, "ok": False, "reason": "wrong_pin"})
        elif self.pair_mode == "timeout":
            self._emit("pair.result", {"mac": mac, "ok": False, "reason": "timeout"})
        # manual: 等 pair.confirm

    def car_answer(self) -> None:
        """模拟车机接听来电。"""
        if self.call["state"] != "incoming":
            return
        self._emit("hfp.at", {"at": "ATA"})
        self._set_call("active")

    def car_hangup(self) -> None:
        """模拟车机挂断。"""
        self._emit("hfp.at", {"at": "AT+CHUP"})
        self._set_call("idle")

    def car_dial(self, number: str) -> None:
        """模拟车机拨号(HF→AG ATD)。"""
        self._emit("hfp.at", {"at": "ATD", "arg": number})
        self._set_call("outgoing", number=number)

    def car_connect(self, mac: str = "AA:BB:CC:DD:EE:01") -> None:
        """模拟车机主动连入(配对完成后拉起 profiles)。"""
        self.peer_mac = mac
        for profile in ("a2dp", "avrcp", "hfp"):
            self.connections[profile] = "connected"
            self._emit("bt.conn", {"profile": profile, "mac": mac, "state": "connected"})
        self._emit("bt.a2dp.state", {"state": "connected"})
        self._emit("bt.avrcp.state", {"state": "connected"})
        self._emit("bt.hfp.state", {**self.call, "state": "connected"})

    def track_info(self) -> dict:
        return dict(self.track)

    # ---- 事件与命令处理 -------------------------------------------

    def _emit(self, evt: str, data: dict) -> None:
        self.events_log.append((evt, data))
        self._send_ctrl({"evt": evt, "data": data})

    def _send_ctrl(self, obj: dict) -> None:
        self._req_id += 1
        self.device_io.device_write(protocol.pack_ctrl(obj, self._req_id))

    def _respond(self, req: dict, ok: bool, result=None, error: str = "") -> None:
        payload: dict = {"id": req["id"], "ok": ok}
        if ok:
            payload["result"] = result or {}
        else:
            payload["error"] = error
        self.device_io.device_write(
            protocol.pack_frame(protocol.TYPE_CTRL,
                                json.dumps(payload, ensure_ascii=False).encode("utf-8"), req["id"])
        )

    def _start_threads(self) -> None:
        self._parser_thread = threading.Thread(target=self._rx_loop, name="sim-rx", daemon=True)
        self._parser_thread.start()
        self._consume_thread = threading.Thread(target=self._consume_loop, name="sim-consume", daemon=True)
        self._consume_thread.start()

    def close(self) -> None:
        self._stop.set()
        self.device_io.close()

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            chunk = self.device_io.device_read(4096)
            if not chunk:
                continue
            for frame in self._parser.feed(chunk):
                self._dispatch(frame)

    def _dispatch(self, frame: protocol.Frame) -> None:
        try:
            if not frame.is_ctrl:
                self._on_audio(frame)
                return
            obj = json.loads(frame.payload.decode("utf-8"))
        except Exception:  # 坏帧/半帧:丢弃,不让接收线程死亡
            return
        if "cmd" not in obj:
            return
        handler = getattr(self, "_cmd_" + obj["cmd"].replace(".", "_"), None)
        if handler is None:
            self._respond(obj, False, error=f"unknown_cmd:{obj['cmd']}")
            return
        try:
            result = handler(obj.get("args", {}) or {})
            self._respond(obj, True, result)
        except _NotImplemented as exc:
            self._respond(obj, False, error=str(exc))
        except _CmdFail as exc:
            self._respond(obj, False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            self._respond(obj, False, error=f"internal:{exc}")

    # ---- 命令实现 -------------------------------------------------

    def _cmd_sys_info(self, args: dict) -> dict:
        return {"name": self.name, "fw": "sim-0.1.0", "chip": "ESP32-D0WDQ6(sim)",
                "profiles": ["a2dp", "avrcp", "hfp_ag"], "buffer_size": SESSION_BUFFER}

    def _cmd_sys_echo(self, args: dict) -> dict:
        return {"msg": args.get("msg", "")}

    def _cmd_sys_reset(self, args: dict) -> dict:
        # v0.3.0 起 sys.reset 是纯重启:保留名字/绑定/回连对象,只清通话状态
        self._set_call("idle")
        return {}

    def _cmd_sys_factory(self, args: dict) -> dict:
        self.bonded.clear()
        self.connections = {p: "disconnected" for p in self.connections}
        self.name = "VPHONE-01"
        self.peer_mac = None
        self._set_call("idle")
        return {}

    def _cmd_bt_set_name(self, args: dict) -> dict:
        self.name = args["name"]
        return {"name": self.name}

    def _cmd_bt_set_discoverable(self, args: dict) -> dict:
        self.discoverable = args.get("mode") in ("disc", "disc_conn")
        return {"discoverable": self.discoverable}

    def _cmd_pair_mode(self, args: dict) -> dict:
        mode = args["mode"]
        if mode not in ("auto", "manual", "reject", "wrong_pin", "timeout"):
            raise ValueError(f"bad mode: {mode}")
        self.pair_mode = mode
        return {"mode": mode}

    def _cmd_pair_confirm(self, args: dict) -> dict:
        accept = bool(args.get("accept"))
        mac = "AA:BB:CC:DD:EE:01"
        if accept:
            self.bonded[mac] = "CAR-HU"
            self._emit("pair.result", {"mac": mac, "ok": True, "reason": ""})
        else:
            self._emit("pair.result", {"mac": mac, "ok": False, "reason": "rejected"})
        return {"accept": accept}

    def _cmd_pair_policy(self, args: dict) -> dict:
        if "auto_accept" in args:
            self.auto_accept = bool(args["auto_accept"])
        if "auto_reconnect" in args:
            self.auto_reconnect = bool(args["auto_reconnect"])
        return {"auto_accept": self.auto_accept, "auto_reconnect": self.auto_reconnect}

    def _cmd_pair_list(self, args: dict) -> dict:
        return {"devices": [{"mac": mac, "name": name} for mac, name in self.bonded.items()]}

    def _cmd_pair_remove(self, args: dict) -> dict:
        self.bonded.pop(args["mac"], None)
        return {}

    def _cmd_conn_connect(self, args: dict) -> dict:
        mac = args["mac"]
        self.peer_mac = mac
        profiles = args.get("profiles", ["a2dp", "hfp"])
        for profile in profiles:
            self.connections[profile] = "connected"
            self._emit("bt.conn", {"profile": profile, "mac": mac, "state": "connected"})
        # 车机(AVRCP CT)随 A2DP 建立 AVRCP,固件侧由对端拉起
        if "a2dp" in profiles:
            self.connections["avrcp"] = "connected"
            self._emit("bt.conn", {"profile": "avrcp", "mac": mac, "state": "connected"})
            self._emit("bt.a2dp.state", {"state": "connected"})
            self._emit("bt.avrcp.state", {"state": "connected"})
        if "hfp" in profiles:
            self._emit("bt.hfp.state", {**self.call, "state": "connected"})
        return {"mac": mac, "connected": profiles}

    def _cmd_conn_disconnect(self, args: dict) -> dict:
        profile = args.get("profile", "all")
        targets = ("a2dp", "avrcp", "hfp") if profile == "all" else (profile,)
        for p in targets:
            self.connections[p] = "disconnected"
            self._emit("bt.conn", {"profile": p, "state": "disconnected"})
        if "a2dp" in targets:
            self._emit("bt.a2dp.state", {"state": "disconnected"})
            self.play_state = "stopped"
        if "avrcp" in targets:
            self._emit("bt.avrcp.state", {"state": "disconnected"})
        if "hfp" in targets:
            self._set_call("idle")
            self._emit("bt.hfp.state", {**self.call, "state": "disconnected"})
        if profile == "all":
            self.peer_mac = None
        return {}

    def _cmd_bt_rssi(self, args: dict) -> dict:
        if not self.peer_mac:
            raise _CmdFail("no acl peer yet(pair/connect first)")
        return {"addr": self.peer_mac, "rssi_delta": -3}

    def _cmd_conn_status(self, args: dict) -> dict:
        return {"name": self.name, "discoverable": self.discoverable,
                "connections": dict(self.connections), "play_state": self.play_state,
                "call": dict(self.call), "bonded": len(self.bonded)}

    def _cmd_audio_open(self, args: dict) -> dict:
        self._session = _Session(args.get("sink", "a2dp"), int(args["rate"]),
                                 int(args["channels"]), args["codec"])
        return {"buffer_size": SESSION_BUFFER, "free": SESSION_BUFFER}

    def _cmd_audio_stop(self, args: dict) -> dict:
        self._session = None
        return {}

    def _cmd_music_play(self, args: dict) -> dict:
        if self.connections.get("a2dp") != "connected":
            raise _CmdFail("a2dp not connected (pair/connect car first)")
        if self._session is None:
            raise _CmdFail("audio not opened (audio.open first)")
        self._session.playing = True
        self.play_state = "playing"
        return {}

    def _cmd_music_pause(self, args: dict) -> dict:
        self.play_state = "paused"
        if self._session:
            self._session.playing = False
        return {}

    def _cmd_music_resume(self, args: dict) -> dict:
        if self.connections.get("a2dp") != "connected":
            raise _CmdFail("a2dp not connected (pair/connect car first)")
        if self._session is None:
            raise _CmdFail("audio not opened (audio.open first)")
        self.play_state = "playing"
        self._session.playing = True
        return {}

    def _cmd_music_stop(self, args: dict) -> dict:
        self.play_state = "stopped"
        if self._session:
            self._session.playing = False
        return {}

    def _cmd_avrcp_metadata(self, args: dict) -> dict:
        self.track = dict(args)
        result = {"applied": True}
        if self.connections.get("avrcp") != "connected":
            result["warn"] = "avrcp not connected; metadata cached only"
        return result

    def _cmd_hfp_incoming(self, args: dict) -> dict:
        if self.connections.get("hfp") != "connected":
            raise _CmdFail("hfp not connected (pair/connect car first)")
        self._set_call("incoming", number=args["number"], call_setup=1)
        return {}

    def _cmd_hfp_ring(self, args: dict) -> dict:
        return {"ring": bool(args.get("on", True))}

    def _cmd_hfp_answer(self, args: dict) -> dict:
        self._set_call("active")
        return {}

    def _cmd_hfp_hangup(self, args: dict) -> dict:
        self._set_call("idle")
        return {}

    def _cmd_hfp_dial(self, args: dict) -> dict:
        if self.connections.get("hfp") != "connected":
            raise _CmdFail("hfp not connected (pair/connect car first)")
        self._set_call("outgoing", number=args["number"])
        return {}

    def _cmd_hfp_voice(self, args: dict) -> dict:
        self.voice_on = bool(args.get("on", True))
        return {"voice": self.voice_on}

    # ---- WiFi(模拟:状态机简化,立即成功) -------------------------

    def _cmd_net_wifi_sta(self, args: dict) -> dict:
        self.net_state["sta_ssid"] = args["ssid"]
        self.net_state["sta_connected"] = True
        self.net_state["sta_ip"] = "192.168.1.233"
        self._emit("net.sta", {"connected": True, "ip": self.net_state["sta_ip"]})
        return {"connecting": True}

    def _cmd_net_wifi_scan(self, args: dict) -> dict:
        return {"aps": [
            {"ssid": "Lab-Router", "rssi": -41, "auth": "key"},
            {"ssid": "Phone-Hotspot", "rssi": -58, "auth": "key"},
        ]}

    def _cmd_net_ap_start(self, args: dict) -> dict:
        self.net_state["ap_ssid"] = args["ssid"]
        self.net_state["started"] = True
        self._emit("net.ap", {"state": "started"})
        return {}

    def _cmd_net_ap_stop(self, args: dict) -> dict:
        self.net_state = {"sta_ssid": "", "sta_connected": False, "sta_ip": "",
                          "ap_ssid": "", "started": False}
        self._emit("net.ap", {"state": "stopped"})
        return {}

    def _cmd_net_status(self, args: dict) -> dict:
        return {"sta": {"ssid": self.net_state["sta_ssid"],
                        "connected": self.net_state["sta_connected"],
                        "ip": self.net_state["sta_ip"]},
                "ap": {"ssid": self.net_state["ap_ssid"]},
                "started": self.net_state["started"]}

    def _cmd_net_tether_pan(self, args: dict) -> dict:
        raise _NotImplemented("not_implemented:tether_phase2")

    def _cmd_lyrics_push(self, args: dict) -> dict:
        raise _NotImplemented("not_implemented:lyrics_phase3")

    # ---- 音频帧与消耗模拟 -----------------------------------------

    def _on_audio(self, frame: protocol.Frame) -> None:
        eos = len(frame.payload) > 1 and (frame.payload[1] & protocol.FLAG_EOS)
        data = frame.payload[2:]
        if self._session is None:
            return
        self._session.buffered += data
        if eos:
            self._session.eos = True

    def _consume_loop(self) -> None:
        """模拟固件消耗音频缓冲:按采样率×倍速扣减,周期性上报 free。

        注意必须无条件周期上报:若只在 free 变化时上报,当 PC 用完额度
        且设备恰好把缓冲清空(free 回到初始值)时会永不触发,造成死锁。
        """
        while not self._stop.is_set():
            time.sleep(0.05)
            session = self._session
            free = SESSION_BUFFER
            if session is not None:
                # A2DP 会话按播放状态消耗;HFP 通话语音会话建立即消耗
                if session.playing or session.sink == "hfp":
                    consume = int(session.bytes_per_second * 0.05 * self.speed)
                    del session.buffered[:consume]
                    if session.eos and not session.buffered:
                        session.playing = False
                        session.eos = False
                        if session.sink == "a2dp":
                            self.play_state = "stopped"
                            self._emit("music.ended", {})
                free = SESSION_BUFFER - len(session.buffered)
            self._emit("audio.buffer", {"free": free})

    def _set_call(self, state: str, number: str = None, call_setup: int = None) -> None:  # type: ignore[assignment]
        if number is not None:
            self.call["number"] = number
        if call_setup is not None:
            self.call["call_setup"] = call_setup
        self.call["state"] = state
        self.call["call_active"] = 1 if state == "active" else 0
        if state == "idle":
            self.call["call_setup"] = 0
            self.call["number"] = ""
        self._emit("bt.hfp.state", dict(self.call))
