"""Serialized Apple Silicon inference using PFN's official MLX checkpoints."""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import platform
import re
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

log = logging.getLogger(__name__)
PROMPT_VERSION = "pfml3-cp-v10-glossary"
SUPPORTED_MODEL_IDS = ("0.8b", "2b", "8b")
DEFAULT_MODEL_ID = "2b"
PLACEHOLDER = re.compile(r"\[\[PLM_\d+\]\]|</?plm-[A-Za-z0-9]+>")
FORMAT_TAG = re.compile(r"<(\/?)plm-([A-Za-z0-9]+)>")

# Revisions are immutable: model-specific Python is executed only from these
# official checkpoints. Update deliberately when revalidating an upstream release.
MODEL_SPECS: dict[str, dict[str, Any]] = {
    "0.8b": {"name": "PLaMo 3 Translate 0.8B", "weight_gb": 0.98, "min_memory_gb": 4},
    "2b": {"name": "PLaMo 3 Translate 2B", "weight_gb": 2.41, "min_memory_gb": 8},
    "8b": {"name": "PLaMo 3 Translate 8B", "weight_gb": 6.07, "min_memory_gb": 16},
    "31b": {"name": "PLaMo 3 Translate 31B", "weight_gb": 18.66, "min_memory_gb": 32},
}
REVISIONS = {
    "0.8b": "e5fe5b7f26ebaeae48db0ce644b932d1d9b91aaa",
    "2b": "63777ddc6dc80aacacd92a1bf6966105f2f6270c",
    "8b": "9d87684408bdf34500b7d6115c257d83350c1fe5",
    "31b": "e558c8fba45054df70141bc72295024de9b57a44",
}
for _size, _spec in MODEL_SPECS.items():
    _spec["repo"] = f"pfnet/plamo-3-translate-{_size}-MLX-4bit"
    _spec["revision"] = REVISIONS[_size]

LANGUAGES = {
    "English": "en",
    "Japanese": "ja",
    "Russian": "ru",
    "Chinese": "zh",
    "Indonesian": "id",
    "auto": None,
    "en": "en",
    "ja": "ja",
    "ru": "ru",
    "zh": "zh",
    "id": "id",
}
# Keep checked corrections and explicitly requested initial terms. The model
# handles inflections from these canonical terms; do not enumerate plural variants.
TERMS = {
    "modular inverse": "逆元",
    "proper prefix": "真の接頭辞",
    "parallel edge": "多重辺",
    "prefix sum": "累積和",
    "jury": "ジャッジ",
    "subarray": "連続部分配列",
    "increment": "インクリメント",
    "pairwise disjoint": "互いに交差しない",
    "sequence": "列",
    "visualizer": "ビジュアライザ",
    "forest": "森",
}


def default_glossary() -> list[dict[str, str]]:
    """Copy initial editor contents; explicit request contents remain authoritative."""
    return [{"source": source, "target": target} for source, target in TERMS.items()]


def preview_prefix(text: str) -> str:
    """Withhold partial UTF-8 and model-control text from unverified previews."""
    return text.split("<|", 1)[0].removesuffix("<").rstrip("\ufffd")


def check_placeholders(source: str, translated: str) -> None:
    """Allow Japanese word order while rejecting lost/duplicated/new DOM slots."""
    if Counter(PLACEHOLDER.findall(source)) != Counter(PLACEHOLDER.findall(translated)):
        raise ValueError("数式・装飾の識別子が変化しました。この段落は原文を保持してください。")
    if not translated.strip():
        raise ValueError("空の訳文が生成されました。この段落は原文を保持してください。")
    stack: list[str] = []
    for tag in FORMAT_TAG.finditer(translated):
        closing, name = tag.groups()
        if closing:
            if not stack or stack.pop() != name:
                raise ValueError(
                    "書式タグの入れ子が変化しました。この段落は原文を保持してください。"
                )
        else:
            stack.append(name)
    if stack:
        raise ValueError("書式タグが閉じていません。この段落は原文を保持してください。")


