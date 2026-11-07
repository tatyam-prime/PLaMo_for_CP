"""Profile paragraph latency using cached weights and the production prompt.

Never downloads weights or changes app settings. Reports load, prompt encoding,
prefill, generation and decoding separately; leaves real model outputs local.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path


def app_state() -> str:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:44894/api/health", timeout=1) as response:
            return json.load(response).get("engine", {}).get("state", "unknown")
    except OSError:
        return "unavailable"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["0.8b", "2b", "8b"], default="2b")
    parser.add_argument("--output", type=Path, default=Path(".local/paragraph-profile.json"))
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["PLAMO_DOWNLOAD_MIBPS"] = "0.5"
    state = app_state()
    if state not in {"idle", "unavailable"}:
        raise RuntimeError(f"The app is {state}; avoid overlapping GPU work.")

    import mlx.core as mx
    import mlx_lm
    from huggingface_hub.constants import HF_HUB_CACHE

    from plamo_cp.download import cached_snapshot
    from plamo_cp.engine import MODEL_SPECS, Engine

    if cached_snapshot(MODEL_SPECS[args.model], cache_dir=HF_HUB_CACHE) is None:
        raise RuntimeError("The model must already be fully cached; no download allowed.")
    engine = Engine(cache_dir=HF_HUB_CACHE)
    started = time.perf_counter()
    engine.prepare(args.model)
    loaded = time.perf_counter() - started
    underlying = engine._tokenizer._tokenizer
    original_encode = underlying.encode
    original_decode = underlying.decode
    original_stream = mlx_lm.stream_generate
    current = {}

    def measured(name, function):
        def wrapped(*positional, **keywords):
            started = time.perf_counter()
            try:
                return function(*positional, **keywords)
            finally:
                current[name] = current.get(name, 0) + time.perf_counter() - started
                current[name + "_calls"] = current.get(name + "_calls", 0) + 1

        return wrapped

    underlying.encode = measured("encoding_seconds", original_encode)
    underlying.decode = measured("decoding_seconds", original_decode)

    def stream(*positional, **keywords):
        started = time.perf_counter()
        generator = original_stream(*positional, **keywords)
        try:
            for response in generator:
                if "first_token_seconds" not in current:
                    current["first_token_seconds"] = time.perf_counter() - started
                current.update(
                    prompt_tokens=response.prompt_tokens,
                    prefill_seconds=response.prompt_tokens / response.prompt_tps,
                    generation_tokens=response.generation_tokens,
                    generation_tokens_per_second=response.generation_tps,
                    peak_memory_gb=response.peak_memory,
                )
                yield response
        finally:
            generator.close()

    mlx_lm.stream_generate = stream
    short = "You are given two positive integers [[PLM_0]] and [[PLM_1]]. Find their sum."
    medium = (
        "You are given an array of [[PLM_0]] integers. In one operation, you may choose "
        "an element and decrease it by one. You may perform this operation any number "
        "of times, including zero. Determine the minimum number of operations required "
        "to make all elements of the array equal. Print the minimum number of operations."
    )
    long = (
        "Alice and Bob are playing a game on a finite undirected graph. Every vertex "
        "is initially uncolored. On each turn, the current player chooses an uncolored "
        "vertex and assigns it either red or blue. A move is valid only when no two "
        "adjacent vertices have the same color. Alice moves first, and the players "
        "alternate turns. A player who has no valid move loses the game. Both players "
        "know the whole graph and play optimally. Your task is to determine whether "
        "Alice has a winning strategy. The graph may be disconnected, but it has no "
        "self-loops or multiple edges. You must answer the question independently "
        "for each test case. Do not assume that the graph is a tree or that it is "
        "bipartite. In particular, cycles of odd length are allowed."
    )
    history = [
        {
            "source": "This is an independent test case.",
            "translation": "これは独立したテストケースである。",
        },
        {
            "source": "An operation changes one element.",
            "translation": "操作では1つの要素を変更する。",
        },
        {
            "source": "Find the smallest possible answer.",
            "translation": "可能な答えの最小値を求めよ。",
        },
    ]
    results = []
    try:
        for name, text, previous in [
            ("short-warmup", short, []),
            ("short", short, []),
            ("medium", medium, []),
            ("medium-history", medium, history),
            ("long", long, []),
        ]:
            before = app_state()
            if before not in {"idle", "unavailable"}:
                raise RuntimeError("The app started GPU work; abort the benchmark.")
            current = {"case": name, "source_characters": len(text), "history_pairs": len(previous)}
            started = time.perf_counter()
            output = engine.translate(text, model=args.model, history=previous)
            current.update(
                total_seconds=time.perf_counter() - started,
                output_characters=len(output),
                output=output,
                app_state_after=app_state(),
            )
            results.append(current)
            print(
                json.dumps({key: value for key, value in current.items() if key != "output"}),
                flush=True,
            )
    finally:
        underlying.encode = original_encode
        underlying.decode = original_decode
        mlx_lm.stream_generate = original_stream
        engine.unload()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "model": args.model,
                    "load_seconds": loaded,
                    "device": mx.device_info(),
                    "runs": results,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
