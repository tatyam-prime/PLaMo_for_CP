"""Bounded binary frames for the application's owned native inference child."""

from __future__ import annotations

import json
import struct

VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024
MAX_TEXT_CHARACTERS = 131072
MAX_SOURCE_CHARACTERS = 8000


class ProtocolError(RuntimeError):
    pass


def _read_exact(stream, length, *, initial=False):
    parts, received = [], 0
    while received < length:
        data = stream.read(length - received)
        if not data:
            if initial and received == 0:
                return None
            raise ProtocolError("推論プロセスとの通信が途中で終了しました。")
        parts.append(data)
        received += len(data)
    return b"".join(parts)


def read_frame(stream):
    header = _read_exact(stream, 4, initial=True)
    if header is None:
        return None
    size = struct.unpack("!I", header)[0]
    if not 0 < size <= MAX_FRAME_BYTES:
        raise ProtocolError("推論プロセスの通信サイズが不正です。")
    try:

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ProtocolError("推論プロセスの通信項目が重複しています。")
                value[key] = item
            return value

        def invalid_constant(_value):
            raise ProtocolError("推論プロセスの数値が不正です。")

        frame = json.loads(
            _read_exact(stream, size).decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=invalid_constant,
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ProtocolError("推論プロセスの通信形式が不正です。") from error
    if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
        raise ProtocolError("推論プロセスの通信形式が不正です。")
    return frame


def write_frame(stream, frame):
    try:
        data = json.dumps(
            frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    except (ValueError, TypeError, UnicodeError) as error:
        raise ProtocolError("推論プロセスへ要求を送信できません。") from error
    if not 0 < len(data) <= MAX_FRAME_BYTES:
        raise ProtocolError("推論プロセスの通信サイズが不正です。")
    data = struct.pack("!I", len(data)) + data
    position = 0
    while position < len(data):
        written = stream.write(data[position:])
        if not written:
            raise ProtocolError("推論プロセスへの通信が終了しました。")
        position += written
    stream.flush()


def identifier(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or not value.isascii()
        or not all(character.isalnum() or character in "_-" for character in value)
    ):
        raise ProtocolError("推論プロセスの識別子が不正です。")
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ProtocolError("推論プロセスの数値が不正です。")
    return value


def text(value, maximum=MAX_TEXT_CHARACTERS):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ProtocolError("推論プロセスの文字列が不正です。")
    return value


def fields(frame, required, optional=()):
    if not set(required) <= frame.keys() or not frame.keys() <= set(required) | set(optional):
        raise ProtocolError("推論プロセスの通信項目が不正です。")
