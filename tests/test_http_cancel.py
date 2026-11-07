import socket
import threading

import pytest

from plamo_cp.http_cancel import CancellableSocket


def test_buffered_reads_survive_poll_timeouts_and_keep_the_original_idle_deadline():
    receiving, sending = socket.socketpair()
    receiving.settimeout(1)
    cancelled = threading.Event()
    deliver = threading.Timer(0.12, sending.sendall, args=(b"ready\n",))
    guard = threading.Timer(2, cancelled.set)
    with (
        sending,
        CancellableSocket(receiving, cancelled.is_set) as stream,
        stream.makefile("rb") as reader,
    ):
        deliver.start()
        guard.start()
        try:
            assert reader.readline() == b"ready\n"
            assert stream.gettimeout() == 1
            stream.settimeout(0.02)
            with pytest.raises(TimeoutError):
                reader.read(1)
            assert stream.gettimeout() == 0.02
        finally:
            deliver.cancel()
            guard.cancel()
            deliver.join(timeout=1)
            guard.join(timeout=1)
