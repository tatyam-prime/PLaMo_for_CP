"""Owned native decode loop: one GPU thread, bounded requests, no HTTP server."""

from __future__ import annotations

import codecs
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .engine import check_placeholders, preview_prefix, translation_options, validate_history
from .llama_backend import build_prompt
from .marker_codec import prepare_model_input
from .native_ipc import (
    MAX_SOURCE_CHARACTERS,
    VERSION,
    ProtocolError,
    fields,
    identifier,
    integer,
    read_frame,
    text,
    write_frame,
)
from .performance import MAX_QUIET_LEVEL

CONTEXT_SIZE = 32768
MAX_ACTIVE = 32
BATCH_SIZE = 256
PARAMETERS = frozenset(
    {"source_lang", "target_lang", "document_type", "history", "glossary", "source_context"}
)


class Commands:
    def __init__(self, quiet_level=0):
        self.condition = threading.Condition()
        self.pending = deque()
        self.identifiers = set()
        self.cancelled = set()
        self.quiet_level = quiet_level
        self.closing = False
        self.failure = None

    def receive(self, frame, emit):
        kind = frame["type"]
        with self.condition:
            if kind == "submit":
                fields(frame, {"type", "id", "text", "parameters"})
                key = identifier(frame["id"])
                text(frame["text"], MAX_SOURCE_CHARACTERS)
                parameters = frame["parameters"]
                if not isinstance(parameters, dict) or not parameters.keys() <= PARAMETERS:
                    raise ProtocolError("推論要求の設定が不正です。")
                if key in self.identifiers or len(self.identifiers) >= 64:
                    raise ProtocolError("推論要求の識別子が重複したか、件数が多すぎます。")
                self.identifiers.add(key)
                self.pending.append(frame)
            elif kind == "cancel":
                fields(frame, {"type", "id"})
                key = identifier(frame["id"])
                if key in self.identifiers:
                    self.cancelled.add(key)
            elif kind == "quiet":
                fields(frame, {"type", "id", "level"})
                key = identifier(frame["id"])
                self.quiet_level = integer(frame["level"], 0, MAX_QUIET_LEVEL)
                emit({"type": "quiet_applied", "id": key, "level": self.quiet_level})
            elif kind == "close":
                fields(frame, {"type"})
                self.closing = True
            else:
                raise ProtocolError("未知の推論要求です。")
            self.condition.notify_all()

    def finished(self, key):
        with self.condition:
            self.identifiers.discard(key)
            self.cancelled.discard(key)

    def wait_duty(self, elapsed):
        started = time.perf_counter()
        measured = min(0.25, max(0.0, elapsed))
        with self.condition:
            while self.quiet_level and not self.closing and not self.cancelled:
                ratio = 5 ** (self.quiet_level / MAX_QUIET_LEVEL) - 1
                remaining = started + measured * ratio - time.perf_counter()
                if remaining <= 0:
                    break
                self.condition.wait(min(0.02, remaining))


@dataclass
class Sequence:
    key: str
    source: str
    prompt: list[int]
    budget: int
    codec: object
    slot: int | None = None
    cursor: int = 0
    generated: list[int] = field(default_factory=list)
    pieces: list[bytes] = field(default_factory=list)
    output: str = ""
    decoder: object = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")())
    next_token: int | None = None
    preview_at: float = float("-inf")
    preview_enabled: bool = True

    @property
    def cost(self):
        return len(self.prompt) + self.budget


def prepare(bridge, frame):
    parameters = dict(frame["parameters"])
    parameters.setdefault("source_lang", "English")
    parameters.setdefault("target_lang", "Japanese")
    history = validate_history(parameters.pop("history", None))
    source = frame["text"]
    options = translation_options(source, **parameters)

    def tokens():
        return bridge.tokenize(
            build_prompt(source, history=history, **parameters), parse_special=True
        )

    prompt = tokens()
    budget = max(512, len(bridge.tokenize(source)) * 3 + 128)
    while len(prompt) + budget > CONTEXT_SIZE:
        if not history:
            raise ValueError("段落が長すぎます。短く区切って翻訳してください。")
        history = history[1:]
        prompt = tokens()
    if not prompt:
        raise ValueError("翻訳する段落が空です。")
    return Sequence(
        frame["id"], source, prompt, budget, prepare_model_input(source, options, history).codec
    )


