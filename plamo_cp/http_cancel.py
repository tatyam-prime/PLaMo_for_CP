"""専用の plain-HTTP socket に対する、有界な読み取りとキャンセル。

別スレッドの shutdown が、タイムアウト付き recv の待機を起こすとは限らない。
短い待機の再試行は SocketIO より下で行い、一時的な TimeoutError によって
makefile() のバッファーが以後読み取れなくなることを避ける。
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from contextlib import suppress

READ_POLL_INTERVAL = 0.05


class CancellableSocket(socket.socket):
    """makefile() より前に所有権を移し、通常の socket/file の寿命管理を保つ。"""

    def __init__(self, stream: socket.socket, cancelled: Callable[[], bool]):
        timeout = stream.gettimeout()
        super().__init__(stream.family, stream.type, stream.proto, fileno=stream.detach())
        self.settimeout(timeout)
        self._read_cancelled = cancelled

    def recv_into(self, buffer, nbytes=0, flags=0):
        timeout = self.gettimeout()
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                if self._read_cancelled():
                    raise OSError("HTTP read cancelled")
                if timeout == 0:
                    return super().recv_into(buffer, nbytes, flags)
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("timed out")
                self.settimeout(
                    READ_POLL_INTERVAL if remaining is None else min(READ_POLL_INTERVAL, remaining)
                )
                try:
                    return super().recv_into(buffer, nbytes, flags)
                except TimeoutError:
                    # 一時的な期限切れを SocketIO へ漏らさず、元の読取期限まで再試行する。
                    pass
        finally:
            with suppress(OSError):
                self.settimeout(timeout)
