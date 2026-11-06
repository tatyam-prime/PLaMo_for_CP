"""Profile cached model readiness, separating imports, tokenizer and GPU work.

Only already cached weights are read. Each variant runs in a fresh process. The
lazy variant explicitly evaluates all parameters before calling itself ready.
No generation, speculative decoding, or model download is performed.
"""

from __future__ import annotations

import argparse
import builtins
import importlib.machinery
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path


def worker(mode: str, output: Path) -> None:
    started = time.perf_counter()
    timings = {}
    calls = {}

    def timed(name, function):
        def wrapped(*args, **kwargs):
            tick = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                timings[name] = timings.get(name, 0) + time.perf_counter() - tick
                calls[name] = calls.get(name, 0) + 1

        return wrapped

    imported = time.perf_counter()
    import mlx.core as mx
    from mlx import nn
    from mlx_lm import utils

    timings["import_mlx_lm"] = time.perf_counter() - imported
    from plamo_cp.download import cached_snapshot
    from plamo_cp.engine import MODEL_SPECS, Engine

    cache_start = time.perf_counter()
    path = cached_snapshot(MODEL_SPECS["2b"])
    timings["resolve_cached_snapshot"] = time.perf_counter() - cache_start
    if path is None:
        raise RuntimeError("The 2B model is not fully cached; no download is allowed")

    # Include all normal operations unchanged; wrappers record inclusive times.
    for name in ("load_model", "load_tokenizer"):
        setattr(utils, name, timed(name, getattr(utils, name)))
    mx.load = timed("mx_load_safetensors", mx.load)
    mx.eval = timed("mx_eval", mx.eval)
    nn.quantize = timed("build_quantized_layers", nn.quantize)
    original_exec = importlib.machinery.SourceFileLoader.exec_module

    def source_exec(loader, module):
        name = str(getattr(loader, "path", ""))
        if name.endswith("modeling_plamo_mlx.py"):
            result = timed("model_runtime_module", lambda: original_exec(loader, module))()
            module.Model.__init__ = timed("construct_model", module.Model.__init__)
            return result
        if name.endswith("tokenization_plamo.py"):
            result = timed("tokenizer_runtime_module", lambda: original_exec(loader, module))()
            module.AhoCorasick.build = timed(
                "build_tokenizer_aho_corasick", module.AhoCorasick.build
            )
            return result
        return original_exec(loader, module)

    importlib.machinery.SourceFileLoader.exec_module = source_exec
    original_import = builtins.__import__
    numba_depth = 0

    def imported_module(name, *args, **kwargs):
        nonlocal numba_depth
        if name != "numba":
            return original_import(name, *args, **kwargs)
        outer = numba_depth == 0
        numba_depth += 1
        tick = time.perf_counter()
        try:
            return original_import(name, *args, **kwargs)
        finally:
            numba_depth -= 1
            if outer:
                timings["import_numba_in_tokenizer"] = (
                    timings.get("import_numba_in_tokenizer", 0) + time.perf_counter() - tick
                )

    builtins.__import__ = imported_module
    load_start = time.perf_counter()
    model, tokenizer = utils.load(
        path,
        lazy=mode == "lazy",
        trust_remote_code=True,
        tokenizer_config={"trust_remote_code": True},
    )
    timings["load_return"] = time.perf_counter() - load_start
    sync_start = time.perf_counter()
    mx.synchronize()
    timings["post_load_synchronize"] = time.perf_counter() - sync_start
    if mode == "lazy":
        evaluation = time.perf_counter()
        mx.eval(model.parameters())
        mx.synchronize()
        timings["explicit_lazy_evaluation"] = time.perf_counter() - evaluation
    timings["total_first_ready"] = time.perf_counter() - started
    # Test the existing production reuse path without loading or translating again.
    engine = Engine()
    engine._model, engine._tokenizer, engine._loaded = model, tokenizer, "2b"
    reuse = time.perf_counter()
    engine.prepare("2b")
    timings["same_model_prepare_reuse"] = time.perf_counter() - reuse
    report = {
        "mode": mode,
        "timings_seconds": timings,
        "calls": calls,
        "peak_memory_gb": mx.get_peak_memory() / 1e9,
        "device": mx.device_info(),
        "spec": MODEL_SPECS["2b"],
        "platform": platform.platform(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("mlx", "mlx-lm", "transformers", "numba")
        },
        "offline": True,
        "performed_generation": False,
        "notes": "Inclusive stage counters overlap; warm OS file cache, fresh Python process.",
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"mode": mode, "timings_seconds": timings}, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["baseline", "lazy"])
    parser.add_argument("--output", type=Path, default=Path(".local/model-load-profile.json"))
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["PLAMO_DOWNLOAD_MIBPS"] = ".5"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.worker:
        worker(args.worker, args.output)
        return
    results = []
    for mode in ("baseline", "lazy"):
        output = args.output.with_name(f"{args.output.stem}-{mode}.json")
        subprocess.run(
            [sys.executable, __file__, "--worker", mode, "--output", str(output)], check=True
        )
        results.append(json.loads(output.read_text()))
    args.output.write_text(json.dumps({"runs": results}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
