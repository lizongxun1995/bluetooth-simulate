# -*- coding: utf-8 -*-
"""BTWinRT 蓝牙模拟 GUI —— Windows 原生(WinRT)引擎 btwinrt.dll 的 tkinter 前端。

运行:  python pydemo/gui.py            (需已 pip install pythonnet, DLL 在 pydemo/lib/)
自检:  python pydemo/gui.py --selftest  (不开窗口, 跑一遍引擎核心调用)

能力对应实测判定门:
  媒体元数据(门B): 推送假歌名/歌手 + 播放状态 —— 车机歌词是云端按歌名匹配,
                    标题填真实歌名(如 青花瓷/周杰伦)可带出真歌词。
  静音流(门B补充): 向车机音频端点渲染空帧, 激活 A2DP 让元数据/状态真正上屏。
  来电(门C): CallControl 动态 GetDefault + IndicateNewIncomingCall。
  配对: 32feet.NET 扫描/配对 —— CARKIT-1 实测不认脚本配对, 首次须 Windows 设置手动配。
"""
import os
import queue
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "pydemo", "lib")
os.environ.setdefault("DOTNET_ROOT", r"C:\Program Files\dotnet")

# ---------- 加载 .NET 引擎 (pythonnet + coreclr) ----------
from pythonnet import load
load("coreclr")
import clr  # noqa: E402
from System.Reflection import Assembly  # noqa: E402

# 整目录加载: LoadFrom 上下文会探测同目录依赖, 32feet 的老包依赖(ConfigurationManager 等)也一并就位
for _f in sorted(os.listdir(LIB)):
    if _f.lower().endswith(".dll"):
        Assembly.LoadFrom(os.path.join(LIB, _f))
from BtWinRT import Engine  # noqa: E402


# ---------------------------------------------------------------- 自检模式
def selftest():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("[selftest] Engine 构造...")
    eng = Engine()
    eng.OnLog += lambda m: print("[log]", m)
    eng.OnButton += lambda b: print("[button]", b)
    print("[selftest] Init:", eng.Init())
    eng.SetTrack("青花瓷", "周杰伦", "测试专辑", 240)
    eng.SetStatus("playing")
    print("[selftest] sessions:")
    for s in eng.ListSessions():
        print("   ", s)
    print("[selftest] endpoints:")
    for e in eng.ListAudioEndpoints():
        print("   ", e)
    print("[selftest] call:", eng.CallIncoming("13800138000"))
    eng.Dispose()
    print("[selftest] 完成")


if "--selftest" in sys.argv:
    selftest()
    sys.exit(0)


# ---------------------------------------------------------------- GUI
import tkinter as tk  # noqa: E402
from tkinter import scrolledtext, ttk  # noqa: E402

