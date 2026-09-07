#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vphone 图形控制台 —— 基于 vphone_lib.VPhone 的 tkinter 简易界面。

运行: python vphone_gui.py [--serial 手机序列号]
功能: 媒体(门B)/电话(门C)/蓝牙扫描配对 全按钮化 + 实时事件流(车机按键/配对状态)。
"""
import argparse
import os
import queue
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from vphone_lib import VPhone, VPhoneError


class App:
    def __init__(self, root, serial=None):
        import tkinter as tk
        from tkinter import scrolledtext, ttk
        self.tk, self.ttk, self.scrolledtext = tk, ttk, scrolledtext

        self.root = root
        root.title("VPhone 蓝牙测试控制台 (虚拟手机)")
        root.geometry("980x720")
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.vp: VPhone | None = None
        self.bt_devs: list[dict] = []

        self._build()
        root.after(100, self._drain)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._connect(serial)

    # ---------- 构建 ----------
    def _build(self):
        tk, ttk = self.tk, self.ttk
        scrolledtext = self.scrolledtext
        pad = {"padx": 6, "pady": 3}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="手机序列号:").pack(side="left")
        self.e_serial = ttk.Entry(top, width=22)
        self.e_serial.pack(side="left", padx=4)
        ttk.Button(top, text="连接", command=lambda: self._connect()).pack(side="left")
        self.lbl_conn = ttk.Label(top, text="未连接", foreground="#c22")
        self.lbl_conn.pack(side="left", padx=8)

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, **pad)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        # ---- 媒体(门B) ----
        f1 = ttk.LabelFrame(left, text="媒体 (门B: 元数据→车机 AVRCP; 车机按键回流见事件流)")
        f1.pack(fill="x", pady=2)
        r1 = ttk.Frame(f1); r1.pack(fill="x", padx=4, pady=2)
        for label, attr, default, w in (("标题", "e_title", "青花瓷", 22),
                                        ("歌手", "e_artist", "周杰伦", 12),
                                        ("专辑", "e_album", "测试专辑", 12),
                                        ("时长s", "e_dur", "229", 5)):
            ttk.Label(r1, text=label).pack(side="left")
            e = ttk.Entry(r1, width=w); e.insert(0, default); e.pack(side="left", padx=(2, 8))
            setattr(self, attr, e)
        ttk.Button(r1, text="推送元数据", command=self._push_track).pack(side="left")
        r2 = ttk.Frame(f1); r2.pack(fill="x", padx=4, pady=2)
        for text, fn in (("▶播放", lambda: self.vp.play()), ("⏸暂停", lambda: self.vp.pause()),
                         ("⏮上一曲", lambda: self.vp.prev()), ("⏭下一曲", lambda: self.vp.next())):
            ttk.Button(r2, text=text, command=lambda f=fn: self._run(f)).pack(side="left", padx=2)
        self.var_silence = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="静音流(保活A2DP)", variable=self.var_silence,
                        command=self._set_silence).pack(side="left", padx=10)
        ttk.Button(r2, text="载入demo播放列表", command=self._load_playlist).pack(side="left", padx=2)

        # ---- 电话(门C) ----
        f2 = ttk.LabelFrame(left, text="电话 (门C: 虚拟来电/去电, 全模拟无需SIM; 车机接听挂断回流见事件流)")
        f2.pack(fill="x", pady=2)
        r3 = ttk.Frame(f2); r3.pack(fill="x", padx=4, pady=2)
        ttk.Label(r3, text="号码:").pack(side="left")
        self.e_num = ttk.Entry(r3, width=16); self.e_num.insert(0, "13800138000")
        self.e_num.pack(side="left", padx=4)
        for text, fn in (("📞模拟来电", self._incoming), ("✔接听", lambda: self.vp.answer()),
                         ("✖挂断", lambda: self.vp.hangup()),
                         ("保持", lambda: self.vp.hold(True)),
                         ("恢复", lambda: self.vp.hold(False)),
                         ("拨出", self._dial),
                         ("🎧蓝牙通话音频", lambda: self.vp.audio_bt())):
            ttk.Button(r3, text=text, command=lambda f=fn: self._run(f)).pack(side="left", padx=2)

        # ---- 蓝牙 ----
        f3 = ttk.LabelFrame(left, text="蓝牙 (扫描/配对不出App; 已配对设备带 [A2DP已连]/[HFP已连] 标记)")
        f3.pack(fill="both", expand=True, pady=2)
        r4 = ttk.Frame(f3); r4.pack(fill="x", padx=4, pady=2)
        ttk.Button(r4, text="🔍扫描(约12s)", command=self._scan).pack(side="left")
        ttk.Button(r4, text="已配对/状态", command=lambda: self._run(self.vp.bt_state)).pack(side="left", padx=2)
        ttk.Button(r4, text="配对选中", command=self._bond_sel).pack(side="left", padx=2)
        ttk.Button(r4, text="解配选中", command=self._unpair_sel).pack(side="left", padx=2)
        ttk.Button(r4, text="启用电话账号", command=lambda: self._run(self.vp.enable_account)).pack(side="right")
        self.lb_bt = tk.Listbox(f3, height=5)
        self.lb_bt.pack(fill="both", expand=True, padx=4, pady=2)

        # ---- 事件流 ----
        f4 = ttk.LabelFrame(body, text="事件流 (logcat TAG=VPhone: 车机按键/配对状态/命令回执)")
        f4.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.txt = scrolledtext.ScrolledText(f4, width=52, state="disabled",
                                             font=("Consolas", 9),
                                             background="#111", foreground="#ddd")
        self.txt.pack(fill="both", expand=True, padx=4, pady=4)
        self.txt.tag_configure("btn", foreground="#ffd166")
        self.txt.tag_configure("bt", foreground="#7ec8ff")

    # ---------- 连接 ----------
    def _connect(self, serial=None):
        serial = serial or self.e_serial.get().strip() or None

        def task():
            try:
                vp = VPhone(serial=serial, autostart=True)
                vp.on_event = lambda line: self.q.put(("evt", line))
                vp.start_events()
                self.q.put(("ready", vp))
            except Exception as e:
                self.q.put(("connfail", f"{e}"))
        self.pool.submit(task)

    # ---------- 动作 ----------
    def _run(self, fn):
        if self.vp is None:
            self.log("!! 未连接")
            return

        def task():
            try:
                r = fn()
                if r is not None:
                    self.q.put(("log", str(r)))
            except Exception:
                self.q.put(("log", "[错误] " + traceback.format_exc(limit=3).replace("\n", " | ")))
        self.pool.submit(task)

    def _push_track(self):
        self._run(lambda: self.vp.set_track(
            self.e_title.get(), self.e_artist.get(), self.e_album.get(),
            int(self.e_dur.get() or 240)))

    def _load_playlist(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_pl.txt")
        self._run(lambda: self.vp.playlist(file=path))

    def _incoming(self):
        self._run(lambda: self.vp.incoming(self.e_num.get()))

    def _dial(self):
        self._run(lambda: self.vp.dial(self.e_num.get()))

    def _set_silence(self):
        self._run(lambda: self.vp.silence(self.var_silence.get()))

    def _scan(self):
        def task():
            try:
                devs = self.vp.bt_scan()
                self.q.put(("devs", devs))
                self.q.put(("log", f"扫描完成: {len(devs)} 台(双击列表项=配对)"))
            except Exception as e:
                self.q.put(("log", f"扫描失败: {e}"))
        self._run(lambda: None) if self.vp is None else self.pool.submit(task)

    def _sel_dev(self):
        sel = self.lb_bt.curselection()
        if not sel:
            self.log("!! 先在列表选中设备")
            return None
        return self.bt_devs[sel[0]]

    def _bond_sel(self):
        d = self._sel_dev()
        if d:
            self._run(lambda: self.vp.bt_bond(d["mac"]))

    def _unpair_sel(self):
        d = self._sel_dev()
        if d:
            self._run(lambda: self.vp.bt_unpair(d["mac"]))

    # ---------- 主线程泵 ----------
    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "evt":
                    tag = "btn" if "车机" in payload else ("bt" if "[蓝牙]" in payload else None)
                    self.log(payload, tag=tag)
                elif kind == "ready":
                    self.vp = payload
                    self.e_serial.delete(0, "end")
                    self.e_serial.insert(0, self.vp.serial or "")
                    self.lbl_conn.config(text=f"已连接 {self.vp.serial or '(单设备)'}",
                                         foreground="#0a7")
                    self.log("=== 已连接, 服务已拉起 ===")
                    self._run(self.vp.bt_state)
                elif kind == "connfail":
                    self.lbl_conn.config(text="连接失败", foreground="#c22")
                    self.log("!! 连接失败: " + payload)
                elif kind == "devs":
                    self.bt_devs = payload
                    self.lb_bt.delete(0, "end")
                    for d in payload:
                        self.lb_bt.insert("end", f"{d['name']}  {d['mac']}  {d['rssi']}dBm")
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def log(self, msg: str, tag=None):
        import datetime
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.txt.config(state="normal")
        self.txt.insert("end", f"[{stamp}] {msg}\n", tag)
        self.txt.see("end")
        self.txt.config(state="disabled")

    def _on_close(self):
        try:
            if self.vp:
                self.vp.stop_events()
            self.pool.shutdown(wait=False)
        finally:
            self.root.destroy()


def main():
    import tkinter as tk
    ap = argparse.ArgumentParser(description="vphone 图形控制台")
    ap.add_argument("--serial", help="手机序列号(多设备时必填)")
    args = ap.parse_args()
    root = tk.Tk()
    App(root, args.serial)
    root.mainloop()


if __name__ == "__main__":
    main()
