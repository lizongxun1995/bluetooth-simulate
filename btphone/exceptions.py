"""btphone 异常类型。"""

from __future__ import annotations


class BtPhoneError(Exception):
    """基类。"""


class TransportError(BtPhoneError):
    """串口打开失败/断连/超时。"""


class CommandError(BtPhoneError):
    """固件返回 ok=false。"""

    def __init__(self, cmd: str, error: str) -> None:
        super().__init__(f"命令 {cmd} 失败: {error}")
        self.cmd = cmd
        self.error = error


class WaitTimeout(BtPhoneError):
    """wait_event 超时。"""


class AudioFormatError(BtPhoneError):
    """音频文件无法解码/参数不支持。"""
