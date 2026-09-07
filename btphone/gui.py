"""btphone 图形界面 demo(tkinter)。

把蓝牙(命名/可见/配对/音乐/通话)与 WiFi(热点/STA/扫描)功能放进一个窗口,
供手工调试与演示;自动化测试请直接用 BtPhone 库(见 examples/)。

启动:
    btphone-gui                 # 真机,启动时选串口
    python -m btphone.gui --sim # 无硬件,内置模拟器(含"车机模拟"页)

所有阻塞调用走线程池,事件经队列回流主线程,界面不卡顿。
"""

from __future__ import annotations

import argparse
import queue
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import tkinter as tk

from . import BtPhone, SimPhone
from .media import make_tone_wav

MUSIC_EXT = [("音频", "*.wav *.mp3 *.flac *.m4a *.aac *.ogg"), ("所有文件", "*.*")]


class PhoneGui:
    MAX_LOG_LINES = 2000  # 日志上限:超长会话防止 Text 无限膨胀拖慢渲染

    def __init__(self, root: tk.Tk, sim_on_start: bool = False) -> None:
        self.root = root
        root.title("VPHONE 车机模拟控制台 (btphone)")
        root.geometry("1020x700")

        self.phone: BtPhone | None = None
        self.sim: SimPhone | None = None
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._evt_q: "queue.Queue[tuple]" = queue.Queue()
        self._ui_q: "queue.Queue[Callable]" = queue.Queue()
        self._playlist: list[str] = []
        self._pl_idx = -1
        self._link_lost = False  # 链路中断标记(工作线程读写,UI 状态灯据此切换)
        self._latency_sick = False  # USB 高延迟病理态(HUB/驱动批量滞留数据)
        # 蓝牙 profile 连接态(车机没拉起 profile 时音乐/通话命令都会被固件拒绝)
        self._profiles: dict[str, str | None] = {"a2dp": None, "avrcp": None, "hfp": None}

        self._build()
        self._drain_events()
        if sim_on_start:
            self._do_connect(sim=True)

    # ---------------- 布局 ----------------

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill="both", expand=True)

        # 连接条
        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        ttk.Label(bar, text="串口:").pack(side="left")
        self.cmb_port = ttk.Combobox(bar, width=22, state="readonly")
        self.cmb_port.pack(side="left", padx=(2, 4))
        ttk.Button(bar, text="刷新", width=5, command=self._refresh_ports).pack(side="left")
        self.btn_connect = ttk.Button(bar, text="连接", command=self._kick(self._do_connect))
        self.btn_connect.pack(side="left", padx=4)
        self.btn_sim = ttk.Button(bar, text="连接模拟器", command=self._kick(self._do_connect, True))
        self.btn_sim.pack(side="left")
        self.lbl_link = ttk.Label(bar, text="●未连接", foreground="#888")
        self.lbl_link.pack(side="left", padx=12)
        self.lbl_profiles = ttk.Label(bar, text="A2DP- AVRCP- HFP-", foreground="#888")
        self.lbl_profiles.pack(side="left")
        self.lbl_info = ttk.Label(bar, text="")
        self.lbl_info.pack(side="left")
        # 注意:info() 是阻塞串口请求,必须走线程池,否则点一下冻住整个界面
        ttk.Button(bar, text="刷新状态", width=8, command=self._kick(self._refresh_info)).pack(side="right")
        self._refresh_ports()

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True, pady=(6, 0))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # 左列:设备控制
        left = ttk.Labelframe(body, text="设备 / 配对", padding=8)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 6))

        row = ttk.Frame(left)
        row.grid(row=0, column=0, sticky="we")
        ttk.Label(row, text="蓝牙名").pack(side="left")
        self.var_name = tk.StringVar(value="VPHONE-01")
        ttk.Entry(row, textvariable=self.var_name, width=16).pack(side="left", padx=4)
        # 注意:点击时才取 var 的值,须直接调 _run;不能写 lambda: self._kick(...)
        # ——_kick 是工厂,返回的闭包若没人调用,点击就什么都不发生
        self.btn_set_name = ttk.Button(
            row, text="设置", name="btn_set_name",
            command=lambda: self._run(self._set_name, self.var_name.get()))
        self.btn_set_name.pack(side="left")

        row = ttk.Frame(left)
        row.grid(row=1, column=0, sticky="we", pady=(6, 0))
        self.btn_disc = ttk.Button(row, text="设为可见(60s)", command=self._kick(self._set_discoverable))
        self.btn_disc.pack(side="left")
        self.btn_disc_off = ttk.Button(row, text="关闭可见", command=self._kick(self._set_discoverable, False, 0))
        self.btn_disc_off.pack(side="left", padx=4)

        row = ttk.Frame(left)
        row.grid(row=2, column=0, sticky="we", pady=(10, 0))
        ttk.Label(row, text="配对模式").pack(side="left")
        self.cmb_pair = ttk.Combobox(row, width=11, state="readonly",
                                     values=["auto", "manual", "reject", "wrong_pin", "timeout"])
        self.cmb_pair.current(0)
        self.cmb_pair.pack(side="left", padx=4)
        self.cmb_pair.bind("<<ComboboxSelected>>", lambda e: self._run(self._set_pair_mode, self.cmb_pair.get()))
        self.btn_pair_ok = ttk.Button(row, text="接受", state="disabled",
                                      command=self._kick(self._pair_confirm, True))
        self.btn_pair_ok.pack(side="left")
        self.btn_pair_no = ttk.Button(row, text="拒绝", state="disabled",
                                      command=self._kick(self._pair_confirm, False))
        self.btn_pair_no.pack(side="left", padx=4)

        row = ttk.Frame(left)
        row.grid(row=3, column=0, sticky="we", pady=(10, 0))
        ttk.Label(row, text="对端 MAC").pack(side="left")
        self.var_mac = tk.StringVar(value="AA:BB:CC:DD:EE:FF")
        ttk.Entry(row, textvariable=self.var_mac, width=17).pack(side="left", padx=4)
        row = ttk.Frame(left)
        row.grid(row=4, column=0, sticky="we", pady=(4, 0))
        ttk.Button(row, text="主动连接", command=lambda: self._run(self._conn_connect, self.var_mac.get())).pack(side="left")
        ttk.Button(row, text="断开全部", command=self._kick(self._conn_disconnect)).pack(side="left", padx=4)
        ttk.Button(row, text="绑定列表", command=self._kick(self._list_bonded)).pack(side="left")

        self.txt_conn = tk.Text(left, height=7, width=30, state="disabled", font=("Consolas", 9))
        self.txt_conn.grid(row=5, column=0, sticky="we", pady=(10, 0))
        left.columnconfigure(0, weight=1)

        # 右侧:功能页
        self.nb = ttk.Notebook(body)
        self.nb.grid(row=0, column=1, sticky="nsew")

        self._build_music_tab()
        self._build_calls_tab()
        self._build_net_tab()
        self._build_car_tab()

        # 底部日志
        logbox = ttk.Labelframe(outer, text="事件 / 日志", padding=4)
        logbox.pack(fill="both", expand=True, pady=(6, 0))
        self.txt_log = ScrolledText(logbox, height=10, state="disabled", font=("Consolas", 9))
        self.txt_log.pack(fill="both", expand=True)
        for tag, color in (("evt", "#0a58ca"), ("ok", "#0a7a2f"), ("err", "#c62828"), ("cmd", "#555")):
            self.txt_log.tag_configure(tag, foreground=color)
        ttk.Button(logbox, text="清空", command=self._clear_log).pack(anchor="e")

    def _build_music_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="音乐 A2DP/AVRCP")

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="添加文件...", command=self._pick_files).pack(side="left")
        ttk.Button(row, text="生成测试音×3", command=self._gen_tones).pack(side="left", padx=4)
        ttk.Button(row, text="清空列表", command=self._clear_playlist).pack(side="left")

        self.lst_files = tk.Listbox(tab, height=6)
        self.lst_files.pack(fill="x", pady=6)

        row = ttk.Frame(tab)
        row.pack(fill="x")
        for text, cmd in (("▶ 播放", lambda: self._run(self._music_play, self.var_loop.get(),
                                                           (self.lst_files.curselection() or [0])[0])),
                          ("⏸ 暂停", self._kick(self._music_pause)),
                          ("⏵ 继续", self._kick(self._music_resume)),
                          ("■ 停止", self._kick(self._music_stop)),
                          ("⏮ 上一首", self._kick(self._music_prev)),
                          ("⏭ 下一首", self._kick(self._music_next))):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=(0, 4))
        self.var_loop = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="循环", variable=self.var_loop).pack(side="left", padx=8)
        self.lbl_track = ttk.Label(row, text="当前: -")
        self.lbl_track.pack(side="left", padx=8)

        sep = ttk.Separator(tab)
        sep.pack(fill="x", pady=8)
        ttk.Label(tab, text="推送曲目信息(车机媒体面板显示)").pack(anchor="w")
        grid = ttk.Frame(tab)
        grid.pack(fill="x", pady=4)
        self.var_title = tk.StringVar(value="测试曲目 01")
        self.var_artist = tk.StringVar(value="VPHONE 乐队")
        self.var_album = tk.StringVar(value="车载测试专辑")
        for i, (lbl, var) in enumerate((("歌名", self.var_title), ("歌手", self.var_artist), ("专辑", self.var_album))):
            ttk.Label(grid, text=lbl).grid(row=0, column=i * 2, padx=(8 if i else 0, 2), sticky="e")
            ttk.Entry(grid, textvariable=var, width=14).grid(row=0, column=i * 2 + 1)
        ttk.Button(tab, text="推送元数据", command=lambda: self._run(
            self._push_metadata, self.var_title.get(), self.var_artist.get(), self.var_album.get())).pack(anchor="w", pady=4)

    def _build_calls_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="通话 HFP")

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="号码").pack(side="left")
        self.var_number = tk.StringVar(value="13800138000")
        ttk.Entry(row, textvariable=self.var_number, width=16).pack(side="left", padx=4)

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=8)
        for text, cmd in (("📞 模拟来电", lambda: self._run(self._calls_incoming, self.var_number.get())),
                          ("✋ 手机侧接听", self._kick(self._calls_answer)),
                          ("📵 挂断", self._kick(self._calls_hangup)),
                          ("📤 模拟拨出", lambda: self._run(self._calls_dial, self.var_number.get()))):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=(0, 4))

        sep = ttk.Separator(tab)
        sep.pack(fill="x", pady=6)
        ttk.Label(tab, text="通话语音(接通后循环推流的音频)").pack(anchor="w")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=4)
        self.var_voice = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.var_voice, width=46, state="readonly").pack(side="left")
        ttk.Button(row, text="选择...", command=self._pick_voice).pack(side="left", padx=4)
        ttk.Button(row, text="清除", command=lambda: self.var_voice.set("")).pack(side="left")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="语音开", command=lambda: self._run(self._voice_start, self.var_voice.get())).pack(side="left")
        ttk.Button(row, text="语音关", command=self._kick(self._voice_stop)).pack(side="left", padx=4)
        self.lbl_call = ttk.Label(tab, text="通话状态: 空闲")
        self.lbl_call.pack(anchor="w", pady=8)

    def _build_net_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="WiFi / 网络")

        box = ttk.Labelframe(tab, text="热点 AP(车机连此热点)", padding=6)
        box.pack(fill="x")
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="SSID").pack(side="left")
        self.var_ap_ssid = tk.StringVar(value="VPHONE-AP")
        ttk.Entry(row, textvariable=self.var_ap_ssid, width=16).pack(side="left", padx=4)
        ttk.Label(row, text="密码").pack(side="left")
        self.var_ap_pwd = tk.StringVar(value="12345678")
        ttk.Entry(row, textvariable=self.var_ap_pwd, width=14).pack(side="left", padx=4)
        row = ttk.Frame(box)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="开启热点", command=lambda: self._run(self._net_ap, True, self.var_ap_ssid.get(), self.var_ap_pwd.get())).pack(side="left")
        ttk.Button(row, text="关闭热点", command=lambda: self._run(self._net_ap, False, "", "")).pack(side="left", padx=4)
        self.lbl_ap = ttk.Label(box, text="")
        self.lbl_ap.pack(anchor="w")

        box = ttk.Labelframe(tab, text="STA 接入(板子连路由器/电脑热点)", padding=6)
        box.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="SSID").pack(side="left")
        self.var_sta_ssid = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.var_sta_ssid, width=16).pack(side="left", padx=4)
        ttk.Label(row, text="密码").pack(side="left")
        self.var_sta_pwd = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.var_sta_pwd, width=14, show="*").pack(side="left", padx=4)
        ttk.Button(row, text="连接 STA", command=lambda: self._run(self._net_sta, self.var_sta_ssid.get(), self.var_sta_pwd.get())).pack(side="left")
        self.lbl_sta = ttk.Label(box, text="")
        self.lbl_sta.pack(anchor="w", pady=2)

        box = ttk.Labelframe(tab, text="周围 AP", padding=6)
        box.pack(fill="both", expand=True, pady=(8, 0))
        ttk.Button(box, text="🔍 扫描(阻塞 1.5~3s)", command=self._kick(self._net_scan)).pack(anchor="w")
        cols = ("ssid", "rssi", "auth")
        self.tv_scan = ttk.Treeview(box, columns=cols, show="headings", height=7)
        for c, w, txt in (("ssid", 220, "SSID"), ("rssi", 70, "信号"), ("auth", 60, "认证")):
            self.tv_scan.heading(c, text=txt)
            self.tv_scan.column(c, width=w, anchor="w")
        self.tv_scan.pack(fill="both", expand=True, pady=4)
        self.lbl_net = ttk.Label(tab, text="")
        self.lbl_net.pack(anchor="w")

    def _build_car_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="车机模拟(仅模拟器)")
        self.tab_car = tab
        ttk.Label(tab, text="连接模拟器后,用下面的按钮扮演车机,触发手机侧的联动。", wraplength=520).pack(anchor="w")
        box = ttk.Labelframe(tab, text="配对 / 连接", padding=6)
        box.pack(fill="x", pady=4)
        ttk.Button(box, text="车机发起配对", command=lambda: self._car("car_pair")).pack(side="left")
        ttk.Button(box, text="车机连入(a2dp+hfp)", command=lambda: self._car("car_connect")).pack(side="left", padx=4)
        box = ttk.Labelframe(tab, text="播放控制(验证 AVRCP 联动)", padding=6)
        box.pack(fill="x", pady=4)
        for text, key in (("▶ 播放", "play"), ("⏸ 暂停", "pause"), ("⏭ 下一首", "next"), ("⏮ 上一首", "prev")):
            ttk.Button(box, text=text, command=lambda k=key: self._car("car_press", k)).pack(side="left", padx=(0, 4))
        box = ttk.Labelframe(tab, text="通话", padding=6)
        box.pack(fill="x", pady=4)
        ttk.Button(box, text="车机接听", command=lambda: self._car("car_answer")).pack(side="left")
        ttk.Button(box, text="车机挂断", command=lambda: self._car("car_hangup")).pack(side="left", padx=4)

    # ---------------- 基础设施 ----------------

    def _kick(self, fn, *args):
        """command= 工厂:返回闭包,点击时检查连接并把动作丢线程池。"""
        return lambda: self._run(fn, *args)

    def _run(self, fn, *args):
        if self.phone is None:
            self._log("请先连接(串口或模拟器)", "err")
            return
        self._pool.submit(self._safe, fn, *args)

    def _post(self, fn):
        """把 UI 更新投递到主线程执行。工作线程绝不能直接调 tkinter——
        连 root.after() 也不行:跨线程调用只有主线程恰好跑在 mainloop 里
        才会被处理,否则永久挂起该工作线程(表现为界面偶发卡死)。
        统一走线程安全队列,由主线程 _drain_events 消费。"""
        self._ui_q.put(fn)

    def _safe(self, fn, *args):
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            msg = f"{fn.__name__} 失败: {exc}"
            self._log(msg, "err")
            if not self.sim and self.phone is not None and not self.phone._transport.is_open:
                # 链路被打断(推流/射频瞬态):标记状态,下次操作 transport 会自动重连
                self._link_lost = True
                self._post(lambda: self.lbl_link.configure(text="●重连中", foreground="#c06000"))
                self._log("  [link] 链路中断;下次操作会自动重连(通常几秒,最长约2分钟)", "cmd")
            # WiFi 射频上电可能打掉 USB 链路:自动无复位重连
            if self._is_net_op(fn.__name__) and not self.sim and self.phone is not None:
                try:
                    self._log("  [blip] 疑似射频上电掉线,等待板子回来...", "cmd")
                    self.phone.wait_reconnect(timeout=90)
                    self._log("  [blip] 已重连,请重试该操作", "ok")
                except Exception as exc2:  # noqa: BLE001
                    self._log(f"  [blip] 重连失败: {exc2}", "err")
            return
        # 操作成功且此前链路中断过:transport 自动重连已生效,恢复状态灯
        if not self.sim and self.phone is not None and self._link_lost:
            self._link_lost = False
            self._post(lambda: self.lbl_link.configure(text="●已连接", foreground="#0a7a2f"))
            self._log("  [link] 链路已自动恢复", "ok")

    @staticmethod
    def _is_net_op(name: str) -> bool:
        return name in ("_net_ap", "_net_sta", "_net_scan")

    def _log(self, msg: str, tag: str = "cmd") -> None:
        # 工作线程也能直接调:经 UI 队列转到主线程执行(tkinter 只允许主线程碰控件)
        if threading.current_thread() is not threading.main_thread():
            self._post(lambda: self._log(msg, tag))
            return
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n", tag)
        self.txt_log.see("end")
        lines = int(self.txt_log.index("end-1c").split(".")[0])
        if lines > self.MAX_LOG_LINES:
            self.txt_log.delete("1.0", f"{lines - self.MAX_LOG_LINES}.0")
        self.txt_log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def _refresh_ports(self) -> None:
        from serial.tools import list_ports

        items = [f"{p.device}  {p.description}" for p in list_ports.comports()]
        self.cmb_port["values"] = items
        if items:
            self.cmb_port.current(0)

    # ---------------- 连接 / 事件泵 ----------------

    def _do_connect(self, sim: bool = False, port: str = "") -> None:
        if self.phone is not None:
            return
        if sim:
            self.sim = SimPhone(name="VPHONE-01")
            phone = BtPhone("SIM", io=self.sim.device_io)
        else:
            sel = port or self.cmb_port.get()
            if not sel:
                self._post(lambda: self._log("没有可用串口(或选模拟器)", "err"))
                return
            phone = BtPhone(sel.split()[0])
        # audio.buffer 心跳(50ms 一条)不进 UI,避免日志刷屏
        phone.on_event("*", lambda n, d: None if n == "audio.buffer" else self._evt_q.put((n, d)))
        self.phone = phone
        self._post(lambda: self._on_connected(sim))

    def _on_connected(self, sim: bool) -> None:
        self.lbl_link.configure(text="●已连接", foreground="#0a7a2f")
        self.btn_connect.configure(state="disabled")
        self.btn_sim.configure(state="disabled")
        self._log(f"连接成功({'模拟器' if sim else '真机'})", "ok")
        self._pool.submit(self._safe, self._refresh_info)
        self._car_sync_tab(sim)

    def _car_sync_tab(self, sim: bool) -> None:
        state = "normal" if sim else "disabled"
        for w in self.tab_car.winfo_children():
            for child in w.winfo_children():
                if isinstance(child, ttk.Button):
                    child.configure(state=state)
        if not sim:
            self.nb.tab(self.tab_car, text="车机模拟(需模拟器)")

    def _drain_events(self) -> None:
        self._watch_link_latency()
        # 先执行工作线程投递的 UI 更新,再处理设备事件(都在主线程)
        while True:
            try:
                fn = self._ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except tk.TclError:
                pass  # 窗口销毁后残留的回调,无害
        while True:
            try:
                name, data = self._evt_q.get_nowait()
            except queue.Empty:
                break
            self._log(f"◀ {name} {data}", "evt")
            self._on_event(name, data)
        self.root.after(80, self._drain_events)

    def _watch_link_latency(self) -> None:
        """心跳(50ms 一条)断流超秒级 = USB 链路高延迟病理态:HUB 转发芯片/
        驱动把数据批量滞留数秒,命令往返 3~10s。数据不丢、板子没事,但操作
        会变慢;重插板子或换 HUB 口可恢复。"""
        tr = self.phone._transport if self.phone is not None else None
        sick = bool(tr is not None and not self.sim and tr.is_open and tr.link_sick())
        if sick == self._latency_sick:
            return
        self._latency_sick = sick
        if sick:
            if not self._link_lost:
                self.lbl_link.configure(text="●高延迟", foreground="#b8860b")
            self._log("[link] USB 链路高延迟(命令响应 3~10s):数据没丢,是 HUB/驱动滞留;"
                      "重插板子/换 HUB 口可恢复", "err")
        else:
            if not self._link_lost and self.lbl_link.cget("text") == "●高延迟":
                self.lbl_link.configure(text="●已连接", foreground="#0a7a2f")
                self._log("[link] 链路延迟已恢复", "ok")

    def _on_event(self, name: str, d: dict) -> None:
        # 注意:本方法在主线程跑,绝不能调 phone.* 阻塞请求——一律丢线程池
        if name == "pair.request":
            self.btn_pair_ok.configure(state="normal")
            self.btn_pair_no.configure(state="normal")
            self._log("车机请求配对,可在左侧点[接受/拒绝](manual 模式)", "ok")
        elif name == "pair.result":
            ok = d.get("ok")
            self._log(f"配对结果: {'成功' if ok else '失败(' + str(d.get('reason')) + ')'}", "ok" if ok else "err")
            self.btn_pair_ok.configure(state="disabled")
            self.btn_pair_no.configure(state="disabled")
            self._submit_refresh_conn()
        elif name == "bt.conn":
            prof = str(d.get("profile", ""))
            if prof in self._profiles:
                self._profiles[prof] = str(d.get("state", ""))
                self._render_profiles()
            self._submit_refresh_conn()
        elif name in ("bt.a2dp.state", "bt.avrcp.state", "bt.hfp.state"):
            prof = {"bt.a2dp.state": "a2dp", "bt.avrcp.state": "avrcp", "bt.hfp.state": "hfp"}[name]
            state = str(d.get("state", ""))
            if name != "bt.hfp.state" or state in ("connected", "disconnected"):
                self._profiles[prof] = state
                self._render_profiles()
            self._submit_refresh_conn()
            if name == "bt.hfp.state":
                txt = {"idle": "空闲", "incoming": "来电响铃", "outgoing": "呼出",
                       "active": "通话中", "connected": "HFP 已连接"}.get(state, str(state))
                self.lbl_call.configure(text=f"通话状态: {txt}")
        elif name == "music.ended":
            if self._playlist and 0 <= self._pl_idx < len(self._playlist) - 1:
                self._pl_idx += 1
            elif not (self.phone and self.var_loop.get()):
                self.lbl_track.configure(text="当前: 播放结束")
                return
            self._update_track_label()

    def _render_profiles(self) -> None:
        """顶栏 profile 指示灯:绿=已连接,黄=连接中,灰=未连接/未知。"""
        parts = []
        for prof, label in (("a2dp", "A2DP"), ("avrcp", "AVRCP"), ("hfp", "HFP")):
            state = self._profiles.get(prof)
            mark = {"connected": "●", "connecting": "◐", "disconnecting": "◐"}.get(state or "", "○")
            parts.append(f"{label}{mark}")
        all_up = all(s == "connected" for s in self._profiles.values())
        self.lbl_profiles.configure(text=" ".join(parts),
                                    foreground="#0a7a2f" if all_up else "#888")

    # ---------------- 左列动作 ----------------

    def _set_name(self, name: str) -> None:
        r = self.phone.set_name(name)
        self._log(f"set_name → {r}")
        self._refresh_info()  # 回读并刷新左栏/标题的名字显示

    def _set_discoverable(self, on: bool = True, timeout_s: int = 60) -> None:
        self.phone.set_discoverable(on, timeout_s=timeout_s)
        self._log(f"可见性: {'开(' + str(timeout_s) + 's)' if on else '关'} — 请在车机上搜索配对", "ok")

    def _set_pair_mode(self, mode: str) -> None:
        self.phone.pairing.set_mode(mode)
        self._log(f"配对模式 → {mode}", "ok")

    def _pair_confirm(self, accept: bool) -> None:
        self.phone.pairing.confirm(accept)
        self._log(f"配对确认: {'接受' if accept else '拒绝'}")

    def _conn_connect(self, mac: str) -> None:
        r = self.phone.conn.connect(mac)
        self._log(f"主动连接 {mac} → {r}", "ok")

    def _conn_disconnect(self) -> None:
        self.phone.conn.disconnect("all")
        self._log("已断开全部 profile")

    def _list_bonded(self) -> None:
        devs = self.phone.pairing.list_bonded()
        self._log(f"绑定设备 {len(devs)} 个: {devs}")

    def _refresh_info(self) -> None:
        """拉系统信息并刷新状态条(阻塞串口请求,只允许在工作线程跑)。"""
        if self.phone is None:
            return
        info = self.phone.info()
        def render():
            self.lbl_info.configure(
                text=f"{info.get('name', '')} | 空闲堆 {info.get('free_heap', 0) // 1024}K | wifi {info.get('wifi_init')}")
        self._post(render)
        self._refresh_conn()

    def _refresh_conn(self) -> None:
        """拉连接状态并渲染(阻塞串口请求,只允许在工作线程跑)。"""
        if self.phone is None:
            return
        try:
            st = self.phone.conn.status()
        except Exception:  # noqa: BLE001
            return  # 链路忙/中断:保留旧内容,避免事件风暴刷屏报错
        self._post(lambda: self._set_conn_text(st))

    def _submit_refresh_conn(self) -> None:
        if self.phone is not None:
            self._pool.submit(self._safe, self._refresh_conn)

    def _set_conn_text(self, st: dict) -> None:
        """渲染连接状态框(纯 UI,只允许在主线程跑)。"""
        conns = st.get("connections") or {}
        if isinstance(conns, dict):
            for prof in ("a2dp", "avrcp", "hfp"):
                val = conns.get(prof)
                if val is not None:
                    self._profiles[prof] = val if isinstance(val, str) else (
                        "connected" if val else "disconnected")
            self._render_profiles()
        self.txt_conn.configure(state="normal")
        self.txt_conn.delete("1.0", "end")
        self.txt_conn.insert("end", f"名字: {st.get('name', '-')}\n")
        self.txt_conn.insert("end", f"配对模式: {st.get('pair_mode', '-')}\n")
        peer = conns.get("peer") if isinstance(conns, dict) else None
        if peer:
            self.txt_conn.insert("end", f"对端: {peer}\n")
        for p in ("a2dp", "avrcp", "hfp"):
            s = conns.get(p) if isinstance(conns, dict) else None
            if isinstance(s, bool):
                s = "connected" if s else "disconnected"
            self.txt_conn.insert("end", f"{p}: {s or '未连接'}\n")
        self.txt_conn.configure(state="disabled")

    # ---------------- 音乐 ----------------

    def _pick_files(self) -> None:
        paths = filedialog.askopenfilenames(title="选择音频文件", filetypes=MUSIC_EXT)
        for p in paths:
            self._playlist.append(p)
            self.lst_files.insert("end", p)

    def _gen_tones(self) -> None:
        import os
        import tempfile

        paths = []
        for i, freq in enumerate((440, 660, 880), 1):
            p = os.path.join(tempfile.gettempdir(), f"tone_{i}_{freq}hz.wav")
            make_tone_wav(p, duration_s=5.0, freq=freq)
            paths.append(p)
            self._playlist.append(p)
            self.lst_files.insert("end", p)
        self._log(f"已生成 {len(paths)} 个测试音(临时目录)", "ok")

    def _clear_playlist(self) -> None:
        self._playlist.clear()
        self.lst_files.delete(0, "end")
        self._pl_idx = -1
        self.lbl_track.configure(text="当前: -")

    def _music_play(self, loop: bool, start: int | None = None) -> None:
        if not self._playlist:
            self._log("播放列表为空(先添加文件或生成测试音)", "err")
            return
        self._pl_idx = start if start is not None else 0
        r = self.phone.music.play(files=self._playlist, loop=loop)
        self._log(f"播放 {r} (从第 {self._pl_idx + 1} 首起,车机应出声)", "ok")
        self._update_track_label()

    def _music_pause(self) -> None:
        self.phone.music.pause()
        self._log("暂停")

    def _music_resume(self) -> None:
        self.phone.music.resume()
        self._log("继续播放")

    def _music_stop(self) -> None:
        self.phone.music.stop()
        self._log("停止")
        self._post(lambda: self.lbl_track.configure(text="当前: -"))

    def _music_next(self) -> None:
        self.phone.music.next()
        if self._playlist:
            self._pl_idx = (self._pl_idx + 1) % len(self._playlist)
        self._update_track_label()

    def _music_prev(self) -> None:
        self.phone.music.prev()
        if self._playlist:
            self._pl_idx = (self._pl_idx - 1) % len(self._playlist)
        self._update_track_label()

    def _update_track_label(self) -> None:
        if 0 <= self._pl_idx < len(self._playlist):
            import os

            name = os.path.basename(self._playlist[self._pl_idx])
            self._post(lambda: self.lbl_track.configure(text=f"当前: {name}"))

    def _push_metadata(self, title: str, artist: str, album: str) -> None:
        self.phone.music.set_track_info(title=title, artist=artist, album=album)
        self._log(f"元数据已推送: {title} / {artist} (车机媒体面板应刷新)", "ok")

    # ---------------- 通话 ----------------

    def _calls_incoming(self, number: str) -> None:
        self.phone.calls.incoming(number)
        self._log(f"模拟来电 {number} — 车机应响铃", "ok")

    def _calls_answer(self) -> None:
        self.phone.calls.answer()
        self._log("手机侧接听")

    def _calls_hangup(self) -> None:
        self.phone.calls.hangup()
        self._log("已挂断")

    def _calls_dial(self, number: str) -> None:
        self.phone.calls.dial(number)
        self._log(f"模拟拨出 {number} — 车机应显示呼出", "ok")

    def _pick_voice(self) -> None:
        p = filedialog.askopenfilename(title="选择通话语音文件", filetypes=MUSIC_EXT)
        if p:
            self.var_voice.set(p)
            self.phone.calls.set_voice(p)
            self._log(f"通话语音 → {p}")

    def _voice_start(self, voice: str) -> None:
        if voice:
            self.phone.calls.set_voice(voice)
        self.phone.calls.voice_start()
        self._log("通话语音推流开")

    def _voice_stop(self) -> None:
        self.phone.calls.voice_stop()
        self._log("通话语音推流关")

    # ---------------- WiFi ----------------

    def _net_ap(self, on: bool, ssid: str, pwd: str) -> None:
        if on:
            self.phone.net.ap_start(ssid, pwd)
            self._post(lambda: self.lbl_ap.configure(
                text=f"热点 {ssid} 指令已发(射频上电可能引起 USB 暂态掉线)"))
            self._log(f"热点开启指令已发: {ssid}", "ok")
        else:
            self.phone.net.ap_stop()
            self._post(lambda: self.lbl_ap.configure(text="热点已关"))
            self._log("热点关闭")

    def _net_sta(self, ssid: str, pwd: str) -> None:
        self.phone.net.wifi_sta(ssid, pwd)
        self._log(f"STA 接入指令已发: {ssid} (结果看 net.sta 事件)")

    def _net_scan(self) -> None:
        aps = self.phone.net.wifi_scan()
        rows = [(ap.get("ssid", ""), ap.get("rssi"), ap.get("auth")) for ap in aps]
        self._log(f"扫描到 {len(aps)} 个 AP", "ok")

        def fill() -> None:
            self.tv_scan.delete(*self.tv_scan.get_children())
            for r in rows:
                self.tv_scan.insert("", "end", values=r)

        self._post(fill)

    # ---------------- 车机模拟 ----------------

    def _car(self, method: str, *args) -> None:
        if self.sim is None:
            return
        getattr(self.sim, method)(*args)
        self._log(f"[车机] {method} {args or ''}")

    # ---------------- 退出 ----------------

    def shutdown(self) -> None:
        if self.phone is not None:
            try:
                self.phone.close()
            except Exception:
                pass
        if self.sim is not None:
            self.sim.close()
        self._pool.shutdown(wait=False)


def main() -> None:
    ap = argparse.ArgumentParser(description="btphone 图形控制台")
    ap.add_argument("--sim", action="store_true", help="启动即连接内置模拟器")
    ap.add_argument("--port", default="", help="直接连接指定串口")
    args = ap.parse_args()

    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    gui = PhoneGui(root, sim_on_start=args.sim)
    if args.port:
        gui.cmb_port.set(args.port)
        # 直连不经 _run(它有"未连接"守卫,会拦掉首次连接)
        gui._pool.submit(gui._safe, gui._do_connect, False, args.port)
    root.protocol("WM_DELETE_WINDOW", lambda: (gui.shutdown(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
