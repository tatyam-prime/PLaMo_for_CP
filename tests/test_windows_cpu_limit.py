from __future__ import annotations

import ctypes
import os
import subprocess
from types import SimpleNamespace

import pytest

from plamo_cp import windows_cpu_limit as limits


class OwnedChild(subprocess.Popen):
    def __init__(self, *, arguments=None, pid=45678, handle=98765):
        # A Popen-shaped child without invoking CreateProcess or touching the OS.
        self._child_created = False
        self.args = (
            arguments if arguments is not None else [r"C:\app\llama-server.exe", "--threads", "8"]
        )
        self.pid = pid
        self._handle = handle
        self.returncode = None

    def poll(self):
        return self.returncode


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.close_failure = False

    def operation(self, name, *arguments):
        self.calls.append((name, *arguments))
        if self.fail == name or (name == "close" and self.close_failure):
            raise OSError(50, "unsupported mock environment")

    def create(self):
        self.operation("create")
        return 321

    def set_cpu_rate(self, job, flags, rate):
        self.operation("set", job, flags, rate)

    def assign(self, job, process):
        self.operation("assign", job, process)

    def close(self, job):
        self.operation("close", job)


def controller(*, api=None, process=None, threads=8, cores=16):
    return limits.OwnedProcessCpuLimit(
        process if process is not None else OwnedChild(), threads, cores, _api=api
    )


@pytest.mark.parametrize(
    "threads,cores,rate",
    [
        (8, 16, 2508),
        (8, 32, 1254),
        (8, 64, 627),
        (8, 8, 5017),
        (8, 4, 5017),
        (1, 1, 5017),
        (1, 3, 1672),
        (1, 100000, 1),
    ],
)
def test_legacy_rate_scales_configured_threads_rather_than_the_whole_machine(threads, cores, rate):
    assert limits.quiet_cpu_rate(threads, cores) == rate


@pytest.mark.parametrize("threads,cores", [(0, 8), (9, 8), (True, 8), (1, 0), (1, True)])
def test_invalid_cpu_budget_cannot_reach_win32(threads, cores):
    with pytest.raises(ValueError):
        controller(threads=threads, cores=cores)


def test_lifecycle_targets_only_owned_handle_and_reuses_one_job():
    child, api = OwnedChild(), FakeAPI()
    cap = controller(api=api, process=child)
    cap.set_enabled(False)
    assert api.calls == []
    cap.set_enabled(True)
    cap.set_enabled(True)
    assert cap.enabled
    assert api.calls == [("create",), ("set", 321, 5, 2508), ("assign", 321, 98765)]
    cap.set_enabled(False)
    assert not cap.enabled
    assert api.calls[-1] == ("set", 321, 0, 10000)
    cap.set_enabled(True)
    assert api.calls[-1] == ("set", 321, 5, 2508)
    assert sum(call[0] == "assign" for call in api.calls) == 1
    cap.close()
    assert api.calls[-2:] == [("set", 321, 0, 10000), ("close", 321)]
    assert child._handle == 98765  # Popen, not this helper, owns the process handle.
    cap.close()
    assert sum(call[0] == "close" for call in api.calls) == 1
    with pytest.raises(RuntimeError, match="既に終了"):
        cap.set_enabled(True)


@pytest.mark.parametrize("operation", ["create", "set", "assign"])
def test_first_enable_failure_does_not_claim_enabled_or_affect_another_process(operation):
    api = FakeAPI()
    api.fail = operation
    cap = controller(api=api)
    with pytest.raises(RuntimeError, match="Windows エラー 50"):
        cap.set_enabled(True)
    assert not cap.enabled
    if operation == "create":
        assert api.calls == [("create",)]
    else:
        assert api.calls[-1] == ("close", 321)
    api.fail = None
    cap.set_enabled(True)
    assert cap.enabled
    assert api.calls[-1] == ("assign", 321, 98765)
    cap.close()


