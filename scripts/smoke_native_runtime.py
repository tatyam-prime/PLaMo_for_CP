"""Model-free build check for the verified Windows bridge ABI and CPU backend.

This build helper enumerates devices; it never opens a model or runs inference.
The application and native worker use their separately embedded Python pins.
"""

from __future__ import annotations

import ctypes
import json
from pathlib import Path

from plamo_cp.native_runtime import BRIDGE_ABI, verify_native_runtime_directory

MAX_DEVICE_INFO = 1024 * 1024


def _buffer(function, *arguments) -> bytes:
    count = function(*arguments, None, 0)
    if type(count) is not int or not 0 <= count <= MAX_DEVICE_INFO:
        raise RuntimeError("The native runtime returned invalid device information.")
    output = (ctypes.c_uint8 * count)()
    if function(*arguments, output, count) != count:
        raise RuntimeError("The native runtime device information changed size.")
    return bytes(output)


def smoke_runtime(directory: Path, pins: dict, *, loader=None) -> list[dict]:
    runtime = verify_native_runtime_directory(directory, pins)
    loader = loader or ctypes.CDLL
    # Resolve dependencies only in the verified DLL directory and System32.
    library = loader(str(runtime.bridge), winmode=0x00000100 | 0x00001000)
    byte_pointer = ctypes.POINTER(ctypes.c_uint8)
    library.cp_llama_abi_version.argtypes = []
    library.cp_llama_abi_version.restype = ctypes.c_uint32
    library.cp_llama_devices.argtypes = [ctypes.c_char_p, byte_pointer, ctypes.c_int32]
    library.cp_llama_devices.restype = ctypes.c_int32
    library.cp_llama_last_error.argtypes = [byte_pointer, ctypes.c_int32]
    library.cp_llama_last_error.restype = ctypes.c_int32
    if library.cp_llama_abi_version() != BRIDGE_ABI:
        raise RuntimeError("The native bridge ABI does not match the application.")
    try:
        devices = json.loads(_buffer(library.cp_llama_devices, str(directory).encode("utf-8")))
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("The native bridge returned malformed device information.") from error
    if (
        not isinstance(devices, list)
        or len(devices) > 256
        or not all(isinstance(device, dict) for device in devices)
        or not any(device.get("type") == "cpu" for device in devices)
    ):
        raise RuntimeError("The native runtime did not expose its CPU fallback backend.")
    return devices
