"""An optional CPU hard cap for our own Windows llama-server child.

The retained Popen HANDLE identifies the child even if its PID is later reused.
No process is opened by PID. Only CPU rate information is changed: no priority,
power settings, kill-on-close flag, or limits on the Python application itself.

Windows 8+ supports nested jobs. A containing job's quota can further reduce
this cap; RDS Dynamic Fair Share Scheduling can make CPU rate control unavailable.
The caller must surface such failures rather than saving an unapplied setting.

References:
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_cpu_rate_control_information
https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject
https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import sys
import threading
from subprocess import Popen

from .performance import LEGACY_QUIET_LEVEL, MAX_QUIET_LEVEL

CPU_RATE_INFORMATION = 15
CPU_RATE_ENABLE = 0x1
CPU_RATE_HARD_CAP = 0x4


class _CpuRateInformation(ctypes.Structure):
    # DWORD is always 32 bits on Windows; c_ulong is 64 bits on some test hosts.
    _fields_ = [("ControlFlags", ctypes.c_uint32), ("CpuRate", ctypes.c_uint32)]


def quiet_cpu_rate(
    configured_threads: int, logical_cpus: int, level: int = LEGACY_QUIET_LEVEL
) -> int:
    """Scale the thread budget logarithmically to 20%, in 1/100-percent units."""
    if type(level) is not int or not 0 <= level <= MAX_QUIET_LEVEL:
        raise ValueError(f"静音モードは 0〜{MAX_QUIET_LEVEL} で指定してください。")
    if type(configured_threads) is not int or not 1 <= configured_threads <= 8:
        raise ValueError("推論スレッド数は 1〜8 で指定してください。")
    if type(logical_cpus) is not int or logical_cpus < 1:
        raise ValueError("論理 CPU 数を取得できませんでした。")
    # Integer rounding, clamped to a valid nonzero native rate. This is not a
    # fixed machine-wide cap on machines with more CPUs than inference threads.
    if level == 0:
        return 10000
    rate = (
        10000
        * min(configured_threads, logical_cpus)
        / logical_cpus
        / 5 ** (level / MAX_QUIET_LEVEL)
    )
    return max(1, min(10000, int(rate + 0.5)))


class _Win32JobAPI:
    def __init__(self, kernel32=None):
        if kernel32 is None:
            if sys.platform != "win32":
                raise RuntimeError("この CPU 制限は Windows でのみ利用できます。")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p),
            "AssignProcessToJobObject": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int32),
            "SetInformationJobObject": (
                [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32],
                ctypes.c_int32,
            ),
            "CloseHandle": ([ctypes.c_void_p], ctypes.c_int32),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(kernel32, name)
            function.argtypes = arguments
            function.restype = result

    @staticmethod
    def _failed():
        raise ctypes.WinError(ctypes.get_last_error())

    def create(self):
        # Unnamed, non-inheritable and empty. No other process can find this job
        # by a public name, and no system-wide job is modified.
        job = self._kernel32.CreateJobObjectW(None, None)
        if not job:
            self._failed()
        return job

    def set_cpu_rate(self, job, flags: int, rate: int) -> None:
        information = _CpuRateInformation(flags, rate)
        if not self._kernel32.SetInformationJobObject(
            job, CPU_RATE_INFORMATION, ctypes.byref(information), ctypes.sizeof(information)
        ):
            self._failed()

    def assign(self, job, process_handle: int) -> None:
        if not self._kernel32.AssignProcessToJobObject(job, process_handle):
            self._failed()

    def close(self, job) -> None:
        if not self._kernel32.CloseHandle(job):
            self._failed()


def _change_error(error: OSError) -> RuntimeError:
    code = getattr(error, "winerror", None) or error.errno
    suffix = f"（Windows エラー {code}）" if code is not None else ""
    return RuntimeError(
        f"推論プロセスの CPU 制限を変更できませんでした{suffix}。"
        "実行環境やリモートデスクトップの制約により利用できない場合があります。"
    )


class OwnedProcessCpuLimit:
    """Keep one job for one already-owned Popen child, toggling its CPU cap.

    Construction and disabled mode do not call Windows APIs. set_level() only
    updates the level and rate after a successful native operation. close() explicitly
    disables a live child's cap before closing our job HANDLE; merely closing a
    job would leave the limit active until the child exits. Prefer terminating
    and waiting for the child before close() during normal server shutdown.

    The caller retains ownership of the process HANDLE. This object never
    closes it. A failed disable retains our job HANDLE for a retry or shutdown.
    """

    def __init__(
        self,
        process: Popen,
        configured_threads: int,
        logical_cpus: int | None = None,
        *,
        _api=None,
    ):
        if not isinstance(process, Popen):
            raise TypeError("起動済みの推論子プロセスが必要です。")
        arguments = getattr(process, "args", None)
        if (
            not isinstance(arguments, (list, tuple))
            or not arguments
            or not isinstance(arguments[0], (str, os.PathLike))
            or not isinstance(os.fspath(arguments[0]), str)
            or ntpath.basename(os.fspath(arguments[0])).casefold() != "llama-server.exe"
        ):
            raise ValueError("CPU 制限は起動した llama-server.exe にのみ設定できます。")
        if type(process.pid) is not int or process.pid <= 0 or process.pid == os.getpid():
            raise ValueError("起動済みの推論子プロセスが必要です。")
        if logical_cpus is None:
            logical_cpus = os.cpu_count() or configured_threads
        quiet_cpu_rate(configured_threads, logical_cpus)
        self._configured_threads, self._logical_cpus = configured_threads, logical_cpus
        self.rate = 10000
        self._process = process
        self._api = _api
        self._job = None
        self._assigned = False
        self._level = 0
        self._closed = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._level > 0

    @property
    def level(self) -> int:
        with self._lock:
            return self._level

    def _set_level(self, level: int) -> None:
        if self._process.poll() is not None:
            if level:
                raise RuntimeError("推論プロセスが終了したため CPU 制限を設定できません。")
            self._level, self.rate = 0, 10000
            return
        if level == self._level:
            return
        rate = quiet_cpu_rate(self._configured_threads, self._logical_cpus, level)
        if self._api is None:
            self._api = _Win32JobAPI()
        if not self._assigned:
            # Popen retains the CreateProcess HANDLE. Never reopen by PID: the
            # numeric PID can name an unrelated process after this child exits.
            handle = getattr(self._process, "_handle", None)
            if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
                raise RuntimeError("推論子プロセスの Windows ハンドルを取得できませんでした。")
            job = self._job if self._job is not None else self._api.create()
            try:
                self._api.set_cpu_rate(job, CPU_RATE_ENABLE | CPU_RATE_HARD_CAP, rate)
                self._api.assign(job, handle)
            except OSError:
                # No process was assigned when either native call fails. Retain
                # even an empty handle if cleanup itself fails, for close().
                try:
                    self._api.close(job)
                except OSError:
                    self._job = job
                else:
                    self._job = None
                raise
            self._job = job
            self._assigned = True
        else:
            # CpuRate=0 is invalid even though ignored with ENABLE clear.
            flags = CPU_RATE_ENABLE | CPU_RATE_HARD_CAP if level else 0
            self._api.set_cpu_rate(self._job, flags, rate)
        self._level, self.rate = level, rate

    def set_enabled(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("CPU 制限の有効・無効を指定してください。")
        self.set_level(LEGACY_QUIET_LEVEL if enabled else 0)

    def set_level(self, level: int) -> None:
        if type(level) is not int or not 0 <= level <= MAX_QUIET_LEVEL:
            raise ValueError(f"静音モードは 0〜{MAX_QUIET_LEVEL} で指定してください。")
        with self._lock:
            if self._closed:
                raise RuntimeError("推論子プロセスの CPU 制限は既に終了しています。")
            try:
                self._set_level(level)
            except OSError as error:
                raise _change_error(error) from error

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._level:
                    self._set_level(0)
                if self._job is not None:
                    self._api.close(self._job)
            except OSError as error:
                raise _change_error(error) from error
            self._job = None
            self._closed = True
