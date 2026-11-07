"""Inference contracts checked without loading weights or initializing Metal."""

from __future__ import annotations

import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from plamo_cp.engine import (
    Engine,
    check_placeholders,
    preview_prefix,
    translation_options,
    validate_history,
)

CANONICAL_TERMS = [
    {"source": "modular inverse", "target": "逆元"},
    {"source": "proper prefix", "target": "真の接頭辞"},
    {"source": "parallel edge", "target": "多重辺"},
    {"source": "prefix sum", "target": "累積和"},
    {"source": "jury", "target": "ジャッジ"},
    {"source": "subarray", "target": "連続部分配列"},
    {"source": "increment", "target": "インクリメント"},
    {"source": "pairwise disjoint", "target": "互いに交差しない"},
    {"source": "sequence", "target": "列"},
    {"source": "visualizer", "target": "ビジュアライザ"},
    {"source": "forest", "target": "森"},
    {"source": "node", "target": "頂点"},
]


def test_japanese_options_include_canonical_corrections_and_unique_slots():
    options = translation_options(
        "An ARRAY [[PLM_0]] is a permutation of [[PLM_1]]. Repeat [[PLM_0]].",
        "English",
        "Japanese",
    )
    assert options["source_lang"] == "en"
    assert options["target_lang"] == "ja"
    assert options["form"] == "plain"
    glossary = {term["source"]: term["target"] for term in options["glossary"]}
    assert glossary == {
        "[[PLM_0]]": "[[PLM_0]]",
        "[[PLM_1]]": "[[PLM_1]]",
        **{pair["source"]: pair["target"] for pair in CANONICAL_TERMS},
    }
    assert len(options["glossary"]) == len(glossary)
    assert "Do not solve" in options["context"]


def test_explicit_glossary_passes_all_configured_words_without_filtering():
    options = translation_options(
        "arraylike and integers",
        "en",
        "ja",
        document_type="general",
        glossary=[{"source": "array", "target": "配列"}, {"source": "integers", "target": "整数"}],
    )
    assert options["glossary"] == [
        {"source": "array", "target": "配列"},
        {"source": "integers", "target": "整数"},
    ]


def test_non_japanese_target_omits_japanese_style_and_cp_terms():
    options = translation_options("array [[PLM_12]]", "auto", "en")
    assert options["source_lang"] is None
    assert options["target_lang"] == "en"
    assert "form" not in options
    assert options["glossary"] == [{"source": "[[PLM_12]]", "target": "[[PLM_12]]"}]


@pytest.mark.parametrize(
    "language,code",
    [("Chinese", "zh"), ("zh", "zh"), ("Indonesian", "id"), ("id", "id"), ("auto", None)],
)
def test_chinese_indonesian_and_auto_use_official_language_codes(language, code):
    options = translation_options("给定整数 [[PLM_0]]。", language, "Japanese")
    assert options["source_lang"] == code and options["target_lang"] == "ja"
    assert options["form"] == "plain"
    assert options["glossary"] == [{"source": "[[PLM_0]]", "target": "[[PLM_0]]"}, *CANONICAL_TERMS]
    assert (
        options["context"]
        == translation_options("给定整数 [[PLM_0]]。", "English", "Japanese")["context"]
    )


def test_non_english_surrounding_context_is_not_mislabeled_as_english():
    for language in ("Chinese", "Indonesian", "auto"):
        options = translation_options("文章。", language, "ja", source_context="周围的原文。")
        assert "surrounding source text" in options["context"]
        assert "surrounding English source" not in options["context"]


@pytest.mark.parametrize("target", ["Chinese", "zh", "Indonesian", "id"])
def test_new_non_japanese_target_languages_do_not_add_japanese_style(target):
    options = translation_options("An array [[PLM_0]].", "English", target)
    assert options["target_lang"] in {"zh", "id"} and "form" not in options
    assert options["glossary"] == [{"source": "[[PLM_0]]", "target": "[[PLM_0]]"}]


