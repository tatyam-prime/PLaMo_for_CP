"""Narrow ctypes adapter to the versioned, bundled llama.cpp C ABI bridge."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

MAX_BUFFER = 1024 * 1024


class NativeError(RuntimeError):
    pass


class Bridge:
    def __init__(self, bridge_path, dll_dir, model_path, *, threads=8, library=None):
        self.handle = ctypes.c_void_p()
        self._directory = None
        if library is None:
            if os.name != "nt":
                raise NativeError("この推論エンジンは Windows 用です。")
            self._directory = os.add_dll_directory(str(Path(dll_dir).resolve()))
            library = ctypes.CDLL(str(Path(bridge_path).resolve()))
        self.lib = library
        self._bind()
        if self.lib.cp_llama_abi_version() != 1:
            self.close()
            raise NativeError("推論エンジンのバージョンが一致しません。")
        model = str(Path(model_path).resolve()).encode("utf-8")
        directory = str(Path(dll_dir).resolve()).encode("utf-8")
        if b"\x00" in model or b"\x00" in directory:
            raise NativeError("モデルのパスが不正です。")
        try:
            self._status(
                self.lib.cp_llama_open(
                    model, directory, -1, 32768, 32, 256, threads, -1, ctypes.byref(self.handle)
                )
            )
            if self.lib.cp_llama_context_size(self.handle) != 32768:
                raise NativeError("推論エンジンの文脈サイズが一致しません。")
        except BaseException:
            self.close()
            raise

    def _bind(self):
        pointer, integer, byte = ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint8
        ints, bytes_ = ctypes.POINTER(integer), ctypes.POINTER(byte)
        signatures = {
            "cp_llama_abi_version": ([], ctypes.c_uint32),
            "cp_llama_last_error": ([bytes_, integer], integer),
            "cp_llama_open": (
                [ctypes.c_char_p, ctypes.c_char_p, *([integer] * 6), ctypes.POINTER(pointer)],
                integer,
            ),
            "cp_llama_close": ([pointer], None),
            "cp_llama_tokenize": ([pointer, bytes_, integer, ints, integer, byte, byte], integer),
            "cp_llama_token_piece": ([pointer, integer, bytes_, integer], integer),
            "cp_llama_decode": ([pointer, ints, ints, ints, bytes_, integer], integer),
            "cp_llama_synchronize": ([pointer], integer),
            "cp_llama_sample_greedy": ([pointer, integer], integer),
            "cp_llama_is_eog": ([pointer, integer], integer),
            "cp_llama_seq_remove": ([pointer, integer, integer, integer], integer),
            "cp_llama_context_size": ([pointer], integer),
            "cp_llama_info": ([pointer, bytes_, integer], integer),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = arguments, result

    def _error(self):
        try:
            return self._buffer(self.lib.cp_llama_last_error).decode("utf-8", errors="replace")[
                :1024
            ]
        except Exception:  # noqa: BLE001 - error reporting cannot hide a native failure
            return "推論エンジンでエラーが発生しました。"

    def _status(self, value):
        if value != 0:
            raise NativeError(self._error())

    def _buffer(self, function, *args):
        size = function(*args, None, 0)
        if not 0 <= size <= MAX_BUFFER:
            raise NativeError("推論エンジンの応答サイズが不正です。")
        if size == 0:
            return b""
        buffer = (ctypes.c_uint8 * size)()
        if function(*args, buffer, size) != size:
            raise NativeError("推論エンジンの応答サイズが変化しました。")
        return bytes(buffer)

    def tokenize(self, value, *, add_special=False, parse_special=False):
        raw = value.encode("utf-8")
        data = (ctypes.c_uint8 * len(raw)).from_buffer_copy(raw)
        count = self.lib.cp_llama_tokenize(
            self.handle, data, len(raw), None, 0, add_special, parse_special
        )
        if count < 0:
            raise NativeError(self._error())
        if count > 32768:
            raise ValueError("段落が長すぎます。短く区切って翻訳してください。")
        tokens = (ctypes.c_int32 * count)()
        if (
            self.lib.cp_llama_tokenize(
                self.handle, data, len(raw), tokens, count, add_special, parse_special
            )
            != count
        ):
            raise NativeError(self._error())
        return list(tokens)

    def decode(self, entries):
        count = len(entries)
        if not 1 <= count <= 256:
            raise NativeError("推論バッチのサイズが不正です。")
        arrays = [
            (ctype * count)(*(entry[index] for entry in entries))
            for index, ctype in enumerate(
                (ctypes.c_int32, ctypes.c_int32, ctypes.c_int32, ctypes.c_uint8)
            )
        ]
        self._status(self.lib.cp_llama_decode(self.handle, *arrays, count))

    def synchronize(self):
        self._status(self.lib.cp_llama_synchronize(self.handle))

    def sample(self, index):
        token = self.lib.cp_llama_sample_greedy(self.handle, index)
        if token < 0:
            raise NativeError(self._error())
        return token

    def piece(self, token):
        return self._buffer(self.lib.cp_llama_token_piece, self.handle, token)

    def is_eog(self, token):
        result = self.lib.cp_llama_is_eog(self.handle, token)
        if result not in {0, 1}:
            raise NativeError(self._error())
        return bool(result)

    def remove(self, sequence):
        self._status(self.lib.cp_llama_seq_remove(self.handle, sequence, 0, -1))

    def info(self):
        value = json.loads(self._buffer(self.lib.cp_llama_info, self.handle))
        if not isinstance(value, dict):
            raise NativeError("推論エンジンの情報が不正です。")
        return value

    def close(self):
        if self.handle:
            self.lib.cp_llama_close(self.handle)
            self.handle = ctypes.c_void_p()
        if self._directory is not None:
            self._directory.close()
            self._directory = None