MAX_LOG_LINES = 1500


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("BTWinRT 蓝牙模拟 (Windows 原生引擎)")
        root.geometry("900x680")
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.eng: Engine | None = None
        self.device_macs: list[str] = []

        self._build()
        root.after(100, self._drain)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.log("引擎启动中(加载 .NET 运行时)...")
        self.pool.submit(self._start_engine)

    # ---------- 构建界面 ----------
    def _build(self):
        pad = {"padx": 6, "pady": 3}

        # 引擎状态
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="引擎:").pack(side="left")
        self.lbl_eng = ttk.Label(top, text="启动中...", foreground="#888")
        self.lbl_eng.pack(side="left", padx=6)
        ttk.Label(top, text="提示: 车机歌词=云端按歌名匹配, 标题用真歌名可显示真歌词",
                  foreground="#0a7").pack(side="right")

        # 媒体元数据
        f1 = ttk.LabelFrame(self.root, text="媒体元数据 (SMTC → A2DP/AVRCP)")
        f1.pack(fill="x", **pad)
        row1 = ttk.Frame(f1); row1.pack(fill="x", padx=4, pady=2)
        for label, attr, default, w in (("标题", "e_title", "青花瓷", 24),
                                        ("歌手", "e_artist", "周杰伦", 14),
                                        ("专辑", "e_album", "测试专辑", 14),
                                        ("时长s", "e_len", "240", 5)):
            ttk.Label(row1, text=label).pack(side="left")
            e = ttk.Entry(row1, width=w); e.insert(0, default); e.pack(side="left", padx=(2, 10))
            setattr(self, attr, e)
        ttk.Button(row1, text="推送元数据",
                   command=lambda: self._run(lambda: self.eng.SetTrack(
                       self.e_title.get(), self.e_artist.get(), self.e_album.get(), self._len_sec()))
                   ).pack(side="left")
        row2 = ttk.Frame(f1); row2.pack(fill="x", padx=4, pady=2)
        for text, fn in (("▶ 播放", lambda: self.eng.SetStatus("playing")),
                         ("⏸ 暂停", lambda: self.eng.SetStatus("paused")),
                         ("⏹ 停止", lambda: self.eng.SetStatus("stopped")),
                         ("下一曲", lambda: self.eng.Next()),
                         ("上一曲", lambda: self.eng.Previous())):
            ttk.Button(row2, text=text, command=lambda f=fn: self._run(f)).pack(side="left", padx=2)
        ttk.Button(row2, text="系统会话自检(sessions)",
                   command=lambda: self._run(lambda: "\n".join(self.eng.ListSessions()))
                   ).pack(side="left", padx=10)

        # 静音流
        f2 = ttk.LabelFrame(self.root, text="静音流 (向车机端点渲染空帧 → 激活 A2DP, 元数据才会上屏)")
        f2.pack(fill="x", **pad)
        row3 = ttk.Frame(f2); row3.pack(fill="x", padx=4, pady=2)
        ttk.Label(row3, text="端点:").pack(side="left")
        self.cb_ep = ttk.Combobox(row3, width=36, state="readonly")
        self.cb_ep.pack(side="left", padx=4)
        ttk.Button(row3, text="刷新端点", command=lambda: self._run(self._refresh_endpoints)).pack(side="left", padx=2)
        ttk.Button(row3, text="▶ 开始静音流+联动播放", command=self._silence_on).pack(side="left", padx=2)
        ttk.Button(row3, text="■ 停止静音流",
                   command=lambda: self._run(lambda: self.eng.StopSilence())).pack(side="left", padx=2)

        # 来电
        f3 = ttk.LabelFrame(self.root, text="来电模拟 (CallControl, 每次动态 GetDefault)")
        f3.pack(fill="x", **pad)
        row4 = ttk.Frame(f3); row4.pack(fill="x", padx=4, pady=2)
        ttk.Label(row4, text="号码:").pack(side="left")
        self.e_num = ttk.Entry(row4, width=16); self.e_num.insert(0, "13800138000"); self.e_num.pack(side="left", padx=4)
        ttk.Button(row4, text="📞 模拟来电",
                   command=lambda: self._run(lambda: self.eng.CallIncoming(self.e_num.get()))
                   ).pack(side="left", padx=2)
        ttk.Button(row4, text="✔ 接听(标记激活)", command=lambda: self._run(lambda: self.eng.Answer())).pack(side="left", padx=2)
        ttk.Button(row4, text="挂断", command=lambda: self._run(lambda: self.eng.HangUp())).pack(side="left", padx=2)

        # 扫描/配对/HFP
        f4 = ttk.LabelFrame(self.root, text="蓝牙射频/扫描/配对/通话音频 (配对自动确认不弹窗; CARKIT-1=382A8BC316B1)")
        f4.pack(fill="both", expand=False, **pad)
        row5 = ttk.Frame(f4); row5.pack(fill="x", padx=4, pady=2)
        ttk.Button(row5, text="射频状态", command=lambda: self._run(lambda: self.eng.RadioList())).pack(side="left")
        ttk.Button(row5, text="🔵蓝牙开", command=lambda: self._run(lambda: self.eng.BtSetPower(True))).pack(side="left", padx=2)
        ttk.Button(row5, text="⚪蓝牙关", command=lambda: self._run(lambda: self.eng.BtSetPower(False))).pack(side="left", padx=2)
        ttk.Button(row5, text="扫描 (10~20s)", command=self._scan).pack(side="left", padx=10)
        ttk.Button(row5, text="配对选中", command=self._pair_selected).pack(side="left", padx=2)
        ttk.Button(row5, text="🔁重新配对(清残留)", command=self._repair_selected).pack(side="left", padx=2)
        ttk.Button(row5, text="解除配对选中", command=self._unpair_selected).pack(side="left", padx=2)
        ttk.Button(row5, text="🔗连通话音频(HFP实验)", command=self._hfp_connect).pack(side="left", padx=2)
        self.lb_dev = tk.Listbox(f4, height=6)
        self.lb_dev.pack(fill="both", expand=True, padx=4, pady=2)

        # 日志
        f5 = ttk.LabelFrame(self.root, text="日志 (全部动作/事件, 同步写 btwinrt.log)")
        f5.pack(fill="both", expand=True, **pad)
        self.txt = scrolledtext.ScrolledText(f5, height=10, state="disabled",
                                             font=("Consolas", 9), background="#111", foreground="#ddd")
        self.txt.pack(fill="both", expand=True, padx=4, pady=2)

    # ---------- 引擎线程 ----------
    def _start_engine(self):
        try:
            eng = Engine()
            eng.OnLog += lambda m: self.q.put(("log", str(m)))
            eng.OnButton += lambda b: self.q.put(("btn", str(b)))
            eng.OnScanDone += lambda s: self.q.put(("scan", str(s)))
            msg = str(eng.Init())
            self.eng = eng
            self.q.put(("ready", msg))
        except Exception:
            self.q.put(("fatal", traceback.format_exc()))

    def _run(self, fn):
        """阻塞调用丢工作线程; tkinter 变量已在主线程 lambda 里取完。"""
        if self.eng is None:
            self.log("!! 引擎未就绪")
            return

        def task():
            try:
                r = fn()
                if r is not None:
                    self.q.put(("log", str(r)))
            except Exception:
                self.q.put(("log", "[错误] " + traceback.format_exc(limit=3).replace("\n", " | ")))
        self.pool.submit(task)

    def _refresh_endpoints(self):
        eps = list(self.eng.ListAudioEndpoints())
        self.q.put(("endpoints", eps))
        return f"端点 {len(eps)} 个"

    def _silence_on(self):
        ep = self.cb_ep.get()
        if not ep:
            self.log("!! 先选端点(刷新端点; 车机=耳机/远程音频 类)")
            return
        title, artist, album, ln = self.e_title.get(), self.e_artist.get(), self.e_album.get(), self._len_sec()

        def task():
            msg = self.eng.StartSilence(ep)
            self.eng.SetStatus("playing")
            self.eng.SetTrack(title, artist, album, ln)
            return msg + " + 已联动: Playing + 重推元数据(含时间轴)"
        self._run(task)

    def _len_sec(self) -> int:
        try:
            return max(1, int(self.e_len.get()))
        except ValueError:
            return 240

    def _hfp_connect(self):
        # 优先用扫描列表选中项, 否则默认台架车机 CARKIT-1
        sel = self.lb_dev.curselection()
        mac = self.device_macs[sel[0]] if sel else "382A8BC316B1"
        self._run(lambda: self.eng.HfpConnect(mac))

    def _scan(self):
        if self.eng is None:
            self.log("!! 引擎未就绪")
            return
        self.log("扫描中, 约 10~20 秒...")
        self.eng.ScanAsync()  # 非阻塞, 结果经 OnScanDone 事件回流

    def _sel_mac(self) -> str | None:
        sel = self.lb_dev.curselection()
        if not sel:
            self.log("!! 先在列表里选中设备")
            return None
        return self.device_macs[sel[0]]

    def _pair_selected(self):
        mac = self._sel_mac()
        if mac:
            self._run(lambda: None if self.eng.Pair(mac, "1234") else None)  # 引擎已写日志

    def _repair_selected(self):
        # 11:16 台架: 车机侧已删配对而 Windows 残留链路密钥 -> PairAsync 回 AlreadyPaired 短路,
        # 配对流程根本没跑。force=True 先清 Windows 记录再全新无头配对。
        mac = self._sel_mac()
        if mac:
            self.log(f"重新配对 {mac}: 先清 Windows 残留记录再全新配对 (车机侧若也挂着旧记录, 先在车机屏幕删除)")
            self._run(lambda: None if self.eng.Pair(mac, "1234", True) else None)

    def _unpair_selected(self):
        mac = self._sel_mac()
        if mac:
            self._run(lambda: None if self.eng.Unpair(mac) else None)  # 引擎已写日志

    # ---------- 主线程泵 ----------
    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "btn":
                    self.log(f">> 车机按键: {payload} <<", tag="btn")
                elif kind == "ready":
                    self.lbl_eng.config(text=payload, foreground="#0a7")
                    self.log("=== 引擎就绪 ===")
                    self._run(self._refresh_endpoints)
                elif kind == "fatal":
                    self.lbl_eng.config(text="启动失败", foreground="#c22")
                    self.log("[致命] " + payload)
                elif kind == "scan":
                    self.device_macs = []
                    self.lb_dev.delete(0, "end")
                    for line in payload.splitlines():
                        parts = [p.strip() for p in line.split("|")]
                        if len(parts) >= 3:
                            self.device_macs.append(parts[2])
                            self.lb_dev.insert("end", line)
                    self.log(f"扫描完成: {len(self.device_macs)} 台 (配对用 MAC 更稳)")
                elif kind == "endpoints":
                    self.cb_ep["values"] = payload
                    if payload:
                        self.cb_ep.set(payload[0])
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def log(self, msg: str, tag: str = ""):
        import datetime
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.txt.config(state="normal")
        self.txt.insert("end", f"[{stamp}] {msg}\n", tag or None)
        if tag:
            self.txt.tag_configure("btn", foreground="#ffd166")
        lines = int(self.txt.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.txt.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.txt.see("end")
        self.txt.config(state="disabled")

    def _on_close(self):
        try:
            if self.eng is not None:
                self.pool.submit(lambda: self.eng.Dispose())
            self.pool.shutdown(wait=False)
        finally:
            self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