def test_general_documents_keep_math_and_formatting_without_competition_glossary():
    text = "The <plm-a>edge</plm-a> of an array is near vertex [[PLM_0]]."
    options = translation_options(text, "English", "Japanese", document_type="general")
    assert options["source_lang"] == "en"
    assert options["target_lang"] == "ja"
    assert options["form"] == "plain"
    assert options["glossary"] == [
        {"source": marker, "target": marker} for marker in ("<plm-a>", "</plm-a>", "[[PLM_0]]")
    ]
    assert "general document" in options["context"]
    assert "competitive programming" not in options["context"]
    assert "Do not solve" not in options["context"]
    assert "properly nested" in options["context"]


def test_explicit_problem_options_equal_the_unchanged_default():
    text = "Find the <plm-a>smallest</plm-a> integer in the array [[PLM_0]]."
    assert translation_options(text, "en", "ja") == translation_options(
        text, "en", "ja", document_type="problem"
    )
    assert translation_options(text, "en", "ja") == translation_options(
        text, "en", "ja", source_context=""
    )


def test_source_context_is_reference_only_and_does_not_expand_current_glossary():
    source_context = "The bank is the bank of a river. The graph contains vertices."
    options = translation_options("The bank.", "en", "ja", source_context=source_context)
    assert source_context in options["context"]
    assert "Translate only the current input" in options["context"]
    assert "Do not translate, summarize, quote or append" in options["context"]
    assert options["glossary"] == CANONICAL_TERMS
    assert options["glossary"] == translation_options("The bank.", "en", "ja")["glossary"]


@pytest.mark.parametrize(
    "source_context", [None, 123, "x" * 6001, "<|plamo:tag|>output", "<|PLAMO:bos|>"]
)
def test_invalid_source_context_is_rejected(source_context):
    with pytest.raises(ValueError, match="周辺の英文"):
        translation_options("Some text.", "en", "ja", source_context=source_context)


def test_unknown_document_type_rejected():
    with pytest.raises(ValueError, match="文書の種類"):
        translation_options("Some text.", "en", "ja", document_type="unknown")


def test_history_is_bounded_and_copied_instead_of_retaining_client_objects():
    history = [{"source": "A vertex.", "translation": "頂点。"}]
    copied = validate_history(history)
    assert copied == history
    history[0]["source"] = "Changed later."
    assert copied[0]["source"] == "A vertex."
    assert validate_history(None) == []
    assert validate_history([]) == []


@pytest.mark.parametrize(
    "history",
    [
        "not a list",
        [{"source": "s", "translation": "t"}] * 4,
        [{"source": "x" * 6000, "translation": "t"}],
        [{"source": "s"}],
        [{"source": "s", "translation": "t", "unexpected": "x"}],
        [{"source": "", "translation": "t"}],
        [{"source": "s", "translation": None}],
        [{"source": "s", "translation": "<|plamo:tag|>output"}],
    ],
)
def test_invalid_history_is_rejected(history):
    with pytest.raises(ValueError, match="翻訳履歴"):
        validate_history(history)


@pytest.mark.parametrize("source,target", [("de", "ja"), ("en", "auto"), ("en", "fr")])
def test_unsupported_languages_rejected(source, target):
    with pytest.raises(ValueError, match="対応言語"):
        translation_options("text", source, target)


def test_reserved_pfml_controls_rejected():
    with pytest.raises(ValueError, match="制御トークン"):
        translation_options("text <|plamo:tag|>output", "en", "ja")


def test_placeholder_reordering_is_allowed_for_japanese_grammar():
    check_placeholders("[[PLM_0]] followed by [[PLM_1]]", "[[PLM_1]] の前は [[PLM_0]]")


