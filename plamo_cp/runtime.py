"""Choose the local inference backend without downloading anything."""

import platform


def make_engine():
    if platform.system() == "Windows":
        from .llama_backend import LlamaEngine

        return LlamaEngine()
    from .engine import Engine

    return Engine()