def validate_glossary(glossary: list[dict] | None) -> list[dict[str, str]]:
    """Copy bounded user terms without permitting control/protection tokens."""
    if glossary is None:
        return []
    if not isinstance(glossary, list) or len(glossary) > 64:
        raise ValueError("用語集は最大64組まで指定できます。")
    copied: dict[str, dict[str, str]] = {}
    total = 0
    for pair in glossary:
        if not isinstance(pair, dict) or set(pair) != {"source", "target"}:
            raise ValueError("用語集には source と target を指定してください。")
        source, target = pair["source"], pair["target"]
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or not source.strip()
            or not target.strip()
        ):
            raise ValueError("用語集には空でない原語と訳語を指定してください。")
        total += len(source) + len(target)
        if total > 6000:
            raise ValueError("用語集は合計6000文字以内にしてください。")
        for value in (source, target):
            folded = value.casefold()
            if any(token in folded for token in ("<|plamo:", "[[plm_", "<plm", "</plm")):
                raise ValueError(
                    "用語集にモデルの制御トークンや数式・書式の識別子を含めることはできません。"
                )
        source, target = source.strip(), target.strip()
        copied[source.casefold()] = {"source": source, "target": target}
    return list(copied.values())


def validate_source_context(source_context: str) -> str:
    """Reference source text stays bounded and cannot introduce PFML messages."""
    if not isinstance(source_context, str) or len(source_context) > 6000:
        raise ValueError("周辺の英文は6000文字以内にしてください。")
    if "<|plamo:" in source_context.casefold():
        raise ValueError("周辺の英文にモデルの制御トークンを含めることはできません。")
    return source_context if source_context.strip() else ""


def translation_options(
    text: str,
    source_lang: str,
    target_lang: str,
    document_type: str = "problem",
    glossary: list[dict] | None = None,
    source_context: str = "",
) -> dict[str, Any]:
    if document_type not in {"problem", "general"}:
        raise ValueError("文書の種類は problem または general を指定してください。")
    if source_lang not in LANGUAGES or target_lang not in LANGUAGES or target_lang == "auto":
        raise ValueError(
            "対応言語は English/en, Japanese/ja, Russian/ru, Chinese/zh, Indonesian/id です。"
        )
    if "<|plamo:" in text:
        raise ValueError("モデルの制御トークンを含む文章は翻訳できません。")
    surrounding = validate_source_context(source_context)
    configured = (
        default_glossary()
        if glossary is None and document_type == "problem" and LANGUAGES[target_lang] == "ja"
        else validate_glossary(glossary)
    )
    slots = list(dict.fromkeys(PLACEHOLDER.findall(text)))
    # Protection identities are internal. User terms are passed as configured,
    # including singular lemmas that the model can apply to inflected words.
    terms = [{"source": slot, "target": slot} for slot in slots]
    terms.extend(configured)
    options: dict[str, Any] = {
        "source_lang": LANGUAGES[source_lang],
        "target_lang": LANGUAGES[target_lang],
        "context": (
            "This is a competitive programming problem statement. Preserve every condition, "
            "quantifier, number and variable exactly. Tokens of the form [[PLM_0]] are "
            "placeholders for original mathematics. Copy each placeholder "
            "verbatim exactly once. Do not solve the problem or add explanations. "
            "HTML tags mark formatting around words. Preserve each opening and closing "
            "tag exactly, maintaining valid nesting. Translate the text inside tags normally."
        ),
        "glossary": terms,
    }
    if document_type == "general":
        options["context"] = (
            "This is a general document. Translate the text faithfully without adding or "
            "omitting information. Preserve every condition, quantifier, number and variable "
            "exactly. Tokens of the form [[PLM_0]] are placeholders for original mathematics. "
            "Copy each placeholder verbatim exactly once. HTML tags mark formatting around "
            "words. Preserve each opening and closing tag exactly, maintaining valid nesting. "
            "Translate the text inside tags normally."
        )
    if LANGUAGES[target_lang] == "ja":
        options["form"] = "plain"
    if surrounding:
        reference_language = "English source" if LANGUAGES[source_lang] == "en" else "source text"
        options["context"] += (
            f"\n\nUse the following surrounding {reference_language} only to choose consistent "
            "terminology and resolve references. Translate only the current input. "
            "Do not translate, summarize, quote or append this reference passage.\n"
            "Surrounding source:\n" + surrounding
        )
    return options