@pytest.mark.parametrize(
    "translated",
    [
        "[[PLM_0]] のみ",  # lost slot
        "[[PLM_0]] [[PLM_1]] [[PLM_1]]",  # duplicated slot
        "[[PLM_0]] [[PLM_1]] [[PLM_2]]",  # unknown slot
        "[[PLM_0]] [[PLM_01]]",  # identifier changed
    ],
)
def test_changed_placeholder_multiplicity_rejected(translated):
    with pytest.raises(ValueError, match="識別子"):
        check_placeholders("[[PLM_0]] and [[PLM_1]]", translated)


def test_empty_translation_rejected():
    with pytest.raises(ValueError, match="空の訳文"):
        check_placeholders("Hello.", " \n\t")


def test_formatting_tags_and_math_slots_both_enter_identity_glossary():
    options = translation_options(
        "Find the <plm-a>smallest <plm-b>integer</plm-b></plm-a> above [[PLM_0]].",
        "en",
        "ja",
    )
    glossary = {term["source"]: term["target"] for term in options["glossary"]}
    for marker in ["<plm-a>", "</plm-a>", "<plm-b>", "</plm-b>", "[[PLM_0]]"]:
        assert glossary[marker] == marker
    assert "integer" not in glossary
    assert all(glossary[pair["source"]] == pair["target"] for pair in CANONICAL_TERMS)


def test_valid_formatting_groups_may_move_with_japanese_word_order():
    check_placeholders(
        "<plm-a>smallest</plm-a> element above [[PLM_0]]",
        "[[PLM_0]] より大きい <plm-a>最小の</plm-a> 要素",
    )


@pytest.mark.parametrize(
    "translated",
    [
        "<plm-a>最小",  # missing closing tag
        "<plm-a>最小</plm-a></plm-a>",  # duplicated closing tag
        "<plm-a>最小</plm-b>",  # renamed closing tag
        "</plm-a>最小<plm-a>",  # reversed pair with unchanged counts
        "<plm-a>最小</plm-a><plm-b>余分</plm-b>",  # new formatting group
    ],
)
def test_corrupt_formatting_tags_rejected(translated):
    with pytest.raises(ValueError):
        check_placeholders("<plm-a>smallest</plm-a>", translated)


def test_crossed_nested_tags_are_rejected_even_when_every_tag_is_present():
    source = "<plm-a>lexicographically <plm-b>smallest</plm-b></plm-a>"
    crossed = "<plm-a>辞書順で <plm-b>最小</plm-a></plm-b>"
    with pytest.raises(ValueError):
        check_placeholders(source, crossed)


class FakeTokenizer:
    bos_token = "<|plamo:bos|>"

    def __init__(self):
        self.encode_calls = []
        self.template_calls = []
        self.prompt_token_count = 10

    def apply_chat_template(self, messages, **options):
        self.template_calls.append((messages, options))
        return self.bos_token + "translation prompt"

    def encode(self, text, **kwargs):
        self.encode_calls.append((text, kwargs))
        if text.startswith(self.bos_token):
            return list(range(self.prompt_token_count))
        return list(range(len(text.split())))


class FakeStream:
    def __init__(self, responses, cancelled=None, error=None):
        self.responses = responses
        self.cancelled = cancelled
        self.error = error
        self.closed = False

    def __iter__(self):
        for text, finish_reason in self.responses:
            if self.cancelled is not None:
                self.cancelled.set()
            yield SimpleNamespace(text=text, finish_reason=finish_reason)
        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True


@pytest.fixture
def inference(monkeypatch):
    """Replace only inference imports; production Engine logic still runs."""
    engine = Engine()
    engine._tokenizer = FakeTokenizer()
    engine._model = object()
    loaded = []
    monkeypatch.setattr(engine, "_load", loaded.append)
    calls = []
    stream = FakeStream([("配列", None), ("を求めよ。", "stop")])

    def stream_generate(*args, **kwargs):
        calls.append((args, kwargs))
        return stream

    mlx_lm = ModuleType("mlx_lm")
    mlx_lm.stream_generate = stream_generate
    sample_utils = ModuleType("mlx_lm.sample_utils")
    sample_utils.make_sampler = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "mlx_lm", mlx_lm)
    monkeypatch.setitem(sys.modules, "mlx_lm.sample_utils", sample_utils)
    return SimpleNamespace(engine=engine, stream=stream, calls=calls, loaded=loaded)


