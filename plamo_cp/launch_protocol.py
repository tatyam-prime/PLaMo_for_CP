"""Fixed desktop-launch argument grammar; protocol data never becomes a command."""

from __future__ import annotations

from collections.abc import Sequence

OPEN_URL = "plamo-for-cp://open"


def launch_mode(arguments: Sequence[str]) -> str:
    """Reject mixed modes before dispatching any private server entry point."""
    args = list(arguments)
    if not args:
        return "app"
    if args == ["--open-url", OPEN_URL]:
        return "open"
    if args == ["--server-child"]:
        return "server"
    if args == ["--native-worker"]:
        return "native"
    if (
        len(args) == 3
        and args[:2] == ["--server-child", "--port"]
        and args[2].isascii()
        and args[2].isdigit()
        and len(args[2]) <= 5
        and 1 <= int(args[2]) <= 65535
    ):
        return "server"
    raise ValueError("Invalid desktop launch arguments")
