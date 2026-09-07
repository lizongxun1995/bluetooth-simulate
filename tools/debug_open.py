"""复现 transport.open 失败,抓 _handle_broken 的真实原因。"""
import sys
import time

sys.path.insert(0, ".")
from btphone.transport import SerialTransport  # noqa: E402

tr = SerialTransport("COM7")
orig_broken = tr._handle_broken
t0 = time.time()


def spy(reason: str) -> None:
    print(f"[{time.time()-t0:6.2f}s] BROKEN: {reason}", flush=True)
    orig_broken(reason)


tr._handle_broken = spy

orig_close = tr.close
def spy_close():
    print(f"[{time.time()-t0:6.2f}s] close()", flush=True)
    orig_close()


tr.close = spy_close
orig_start = tr._start_session
def spy_start():
    print(f"[{time.time()-t0:6.2f}s] _start_session()", flush=True)
    orig_start()


tr._start_session = spy_start

try:
    tr.open(reset=True)
    print("OPEN OK")
except Exception as exc:
    print(f"OPEN FAIL: {exc}")