def test_translate_includes_final_segment_and_uses_greedy_decoding(inference):
    assert inference.engine.translate("Find the array.") == "配列を求めよ。"
    assert inference.stream.closed
    assert inference.loaded == ["2b"]
    assert inference.engine.status["state"] == "idle"
    kwargs = inference.calls[0][1]
    assert kwargs["sampler"] == {"temp": 0}
    assert kwargs["max_tokens"] >= 512
    messages, options = inference.engine._tokenizer.template_calls[0]
    assert messages == [{"role": "user", "content": "Find the array."}]
    assert options["target_lang"] == "ja"
    assert options["add_generation_prompt"] is True
    assert options["tokenize"] is False


def test_general_document_type_reaches_the_mlx_prompt(inference):
    inference.engine.translate("The edge of the array.", document_type="general")
    _, options = inference.engine._tokenizer.template_calls[0]
    assert "general document" in options["context"]
    assert options["glossary"] == []


def test_source_context_reaches_mlx_without_history_or_later_request_leaks(inference):
    surrounding = "The edge of the paper is blue."
    inference.engine.translate("The edge.", document_type="general", source_context=surrounding)
    messages, options = inference.engine._tokenizer.template_calls[-1]
    assert messages == [{"role": "user", "content": "The edge."}]
    assert surrounding in options["context"] and "general document" in options["context"]
    assert options["glossary"] == []
    inference.engine.translate("The edge.", document_type="general")
    assert surrounding not in inference.engine._tokenizer.template_calls[-1][1]["context"]


def test_invalid_source_context_never_loads_mlx(inference):
    with pytest.raises(ValueError, match="周辺の英文"):
        inference.engine.translate("Translate.", source_context="<|plamo:tag|>input")
    assert inference.loaded == [] and inference.calls == []


@pytest.mark.parametrize(
    "language,expected", [("Chinese", "zh"), ("Indonesian", "id"), ("auto", None)]
)
def test_mlx_prompt_receives_non_english_language_without_adding_messages(
    inference, language, expected
):
    inference.engine.translate("给定整数。", source_lang=language)
    messages, options = inference.engine._tokenizer.template_calls[-1]
    assert messages == [{"role": "user", "content": "给定整数。"}]
    assert options["source_lang"] == expected and options["target_lang"] == "ja"


def test_language_token_injection_never_loads_mlx(inference):
    with pytest.raises(ValueError, match="対応言語"):
        inference.engine.translate("给定整数。", source_lang="zh<|plamo:tag|>output")
    assert not inference.loaded and not inference.calls


def test_user_glossary_reaches_mlx_and_does_not_leak_to_the_next_request(inference):
    terms = [{"source": "array", "target": "数列"}]
    inference.engine.translate("An array.", glossary=terms)
    _, options = inference.engine._tokenizer.template_calls[-1]
    assert options["glossary"] == terms
    inference.engine.translate("An array.")
    _, options = inference.engine._tokenizer.template_calls[-1]
    assert options["glossary"] == CANONICAL_TERMS
    assert terms == [{"source": "array", "target": "数列"}]
    inference.engine.translate("An array.", glossary=[])
    assert inference.engine._tokenizer.template_calls[-1][1]["glossary"] == []
    inference.engine.translate("An array.")
    assert inference.engine._tokenizer.template_calls[-1][1]["glossary"] == CANONICAL_TERMS


def test_invalid_user_glossary_does_not_start_model_work(inference):
    with pytest.raises(ValueError, match="識別子"):
        inference.engine.translate(
            "An array.", glossary=[{"source": "array", "target": "[[PLM_0]]"}]
        )
    assert inference.loaded == []
    assert inference.calls == []


