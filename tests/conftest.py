import threading

import pytest

from btphone import BtPhone, SimPhone


@pytest.fixture()
def phone_sim():
    sim = SimPhone(name="VPHONE-TEST", speed=50)
    phone = BtPhone("SIM", io=sim.device_io)
    # 模拟"车机已连上"(配对完成后 profiles 已拉起)——v0.3.0 起
    # a2dp/hfp 未连接时 music.play / hfp.incoming 会被固件拒绝
    sim.car_connect()
    yield phone, sim
    phone.close()
    sim.close()


@pytest.fixture()
def wav_file(tmp_path):
    from btphone.media import make_tone_wav

    path = str(tmp_path / "tone.wav")
    make_tone_wav(path, duration_s=1.0, freq=440.0)
    return path


def wait_event_thread(phone, pattern, results, timeout=15):
    def run():
        try:
            results.append(phone.wait_event(pattern, timeout=timeout))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
