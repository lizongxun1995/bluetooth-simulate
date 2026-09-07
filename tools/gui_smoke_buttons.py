"""GUI 按钮接线冒烟:真实点击(invoke)"设置蓝牙名",验证动作经线程池真正到达设备。
曾经的事故:command=lambda: self._kick(...) 中 _kick 返回的闭包被丢弃,点击静默无效。
用法: python tools/gui_smoke_buttons.py   (需要显示器;不进 pytest)
"""
import time

import tkinter as tk

from btphone.gui import PhoneGui


def wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def main() -> None:
    root = tk.Tk()
    root.withdraw()
    gui = PhoneGui(root)        # 不自动连接
    gui._do_connect(sim=True)   # 同步连上模拟器
    assert gui.phone is not None

    # 点击"设置"按钮:改名必须真正生效
    gui.var_name.set("VP-GUI-TEST")
    gui.btn_set_name.invoke()
    assert wait_until(lambda: gui.sim.name == "VP-GUI-TEST"), \
        f"改名按钮点击无效,sim.name={gui.sim.name!r}"
    print("OK: 设置蓝牙名 →", gui.sim.name)

    # 配对模式下拉走同一条 _run 路径
    gui._run(gui._set_pair_mode)
    assert wait_until(lambda: gui.sim.pair_mode == gui.cmb_pair.get()), "配对模式切换无效"
    print("OK: 配对模式 →", gui.sim.pair_mode)

    gui.shutdown()
    root.destroy()
    print("GUI 按钮接线冒烟通过")


if __name__ == "__main__":
    main()