class Owner:
    def __init__(self, bridge, commands, emit):
        self.bridge, self.commands, self.emit = bridge, commands, emit
        self.active = {}
        self.slots = deque(range(MAX_ACTIVE))
        self.waiting = None
        self.reserved = 0
        self.stops = [
            bridge.tokenize(value, parse_special=True)
            for value in ("<|plamo:tag|>", "<|plamo:bos|>")
        ]

    def finish(self, state, *, error=None, cancelled=False, stop_length=0):
        if state.slot is not None:
            # Complete all queued device operations before returning this KV
            # ownership. Another sequence's slots and positions stay untouched.
            self.bridge.remove(state.slot)
            self.bridge.synchronize()
            self.active.pop(state.slot)
            self.slots.append(state.slot)
            self.reserved -= state.cost
        self.commands.finished(state.key)
        if cancelled:
            self.emit({"type": "cancelled", "id": state.key})
            return
        if error is None:
            try:
                pieces = state.pieces[:-stop_length] if stop_length else state.pieces
                translated = state.codec.decode(b"".join(pieces).decode("utf-8").strip())
                if len(translated) > 131072:
                    raise ValueError("訳文が長すぎます。原文を保持してください。")
                check_placeholders(state.source, translated)
            except (ValueError, UnicodeError) as failure:
                error = str(failure)
            else:
                self.emit({"type": "result", "id": state.key, "text": translated})
                return
        self.emit({"type": "error", "id": state.key, "message": str(error)[:1024]})

    def admit(self):
        while len(self.active) < MAX_ACTIVE:
            if self.commands.closing:
                return
            if self.waiting is None:
                with self.commands.condition:
                    if not self.commands.pending:
                        return
                    frame = self.commands.pending.popleft()
                    cancelled = frame["id"] in self.commands.cancelled
                if cancelled:
                    self.commands.finished(frame["id"])
                    self.emit({"type": "cancelled", "id": frame["id"]})
                    continue
                try:
                    self.waiting = prepare(self.bridge, frame)
                except (ValueError, TypeError) as error:
                    self.commands.finished(frame["id"])
                    self.emit({"type": "error", "id": frame["id"], "message": str(error)[:1024]})
                    continue
            state = self.waiting
            if state.key in self.commands.cancelled:
                self.finish(state, cancelled=True)
                self.waiting = None
                continue
            if state.cost + self.reserved > CONTEXT_SIZE:
                return  # FCFS: do not skip a large request to admit smaller ones.
            state.slot = self.slots.popleft()
            self.reserved += state.cost
            self.active[state.slot] = state
            self.waiting = None

    def cancellations(self):
        with self.commands.condition:
            cancelled = set(self.commands.cancelled)
        for state in list(self.active.values()):
            if state.key in cancelled:
                self.finish(state, cancelled=True)
        if self.waiting is not None and self.waiting.key in cancelled:
            self.finish(self.waiting, cancelled=True)
            self.waiting = None

    def step(self):
        self.cancellations()
        self.admit()
        if self.commands.closing:
            return False
        entries, logits = [], []
        for state in self.active.values():
            if state.next_token is not None:
                logits.append((len(entries), state))
                entries.append(
                    (state.next_token, len(state.prompt) + len(state.generated) - 1, state.slot, 1)
                )
                state.next_token = None
        # Existing generation goes first. At most one FCFS prefill chunk joins
        # that same decode, bounded by the total logical batch of 256 tokens.
        prefill = next(
            (state for state in self.active.values() if state.cursor < len(state.prompt)), None
        )
        if prefill is not None:
            end = min(len(prefill.prompt), prefill.cursor + BATCH_SIZE - len(entries))
            for position in range(prefill.cursor, end):
                last = position == len(prefill.prompt) - 1
                if last:
                    logits.append((len(entries), prefill))
                entries.append((prefill.prompt[position], position, prefill.slot, int(last)))
            prefill.cursor = end
        if not entries:
            return False
        started = time.perf_counter()
        self.bridge.decode(entries)
        if self.commands.quiet_level:
            self.bridge.synchronize()
        # Sample every logit row before any KV removal can change native state.
        sampled = [(state, self.bridge.sample(index)) for index, state in logits]
        elapsed = time.perf_counter() - started
        for state, token in sampled:
            if state.key in self.commands.cancelled:
                self.finish(state, cancelled=True)
                continue
            if self.bridge.is_eog(token):
                self.finish(state)
                continue
            state.generated.append(token)
            state.pieces.append(self.bridge.piece(token))
            stop = next(
                (
                    len(pattern)
                    for pattern in self.stops
                    if pattern and state.generated[-len(pattern) :] == pattern
                ),
                0,
            )
            if stop:
                self.finish(state, stop_length=stop)
                continue
            if len(state.generated) >= state.budget:
                self.finish(state, error="訳文が生成上限に達しました。原文を保持してください。")
                continue
            try:
                state.output += state.decoder.decode(state.pieces[-1], final=False)
                if len(state.output) > 131072:
                    raise ValueError("訳文が長すぎます。原文を保持してください。")
            except (ValueError, UnicodeError) as error:
                self.finish(state, error=str(error))
                continue
            state.next_token = token
            if state.preview_enabled and time.monotonic() - state.preview_at >= 0.05:
                state.preview_at = time.monotonic()
                try:
                    draft = state.codec.decode(preview_prefix(state.output[:16384]), partial=True)
                except ValueError:
                    state.preview_enabled = False
                else:
                    if draft.strip():
                        self.emit({"type": "preview", "id": state.key, "text": draft[:16384]})
        self.commands.wait_duty(elapsed)
        return True

    def run(self):
        try:
            while not self.commands.closing:
                if not self.step():
                    with self.commands.condition:
                        if not self.commands.closing:
                            self.commands.condition.wait(0.02)
            if self.commands.failure:
                raise self.commands.failure
        finally:
            for state in list(self.active.values()):
                self.finish(state, cancelled=True)
            if self.waiting is not None:
                self.finish(self.waiting, cancelled=True)


