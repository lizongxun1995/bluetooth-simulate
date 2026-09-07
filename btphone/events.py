"""事件总线:订阅固件异步事件,支持回调与 wait_event 断言式等待。

事件带全局递增序号并保留历史缓冲;wait_event 支持 after 位置参数,
用于可靠等待"刚执行的命令"期间发出的事件(避免先发后等竞态)。
"""

from __future__ import annotations

import collections
import fnmatch
import itertools
import threading
import time
from typing import Callable, Optional

from .exceptions import WaitTimeout

EventName = str  # 支持 "bt.a2dp.state" 精确名与 "hfp.*" 通配


class EventBus:
    def __init__(self, history: int = 512) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[str, Callable[[str, dict], None]]] = []
        self._waiters: list[_Waiter] = []
        self._seq = itertools.count(1)
        self._history: collections.deque = collections.deque(maxlen=history)

    def position(self) -> int:
        """当前事件流位置(配合 wait_event 的 after 参数使用)。"""
        with self._lock:
            return next(self._seq) - 1

    def on(self, pattern: EventName, callback: Callable[[str, dict], None]) -> Callable[..., None]:
        """订阅事件(支持 fnmatch 通配)。返回取消函数。"""
        entry = (pattern, callback)

        def _cancel() -> None:
            with self._lock:
                if entry in self._subscribers:
                    self._subscribers.remove(entry)

        with self._lock:
            self._subscribers.append(entry)
        return _cancel

    def wait_event(
        self,
        pattern: EventName,
        timeout: Optional[float] = 10.0,
        predicate: Optional[Callable[[dict], bool]] = None,
        after: int = -1,
    ) -> dict:
        """阻塞等待匹配事件,返回 data(不影响读取游标)。

        after: 只匹配序号大于该位置的事件(-1=历史缓冲全部可匹配)。
        超时抛 WaitTimeout。
        """
        data, _ = self.wait_event_ext(pattern, timeout=timeout, predicate=predicate, after=after)
        return data

    def wait_event_ext(
        self,
        pattern: EventName,
        timeout: Optional[float] = 10.0,
        predicate: Optional[Callable[[dict], bool]] = None,
        after: int = -1,
    ) -> tuple[dict, int]:
        """同 wait_event,但返回 (data, 事件序号),供调用方推进读取游标。"""
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._lock:
            for _seq, name, data in self._history:
                if _seq <= after:
                    continue
                if fnmatch.fnmatchcase(name, pattern) and self._predicate_ok(predicate, data):
                    return data, _seq
        waiter = _Waiter(pattern, predicate)
        with self._lock:
            self._waiters.append(waiter)
        try:
            while True:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise WaitTimeout(f"等待事件 {pattern} 超时({timeout}s)")
                # 等待期间历史可能有新事件,轮询历史 + waiter 事件双通道
                if waiter.done.wait(0.05):
                    if waiter.error is not None:
                        raise waiter.error
                    return waiter.data or {}, waiter.matched_seq
                with self._lock:
                    for _seq, name, data in self._history:
                        if _seq <= after:
                            continue
                        if fnmatch.fnmatchcase(name, pattern) and self._predicate_ok(predicate, data):
                            return data, _seq
        finally:
            with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    @staticmethod
    def _predicate_ok(predicate: Optional[Callable[[dict], bool]], data: dict) -> bool:
        if predicate is None:
            return True
        try:
            return bool(predicate(data))
        except Exception:
            return False

    def emit(self, name: str, data: dict) -> None:
        with self._lock:
            seq = next(self._seq)
            self._history.append((seq, name, data))
            subscribers = list(self._subscribers)
            waiters = list(self._waiters)
        for pattern, callback in subscribers:
            if fnmatch.fnmatchcase(name, pattern):
                try:
                    callback(name, data)
                except Exception:  # 回调异常不影响其他订阅者
                    pass
        for waiter in waiters:
            if waiter.try_match(name, data):
                waiter.matched_seq = seq
                break


class _Waiter:
    def __init__(self, pattern: str, predicate: Optional[Callable[[dict], bool]]) -> None:
        self.pattern = pattern
        self.predicate = predicate
        self.done = threading.Event()
        self.data: Optional[dict] = None
        self.matched_seq = 0
        self.error: Optional[Exception] = None

    def try_match(self, name: str, data: dict) -> bool:
        if self.done.is_set() or not fnmatch.fnmatchcase(name, self.pattern):
            return False
        if not EventBus._predicate_ok(self.predicate, data):
            return False
        self.data = data
        self.done.set()
        return True


class Deadline:
    """简单计时辅助。"""

    def __init__(self, timeout: Optional[float]) -> None:
        self.start = time.monotonic()
        self.timeout = timeout

    def remaining(self) -> Optional[float]:
        if self.timeout is None:
            return None
        return max(0.0, self.timeout - (time.monotonic() - self.start))

    def expired(self) -> bool:
        r = self.remaining()
        return r is not None and r <= 0
