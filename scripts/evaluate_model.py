"""Save a small reproducible CP translation smoke run; no semantic score is inferred."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from plamo_cp.engine import MODEL_SPECS, PLACEHOLDER, PROMPT_VERSION, Engine

# Original synthetic snippets, chosen to exercise conditions and DOM placeholders.
CASES = [
    ("heading-input", "Input"),
    ("heading-output", "Output"),
    ("heading-sample", "Sample Input 1"),
    (
        "array-quantifiers",
        (
            "You are given an array of [[PLM_0]] integers. For every index [[PLM_1]], "
            "you may decrease the corresponding element by one at most once. "
            "Determine whether the array can become non-decreasing."
        ),
    ),
    (
        "negation-distinct",
        (
            "There must not be two distinct vertices with the same color connected by an edge. "
            "A vertex is not adjacent to itself. The graph is not necessarily connected."
        ),
    ),
    (
        "at-least-at-most",
        (
            "Choose at least one and at most [[PLM_0]] elements. "
            "Each chosen element must be strictly greater than [[PLM_1]]. "
            "Print -1 if no such choice exists."
        ),
    ),
    (
        "paired-formatting",
        (
            "Print <plm-a>YES</plm-a> if a valid permutation exists, "
            "and <plm-b>NO</plm-b> otherwise."
        ),
    ),
    (
        "nested-formatting",
        (
            "You must find the <plm-a>lexicographically <plm-b>smallest</plm-b></plm-a> "
            "subsequence of length [[PLM_0]]."
        ),
    ),
    (
        "largest-formatting",
        "Find the <plm-a>lexicographically largest</plm-a> valid permutation of length [[PLM_0]].",
    ),
    (
        "second-smallest-formatting",
        "Output the <plm-a>second smallest</plm-a> element of the array, not its minimum.",
    ),
    (
        "nested-condition-formatting",
        "A path is <plm-a>simple if <plm-b>no vertex appears more than once</plm-b></plm-a>.",
    ),
    (
        "cses-like-dice",
        (
            "Your task is to count the number of ways to construct the sum [[PLM_0]] "
            "by throwing a dice one or more times. Each throw produces an outcome between "
            "[[PLM_1]] and [[PLM_2]]. Two ways are different if the sequences of outcomes differ. "
            "Print the answer modulo [[PLM_3]]."
        ),
    ),
    (
        "cf-like-test-cases",
        (
            "The first line contains the number of test cases [[PLM_0]]. "
            "The description of the test cases follows. It is guaranteed that the sum of "
            "[[PLM_1]] over all test cases does not exceed [[PLM_2]]."
        ),
    ),
    (
        "inequality-parity",
        (
            "For each query, output the smallest integer [[PLM_0]] such that [[PLM_1]]. "
            "If [[PLM_2]] is even, the answer must be odd; otherwise, the answer must be even."
        ),
    ),
    (
        "substring-subsequence",
        (
            "A substring consists of consecutive characters. A subsequence can be obtained "
            "by deleting zero or more characters without changing the order of the remaining "
            "characters. The empty subsequence is allowed, but the empty substring is not."
        ),
    ),
    (
        "numbers-unchanged",
        (
            "The time limit is 2 seconds and the memory limit is 256 megabytes. "
            "There are exactly 100 queries. You may ask no more than 17 questions per query."
        ),
    ),
    (
        "multiple-formula-slots",
        (
            "If [[PLM_0]], replace [[PLM_1]] with [[PLM_2]]. "
            "Otherwise, replace [[PLM_3]] with [[PLM_4]]. "
            "Continue until [[PLM_5]] holds."
        ),
    ),
]


class EvaluationEngine(Engine):
    """Retain generated text when the public engine rejects corrupt placeholders."""

    generated: str | None = None

    def _generate(self, text: str, options: dict[str, Any], cancelled: threading.Event) -> str:
        result = super()._generate(text, options, cancelled)
        self.generated = result
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=list(MODEL_SPECS), default="0.8b")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path(f".local/evaluation-{args.model}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "model": args.model,
        "spec": MODEL_SPECS[args.model],
        "prompt_version": PROMPT_VERSION,
        "platform": platform.platform(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("mlx", "mlx-lm", "transformers", "numba")
        },
        "scope": "Synthetic snippets; placeholder checks only, no automatic semantic grade.",
        "cases": [],
    }
    engine = EvaluationEngine()
    for name, source in [("warmup", "Translate this sentence into Japanese."), *CASES]:
        engine.generated = None
        started = time.perf_counter()
        error = None
        try:
            translation = engine.translate(source, model=args.model)
        except (ValueError, RuntimeError, OSError) as exc:
            translation = engine.generated
            error = f"{type(exc).__name__}: {exc}"
        record = {
            "id": name,
            "source": source,
            "translation": translation,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error": error,
            "source_placeholders": PLACEHOLDER.findall(source),
            "translation_placeholders": PLACEHOLDER.findall(translation or ""),
            "source_format_tags": re.findall(r"</?plm-[a-z]+>", source),
            "translation_format_tags": re.findall(r"</?plm-[a-z]+>", translation or ""),
        }
        if name == "warmup":
            report["warmup"] = record
        else:
            report["cases"].append(record)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {"id": name, "elapsed": record["elapsed_seconds"], "error": error},
                ensure_ascii=False,
            ),
            flush=True,
        )
    print(f"Saved {output}", flush=True)


if __name__ == "__main__":
    main()