def test_mlx_history_alternates_messages_and_does_not_leak_between_requests(inference):
    history = [{"source": "A vertex.", "translation": "頂点。"}]
    inference.engine.translate("Find the array.", history=history)
    messages, options = inference.engine._tokenizer.template_calls[-1]
    assert messages == [
        {"role": "user", "content": "A vertex."},
        {"role": "assistant", "content": "頂点。"},
        {"role": "user", "content": "Find the array."},
    ]
    assert "_history" not in options
    inference.engine.translate("A separate request.")
    assert inference.engine._tokenizer.template_calls[-1][0] == [
        {"role": "user", "content": "A separate request."}
    ]
    assert history == [{"source": "A vertex.", "translation": "頂点。"}]


def test_mlx_context_budget_drops_oldest_history_and_keeps_current_output_budget(inference):
    tokenizer = inference.engine._tokenizer

    def template(messages, **options):
        tokenizer.template_calls.append((messages, options))
        return tokenizer.bos_token + str(len(messages))

    def encode(text, **kwargs):
        if text.startswith(tokenizer.bos_token):
            return list(range(int(text.removeprefix(tokenizer.bos_token)) * 10000))
        return [42]

    tokenizer.apply_chat_template = template
    tokenizer.encode = encode
    history = [{"source": f"source {index}", "translation": f"訳文{index}"} for index in range(3)]
    inference.engine.translate("Current paragraph.", history=history)
    attempts = tokenizer.template_calls
    assert [len(messages) for messages, _ in attempts] == [7, 5, 3]
    assert attempts[-1][0] == [
        {"role": "user", "content": "source 2"},
        {"role": "assistant", "content": "訳文2"},
        {"role": "user", "content": "Current paragraph."},
    ]
    assert inference.calls[0][1]["max_tokens"] == 512
    assert len(history) == 3


def test_mlx_oversized_current_text_is_rejected_after_all_history_is_dropped(inference):
    inference.engine._tokenizer.prompt_token_count = 32768
    with pytest.raises(ValueError, match="32768"):
        inference.engine.translate(
            "Current paragraph.", history=[{"source": "Old.", "translation": "前。"}]
        )
    assert [len(messages) for messages, _ in inference.engine._tokenizer.template_calls] == [3, 1]
    assert inference.calls == []


def test_invalid_document_type_does_not_start_model_work(inference):
    with pytest.raises(ValueError, match="文書の種類"):
        inference.engine.translate("Translate.", document_type="unknown")
    assert inference.loaded == []
    assert inference.calls == []


def test_chat_template_bos_is_not_added_a_second_time(inference):
    inference.engine.translate("Translate.")
    prompt, kwargs = inference.engine._tokenizer.encode_calls[0]
    assert prompt.startswith("<|plamo:bos|>")
    assert kwargs["add_special_tokens"] is False


@pytest.mark.parametrize("responses", [[("partial", "length")], []])
def test_truncated_or_missing_generation_never_returns_success(inference, responses):
    inference.stream.responses = responses
    with pytest.raises(RuntimeError, match="生成上限"):
        inference.engine.translate("Translate.")
    assert inference.stream.closed
    assert inference.engine.status["state"] == "idle"


def test_cancellation_before_loading_avoids_model_work(inference):
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError, match="停止"):
        inference.engine.translate("Translate.", cancelled=cancelled)
    assert inference.loaded == []
    assert inference.calls == []
    assert inference.engine.status["state"] == "idle"


def test_cancellation_during_generation_closes_stream(inference):
    cancelled = threading.Event()
    inference.stream.cancelled = cancelled
    with pytest.raises(InterruptedError, match="停止"):
        inference.engine.translate("Translate.", cancelled=cancelled)
    assert inference.stream.closed
    assert inference.engine.status["state"] == "idle"


def test_generation_error_closes_stream_and_restores_status(inference):
    inference.stream.error = RuntimeError("inference failed")
    with pytest.raises(RuntimeError, match="inference failed"):
        inference.engine.translate("Translate.")
    assert inference.stream.closed
    assert inference.engine.status["state"] == "idle"


