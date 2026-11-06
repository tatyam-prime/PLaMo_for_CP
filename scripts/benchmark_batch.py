"""Compare real sequential/batched MLX translation without downloads or settings writes."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from benchmark_paragraphs import app_state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["0.8b", "2b"], default="2b")
    parser.add_argument("--output", type=Path, default=Path(".local/batch-profile.json"))
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["PLAMO_DOWNLOAD_MIBPS"] = "0.5"
    if app_state() not in {"idle", "unavailable"}:
        raise RuntimeError("The app is using the GPU; do not overlap inference.")

    import mlx.core as mx
    from huggingface_hub.constants import HF_HUB_CACHE
    from mlx_lm.generate import BatchGenerator
    from mlx_lm.sample_utils import make_sampler

    from plamo_cp.download import cached_snapshot
    from plamo_cp.engine import MODEL_SPECS, Engine, check_placeholders, translation_options

    if cached_snapshot(MODEL_SPECS[args.model], cache_dir=HF_HUB_CACHE) is None:
        raise RuntimeError("The model must be cached; downloading is disabled.")
    engine = Engine(cache_dir=HF_HUB_CACHE)
    engine.prepare(args.model)
    sources = [
        (
            "You are given an array of [[PLM_0]] integers. In one operation, you may choose "
            "an element and decrease it by one. Determine the minimum number of operations "
            "required to make all elements equal. Print the minimum number of operations."
        ),
        (
            "For each test case, the first line contains two positive integers [[PLM_0]] "
            "and [[PLM_1]]. The next line contains a permutation. Each value appears exactly "
            "once. Determine whether the permutation can be sorted using the allowed operations."
        ),
        (
            "Consider a connected undirected graph without self-loops or multiple edges. "
            "A path is a sequence of distinct vertices such that consecutive vertices are "
            "adjacent. Find the maximum number of vertices in a path that starts at vertex [[PLM_0]]."
        ),
        (
            "Output <plm-a>the smallest</plm-a> integer [[PLM_0]] satisfying the condition. "
            "If there is no such integer, output [[PLM_1]]. The answer for each test case "
            "must be printed on a separate line."
        ),
    ]
    tokenizer = engine._tokenizer
    prompts = [
        tokenizer.encode(
            tokenizer.apply_chat_template(
                [{"role": "user", "content": text}],
                **translation_options(text, "English", "Japanese"),
                add_generation_prompt=True,
                tokenize=False,
            ),
            add_special_tokens=False,
        )
        for text in sources
    ]
    engine.translate("Print the answer.", model=args.model)
    results = []

    def verify_idle() -> None:
        if app_state() not in {"idle", "unavailable"}:
            raise RuntimeError("The app started using the GPU; stop the benchmark.")

    try:
        verify_idle()
        started = time.perf_counter()
        outputs, completed = [], []
        for text in sources:
            outputs.append(engine.translate(text, model=args.model))
            completed.append(time.perf_counter() - started)
        baseline = {
            "mode": "sequential",
            "seconds": time.perf_counter() - started,
            "completed_seconds": completed,
            "outputs": outputs,
            "app_state_after": app_state(),
        }
        results.append(baseline)
        print(json.dumps(baseline, ensure_ascii=False), flush=True)
        for size in [2, 4]:
            verify_idle()
            started = time.perf_counter()
            outputs, completed = [], []
            for start in range(0, len(sources), size):
                generator = BatchGenerator(
                    engine._model,
                    stop_tokens=[[token] for token in tokenizer.eos_token_ids],
                    sampler=make_sampler(temp=0),
                    completion_batch_size=size,
                    prefill_batch_size=size,
                )
                uids = generator.insert(prompts[start : start + size], [512] * size)
                tokens = {uid: [] for uid in uids}
                finish = {}
                try:
                    while responses := generator.next_generated():
                        for response in responses:
                            if response.finish_reason != "stop":
                                tokens[response.uid].append(response.token)
                            if response.finish_reason is not None:
                                if response.finish_reason != "stop":
                                    raise RuntimeError("Batch output reached its token limit.")
                                finish[response.uid] = time.perf_counter() - started
                finally:
                    generator.close()
                for index, uid in enumerate(uids):
                    output = tokenizer.decode(tokens[uid]).strip()
                    check_placeholders(sources[start + index], output)
                    outputs.append(output)
                    completed.append(finish[uid])
            report = {
                "mode": f"batch-{size}",
                "seconds": time.perf_counter() - started,
                "completed_seconds": completed,
                "outputs": outputs,
                "exact_matches": [one == two for one, two in zip(baseline["outputs"], outputs)],
                "app_state_after": app_state(),
            }
            results.append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        engine.unload()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {"model": args.model, "device": mx.device_info(), "runs": results},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