def worker_stdio():
    """PyInstaller windowed executables can have sys.stdin/stdout set to None."""
    if sys.stdin is not None and sys.stdout is not None:
        return sys.stdin.buffer, sys.stdout.buffer
    if os.name != "nt":
        raise ProtocolError("推論プロセスの通信ハンドルがありません。")
    import ctypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [ctypes.c_uint32]
    kernel.GetStdHandle.restype = ctypes.c_void_p
    streams = []
    for number, flags, mode in ((-10, os.O_RDONLY, "rb"), (-11, os.O_WRONLY, "wb")):
        handle = kernel.GetStdHandle(number & 0xFFFFFFFF)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise ProtocolError("推論プロセスの通信ハンドルがありません。")
        descriptor = msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
        streams.append(os.fdopen(descriptor, mode, buffering=0))
    if sys.stderr is None:
        handle = kernel.GetStdHandle((-12) & 0xFFFFFFFF)
        if handle and handle != ctypes.c_void_p(-1).value:
            descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
            os.dup2(descriptor, 2)
            if descriptor != 2:
                os.close(descriptor)
            sys.stderr = os.fdopen(os.dup(2), "w", encoding="utf-8", errors="replace", buffering=1)
    return tuple(streams)


def main():
    from .native_llama import Bridge
    from .native_runtime import verified_native_runtime

    if len(sys.argv) != 1:
        return 2
    incoming, outgoing = worker_stdio()
    writer_lock = threading.Lock()

    def emit(frame):
        with writer_lock:
            write_frame(outgoing, frame)

    bridge = None
    try:
        frame = read_frame(incoming)
        if frame is None:
            return 0
        fields(
            frame,
            {"type", "version", "model_path", "dll_dir", "bridge_path", "threads", "quiet_level"},
        )
        if frame["type"] != "init" or integer(frame["version"], VERSION, VERSION) != VERSION:
            raise ProtocolError("推論プロセスのバージョンが一致しません。")
        from pathlib import Path

        runtime = verified_native_runtime()
        if runtime is None:
            raise ProtocolError("検証済みの GPU 推論エンジンがありません。")
        for key, path in (("dll_dir", runtime.directory), ("bridge_path", runtime.bridge)):
            if Path(text(frame[key], 8192)).resolve() != path.resolve():
                raise ProtocolError("推論エンジンのパスが一致しません。")
        model_path = Path(text(frame["model_path"], 8192))
        if not model_path.is_absolute() or not model_path.is_file():
            raise ProtocolError("モデルのパスが正しくありません。")
        threads = integer(frame["threads"], 1, 8)
        commands = Commands(integer(frame["quiet_level"], 0, MAX_QUIET_LEVEL))

        def reader():
            try:
                while not commands.closing:
                    value = read_frame(incoming)
                    if value is None:
                        break
                    commands.receive(value, emit)
            except Exception as error:  # noqa: BLE001 - owned child exits on malformed IPC
                commands.failure = error
            finally:
                with commands.condition:
                    commands.closing = True
                    commands.condition.notify_all()

        threading.Thread(target=reader, name="plamo-native-commands", daemon=True).start()
        bridge = Bridge(runtime.bridge, runtime.directory, model_path, threads=threads)
        emit({"type": "ready", "version": VERSION, "info": bridge.info()})
        Owner(bridge, commands, emit).run()
        return 0
    except Exception:  # noqa: BLE001 - keep native errors and translations off stdout
        try:
            emit(
                {"type": "fatal", "message": "GPU 推論プロセスが終了しました。再準備してください。"}
            )
        except (OSError, ProtocolError):
            pass
        return 1
    finally:
        if bridge is not None:
            bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