def test_context_budget_is_checked_before_generation(inference):
    inference.engine._tokenizer.prompt_token_count = 32768
    with pytest.raises(ValueError, match="32768"):
        inference.engine.translate("Translate.")
    assert inference.calls == []
    assert inference.engine.status["state"] == "idle"


def test_corrupt_placeholder_output_is_rejected_after_generation(inference):
    inference.stream.responses = [("数式が欠落した訳文。", "stop")]
    with pytest.raises(ValueError, match="識別子"):
        inference.engine.translate("Given [[PLM_0]].")
    assert inference.stream.closed
    assert inference.engine.status["state"] == "idle"


def test_unknown_model_does_not_begin_loading(inference):
    with pytest.raises(ValueError, match="モデルサイズ"):
        inference.engine.translate("Translate.", model="120b")
    assert inference.loaded == []
    assert inference.calls == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("途中訳", "途中訳"),
        ("途中訳\ufffd\ufffd", "途中訳"),
        ("途中訳<", "途中訳"),
        ("途中訳<|", "途中訳"),
        ("途中訳<|plamo:tag|>output", "途中訳"),
        ("<|plamo:bos|>hidden", ""),
        ("<plm-a>途中訳</plm-a>", "<plm-a>途中訳</plm-a>"),
    ],
)
def test_preview_withholds_incomplete_characters_and_model_control_tokens(text, expected):
    assert preview_prefix(text) == expected


def test_file_download_does_not_acquire_inference_lock_or_replace_live_model(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    engine = Engine(cache_dir=tmp_path)
    live = object()
    engine._model, engine._loaded, engine._state = live, "2b", "translating"
    cancelled = threading.Event()
    calls = []

    def files(model, progress, **options):
        calls.append((model, options))
        return "downloaded-files"

    monkeypatch.setattr("plamo_cp.engine.download_model", files)
    engine._lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(engine.download, "8b", cancelled=cancelled)
            try:
                assert future.result(timeout=1) == "downloaded-files"
            finally:
                engine._lock.release()
    finally:
        if engine._lock.locked():
            engine._lock.release()
    assert calls == [("8b", {"cache_dir": tmp_path.resolve(), "cancelled": cancelled})]
    assert engine._model is live and engine._loaded == "2b" and engine._state == "translating"


def test_serial_model_input_and_output_use_wire_tags_without_changing_api_markers(inference):
    source = "Find <plm-a>all</plm-a> [[PLM_19]]."
    inference.stream.responses = [('<span id="plm-a">すべて</span>', None), ("<math-a/>。", "stop")]
    assert inference.engine.translate(source, glossary=[]) == "<plm-a>すべて</plm-a>[[PLM_19]]。"
    messages, options = inference.engine._tokenizer.template_calls[-1]
    assert messages == [{"role": "user", "content": 'Find <span id="plm-a">all</span> <math-a/>.'}]
    assert options["glossary"] == [{"source": "<math-a/>", "target": "<math-a/>"}]


def test_serial_quiet_mode_synchronizes_the_library_stream_and_keeps_the_same_output(
    inference, monkeypatch
):
    baseline = inference.engine.translate("Find the array.")
    generation = ModuleType("mlx_lm.generate")
    generation.generation_stream = object()
    parent, core = ModuleType("mlx"), ModuleType("mlx.core")
    synchronized, waits = [], []
    core.synchronize = synchronized.append
    parent.core = core
    monkeypatch.setitem(sys.modules, "mlx", parent)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    monkeypatch.setitem(sys.modules, "mlx_lm.generate", generation)
    monkeypatch.setattr(inference.engine, "_quiet_wait", lambda delay, *_: waits.append(delay))
    inference.engine.set_quiet_mode(True)
    assert inference.engine.translate("Find the array.") == baseline
    assert synchronized == [generation.generation_stream] * 2
    assert len(waits) == 2 and all(wait >= 0 for wait in waits)
    assert inference.stream.closed and inference.engine.status["state"] == "idle"