def test_failed_cleanup_of_empty_job_can_retry_assignment_without_losing_ownership():
    api = FakeAPI()
    api.fail, api.close_failure = "assign", True
    cap = controller(api=api)
    with pytest.raises(RuntimeError):
        cap.set_enabled(True)
    api.fail, api.close_failure = None, False
    cap.set_enabled(True)
    assert cap.enabled
    assert sum(call[0] == "create" for call in api.calls) == 1
    assert api.calls[-1] == ("assign", 321, 98765)
    cap.close()


def test_failed_disable_preserves_enabled_state_and_handle_for_retry():
    api = FakeAPI()
    cap = controller(api=api)
    cap.set_enabled(True)
    api.fail = "set"
    with pytest.raises(RuntimeError):
        cap.set_enabled(False)
    assert cap.enabled
    with pytest.raises(RuntimeError):
        cap.close()
    assert ("close", 321) not in api.calls
    api.fail = None
    cap.close()
    assert not cap.enabled
    assert api.calls[-2:] == [("set", 321, 0, 10000), ("close", 321)]


def test_failed_reenable_preserves_disabled_state():
    api = FakeAPI()
    cap = controller(api=api)
    cap.set_enabled(True)
    cap.set_enabled(False)
    api.fail = "set"
    with pytest.raises(RuntimeError):
        cap.set_enabled(True)
    assert not cap.enabled
    api.fail = None
    cap.close()


def test_stopped_child_never_causes_pid_reopen_or_native_rate_change():
    child, api = OwnedChild(), FakeAPI()
    cap = controller(api=api, process=child)
    cap.set_enabled(True)
    calls = list(api.calls)
    child.returncode = 0
    with pytest.raises(RuntimeError, match="終了"):
        cap.set_enabled(True)
    assert api.calls == calls
    cap.close()
    assert not cap.enabled
    assert api.calls == [*calls, ("close", 321)]


def test_exited_child_cannot_be_enabled_in_the_first_place():
    child, api = OwnedChild(), FakeAPI()
    child.returncode = 0
    with pytest.raises(RuntimeError, match="終了"):
        controller(api=api, process=child).set_enabled(True)
    assert api.calls == []


@pytest.mark.parametrize("handle", [None, 0, -1, "98765", True])
def test_missing_owned_windows_handle_never_falls_back_to_pid(handle):
    api = FakeAPI()
    cap = controller(api=api, process=OwnedChild(handle=handle))
    with pytest.raises(RuntimeError, match="ハンドル"):
        cap.set_enabled(True)
    assert api.calls == []


@pytest.mark.parametrize(
    "process",
    [
        OwnedChild(arguments=["python.exe"]),
        OwnedChild(arguments="llama-server.exe --threads 8"),
        OwnedChild(pid=0),
        OwnedChild(pid=os.getpid()),
    ],
)
def test_unowned_or_unexpected_process_arguments_are_rejected(process):
    with pytest.raises(ValueError):
        controller(api=FakeAPI(), process=process)


def test_an_arbitrary_pid_and_handle_object_is_not_an_owned_popen_child():
    with pytest.raises(TypeError):
        controller(api=FakeAPI(), process=SimpleNamespace(pid=45678, _handle=98765))


def test_failed_close_retains_handle_until_a_successful_cleanup():
    api = FakeAPI()
    cap = controller(api=api)
    cap.set_enabled(True)
    api.close_failure = True
    with pytest.raises(RuntimeError):
        cap.close()
    assert not cap.enabled  # The live child's cap was already disabled.
    api.close_failure = False
    cap.close()
    assert api.calls[-2:] == [("close", 321), ("close", 321)]


def test_close_without_any_enable_never_creates_a_job_or_loads_win32(monkeypatch):
    monkeypatch.setattr(limits.sys, "platform", "darwin")
    cap = controller()
    cap.close()
    cap.close()
    assert not cap.enabled


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_toggle_requires_a_boolean_before_any_native_call(value):
    api = FakeAPI()
    cap = controller(api=api)
    with pytest.raises(ValueError):
        cap.set_enabled(value)
    assert api.calls == []


