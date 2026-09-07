"""GUI 冒烟测试:驱动真实 Tk 界面(模拟器后端),验证按钮动作与事件联动。

tkinter 有显示环境才跑(本仓库目标环境为 Windows,默认满足)。
"""

import time

import pytest

tk = pytest.importorskip("tkinter")

from btphone.gui import PhoneGui  # noqa: E402


def pump(root, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            root.update()
        except tk.TclError:
            pass  # 上个用例窗口销毁后残留的 after 回调,无害
        time.sleep(0.01)


@pytest.fixture(scope="module")
def tcl_root():
    """整模块共用一个 Tcl 解释器。同进程反复 tk.Tk() 在 Windows 上会随机炸
    (如 fonts.tcl 假性缺失);每个用例改用 Toplevel 隔离界面。"""
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture()
def gui(tcl_root):
    win = tk.Toplevel(tcl_root)
    win.withdraw()  # 不弹窗口,逻辑照跑
    g = PhoneGui(win, sim_on_start=True)
    pump(win, 2.0)  # 等连接完成
    assert g.phone is not None, "模拟器未连接"
    yield g, win
    try:
        g.shutdown()
        win.destroy()
    except tk.TclError:
        pass  # 用例失败引发的销毁竞态不影响错误本身


def log_text(g):
    return g.txt_log.get("1.0", "end")


def test_connect_and_music_flow(gui):
    g, root = gui
    assert "连接成功(模拟器)" in log_text(g)
    assert g.lbl_link.cget("text") == "●已连接"

    g.sim.car_connect()  # v0.3.0:a2dp/hfp 未连接时 music.play 会被拒绝
    pump(root, 0.5)
    g._gen_tones()
    assert g.lst_files.size() == 3
    g._music_play(loop=False)
    pump(root, 1.5)
    assert "播放" in log_text(g)
    assert "当前:" in g.lbl_track.cget("text")
    g._music_pause()
    g._music_resume()
    g._music_next()
    pump(root, 0.5)
    g._music_stop()
    pump(root, 0.5)
    assert "停止" in log_text(g)


def test_pairing_and_car_linkage(gui):
    g, root = gui
    g.sim.car_pair()  # auto 模式:自动接受
    pump(root, 1.0)
    assert "配对结果: 成功" in log_text(g)

    g.sim.car_connect()
    pump(root, 1.0)
    assert "a2dp: connected" in g.txt_conn.get("1.0", "end")
    assert "hfp: connected" in g.txt_conn.get("1.0", "end")

    g.sim.car_press("pause")  # 车机按暂停 → avrcp.cmd 事件
    pump(root, 1.0)
    assert "avrcp.cmd" in log_text(g)


def test_calls_flow(gui):
    g, root = gui
    g.sim.car_connect()  # v0.3.0:hfp 未连接时模拟来电会被拒绝
    pump(root, 0.5)
    g._calls_incoming("13800138000")
    pump(root, 0.8)
    assert "模拟来电" in log_text(g)
    g.sim.car_answer()  # 车机接听 → ATA
    pump(root, 1.0)
    assert "ATA" in log_text(g)
    g.sim.car_hangup()
    pump(root, 1.0)
    assert "CHUP" in log_text(g)


def test_net_stub(gui):
    g, root = gui
    g._net_ap(True, "VPHONE-AP", "12345678")
    pump(root, 1.0)
    assert "热点开启指令已发" in log_text(g)


def test_push_metadata(gui):
    g, root = gui
    g._push_metadata("歌A", "歌手B", "专辑C")
    pump(root, 1.0)
    assert "元数据已推送: 歌A / 歌手B" in log_text(g)


def test_requires_connection(tcl_root):
    """未连接时点按钮只提示,不崩。"""
    win = tk.Toplevel(tcl_root)
    win.withdraw()
    g = PhoneGui(win)
    g._gen_tones()  # 本地操作,不需要连接
    g._run(g._music_play, False)  # 与按钮同路径:未连接 → 日志提示,不崩
    pump(win, 0.8)
    assert "请先连接" in log_text(g)
    g.shutdown()
    win.destroy()
