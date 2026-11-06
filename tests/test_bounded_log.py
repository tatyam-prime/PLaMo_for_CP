from __future__ import annotations

import io
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from plamo_cp.bounded_log import (
    LOG_BACKUPS,
    LOG_MAX_BYTES,
    BoundedLog,
    BoundedTextStream,
    drain_stream,
)


def read_retained(path: Path, backups: int = 2) -> bytes:
    files = [path.with_name(f"{path.name}.{index}") for index in range(backups, 0, -1)]
    return b"".join(file.read_bytes() for file in [*files, path] if file.exists())


def test_product_defaults_fit_within_256_kib_per_log_family():
    assert LOG_MAX_BYTES == 128 * 1024
    assert LOG_BACKUPS == 1
    assert LOG_MAX_BYTES * (LOG_BACKUPS + 1) == 256 * 1024


def test_rollover_is_continuous_and_preserves_latest_output(tmp_path):
    path = tmp_path / "desktop.log"
    log = BoundedLog(path, max_bytes=11, backups=2)
    for chunk in [b"Traceback\n", b"native stderr\n", b"remaining output\n"]:
        log.write(chunk)
        files = list(tmp_path.iterdir())
        assert len(files) <= 3
        assert all(file.stat().st_size <= 11 for file in files)
        assert sum(file.stat().st_size for file in files) <= 33
    log.close()
    assert b"Traceback\nnative stderr\nremaining output\n".endswith(read_retained(path))
    assert read_retained(path).endswith(b"remaining output\n")


def test_single_oversized_write_cannot_exceed_per_file_or_total_cap(tmp_path):
    path = tmp_path / "desktop.log"
    log = BoundedLog(path, max_bytes=8, backups=2)
    log.write(bytes(range(200)))
    log.close()
    assert sorted(file.stat().st_size for file in tmp_path.iterdir()) == [8, 8, 8]
    assert read_retained(path) == bytes(range(176, 200))


def test_old_oversized_files_are_repaired_and_obsolete_backup_removed(tmp_path):
    path = tmp_path / "desktop.log"
    for suffix in ["", ".1", ".2", ".3", ".previous"]:
        path.with_name(path.name + suffix).write_bytes(b"old" * 30)
    log = BoundedLog(path, max_bytes=9, backups=1)
    log.write(b"new")
    log.close()
    assert len(list(tmp_path.iterdir())) == 2
    assert all(file.stat().st_size <= 9 for file in tmp_path.iterdir())
    assert read_retained(path).endswith(b"new")


def test_parallel_native_output_and_text_fallback_are_bounded(tmp_path):
    path = tmp_path / "desktop.log"
    log = BoundedLog(path, max_bytes=16)
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(log.write, [b"stderr\n" * 12] * 10))
    stream = BoundedTextStream(log)
    assert stream.write("例外\n") == 3
    stream.flush()
    assert stream.isatty() is False
    assert read_retained(path).endswith("例外\n".encode())
    assert all(file.stat().st_size <= 16 for file in tmp_path.iterdir())


def test_child_stdout_and_stderr_are_both_drained_without_line_buffering(tmp_path):
    path = tmp_path / "desktop.log"
    child = subprocess.Popen(
        [sys.executable, "-c", "import os;os.write(1,b'output');os.write(2,b' native traceback')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    drain_stream(child.stdout, path)
    assert child.wait(timeout=3) == 0
    assert path.read_bytes() == b"output native traceback"


def test_disk_failure_keeps_draining_child_output(monkeypatch, tmp_path):
    from plamo_cp import bounded_log

    class BrokenLog:
        def __init__(self, path):
            raise OSError("Disk is full")

    stream = io.BytesIO(b"native output" * 200)
    monkeypatch.setattr(bounded_log, "BoundedLog", BrokenLog)
    drain_stream(stream, tmp_path / "log")
    assert stream.closed


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS launcher implementation")
def test_native_mac_rotator_handles_oversized_messages_and_continuous_rollover(tmp_path):
    root = Path(__file__).resolve().parents[1]
    main = tmp_path / "main.swift"
    main.write_text(
        "import Foundation\n"
        "let url = URL(fileURLWithPath: CommandLine.arguments[1])\n"
        "let log = try BoundedLog(url: url, maxBytes: 8, backups: 1)\n"
        "try log.append(Data(0..<200))\n"
        "try log.append(Data([200, 201, 202]))\n"
        "log.close()\n"
    )
    executable = tmp_path / "check-rotation"
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            str(root / "packaging/macos/BoundedLog.swift"),
            str(main),
            "-o",
            str(executable),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    directory = tmp_path / "logs"
    directory.mkdir()
    path = directory / "desktop.log"
    for suffix in ["", ".1", ".2", ".previous"]:
        path.with_name(path.name + suffix).write_bytes(b"old output" * 8)
    subprocess.run([str(executable), str(path)], check=True)
    assert len(list(directory.iterdir())) == 2
    assert all(file.stat().st_size <= 8 for file in directory.iterdir())
    assert sum(file.stat().st_size for file in directory.iterdir()) <= 16
    assert read_retained(path).endswith(bytes(range(192, 203)))