def validate_history(history: list[dict] | None) -> list[dict[str, str]]:
    """Copy bounded request-local pairs; never retain client history on the engine."""
    if history is None:
        return []
    if not isinstance(history, list) or len(history) > 3:
        raise ValueError("翻訳履歴は最大3組まで指定できます。")
    copied = []
    total = 0
    for pair in history:
        if not isinstance(pair, dict) or set(pair) != {"source", "translation"}:
            raise ValueError("翻訳履歴には source と translation を指定してください。")
        source, translated = pair["source"], pair["translation"]
        if (
            not isinstance(source, str)
            or not isinstance(translated, str)
            or not source.strip()
            or not translated.strip()
        ):
            raise ValueError("翻訳履歴には空でない原文と訳文を指定してください。")
        if "<|plamo:" in source or "<|plamo:" in translated:
            raise ValueError("翻訳履歴にモデルの制御トークンを含めることはできません。")
        total += len(source) + len(translated)
        if total > 6000:
            raise ValueError("翻訳履歴は合計6000文字以内にしてください。")
        copied.append({"source": source, "translation": translated})
    return copied


def download_model(
    size: str = DEFAULT_MODEL_ID,
    progress=None,
    *,
    cache_dir: str | Path | None = None,
    cancelled: threading.Event | None = None,
) -> str:
    """Prefetch weights and the runtime, excluding unrelated figures/server files."""
    from .download import download_snapshot

    if size not in SUPPORTED_MODEL_IDS:
        raise ValueError(f"モデルサイズは {', '.join(SUPPORTED_MODEL_IDS)} から選択してください。")
    spec = MODEL_SPECS[size]
    from .settings import model_cache_dir

    return download_snapshot(
        spec,
        rate_mib=None,
        progress=progress,
        cache_dir=model_cache_dir(cache_dir),
        cancelled=cancelled,
    )


