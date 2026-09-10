#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vphone 图形控制台 —— 基于 vphone_lib.VPhone 的 tkinter 简易界面。

运行: python vphone_gui.py [--serial 手机序列号]
功能: 媒体(门B: 元数据+电脑音频推车机)/电话(门C: 含通话自定义音频)/联系人(1w压测)/
      蓝牙扫描配对 全按钮化 + 实时结构化事件流(车机按键回流 CAR_*)。

── 线程模型(改代码前必读) ─────────────────────────────────────────────
· 主线程 = tkinter 主循环 + 一切控件读写。后台线程绝不直接碰控件:
  Entry/Combobox/BooleanVar 的 .get() 看似无害, 但 tkinter 非线程安全,
  跨线程调用偶发崩溃 —— 所有控件取值在提交任务前于主线程完成(闭包捕获值)。
· 后台线程 = ThreadPoolExecutor(按钮动作) + _evt_poll/_stat_poll(轮询) +
  lib 内 logcat 线程; 它们只通过 self.q 发消息, 由主线程 _drain() 消费。
· _drain 消息协议(kind → payload → 主线程动作):
    log   → str        → 事件窗追加一行
    evt   → str        → logcat 原始行(兜底通道, 已滤 [事件# 重复)
    tevt  → event dict → 结构化事件(🚗car/⌨cmd 高亮)
    ready → VPhone     → 连接成功: 换 vp 引用+启动代际轮询+环境加固
    connfail → str     → 连接失败提示
    devs  → [dict]     → 蓝牙扫描结果回填列表
    contacts/media/pos/callst → 状态区刷新 / call → 主线程执行可调用对象
· 轮询代际: 每次「连接」成功 _gen+1, 旧代 _evt_poll/_stat_poll 检测到代际
  变化即退出 —— 重复点「连接」不会叠加轮询线程(否则事件双份/进度双刷)。
"""
import argparse
import os
import queue
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from vphone_lib import VPhone, VPhoneError, VEvent

AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus")


class App:
    def __init__(self, root, serial=None):
        import tkinter as tk
        from tkinter import scrolledtext, ttk
        self.tk, self.ttk, self.scrolledtext = tk, ttk, scrolledtext

        self.root = root
        root.title("VPhone 蓝牙测试控制台 (虚拟手机)")
        root.geometry("1040x760")
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.vp: VPhone | None = None
        self.bt_devs: list[dict] = []
        self._evt_stop = threading.Event()
        self._gen = 0            # 连接代际: 每次连接成功 +1, 旧轮询线程见代际变化即退出

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
        ttk.Button(top, text="📦安装APK", command=self._install_apk).pack(side="left", padx=4)
        ttk.Button(top, text="🚀启动App", command=self._launch_app).pack(side="left")
        self.lbl_conn = ttk.Label(top, text="未连接", foreground="#c22")
        self.lbl_conn.pack(side="left", padx=8)

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, **pad)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        # ---- 媒体(门B) ----
        f1 = ttk.LabelFrame(left, text="媒体 (门B: 元数据→车机AVRCP; 电脑音频→车机真实播放; 车机按键回流见事件流)")
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
        self.var_silence = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="静音流(保活A2DP)", variable=self.var_silence,
                        command=self._set_silence).pack(side="left", padx=10)
        r2b = ttk.Frame(f1); r2b.pack(fill="x", padx=4, pady=2)
        ttk.Button(r2b, text="⇧电脑选曲→上传→车机播放", command=self._pick_audio).pack(side="left")
        ttk.Button(r2b, text="🎵播放列表管理", command=self._playlist_mgr).pack(side="left", padx=4)
        # 进度条: 释放跳转(车机/手机拖动同理回流 CAR_SEEK), 平时 1s 跟随刷新
        sp = ttk.Frame(f1); sp.pack(fill="x", padx=8, pady=(0, 0))
        ttk.Label(sp, text="进度s").pack(side="left")
        self._pos_drag = False
        self.scale_pos = tk.Scale(sp, from_=0, to=240, orient="horizontal",
                                  showvalue=True, resolution=1, length=480)
        self.scale_pos.pack(side="left", fill="x", expand=True, padx=6)
        self.scale_pos.bind("<ButtonPress-1>", lambda e: setattr(self, "_pos_drag", True))
        self.scale_pos.bind("<ButtonRelease-1>", self._pos_release)
        self.lbl_media = ttk.Label(f1, text="当前: -(未播放)", foreground="#357")
        self.lbl_media.pack(fill="x", padx=8, pady=(0, 3))

        # ---- 电话(门C) ----
        f2 = ttk.LabelFrame(left, text="电话 (门C: 来电/去电/呼叫等待/保持切换; 车机按键回流见事件流)")
        f2.pack(fill="x", pady=2)
        r3 = ttk.Frame(f2); r3.pack(fill="x", padx=4, pady=2)
        ttk.Label(r3, text="号码:").pack(side="left")
        self.e_num = ttk.Entry(r3, width=16); self.e_num.insert(0, "13800138000")
        self.e_num.pack(side="left", padx=4)
        for text, fn in (("📞模拟来电", self._incoming), ("✔接听/接通", lambda: self.vp.answer()),
                         ("✖挂断", lambda: self.vp.hangup()),
                         ("保持", lambda: self.vp.hold(on=True)),
                         ("恢复", lambda: self.vp.hold(on=False)),
                         ("⇄切换", lambda: self.vp.swap()),
                         ("拨出", self._dial),
                         ("🎧蓝牙通话音频", lambda: self.vp.audio_bt())):
            ttk.Button(r3, text=text, command=lambda f=fn: self._run(f)).pack(side="left", padx=2)
        # 默认手动: 车机拨出后停在拨号态, 由「✔接听/接通」按钮远程摘机(模拟对端接听);
        # 勾上则回到老行为(3s 自动接通)。连接成功时会把勾选框状态同步到手机。
        self.var_autoout = tk.BooleanVar(value=False)
        ttk.Checkbutton(r3, text="车机拨出3s自动接通", variable=self.var_autoout,
                        command=self._set_auto_outgoing).pack(side="left", padx=10)
        r3b = ttk.Frame(f2); r3b.pack(fill="x", padx=4, pady=2)
        ttk.Label(r3b, text="通话音频(对端说话):").pack(side="left")
        self.cb_caudio = ttk.Combobox(r3b, width=32, state="readonly")
        self.cb_caudio.pack(side="left", padx=4)
        ttk.Button(r3b, text="↻刷新", width=3, command=self._refresh_caudio).pack(side="left")
        self.var_caloop = tk.BooleanVar(value=False)
        ttk.Checkbutton(r3b, text="循环", variable=self.var_caloop).pack(side="left")
        ttk.Button(r3b, text="▶播放(需通话中)",
                   command=self._call_audio).pack(side="left", padx=4)
        ttk.Button(r3b, text="⏹停止", command=lambda: self._run(self.vp.call_audio_stop)).pack(side="left")
        # 多路通话状态行(1s 轮询 /status 刷新): 双通话时显示 [active:X,held:Y] 一眼看清
        self.lbl_call = ttk.Label(f2, text="通话: idle", foreground="#753")
        self.lbl_call.pack(fill="x", padx=8, pady=(0, 3))

        # ---- 联系人 ----
        f2b = ttk.LabelFrame(left, text="联系人 (自定义/批量1w → 车机 PBAP 通讯录压测; 只写 vphone 账号)")
        f2b.pack(fill="x", pady=2)
        r5 = ttk.Frame(f2b); r5.pack(fill="x", padx=4, pady=2)
        self.lbl_contacts = ttk.Label(r5, text="计数: ?")
        self.lbl_contacts.pack(side="left", padx=8)
        ttk.Label(r5, text="数量:").pack(side="left")
        self.e_cnt = ttk.Entry(r5, width=8); self.e_cnt.insert(0, "10000")
        self.e_cnt.pack(side="left", padx=2)
        ttk.Button(r5, text="批量生成", command=self._contacts_load).pack(side="left", padx=2)
        ttk.Button(r5, text="导入txt(姓名|号码)", command=self._contacts_file).pack(side="left", padx=2)
        ttk.Button(r5, text="清空", command=self._contacts_clear).pack(side="left", padx=2)
        ttk.Button(r5, text="刷新计数", command=self._contacts_refresh).pack(side="left", padx=2)

        # ---- 蓝牙 ----
        f3 = ttk.LabelFrame(left, text="蓝牙 (扫描/配对不出App; 已配对设备带 [A2DP已连]/[HFP已连] 标记)")
        f3.pack(fill="both", expand=True, pady=2)
        r4 = ttk.Frame(f3); r4.pack(fill="x", padx=4, pady=2)
        ttk.Button(r4, text="🔍扫描(约12s)", command=self._scan).pack(side="left")
        ttk.Button(r4, text="已配对/状态", command=lambda: self._run(self.vp.bt_state)).pack(side="left", padx=2)
        ttk.Button(r4, text="配对选中", command=self._bond_sel).pack(side="left", padx=2)
        ttk.Button(r4, text="解配选中", command=self._unpair_sel).pack(side="left", padx=2)
        ttk.Button(r4, text="🔌重连(断线恢复)", command=self._reconnect_sel).pack(side="left", padx=2)
        ttk.Button(r4, text="⏹断开(保配对)", command=self._disconnect_sel).pack(side="left", padx=2)
        ttk.Button(r4, text="📇授权车机拉通讯录", command=self._allow_car_sel).pack(side="left", padx=2)
        ttk.Button(r4, text="启用电话账号", command=lambda: self._run(self.vp.enable_account)).pack(side="right")
        r4b = ttk.Frame(f3); r4b.pack(fill="x", padx=4, pady=2)
        ttk.Label(r4b, text="蓝牙名:").pack(side="left")
        self.e_btname = ttk.Entry(r4b, width=20)
        self.e_btname.pack(side="left", padx=4)
        ttk.Button(r4b, text="✏改名", command=self._bt_rename).pack(side="left")
        ttk.Label(r4b, text="(车机重连后显示新名)", foreground="#888").pack(side="left", padx=4)
        self.lb_bt = tk.Listbox(f3, height=5)
        self.lb_bt.pack(fill="both", expand=True, padx=4, pady=2)
        # 双击扫描结果 = 配对(与扫描完成提示语一致, 之前只提示没绑定)
        self.lb_bt.bind("<Double-Button-1>", lambda _e: self._bond_sel())

        # ---- 事件流 ----
        f4 = ttk.LabelFrame(body, text="事件流 (结构化 /events: 🚗=车机回流 ⌨=指令; CAR_*/BT_*/... 可断言)")
        f4.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.txt = scrolledtext.ScrolledText(f4, width=56, state="disabled",
                                             font=("Consolas", 9),
                                             background="#111", foreground="#ddd")
        self.txt.pack(fill="both", expand=True, padx=4, pady=4)
        self.txt.tag_configure("car", foreground="#ffd166")
        self.txt.tag_configure("bt", foreground="#7ec8ff")
        self.txt.tag_configure("cmd", foreground="#9f9")

    # ---------- 连接 ----------
    def _connect(self, serial=None):
        serial = serial or self.e_serial.get().strip() or None

        def task():
            try:
                vp = VPhone(serial=serial, autostart=True)
                vp.on_event = self._logcat_line
                vp.start_events()
                self.q.put(("ready", vp))
            except Exception as e:
                self.q.put(("connfail", f"{e}"))
        self.pool.submit(task)

    def _install_apk(self):
        """装 vphone/apk/vphone.apk(仓库自带) → 拉服务 → 联系人为0时询问重灌。
        换机器调试: 连上 USB 点这一个按钮即可完成部署。"""
        serial = self.e_serial.get().strip() or None

        def task():
            try:
                vp = self.vp or VPhone(serial=serial)
            except Exception as e:
                self.q.put(("log", f"!! 连接手机失败: {e}"))
                return
            try:
                self.q.put(("log", "📦 安装中(约10-30s; 华为若弹安装确认框请在手机上点一下)…"))
                self.q.put(("log", vp.install()))
            except Exception as e:
                self.q.put(("log", f"!! 安装失败: {e}"))
                return
            # 装完重连(重建事件流) + 联系人被清则询问重灌
            self.q.put(("call", lambda: self._connect()))
            try:
                cnt = vp.contacts_count()
                if re.search(r"vphone=0\b", cnt):
                    def ask():
                        import tkinter.messagebox as mb
                        if mb.askyesno("联系人", "重装后联系人为 0, 现在重灌 1 万个吗?\n(约2.5分钟, 后台写入)"):
                            self._contacts_load()
                    self.q.put(("call", ask))
            except Exception:
                pass
        self.pool.submit(task)

    def _launch_app(self):
        """拉起 App + 常驻服务(装完处于 stopped 态时广播唤不醒, 必须先 am start)。"""
        if self.vp is None:
            self.log("!! 先点「连接」(或直接点「📦安装APK」)")
            return
        self._run(self.vp.launch)

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
        # 控件取值一律在主线程(提交前)完成, 闭包只带值 —— 后台线程不碰 tkinter
        title, artist, album = (self.e_title.get(), self.e_artist.get(),
                                self.e_album.get())
        try:
            dur = int(self.e_dur.get() or 240)
        except ValueError:
            self.log("!! 时长须为整数(秒)")
            return
        self._run(lambda: self.vp.set_track(title=title, artist=artist, album=album, dur=dur))

    def _incoming(self):
        num = self.e_num.get()
        self._run(lambda: self.vp.incoming(number=num))

    def _dial(self):
        num = self.e_num.get()
        self._run(lambda: self.vp.dial(number=num))

    def _set_silence(self):
        on = self.var_silence.get()
        self._run(lambda: self.vp.silence(on=on))

    def _set_auto_outgoing(self):
        on = self.var_autoout.get()
        self._run(lambda: self.vp.set_auto_outgoing(on=on))

    def _pick_audio(self):
        """电脑上选音频文件 → 上传手机乐库 → 生成播放列表 → 播放(车机真实出声)。"""
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(
            title="选择音频文件(可多选)", filetypes=[("音频", " ".join(AUDIO_EXTS)), ("所有文件", "*.*")])
        if not paths:
            return

        def task():
            try:
                self.q.put(("log", f"推送 {len(paths)} 个文件并播放…"))
                self.q.put(("log", self.vp.play_audio_files(paths=list(paths))))
            except Exception as e:
                self.q.put(("log", f"[错误] {e}"))
        self.pool.submit(task)

    def _playlist_mgr(self):
        """播放列表管理: 当前列表(上移/下移/移除/双击跳播) + 乐库(加入/删除/上传)。
        修改后「✅应用」整表回写, 并保持当前播放曲目不跳变。"""
        tk, ttk = self.tk, self.ttk
        if self.vp is None:
            self.log("!! 未连接")
            return
        win = tk.Toplevel(self.root)
        win.title("播放列表管理")
        win.geometry("680x600")
        pl_items: list[dict] = []

        ttk.Label(win, text="当前播放列表 (双击 = 跳到这首并播放; ▶ = 正在播):").pack(
            anchor="w", padx=10, pady=(8, 0))
        pl = tk.Listbox(win, height=8)
        pl.pack(fill="both", expand=True, padx=10, pady=2)

        def cur_idx():
            try:
                m = re.search(r"idx=(\d+)", self.vp.media_status())
                return int(m.group(1)) if m else 0
            except Exception:
                return 0

        def render(cur=-1):
            pl.delete(0, "end")
            for i, it in enumerate(pl_items):
                mark = "▶ " if i == cur else "   "
                f = f"  📄{it.get('path')}" if it.get("path") else ""
                pl.insert("end", f"{mark}{i + 1}. {it['title']} | {it['artist']}{f}")

        def refresh():
            def task():
                try:
                    items = self.vp.playlist_get()
                    cur = cur_idx()

                    def apply():
                        pl_items[:] = items
                        render(cur)
                    self.q.put(("call", apply))
                except Exception as e:
                    self.q.put(("call", lambda: pl.insert("end", f"!! {e}")))
            self.pool.submit(task)

        def push():
            """整表回写手机, 并跳回当前曲目(回写会重置到第1首)。
            keep(当前曲目号)的查询也放后台 —— 按钮回调里做 HTTP 会冻结整个 GUI。"""
            lines = ["{}|{}|{}|{}|{}".format(
                it["title"], it["artist"], it["album"], it["dur"], it.get("path") or "")
                for it in pl_items]

            def task():
                try:
                    keep = min(cur_idx(), max(len(pl_items) - 1, 0))
                    self.q.put(("log", self.vp.playlist(text="\n".join(lines))))
                    if pl_items:
                        self.q.put(("log", self.vp.media_jump(idx=keep)))
                    self.q.put(("call", refresh))
                except Exception as e:
                    self.q.put(("log", f"[错误] {e}"))
            self.pool.submit(task)

        def sel():
            s = pl.curselection()
            return s[0] if s else None

        def move(d):
            i = sel()
            if i is None:
                return
            j = i + d
            if 0 <= j < len(pl_items):
                pl_items[i], pl_items[j] = pl_items[j], pl_items[i]
                render()
                pl.selection_set(j)

        def remove():
            i = sel()
            if i is not None:
                del pl_items[i]
                render()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=2)
        ttk.Button(bar, text="⬆上移", command=lambda: move(-1)).pack(side="left")
        ttk.Button(bar, text="⬇下移", command=lambda: move(1)).pack(side="left")
        ttk.Button(bar, text="➖移除", command=remove).pack(side="left", padx=4)
        ttk.Button(bar, text="✅应用修改", command=push).pack(side="left", padx=4)
        ttk.Button(bar, text="↻重查手机", command=refresh).pack(side="right")

        def on_dbl(_ev):
            i = sel()
            if i is not None:
                self._run(lambda: "\n".join([self.vp.media_jump(idx=i), self.vp.play()]))
        pl.bind("<Double-Button-1>", on_dbl)

        ttk.Label(win, text="乐库 (上传过的音频文件; 选中→加入播放列表):").pack(
            anchor="w", padx=10, pady=(6, 0))
        lib = tk.Listbox(win, height=6)
        lib.pack(fill="both", expand=True, padx=10, pady=2)

        def refresh_lib():
            def task():
                try:
                    files = [f"{f['name']}  ({f['kb']}KB)" for f in self.vp.list_audio()]
                    self.q.put(("call", lambda: (lib.delete(0, "end"),
                                                 [lib.insert("end", x) for x in files])))
                except Exception as e:
                    self.q.put(("call", lambda: lib.insert("end", f"!! {e}")))
            self.pool.submit(task)

        def lib_sel():
            s = lib.curselection()
            return lib.get(s[0]).split("(")[0].strip() if s else None

        def add_sel():
            n = lib_sel()
            if not n:
                return
            pl_items.append({"title": os.path.splitext(n)[0], "artist": "",
                             "album": "vphone乐库", "dur": 0, "path": n})
            push()          # 加入即应用, 并保持当前曲目

        def del_sel():
            n = lib_sel()
            if n:
                self._run(lambda: self.vp.del_audio(name=n))
                self.q.put(("call", refresh_lib))

        def upload():
            from tkinter import filedialog
            paths = filedialog.askopenfilenames(
                title="选择音频文件(可多选)",
                filetypes=[("音频", " ".join(AUDIO_EXTS)), ("所有文件", "*.*")])
            if not paths:
                return

            def task():
                try:
                    for p in paths:
                        self.q.put(("log", self.vp.upload_audio(path=p)))
                        pl_items.append({"title": os.path.splitext(os.path.basename(p))[0],
                                         "artist": "", "album": "vphone乐库", "dur": 0,
                                         "path": os.path.basename(p)})
                    self.q.put(("call", refresh_lib))
                    push()
                except Exception as e:
                    self.q.put(("log", f"[错误] {e}"))
            self.pool.submit(task)

        bar2 = ttk.Frame(win)
        bar2.pack(fill="x", padx=10, pady=(2, 8))
        ttk.Button(bar2, text="＋加入列表", command=add_sel).pack(side="left")
        ttk.Button(bar2, text="🗑删除文件", command=del_sel).pack(side="left", padx=4)
        ttk.Button(bar2, text="⇧上传电脑音频并加入", command=upload).pack(side="left", padx=4)
        ttk.Button(bar2, text="↻刷新", command=refresh_lib).pack(side="right")

        refresh()
        refresh_lib()

    def _call_audio(self):
        name = self.cb_caudio.get().strip()
        loop = self.var_caloop.get()
        if not name:
            self.log("!! 先在下拉框选一个音频(空则点「↻刷新」)")
            return
        self._run(lambda: self.vp.call_audio(name=name, loop=loop))

    def _refresh_caudio(self):
        """刷新通话音频下拉框(取手机乐库文件名)。"""
        def task():
            try:
                names = [f["name"] for f in self.vp.list_audio()]
                def apply():
                    self.cb_caudio.configure(values=names)
                    if names and self.cb_caudio.get() not in names:
                        self.cb_caudio.set(names[0])
                self.q.put(("call", apply))
                if not names:
                    self.q.put(("log", "乐库为空: 先「⇧电脑选曲」或播放列表管理里上传"))
            except Exception as e:
                self.q.put(("log", f"[错误] {e}"))
        self.pool.submit(task)

    def _bt_rename(self):
        n = self.e_btname.get().strip()
        if not n:
            self.log("!! 先填新蓝牙名")
            return
        self._run(lambda: self.vp.bt_name(name=n))

    def _pos_release(self, _e=None):
        self._pos_drag = False
        v = int(self.scale_pos.get())
        self._run(lambda: self.vp.media_seek(sec=v))

    def _stat_poll(self, gen):
        """后台线程: 1s 刷新"当前曲目/时间"行 —— 车机切歌后 GUI 立即跟上。
        代际不符(用户重新点了「连接」)或连续失败(USB 断连)即自停。"""
        fail = 0
        while not self._evt_stop.is_set() and self._gen == gen:
            try:
                s = self.vp.media_status()
                m = re.search(
                    r'media=(\w+) track="([^"]*)"/(\S*) pos=(\d+)s dur=(\d+)s '
                    r'playlist=(\d+)首 idx=(\d+)', s)
                if m:
                    st, title, artist, pos, dur, n, idx = m.groups()
                    mark = "▶" if st == "playing" else "⏸"
                    txt = f"当前: {mark} 「{title}」/{artist}  {pos}s/{dur}s  第{int(idx) + 1}/{n}首"
                    real = re.search(r"real=(\S+)", s)
                    if real and real.group(1) != "none":
                        txt += f"  📄{real.group(1)}"
                    self.q.put(("media", txt))
                    self.q.put(("pos", (int(pos), int(dur))))
                # 同窗口顺带拉通话状态行(多路: calls=[active:X,held:Y])
                cs = self.vp.http("/status")
                mc = re.search(r"call=(\w+) number=(\S*) calls=(\[[^\]]*\])", cs)
                if mc:
                    cst, cnum, ccalls = mc.groups()
                    ctxt = "通话: " + cst + (f" {cnum}" if cnum else "")
                    if ccalls and ccalls != "[]":
                        ctxt += f"  {ccalls}"
                    self.q.put(("callst", ctxt))
                fail = 0
            except Exception:
                fail += 1
                if fail >= 3:
                    self.q.put(("log", "!! 状态轮询连续 3 次失败(USB断连?服务停了?) —— 已自停, 重新「连接」恢复"))
                    return
            self._evt_stop.wait(1.0)

    def _contacts_load(self):
        try:
            n = int(self.e_cnt.get() or "0")
        except ValueError:
            self.log("!! 数量须为整数")
            return

        def task():
            try:
                self.q.put(("log", f"批量写入 {n} 个联系人(1w 约几十秒)…"))
                self.q.put(("log", self.vp.contacts_load(count=n)))
                self.q.put(("log", self.vp.contacts_count()))
            except Exception as e:
                self.q.put(("log", f"[错误] {e}"))
        self.pool.submit(task)

    def _contacts_file(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(title="联系人文件(每行: 姓名|号码 或 姓名,号码)",
                                          filetypes=[("文本", "*.txt *.csv"), ("所有文件", "*.*")])
        if not path:
            return
        self._run(lambda: self.vp.contacts_import(file=path))

    def _contacts_clear(self):
        self._run(lambda: self.vp.contacts_clear())

    def _contacts_refresh(self):
        def task():
            try:
                self.q.put(("contacts", self.vp.contacts_count()))
            except Exception as e:
                self.q.put(("log", f"[错误] {e}"))
        self.pool.submit(task)

    def _reconnect_sel(self):
        """列表选中了设备就重连它; 没选就自动重连第一台已配对设备。"""
        sel = self.lb_bt.curselection()
        target = self.bt_devs[sel[0]]["mac"] if sel else None
        self._run(lambda: self.vp.bt_reconnect(target=target))

    def _disconnect_sel(self):
        """断开选中设备(没选=当前已连接那台), 保持配对 —— 车机断连/回连测试用。"""
        sel = self.lb_bt.curselection()
        target = self.bt_devs[sel[0]]["mac"] if sel else None
        self._run(lambda: self.vp.bt_disconnect(mac=target))

    def _allow_car_sel(self):
        """授权选中设备(或第一台已连HFP设备)访问联系人/通话记录(PBAP)。"""
        sel = self.lb_bt.curselection()
        target = self.bt_devs[sel[0]]["mac"] if sel else None
        self._run(lambda: self.vp.bt_allow_car(target=target))

    def _scan(self):
        if self.vp is None:
            self.log("!! 未连接")
            return

        def task():
            try:
                devs = self.vp.bt_scan()
                self.q.put(("devs", devs))
                self.q.put(("log", f"扫描完成: {len(devs)} 台(双击列表项=配对)"))
            except Exception as e:
                self.q.put(("log", f"扫描失败: {e}"))
        self.pool.submit(task)

    def _sel_dev(self):
        sel = self.lb_bt.curselection()
        if not sel:
            self.log("!! 先在列表选中设备")
            return None
        return self.bt_devs[sel[0]]

    def _bond_sel(self):
        d = self._sel_dev()
        if d:
            self._run(lambda: self.vp.bt_bond(target=d["mac"]))

    def _unpair_sel(self):
        d = self._sel_dev()
        if d:
            self._run(lambda: self.vp.bt_unpair(target=d["mac"]))

    # ---------- 事件轮询(结构化 /events) ----------
    def _evt_poll(self, gen):
        """后台线程: /events 轮询 → 事件流窗口。车机回流(src=car)高亮。
        首拉只对齐水位(只显示"今后"的事件); 代际不符或连续失败即自停。"""
        last = 0
        first = True
        fail = 0
        while not self._evt_stop.is_set() and self._gen == gen:
            try:
                r = self.vp.events_raw(since=last)
                last = r["last"]
                fail = 0
                if first:   # 首拉只对齐水位
                    first = False
                else:
                    for d in r["events"]:
                        self.q.put(("tevt", VEvent._of(d)))
            except Exception:
                fail += 1
                if fail >= 3:
                    self.q.put(("log", "!! 事件轮询连续 3 次失败(USB断连?) —— 已自停, 重新「连接」恢复"))
                    return
            self._evt_stop.wait(0.5)

    def _logcat_line(self, line):
        """logcat 兜底通道: 结构化事件已由 /events 呈现, 过滤掉重复的 [事件# 行。"""
        if "[事件#" in line:
            return
        self.q.put(("evt", line))

    # ---------- 主线程泵 ----------
    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "evt":
                    tag = "bt" if "[蓝牙]" in payload else None
                    self.log(payload, tag=tag)
                elif kind == "tevt":
                    e = payload   # VEvent(结构化, 字段固定)
                    mark = "🚗" if e.src == "car" else ("⌨" if e.src == "cmd" else " ")
                    tag = "car" if e.src == "car" else ("bt" if e.type.startswith("BT_") else "cmd")
                    self.log(f"{mark} {e}", tag=tag)
                elif kind == "callst":
                    self.lbl_call.config(text=payload)
                elif kind == "ready":
                    old, self.vp = self.vp, payload
                    if old is not None:
                        try:
                            old.stop_events()   # 停掉旧连接的 logcat 线程, 防重复行
                        except Exception:
                            pass
                    self.e_serial.delete(0, "end")
                    self.e_serial.insert(0, self.vp.serial or "")
                    self.lbl_conn.config(text=f"已连接 {self.vp.serial or '(单设备)'}",
                                         foreground="#0a7")
                    self.log("=== 已连接, 服务已拉起 ===")
                    self._run(self.vp.bt_state)
                    # 一次性环境加固(幂等): 授权/配对自动确认/车机拨出走 VPhone 账号
                    self._run(self.vp.grant_perms)
                    self._run(self.vp.enable_autoconfirm)
                    # 车机拨出接通模式以勾选框为准(主线程先取值, 默认手动接通)
                    auto_on = self.var_autoout.get()
                    self._run(lambda: self.vp.set_auto_outgoing(on=auto_on))
                    self._contacts_refresh()
                    self._refresh_caudio()
                    # 代际+1: 旧的 _evt_poll/_stat_poll 线程检测到后代不符即自退,
                    # 不会叠加(重复「连接」曾经会叠出双份事件/双份进度刷新)
                    self._gen += 1
                    gen = self._gen
                    threading.Thread(target=self._evt_poll, args=(gen,), daemon=True).start()
                    threading.Thread(target=self._stat_poll, args=(gen,), daemon=True).start()
                elif kind == "connfail":
                    self.lbl_conn.config(text="连接失败", foreground="#c22")
                    self.log("!! 连接失败: " + payload)
                elif kind == "devs":
                    self.bt_devs = payload
                    self.lb_bt.delete(0, "end")
                    for d in payload:
                        self.lb_bt.insert("end", f"{d['name']}  {d['mac']}  {d['rssi']}dBm")
                elif kind == "contacts":
                    self.lbl_contacts.config(text=f"计数: {payload}")
                elif kind == "media":
                    self.lbl_media.config(text=payload)
                elif kind == "pos":
                    pos, dur = payload
                    if not self._pos_drag:
                        if int(self.scale_pos.cget("to")) != dur:
                            self.scale_pos.config(to=dur)
                        if abs(self.scale_pos.get() - pos) >= 1:
                            self.scale_pos.set(pos)
                elif kind == "call":
                    payload()
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
            self._evt_stop.set()
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
