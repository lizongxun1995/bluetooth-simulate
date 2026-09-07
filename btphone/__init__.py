"""btphone:蓝牙车机模拟测试库。

用 ESP32(原版双模)充当脚本全控的虚拟手机::

    from btphone import BtPhone
    phone = BtPhone("COM3")
    phone.music.play(files=[...])

无硬件时可用模拟器开发调试::

    from btphone import BtPhone, SimPhone
    sim = SimPhone(speed=100)
    phone = BtPhone("SIM", io=sim.device_io)
"""

from .client import BtPhone
from .events import EventBus
from .exceptions import (
    AudioFormatError,
    BtPhoneError,
    CommandError,
    TransportError,
    WaitTimeout,
)
from .simulator import SimPhone

__version__ = "0.1.0"

__all__ = [
    "BtPhone",
    "SimPhone",
    "EventBus",
    "BtPhoneError",
    "CommandError",
    "TransportError",
    "WaitTimeout",
    "AudioFormatError",
    "__version__",
]
