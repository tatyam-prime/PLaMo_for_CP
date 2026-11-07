"""Choose the local inference backend without downloading anything."""

import platform


def make_engine():
    if platform.system() == "Windows":
        from .llama_backend import LlamaEngine
        from .native_runtime import verified_native_runtime

        try:
            runtime = verified_native_runtime()
        except RuntimeError:
            # A damaged native bundle is never loaded. The separate CPU server
            # still verifies its original fixed-SHA archive before execution.
            engine = LlamaEngine()
            engine.native_fallback_reason = "GPU 実行環境を検証できないため CPU を使用します。"
            return engine
        if runtime is not None:
            from .native_engine import NativeEngine

            return NativeEngine(runtime=runtime)
        return LlamaEngine()
    from .engine import Engine

    return Engine()
