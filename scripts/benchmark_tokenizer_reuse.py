"""Compare cached-model switching without downloading or changing model numerics.

Run baseline and reuse modes in separate processes. Compare the resulting token
IDs and translations as well as elapsed prepare time.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import plamo_cp.engine as engine_module

CASES = [
    ("problem", "You are given an array of [[PLM_0]] integers. Find the maximum sum."),
    ("general", "The <plm-a>edge of the page</plm-a> contains a formula [[PLM_0]]."),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["baseline", "reuse"], required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    if args.mode == "baseline":
        engine_module.tokenizer_fingerprint = lambda _: None
    engine = engine_module.Engine()
    if not all(engine.model_available(size) for size in ("0.8b", "2b")):
        parser.error("0.8B と 2B を先に設定画面から準備してください。この比較では取得しません。")
    output = args.output or Path(f".local/tokenizer-reuse-{args.mode}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"mode": args.mode, "sequence": []}
    for size in ("0.8b", "2b", "0.8b"):
        started = time.perf_counter()
        engine.prepare(size)
        elapsed = time.perf_counter() - started
        entry = {"size": size, "prepare_seconds": elapsed, "cases": []}
        for document_type, text in CASES:
            tokens = engine._tokenizer.encode(text, add_special_tokens=False)
            translation = engine.translate(text, model=size, document_type=document_type)
            entry["cases"].append(
                {
                    "type": document_type,
                    "source": text,
                    "token_ids": tokens,
                    "translation": translation,
                }
            )
        report["sequence"].append(entry)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps({"mode": args.mode, "size": size, "prepare_seconds": round(elapsed, 3)}),
            flush=True,
        )


if __name__ == "__main__":
    main()