def tokenizer_fingerprint(path: str | Path) -> str | None:
    """Reuse only byte-identical official tokenizer resources and token IDs."""
    directory = Path(path)
    digest = hashlib.sha256()
    resources = (
        "tokenizer.jsonl",
        "tokenization_plamo.py",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "chat_template.jinja",
        "generation_config.json",
    )
    try:
        for name in resources:
            data = (directory / name).read_bytes()
            digest.update(name.encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(data).digest())
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            return None
        binding = {
            key: config.get(key)
            for key in (
                "tokenizer_class",
                "bos_token_id",
                "eos_token_id",
                "pad_token_id",
                "vocab_size",
            )
        }
        digest.update(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except (OSError, ValueError, TypeError):
        # An incomplete/unrecognized checkpoint always takes the original full
        # loader, which remains responsible for reporting its validation error.
        return None
    return digest.hexdigest()


@dataclass
class TranslationWork:
    key: Any
    text: str
    parameters: dict[str, Any]
    cancelled: threading.Event = field(default_factory=threading.Event)
    progress: Callable[[str], None] | None = None


class Engine:
    model_specs: ClassVar[dict] = {model: MODEL_SPECS[model] for model in SUPPORTED_MODEL_IDS}
    storage_model_specs = MODEL_SPECS
    supported_batch_sizes = tuple(range(1, 17))
    supported_translation_previews = True

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        from .settings import model_cache_dir

        self.cache_dir = model_cache_dir(cache_dir)
        self._lock = threading.Lock()
        self._model = None
        self._tokenizer = None
        self._tokenizer_cache: dict[str, Any] = {}
        self._loaded: str | None = None
        self._state = "idle"

    @property
    def status(self) -> dict[str, Any]:
        from .download import resolve_rate

        return {
            "backend": "mlx",
            "loaded_model": self._loaded,
            "state": self._state,
            "download_rate_mib": resolve_rate(),
        }

    def model_available(self, model: str) -> bool:
        from .download import cached_snapshot

        return (
            model in self.model_specs
            and cached_snapshot(MODEL_SPECS[model], cache_dir=self.cache_dir) is not None
        )

    def set_cache_dir(self, cache_dir: str | Path) -> None:
        directory = Path(cache_dir).expanduser().resolve()
        if directory != self.cache_dir:
            self.unload()
            with self._lock:
                self.cache_dir = directory

    def download(self, model: str = DEFAULT_MODEL_ID, progress=None, cancelled=None) -> str:
        """Download files without touching live weights or the inference lock."""
        return download_model(model, progress, cache_dir=self.cache_dir, cancelled=cancelled)

    def prepare(self, model: str = DEFAULT_MODEL_ID, progress=None) -> None:
        if model not in self.model_specs:
            raise ValueError("モデルを選択してください。")
        with self._lock:
            try:
                self._load(model, progress=progress)
            finally:
                self._state = "idle"

    def unload(self, model: str | None = None) -> None:
        """Release only the selected live weights before their files are removed."""
        with self._lock:
            if model is not None and self._loaded != model:
                return
            had_weights = self._model is not None or self._loaded is not None
            self._model = self._tokenizer = None
            self._loaded = None
            self._state = "idle"
            if had_weights:
                import mlx.core as mx

                gc.collect()
                mx.clear_cache()

    def _load(self, size: str, progress=None) -> None:
        if size == self._loaded:
            return
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("この版は Apple Silicon Mac 用です。MLX を実行できません。")
        memory_gb = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) / 1024**3
        required = MODEL_SPECS[size]["min_memory_gb"]
        if memory_gb < required:
            raise RuntimeError(f"{size} はメモリ {required} GB 以上の Mac で使用してください。")
        import mlx.core as mx
        from mlx_lm import load

        from .download import cached_snapshot

        path = cached_snapshot(MODEL_SPECS[size], cache_dir=self.cache_dir)
        if path is None:
            raise RuntimeError("先にモデルをダウンロードしてください。")
        # Release the previous model only after the requested files are ready.
        self._state = "loading"
        self._model = self._tokenizer = None
        self._loaded = None
        gc.collect()
        mx.clear_cache()
        if progress:
            progress({"stage": "loading"})
        log.info("Loading %s", MODEL_SPECS[size]["repo"])
        fingerprint = tokenizer_fingerprint(path)
        tokenizer = self._tokenizer_cache.get(fingerprint) if fingerprint else None
        if tokenizer is not None:
            from mlx_lm.utils import load_model

            self._model, _ = load_model(Path(path), trust_remote_code=True)
            self._tokenizer = tokenizer
            log.info("Reused verified tokenizer resources for %s", size)
        else:
            self._model, self._tokenizer = load(
                path,
                trust_remote_code=True,
                tokenizer_config={"trust_remote_code": True, "local_files_only": True},
            )
        # End output at either PFML3 boundary; never translate a following message.
        self._tokenizer.add_eos_token("<|plamo:tag|>")
        self._tokenizer.add_eos_token("<|plamo:bos|>")
        if fingerprint is not None:
            # The four pinned models currently share one tokenizer. Bound the
            # cache if future model revisions introduce distinct resources.
            if fingerprint not in self._tokenizer_cache and len(self._tokenizer_cache) >= 2:
                self._tokenizer_cache.pop(next(iter(self._tokenizer_cache)))
            self._tokenizer_cache[fingerprint] = self._tokenizer
        self._loaded = size

    def translate(
        self,
        text: str,
        model: str = DEFAULT_MODEL_ID,
        source_lang: str = "English",
        target_lang: str = "Japanese",
        cancelled: threading.Event | None = None,
        document_type: str = "problem",
        history: list[dict] | None = None,
        glossary: list[dict] | None = None,
        source_context: str = "",
    ) -> str:
        if model not in self.model_specs:
            raise ValueError("指定されたモデルサイズには対応していません。")
        options = translation_options(
            text,
            source_lang,
            target_lang,
            document_type=document_type,
            glossary=glossary,
            source_context=source_context,
        )
        previous = validate_history(history)
        if previous:
            options["_history"] = previous
        cancelled = cancelled or threading.Event()
        with self._lock:
            try:
                if cancelled.is_set():
                    raise InterruptedError("翻訳を停止しました。")
                self._load(model)
                if cancelled.is_set():
                    raise InterruptedError("翻訳を停止しました。")
                self._state = "translating"
                result = self._generate(text, options, cancelled)
                check_placeholders(text, result)
                return result
            finally:
                self._state = "idle"

    def _prepare_prompt(self, text: str, options: dict[str, Any]) -> tuple[list[int], int]:
        template_options = dict(options)
        previous = list(template_options.pop("_history", []))
        max_tokens = None
        while True:
            messages = []
            for pair in previous:
                messages.extend(
                    [
                        {"role": "user", "content": pair["source"]},
                        {"role": "assistant", "content": pair["translation"]},
                    ]
                )
            messages.append({"role": "user", "content": text})
            prompt = self._tokenizer.apply_chat_template(
                messages,
                **template_options,
                add_generation_prompt=True,
                tokenize=False,
            )
            # The official template already includes BOS; do not prepend a second one.
            tokens = self._tokenizer.encode(prompt, add_special_tokens=False)
            # Keep the current output budget when dropping old context.
            if max_tokens is None:
                max_tokens = max(512, len(self._tokenizer.encode(text)) * 3 + 128)
            if len(tokens) + max_tokens <= 32768:
                break
            if previous:
                previous = previous[1:]
                continue
            raise ValueError(
                "段落が長すぎます。入力と出力を合わせて 32768 トークン以内にしてください。"
            )
        return tokens, max_tokens

    def iter_work(
        self,
        model: str,
        take: Callable[[int], list[TranslationWork]],
        capacity: Callable[[], int],
        *,
        cancelled: threading.Event | None = None,
        prefill_batch_size: int = 1,
        prefill_step_size: int = 256,
    ) -> Iterator[tuple[TranslationWork, str | ValueError | InterruptedError]]:
        """Continuously admit independent work and emit each verified completion."""
        if model not in self.model_specs:
            raise ValueError("指定されたモデルサイズには対応していません。")
        cancelled = cancelled or threading.Event()
        generator = None
        pending: dict[int, TranslationWork] = {}
        generated: dict[int, list[int]] = {}
        detokenizers: dict[int, Any] = {}
        preview_times: dict[int, float] = {}
        preview_texts: dict[int, str] = {}
        preview_disabled: set[int] = set()
        loaded = False

        def discard_preview(uid):
            detokenizers.pop(uid, None)
            preview_times.pop(uid, None)
            preview_texts.pop(uid, None)
            preview_disabled.discard(uid)

        def publish_preview(uid, token):
            work = pending[uid]
            if work.progress is None or work.cancelled.is_set() or uid in preview_disabled:
                return
            try:
                decoder = detokenizers.get(uid)
                if decoder is not None:
                    decoder.add_token(token)
                now = time.monotonic()
                if now - preview_times.get(uid, float("-inf")) < 0.05:
                    return
                preview_times[uid] = now
                # The official property returns a separate reset detokenizer
                # per UID. Access .text only at publication time: the generic
                # streaming decoder otherwise repeatedly decodes the full line.
                text = preview_prefix(
                    decoder.text if decoder is not None else self._tokenizer.decode(generated[uid])
                )
                if text and text != preview_texts.get(uid):
                    work.progress(text)
                    preview_texts[uid] = text
            except Exception:  # noqa: BLE001 - optional callbacks cannot stop final inference
                # Preview decoding/callbacks are optional. A failure here must
                # not discard a valid final translation or another client's job.
                preview_disabled.add(uid)
                detokenizers.pop(uid, None)
                log.debug("Disabled a translation preview after its callback failed")

        with self._lock:
            try:
                while True:
                    if cancelled.is_set():
                        raise InterruptedError("翻訳を停止しました。")
                    removed = [uid for uid, work in pending.items() if work.cancelled.is_set()]
                    if removed:
                        generator.remove(removed)
                        for uid in removed:
                            work = pending.pop(uid)
                            generated.pop(uid)
                            discard_preview(uid)
                            yield work, InterruptedError("翻訳を停止しました。")
                    limit = max(1, int(capacity()))
                    if generator is not None:
                        generator.completion_batch_size = limit
                        generator.prefill_batch_size = min(limit, prefill_batch_size)
                    # Start the very first prompt immediately. Once work is in
                    # flight, fill available slots without an accumulation timer.
                    count = 1 if generator is None else max(0, limit - len(pending))
                    admitted = take(count) if count else []
                    if len(admitted) > count:
                        raise RuntimeError("翻訳キューの同時処理数が一致しません。")
                    for work in admitted:
                        if work.cancelled.is_set():
                            yield work, InterruptedError("翻訳を停止しました。")
                            continue
                        try:
                            if not isinstance(work.text, str):
                                raise TypeError("翻訳する段落には文字列を指定してください。")
                            parameters = dict(work.parameters)
                            previous = validate_history(parameters.pop("history", None))
                            options = translation_options(work.text, **parameters)
                            if previous:
                                options["_history"] = previous
                        except (ValueError, TypeError) as error:
                            yield work, ValueError(str(error))
                            continue
                        if not loaded:
                            self._load(model)
                            loaded = True
                            self._state = "translating"
                        try:
                            prompt, budget = self._prepare_prompt(work.text, options)
                        except ValueError as error:
                            yield work, error
                            continue
                        if work.cancelled.is_set():
                            yield work, InterruptedError("翻訳を停止しました。")
                            continue
                        if generator is None:
                            import mlx.core as mx
                            from mlx_lm.generate import BatchGenerator
                            from mlx_lm.sample_utils import make_sampler

                            generator = BatchGenerator(
                                self._model,
                                stop_tokens=[[token] for token in self._tokenizer.eos_token_ids],
                                sampler=make_sampler(temp=0),
                                completion_batch_size=limit,
                                prefill_batch_size=min(limit, prefill_batch_size),
                                prefill_step_size=prefill_step_size,
                                stream=mx.default_stream(mx.default_device()),
                            )
                        uids = generator.insert([prompt], max_tokens=[budget])
                        if len(uids) != 1 or uids[0] in pending:
                            raise RuntimeError("翻訳エンジンの段落識別子が一致しません。")
                        uid = uids[0]
                        pending[uid] = work
                        generated[uid] = []
                        if work.progress is not None:
                            try:
                                detokenizers[uid] = self._tokenizer.detokenizer
                            except AttributeError:
                                # Lightweight test/compatible tokenizers can
                                # fall back to bounded-frequency full decoding.
                                detokenizers[uid] = None
                            except Exception:  # noqa: BLE001 - preview setup is optional
                                preview_disabled.add(uid)
                    if not pending:
                        if admitted:
                            continue
                        return
                    prompt_events, responses = generator.next()
                    if cancelled.is_set():
                        raise InterruptedError("翻訳を停止しました。")
                    if not prompt_events and not responses:
                        for uid, work in list(pending.items()):
                            yield (
                                work,
                                ValueError(
                                    "訳文の終了を確認できませんでした。原文を保持してください。"
                                ),
                            )
                        pending.clear()
                        generated.clear()
                        detokenizers.clear()
                        preview_times.clear()
                        preview_texts.clear()
                        preview_disabled.clear()
                        continue
                    for response in responses:
                        work = pending.get(response.uid)
                        if work is None:
                            raise RuntimeError("翻訳エンジンの段落識別子が一致しません。")
                        if response.finish_reason != "stop":
                            generated[response.uid].append(response.token)
                        if response.finish_reason is None:
                            publish_preview(response.uid, response.token)
                            continue
                        pending.pop(response.uid)
                        tokens = generated.pop(response.uid)
                        discard_preview(response.uid)
                        if work.cancelled.is_set():
                            yield work, InterruptedError("翻訳を停止しました。")
                        elif response.finish_reason != "stop":
                            yield (
                                work,
                                ValueError(
                                    "訳文が生成上限に達しました。原文を保持し、段落を短くしてください。"
                                ),
                            )
                        else:
                            try:
                                translated = self._tokenizer.decode(tokens).strip()
                                check_placeholders(work.text, translated)
                            except ValueError as error:
                                yield work, error
                            else:
                                yield work, translated
            finally:
                if generator is not None:
                    generator.close()
                self._state = "idle"

    def iter_translate(
        self,
        texts: list[str],
        model: str = DEFAULT_MODEL_ID,
        source_lang: str = "English",
        target_lang: str = "Japanese",
        cancelled: threading.Event | None = None,
        document_type: str = "problem",
        history: list[dict] | None = None,
        glossary: list[dict] | None = None,
        source_context: str = "",
        *,
        batch_size: int = 8,
    ) -> Iterator[tuple[int, str | ValueError]]:
        if not isinstance(texts, list) or not 1 <= len(texts) <= 64:
            raise ValueError("翻訳する段落は1〜64件にしてください。")
        if not isinstance(batch_size, int) or not 1 <= batch_size <= 8:
            raise ValueError("まとめて翻訳する段落は1〜8件にしてください。")
        cancelled = cancelled or threading.Event()
        parameters = {
            "source_lang": source_lang,
            "target_lang": target_lang,
            "document_type": document_type,
            "history": history,
            "glossary": glossary,
            "source_context": source_context,
        }
        queue = [
            TranslationWork(index, text, parameters, cancelled) for index, text in enumerate(texts)
        ]

        def take(count):
            result = queue[:count]
            del queue[:count]
            return result

        stream = self.iter_work(
            model, take, lambda: min(batch_size, len(texts)), cancelled=cancelled
        )
        try:
            for work, result in stream:
                if isinstance(result, InterruptedError):
                    raise result
                yield work.key, result
        finally:
            stream.close()

    def translate_batch(
        self,
        texts: list[str],
        model: str = DEFAULT_MODEL_ID,
        source_lang: str = "English",
        target_lang: str = "Japanese",
        cancelled: threading.Event | None = None,
        document_type: str = "problem",
        history: list[dict] | None = None,
        glossary: list[dict] | None = None,
        source_context: str = "",
    ) -> list[str | ValueError]:
        """Backward-compatible ordered wrapper over the completion stream."""
        if not isinstance(texts, list) or not 1 <= len(texts) <= 8:
            raise ValueError("まとめて翻訳する段落は1〜8件にしてください。")
        results: list[str | ValueError | None] = [None] * len(texts)
        for index, result in self.iter_translate(
            texts,
            model,
            source_lang,
            target_lang,
            cancelled,
            document_type,
            history,
            glossary,
            source_context,
            batch_size=len(texts),
        ):
            results[index] = result
        return results

    def _generate(self, text: str, options: dict[str, Any], cancelled: threading.Event) -> str:
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler

        tokens, max_tokens = self._prepare_prompt(text, options)
        stream = stream_generate(
            self._model,
            self._tokenizer,
            prompt=tokens,
            max_tokens=max_tokens,
            sampler=make_sampler(temp=0),
        )
        parts: list[str] = []
        final = None
        try:
            for response in stream:
                if cancelled.is_set():
                    raise InterruptedError("翻訳を停止しました。")
                parts.append(response.text)
                final = response
        finally:
            stream.close()
        if final is None or final.finish_reason != "stop":
            raise RuntimeError("訳文が生成上限に達しました。原文を保持し、段落を短くしてください。")
        return "".join(parts).strip()
