"""Bound child-process output continuously, including long lines and native stderr."""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import BinaryIO

LOG_MAX_BYTES = 128 * 1024
LOG_BACKUPS = 1


class BoundedLog:
    """Keep the newest bytes in current + numbered backups, each below the cap."""

    def __init__(self, path: Path, *, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS):
        if max_bytes < 1 or backups < 0:
            raise ValueError("Invalid log limits")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for file in self.path.parent.glob(f"{self.path.name}.*"):
            suffix = file.name[len(self.path.name) + 1 :]
            if suffix.isdecimal() and int(suffix) > backups:
                file.unlink(missing_ok=True)
        # Repair oversized files left by an older version without reading them whole.
        for file in [self.path, *(self._backup(index) for index in range(1, backups + 1))]:
            if file.is_file() and file.stat().st_size > max_bytes:
                with file.open("rb") as source:
                    source.seek(-max_bytes, 2)
                    tail = source.read(max_bytes)
                file.write_bytes(tail)
        # Older launchers used this unbounded startup-only backup name.
        self.path.with_suffix(self.path.suffix + ".previous").unlink(missing_ok=True)
        self._file = self.path.open("ab", buffering=0)
        self._size = self.path.stat().st_size

    def _backup(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _rotate(self) -> None:
        self._file.close()
        if self.backups:
            self._backup(self.backups).unlink(missing_ok=True)
            for index in range(self.backups - 1, 0, -1):
                previous = self._backup(index)
                if previous.exists():
                    previous.replace(self._backup(index + 1))
            self.path.replace(self._backup(1))
        else:
            self.path.unlink(missing_ok=True)
        self._file = self.path.open("ab", buffering=0)
        self._size = 0

    def write(self, data: bytes) -> int:
        value = memoryview(data)
        with self._lock:
            offset = 0
            while offset < len(value):
                if self._size >= self.max_bytes:
                    self._rotate()
                end = min(len(value), offset + self.max_bytes - self._size)
                written = self._file.write(value[offset:end])
                if not written:
                    raise OSError("Cannot write the application log")
                self._size += written
                offset += written
        return len(value)

    def flush(self) -> None:
        with self._lock:
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()


class BoundedTextStream(io.TextIOBase):
    """Fallback for a windowed process without usable inherited output handles."""

    def __init__(self, log: BoundedLog):
        self.log = log

    @property
    def encoding(self) -> str:
        return "utf-8"

    def write(self, value: str) -> int:
        self.log.write(value.encode("utf-8", errors="replace"))
        return len(value)

    def flush(self) -> None:
        self.log.flush()


def drain_stream(stream: BinaryIO, path: Path) -> None:
    """Drain even on disk failure, so a full pipe cannot stall model execution."""
    log = None
    try:
        try:
            log = BoundedLog(path)
        except OSError:
            pass
        read = getattr(stream, "read1", stream.read)
        while chunk := read(64 * 1024):
            if log is not None:
                try:
                    log.write(chunk)
                except OSError:
                    log.close()
                    log = None
    finally:
        stream.close()
        if log is not None:
            log.close()
