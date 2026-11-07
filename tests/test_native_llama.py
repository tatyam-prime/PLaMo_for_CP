"""ABI buffer, row and ownership checks with a Python fake C library."""

from __future__ import annotations

import ctypes
import json
from types import SimpleNamespace

import pytest

from plamo_cp.native_llama import Bridge, NativeError


class Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *arguments):
        return self.call(*arguments)


def library():
    state = SimpleNamespace(opened=[], closed=[], decoded=[], removed=[], syncs=0)

    def buffer(value, destination, capacity):
        if capacity >= len(value) and destination is not None:
            for index, byte in enumerate(value):
                destination[index] = byte
        return len(value)

    def opened(*args):
        state.opened.append(args[:-1])
        ctypes.cast(args[-1], ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return 0

    def tokenize(_ctx, data, length, destination, capacity, add_special, parse_special):
        assert bytes(data[:length]).decode() == "頂点"
        assert not add_special and parse_special
        if capacity >= 2:
            destination[0], destination[1] = 42, 43
        return 2

    def decode(_ctx, tokens, positions, sequences, logits, count):
        state.decoded.append(
            [
                (tokens[index], positions[index], sequences[index], logits[index])
                for index in range(count)
            ]
        )
        return 0

    def synchronize(_ctx):
        state.syncs += 1
        return 0

    native = SimpleNamespace(
        cp_llama_abi_version=Function(lambda: 1),
        cp_llama_last_error=Function(lambda out, cap: buffer(b"native failure", out, cap)),
        cp_llama_open=Function(opened),
        cp_llama_close=Function(lambda ctx: state.closed.append(ctx.value)),
        cp_llama_context_size=Function(lambda _: 32768),
        cp_llama_tokenize=Function(tokenize),
        cp_llama_decode=Function(decode),
        cp_llama_synchronize=Function(synchronize),
        cp_llama_sample_greedy=Function(lambda _ctx, index: 100 + index),
        cp_llama_token_piece=Function(
            lambda _ctx, _token, out, cap: buffer("頂点".encode(), out, cap)
        ),
        cp_llama_is_eog=Function(lambda _ctx, token: int(token == 0)),
        cp_llama_seq_remove=Function(
            lambda _ctx, seq, p0, p1: state.removed.append((seq, p0, p1)) or 0
        ),
        cp_llama_info=Function(
            lambda _ctx, out, cap: buffer(json.dumps({"gpu": True}).encode(), out, cap)
        ),
    )
    return native, state


def test_abi_uses_opaque_handles_exact_rows_buffers_and_native_sampling(tmp_path):
    native, state = library()
    bridge = Bridge(tmp_path / "bridge.dll", tmp_path, tmp_path / "model.gguf", library=native)
    assert state.opened[0][2:] == (-1, 32768, 32, 256, 8, -1)
    assert bridge.tokenize("頂点", parse_special=True) == [42, 43]
    entries = [(42, 0, 31, 1), (43, 10, 2, 0)]
    bridge.decode(entries)
    assert state.decoded == [entries] and bridge.sample(0) == 100
    assert bridge.piece(100) == "頂点".encode() and bridge.is_eog(0)
    bridge.synchronize()
    bridge.remove(31)
    assert state.syncs == 1 and state.removed == [(31, 0, -1)]
    assert bridge.info() == {"gpu": True}
    bridge.close()
    bridge.close()
    assert state.closed == [123]
    assert native.cp_llama_decode.argtypes[-1] is ctypes.c_int32
    assert native.cp_llama_sample_greedy.restype is ctypes.c_int32


def test_native_failure_cleans_partial_context_and_preserves_error_text(tmp_path):
    native, state = library()
    native.cp_llama_context_size = Function(lambda _: 1024)
    with pytest.raises(NativeError, match="文脈サイズ"):
        Bridge(tmp_path / "bridge.dll", tmp_path, tmp_path / "model.gguf", library=native)
    assert state.closed == [123]


def test_decode_and_native_buffers_are_bounded_before_memory_allocation(tmp_path):
    native, _state = library()
    bridge = Bridge(tmp_path / "bridge.dll", tmp_path, tmp_path / "model.gguf", library=native)
    try:
        for entries in ([], [(1, 0, 0, 1)] * 257):
            with pytest.raises(NativeError):
                bridge.decode(entries)
        native.cp_llama_token_piece.call = lambda *_: 2 * 1024 * 1024
        with pytest.raises(NativeError, match="サイズ"):
            bridge.piece(1)
        native.cp_llama_sample_greedy.call = lambda *_: -1
        with pytest.raises(NativeError, match="native failure"):
            bridge.sample(1)
    finally:
        bridge.close()