def test_native_initialization_is_lazy_and_non_windows_error_is_explicit(monkeypatch):
    monkeypatch.setattr(limits.sys, "platform", "darwin")
    cap = controller()
    cap.set_enabled(False)
    with pytest.raises(RuntimeError, match="Windows"):
        cap.set_enabled(True)
    assert not cap.enabled
    cap.close()


def test_unknown_logical_cpu_count_uses_configured_budget(monkeypatch):
    monkeypatch.setattr(limits.os, "cpu_count", lambda: None)
    cap = limits.OwnedProcessCpuLimit(OwnedChild(), 8, _api=FakeAPI())
    assert cap.rate == 10000 and cap.level == 0
    cap.set_enabled(True)
    assert cap.rate == 5017
    cap.close()


class NativeFunction:
    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *args):
        return self.implementation(*args)


def test_ctypes_abi_uses_two_windows_dwords_and_only_cpu_information_class():
    calls = []

    def set_information(job, information_class, pointer, size):
        info = ctypes.cast(pointer, ctypes.POINTER(limits._CpuRateInformation)).contents
        calls.append(("set", job, information_class, size, info.ControlFlags, info.CpuRate))
        return 1

    kernel = SimpleNamespace(
        CreateJobObjectW=NativeFunction(
            lambda security, name: calls.append(("create", security, name)) or 321
        ),
        SetInformationJobObject=NativeFunction(set_information),
        AssignProcessToJobObject=NativeFunction(
            lambda job, process: calls.append(("assign", job, process)) or 1
        ),
        CloseHandle=NativeFunction(lambda handle: calls.append(("close", handle)) or 1),
    )
    api = limits._Win32JobAPI(kernel)
    cap = controller(api=api)
    cap.set_enabled(True)
    cap.close()
    assert ctypes.sizeof(limits._CpuRateInformation) == 8
    assert limits._CpuRateInformation.CpuRate.offset == 4
    assert calls == [
        ("create", None, None),
        ("set", 321, 15, 8, 5, 2508),
        ("assign", 321, 98765),
        ("set", 321, 15, 8, 0, 10000),
        ("close", 321),
    ]
    assert kernel.CreateJobObjectW.restype is ctypes.c_void_p
    assert kernel.AssignProcessToJobObject.argtypes == [ctypes.c_void_p, ctypes.c_void_p]


@pytest.mark.parametrize(
    "level,rate",
    [
        (0, 10000),
        (1, 3973),
        (2, 3157),
        (3, 2508),
        (4, 1993),
        (5, 1584),
        (6, 1258),
        (7, 1000),
    ],
)
def test_level_rate_is_relative_to_configured_threads(level, rate):
    assert limits.quiet_cpu_rate(8, 16, level) == rate


def test_live_levels_update_one_owned_job_and_preserve_state_on_native_failure():
    api = FakeAPI()
    cap = controller(api=api)
    cap.set_level(1)
    assert cap.level == 1 and cap.rate == 3973 and cap.enabled
    api.fail = "set"
    with pytest.raises(RuntimeError):
        cap.set_level(limits.MAX_QUIET_LEVEL)
    assert cap.level == 1 and cap.rate == 3973
    api.fail = None
    cap.set_level(limits.MAX_QUIET_LEVEL)
    assert cap.level == limits.MAX_QUIET_LEVEL and cap.rate == 1000
    cap.set_enabled(True)
    assert cap.level == limits.LEGACY_QUIET_LEVEL and cap.rate == 2508
    cap.set_level(0)
    assert cap.level == 0 and cap.rate == 10000 and not cap.enabled
    assert api.calls[-1] == ("set", 321, 0, 10000)
    assert sum(call[0] == "assign" for call in api.calls) == 1
    cap.close()


@pytest.mark.parametrize("invalid", [-1, limits.MAX_QUIET_LEVEL + 1, 0.5, True, "1", None])
def test_invalid_levels_do_not_reach_the_native_api(invalid):
    api = FakeAPI()
    cap = controller(api=api)
    with pytest.raises(ValueError):
        cap.set_level(invalid)
    with pytest.raises(ValueError):
        limits.quiet_cpu_rate(8, 16, invalid)
    assert api.calls == [] and cap.level == 0
    cap.close()
