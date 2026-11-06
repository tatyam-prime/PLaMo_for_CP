"""Server entrypoint embedded in the desktop applications.

The native launcher owns the process and shows the setup page. End users never
need to invoke this module or install a Python interpreter.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import platform
import socket
import sys
import threading
from pathlib import Path

APPLICATION_ID = "plamo-for-cp"
CACHE_NAME = "plamo-cp"
DEFAULT_PORT = 8765
HEALTH_TIMEOUT = 0.75
HEALTH_MAX_BYTES = 65536
logger = logging.getLogger(__name__)


def log_directory() -> Path:
    path = Path.home() / ".cache" / CACHE_NAME / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_environment() -> None:
    cache = Path.home() / ".cache" / CACHE_NAME
    (cache / "numba").mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("NUMBA_CACHE_DIR", str(cache / "numba"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())


def service_state(port: int) -> str:
    """Probe loopback only, without redirects, proxy lookup or unbounded reads."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=HEALTH_TIMEOUT)
    connected = False
    active_socket = None
    expired = threading.Event()

    def abort():
        expired.set()
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(HEALTH_TIMEOUT, abort)
    timer.daemon = True
    timer.start()
    try:
        connection.connect()
        connected = True
        active_socket = connection.sock
        if expired.is_set():
            return "other"
        connection.request("GET", "/api/health", headers={"Connection": "close"})
        response = connection.getresponse()
        if response.status != 200 or (
            response.length is not None and response.length > HEALTH_MAX_BYTES
        ):
            return "other"
        body = response.read(HEALTH_MAX_BYTES + 1)
        if expired.is_set() or len(body) > HEALTH_MAX_BYTES:
            return "other"
        value = json.loads(body)
        if isinstance(value, dict) and value.get("app") == APPLICATION_ID:
            return "ours"
        return "other"
    except (OSError, http.client.HTTPException, ValueError, UnicodeError):
        return "other" if connected else "unavailable"
    finally:
        timer.cancel()
        connection.close()


def listening_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if platform.system() == "Windows" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif platform.system() != "Windows":
            # Reopening the app must not wait for recently closed local clients
            # to leave TIME_WAIT. Another live listener still cannot bind here.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        sock.setblocking(False)
        return sock
    except OSError:
        sock.close()
        raise


def restore_standard_streams() -> None:
    """Reconnect a Windows windowed child to its launcher's bounded output pipe."""
    fallback = None
    for name, handle_id in (("stdout", -11), ("stderr", -12)):
        if getattr(sys, name) is not None:
            continue
        stream = None
        if sys.platform == "win32":
            try:
                import ctypes
                import msvcrt
                from ctypes import wintypes

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.GetStdHandle.argtypes = [wintypes.DWORD]
                kernel.GetStdHandle.restype = wintypes.HANDLE
                kernel.GetCurrentProcess.restype = wintypes.HANDLE
                kernel.DuplicateHandle.argtypes = [
                    wintypes.HANDLE,
                    wintypes.HANDLE,
                    wintypes.HANDLE,
                    ctypes.POINTER(wintypes.HANDLE),
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                ]
                kernel.DuplicateHandle.restype = wintypes.BOOL
                handle = kernel.GetStdHandle(handle_id)
                duplicate = wintypes.HANDLE()
                process = kernel.GetCurrentProcess()
                if not handle or not kernel.DuplicateHandle(
                    process, handle, process, ctypes.byref(duplicate), 0, False, 2
                ):
                    raise OSError("No inherited output handle")
                descriptor = msvcrt.open_osfhandle(duplicate.value, os.O_WRONLY | os.O_BINARY)
                stream = os.fdopen(descriptor, "w", encoding="utf-8", buffering=1, errors="replace")
            except (OSError, ValueError):
                pass
        if stream is None:
            from .bounded_log import BoundedLog, BoundedTextStream

            if fallback is None:
                # Different filename avoids racing the launcher's captured native output.
                fallback = BoundedTextStream(BoundedLog(log_directory() / "backend.log"))
            stream = fallback
        setattr(sys, name, stream)


def main() -> int:
    parser = argparse.ArgumentParser(description="PLaMo for CP desktop backend")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535.")
    # PyInstaller's Windows windowed bootloader can set the standard streams to
    # None even when the parent supplied handles. Uvicorn expects isatty().
    restore_standard_streams()
    prepare_environment()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    state = service_state(args.port)
    if state == "ours":
        logger.info("An existing PLaMo for CP instance owns this port.")
        return 0
    if state == "other":
        logger.error("Another application owns the configured local port.")
        return 3
    try:
        sock = listening_socket(args.port)
    except OSError:
        logger.exception("Cannot start the local translation service.")
        return 3
    import uvicorn

    from .api import create_app

    try:
        config = uvicorn.Config(
            create_app(), host="127.0.0.1", port=args.port, workers=1, access_log=False
        )
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(main())
