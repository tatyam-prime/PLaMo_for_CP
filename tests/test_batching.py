"""True-batch contracts with a fake generator; no Metal initialization or weights."""

from __future__ import annotations

import sys
import threading
from collections import deque
from types import ModuleType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from plamo_cp.api import create_app
from plamo_cp.engine import MODEL_SPECS, Engine, TranslationWork


def event(uid, token, reason=None):
    return SimpleNamespace(uid=uid, token=token, finish_reason=reason)


@pytest.fixture
def batched(tmp_path, monkeypatch):
    state = SimpleNamespace(
        generators=[], loads=[], steps=[], failure=None, on_next=None, stream=object()
    )

    class Tokenizer:
        eos_token_ids = (1, 2)

        def __init__(self):
            self.templates = []

        def apply_chat_template(self, messages, **options):
            self.templates.append((messages, options))
            return "prompt:" + messages[-1]["content"]

        def encode(self, text, **options):
            if "oversized" in text:
                return list(range(32768))
            return list(range(max(1, len(text.split()))))

        def decode(self, tokens):
            return "".join(
                f"訳文{token - 100}。"
                if token >= 100
                else {10: "最初の訳文。", 11: "次の訳文<math-a/>。", 12: "破損。"}[token]
                for token in tokens
            )

    class Generator:
        def __init__(self, model, **options):
            self.model, self.options, self.closed = model, options, False
            self.prompts, self.budgets, self.uids, self.deferred = [], [], [], []
            self.removed = []
            state.generators.append(self)

        def insert(self, prompts, max_tokens):
            start = len(self.prompts)
            self.prompts.extend(prompts)
            self.budgets.extend(max_tokens)
            uids = [41, 7, 93, 18, 57, 29, 81, 6, *range(1000, 1064)][start : start + len(prompts)]
            self.uids.extend(uids)
            return uids

        def remove(self, uids):
            self.removed.extend(uids)
            self.uids = [uid for uid in self.uids if uid not in uids]

        def next(self):
            if state.on_next:
                state.on_next()
            if state.failure:
                raise state.failure
            prompt, responses = state.steps.pop(0) if state.steps else ([], [])
            responses = self.deferred + responses
            self.deferred = [response for response in responses if response.uid not in self.uids]
            return prompt, [response for response in responses if response.uid in self.uids]

        def close(self):
            self.closed = True

    parent, generate, sampling = (
        ModuleType("mlx_lm"),
        ModuleType("mlx_lm.generate"),
        ModuleType("mlx_lm.sample_utils"),
    )
    generate.BatchGenerator = Generator
    sampling.make_sampler = lambda **options: options
    monkeypatch.setitem(sys.modules, "mlx_lm", parent)
    monkeypatch.setitem(sys.modules, "mlx_lm.generate", generate)
    monkeypatch.setitem(sys.modules, "mlx_lm.sample_utils", sampling)
    mlx, core = ModuleType("mlx"), ModuleType("mlx.core")
    core.default_device = lambda: "fake-device"
    core.default_stream = lambda device: state.stream
    mlx.core = core
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    engine = Engine(cache_dir=tmp_path)
    engine._model, engine._tokenizer = object(), Tokenizer()
    monkeypatch.setattr(engine, "_load", state.loads.append)
    state.engine = engine
    return state


def test_batched_generation_preserves_uid_order_eos_bounds_and_independent_prompts(batched):
    batched.steps = [
        (["prefill-progress"], []),
        ([], [event(7, 11), event(41, 10)]),
        ([], [event(7, 2, "stop"), event(41, 1, "stop")]),
    ]
    result = batched.engine.translate_batch(
        ["First.", "Second [[PLM_0]]."], source_context="Shared English reference."
    )
    assert result == ["最初の訳文。", "次の訳文[[PLM_0]]。"]
    assert batched.loads == ["2b"]
    generator = batched.generators[0]
    assert generator.closed and generator.budgets == [512, 512]
    assert generator.options == {
        "stop_tokens": [[1], [2]],
        "sampler": {"temp": 0},
        "completion_batch_size": 2,
        "prefill_batch_size": 1,
        "prefill_step_size": 256,
        "stream": batched.stream,
    }
    assert all(
        len(messages) == 1 and "Shared English reference." in options["context"]
        for messages, options in batched.engine._tokenizer.templates
    )
    assert batched.engine.status["state"] == "idle"


def test_continuous_quiet_mode_paces_global_steps_without_changing_the_pool_or_results(
    batched, monkeypatch
):
    batched.steps = [
        (["prefill-progress"], []),
        ([], [event(7, 11), event(41, 10)]),
        ([], [event(7, 2, "stop"), event(41, 1, "stop")]),
    ]
    synchronized, waits = [], []
    monkeypatch.setattr(sys.modules["mlx.core"], "synchronize", synchronized.append, raising=False)
    monkeypatch.setattr(batched.engine, "_quiet_wait", lambda delay, *_: waits.append(delay))
    batched.engine.set_quiet_mode(True)
    assert batched.engine.translate_batch(["First.", "Second [[PLM_0]]."]) == [
        "最初の訳文。",
        "次の訳文[[PLM_0]]。",
    ]
    assert synchronized == [batched.stream] * 3
    assert len(waits) == 3  # Once per global step, not once per active paragraph.
    assert batched.generators[0].options["completion_batch_size"] == 2
    assert batched.generators[0].closed and batched.engine.status["state"] == "idle"


def test_eight_prompts_keep_independent_results_when_uids_finish_in_reverse_order(batched):
    uids = [41, 7, 93, 18, 57, 29, 81, 6]
    batched.steps = [
        ([], [event(uid, 100 + index) for index, uid in reversed(list(enumerate(uids)))]),
        ([], [event(uid, 1, "stop") for uid in reversed(uids)]),
    ]
    result = batched.engine.translate_batch([f"Paragraph {index}." for index in range(8)])
    assert result == [f"訳文{index}。" for index in range(8)]
    generator = batched.generators[0]
    assert generator.options["completion_batch_size"] == 8
    assert generator.options["prefill_batch_size"] == 1
    assert generator.closed


def test_per_prompt_budget_failure_does_not_discard_another_valid_result(batched):
    batched.steps = [([], [event(41, 10)]), ([], [event(41, 1, "stop")])]
    result = batched.engine.translate_batch(["oversized", "Small."])
    assert isinstance(result[0], ValueError) and "32768" in str(result[0])
    assert result[1] == "最初の訳文。"
    assert len(batched.generators[0].prompts) == 1


@pytest.mark.parametrize("failure", ["length", "markers", "empty", "missing-stop"])
def test_bad_batch_segment_retains_valid_sibling(batched, failure):
    failing = []
    if failure == "length":
        failing = [event(7, 11, "length")]
    elif failure == "markers":
        failing = [event(7, 12), event(7, 1, "stop")]
    elif failure == "empty":
        failing = [event(7, 1, "stop")]
    batched.steps = [([], [event(41, 10)]), ([], [event(41, 1, "stop"), *failing])]
    result = batched.engine.translate_batch(["First.", "Second [[PLM_0]]."])
    assert result[0] == "最初の訳文。" and isinstance(result[1], ValueError)
    assert batched.generators[0].closed


def test_batch_cancellation_during_prefill_closes_generator_and_restores_idle(batched):
    cancelled = threading.Event()
    batched.steps = [(["prefill-progress"], [])]
    batched.on_next = cancelled.set
    with pytest.raises(InterruptedError, match="停止"):
        batched.engine.translate_batch(["First.", "Second."], cancelled=cancelled)
    assert batched.generators[0].closed and batched.engine.status["state"] == "idle"


def test_batch_cancelled_before_load_does_no_model_work(batched):
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError):
        batched.engine.translate_batch(["First."], cancelled=cancelled)
    assert not batched.loads and not batched.generators


@pytest.mark.parametrize(
    "language,expected", [("Chinese", "zh"), ("Indonesian", "id"), ("auto", None)]
)
def test_batched_prompts_keep_non_english_and_auto_language_options(batched, language, expected):
    batched.steps = [
        ([], [event(41, 10), event(7, 11)]),
        ([], [event(41, 1, "stop"), event(7, 2, "stop")]),
    ]
    result = batched.engine.translate_batch(
        ["给定整数。", "计算 [[PLM_0]]。"], source_lang=language
    )
    assert all(isinstance(item, str) for item in result)
    assert [options["source_lang"] for _, options in batched.engine._tokenizer.templates] == [
        expected,
        expected,
    ]


def test_runtime_failure_is_global_and_closes_generator(batched):
    batched.failure = RuntimeError("out of memory")
    with pytest.raises(RuntimeError, match="out of memory"):
        batched.engine.translate_batch(["First.", "Second."])
    assert batched.generators[0].closed and batched.engine.status["state"] == "idle"


def test_model_load_validation_failure_is_global_instead_of_retried_per_segment(
    batched, monkeypatch
):
    def load(model):
        batched.loads.append(model)
        raise ValueError("invalid model configuration")

    monkeypatch.setattr(batched.engine, "_load", load)
    with pytest.raises(ValueError, match="invalid model"):
        batched.engine.translate_batch(["First.", "Second."])
    assert batched.loads == ["2b"] and not batched.generators


def test_batch_limit_and_invalid_control_text_do_not_load_weights(batched):
    with pytest.raises(ValueError, match="1〜8"):
        batched.engine.translate_batch(["text"] * 9)
    result = batched.engine.translate_batch(["<|plamo:tag|>output"])
    assert isinstance(result[0], ValueError)
    assert not batched.loads and not batched.generators


def test_stream_emits_a_finished_segment_and_refills_while_a_slow_sibling_is_active(batched):
    batched.steps = [
        (["first-prefill"], []),
        ([], [event(41, 100), event(7, 101)]),
        ([], [event(41, 1, "stop")]),
        ([], [event(93, 102), event(7, 101)]),
        ([], [event(93, 1, "stop")]),
        ([], [event(7, 1, "stop")]),
    ]
    stream = batched.engine.iter_translate(["First.", "Slow.", "Replacement."], batch_size=2)
    assert next(stream) == (0, "訳文0。")
    generator = batched.generators[0]
    assert len(generator.prompts) == 2 and not generator.closed
    assert next(stream) == (2, "訳文2。")
    assert len(generator.prompts) == 3 and not generator.closed
    assert next(stream) == (1, "訳文1。訳文1。")
    with pytest.raises(StopIteration):
        next(stream)
    assert generator.closed


def test_global_engine_stream_can_mix_32_independent_jobs(batched):
    identifiers = [41, 7, 93, 18, 57, 29, 81, 6, *range(1000, 1024)]
    works = deque(
        TranslationWork(
            ("left" if index < 16 else "right", index),
            f"Text {index}.",
            {
                "source_lang": "English",
                "target_lang": "Japanese",
                "source_context": f"Context for {index}.",
            },
        )
        for index in range(32)
    )
    batched.steps = [
        (["first-prefill"], []),
        ([], [event(uid, 100 + index) for index, uid in enumerate(identifiers)]),
        ([], [event(uid, 1, "stop") for uid in reversed(identifiers)]),
    ]

    def take(count):
        return [works.popleft() for _ in range(min(count, len(works)))]

    results = list(batched.engine.iter_work("2b", take, lambda: 32))
    assert len(results) == 32 and all(isinstance(result, str) for _, result in results)
    assert {work.key[0] for work, _ in results} == {"left", "right"}
    assert batched.generators[0].options["completion_batch_size"] == 32
    assert batched.generators[0].options["prefill_batch_size"] == 1
    contexts = [options["context"] for _, options in batched.engine._tokenizer.templates]
    assert all(f"Context for {index}." in contexts[index] for index in range(32))


def test_cancelling_one_work_removes_only_its_uid_and_other_work_completes(batched):
    first, second = threading.Event(), threading.Event()
    works = deque(
        [
            TranslationWork(
                "cancel", "First.", {"source_lang": "English", "target_lang": "Japanese"}, first
            ),
            TranslationWork(
                "keep", "Second.", {"source_lang": "English", "target_lang": "Japanese"}, second
            ),
        ]
    )
    calls = [0]

    def on_next():
        calls[0] += 1
        if calls[0] == 2:
            first.set()

    batched.on_next = on_next
    batched.steps = [
        (["prefill"], []),
        ([], [event(41, 100), event(7, 101)]),
        ([], [event(7, 1, "stop")]),
    ]
    stream = batched.engine.iter_work(
        "2b", lambda count: [works.popleft() for _ in range(min(count, len(works)))], lambda: 2
    )
    results = list(stream)
    assert isinstance(results[0][1], InterruptedError) and results[0][0].key == "cancel"
    assert results[1][0].key == "keep" and results[1][1] == "訳文1。"
    assert batched.generators[0].removed == [41]
    assert not second.is_set()


def test_closing_completion_iterator_releases_generator_and_engine_lock(batched):
    batched.steps = [
        (["prefill"], []),
        ([], [event(41, 100), event(7, 101)]),
        ([], [event(41, 1, "stop")]),
    ]
    stream = batched.engine.iter_translate(["First.", "Second."], batch_size=2)
    assert next(stream)[0] == 0
    stream.close()
    assert batched.generators[0].closed and batched.engine.status["state"] == "idle"
    assert batched.engine._lock.acquire(blocking=False)
    batched.engine._lock.release()


class BatchEngine:
    model_specs = MODEL_SPECS
    supported_batch_sizes = tuple(range(1, 33))

    def __init__(self):
        self.status = {"backend": "mock", "loaded_model": None, "state": "idle"}
        self.calls, self.serial, self.outputs = [], [], {}
        self.failure = None

    def translate(self, text, **options):
        self.serial.append((text, options))
        return "訳: " + text

    def translate_batch(self, texts, **options):
        self.calls.append((texts, options))
        if self.failure:
            raise self.failure
        return [self.outputs.get(text, "訳: " + text) for text in texts]


@pytest.fixture
def batch_client(tmp_path):
    engine = BatchEngine()
    app = create_app(
        engine=engine,
        cache_path=tmp_path / "translations.sqlite3",
        settings_path=tmp_path / "settings.json",
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, engine


def request_body(texts, **changes):
    return {
        "segments": [{"id": str(index), "text": text} for index, text in enumerate(texts)],
        **changes,
    }


def send(client, body):
    return client.post("/api/translate", json=body, headers={"X-Plamo-Client": "userscript"})


def test_api_default_is_serial_and_setup_advertises_engine_capabilities(batch_client):
    client, engine = batch_client
    assert client.get("/api/setup").json()["supported_batch_sizes"] == list(range(1, 33))
    response = send(client, request_body(["First.", "Second."]))
    assert response.status_code == 200 and "errors" not in response.json()
    assert len(engine.serial) == 2 and not engine.calls


@pytest.mark.parametrize("batch_size", [3, 5, 6, 7, 9, 12, 16, 20, 24, 31, 32])
def test_api_accepts_non_power_of_two_client_windows(batch_client, batch_size):
    client, _ = batch_client
    response = send(client, request_body(["First.", "Second."], batch_size=batch_size))
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["translations"]] == ["0", "1"]


@pytest.mark.parametrize("batch_size", [0, 33, 2.5, 32.0, True, False])
def test_api_client_window_requires_an_integer_in_range(batch_client, batch_size):
    client, engine = batch_client
    assert send(client, request_body(["First."], batch_size=batch_size)).status_code == 422
    assert not engine.calls and not engine.serial


def test_api_batches_only_cache_misses_and_keeps_request_order(batch_client):
    client, engine = batch_client
    assert send(client, request_body(["Cached."])).status_code == 200
    texts = ["One.", "Cached.", "Two.", "Three.", "Four.", "Five."]
    body = request_body(
        texts,
        batch_size=4,
        source_context="Shared reference.",
        history=[{"source": "Earlier.", "translation": "前。"}],
    )
    # Warm this same-context entry; cache keys deliberately include reference/history.
    assert (
        send(
            client,
            request_body(
                ["Cached."], source_context=body["source_context"], history=body["history"]
            ),
        ).status_code
        == 200
    )
    response = send(client, body)
    assert response.status_code == 200 and response.json()["cached"] == 1
    assert [item["id"] for item in response.json()["translations"]] == [str(i) for i in range(6)]
    assert [call[0] for call in engine.calls] == [["One.", "Two.", "Three.", "Four.", "Five."]]
    assert all(
        options["source_context"] == body["source_context"]
        and options["history"] == body["history"]
        for _, options in engine.calls
    )
    body["batch_size"] = 2
    assert send(client, body).json()["cached"] == 6
    assert len(engine.calls) == 1


def test_api_partial_errors_do_not_discard_valid_results_or_cache_bad_ones(batch_client):
    client, engine = batch_client
    engine.outputs = {"Bad [[PLM_0]].": "missing marker", "Too long.": ValueError("生成上限")}
    body = request_body(["Good.", "Bad [[PLM_0]].", "Too long."], batch_size=4)
    first = send(client, body)
    assert first.status_code == 200
    assert first.json()["translations"] == [{"id": "0", "text": "訳: Good."}]
    assert [item["id"] for item in first.json()["errors"]] == ["1", "2"]
    assert client.get("/api/cache").json()["entries"] == 1
    engine.outputs.clear()
    second = send(client, body)
    assert second.status_code == 200 and second.json()["cached"] == 1
    assert len(second.json()["translations"]) == 3 and "errors" not in second.json()
    assert engine.calls[-1][0] == ["Bad [[PLM_0]].", "Too long."]
    assert client.get("/api/cache").json()["entries"] == 3


@pytest.mark.parametrize(
    "failure,status", [(RuntimeError("out of memory"), 503), (InterruptedError("stopped"), 499)]
)
def test_api_runtime_and_cancellation_are_global_errors(batch_client, failure, status):
    client, engine = batch_client
    engine.failure = failure
    result = send(client, request_body(["First.", "Second."], batch_size=2))
    assert result.status_code == status
    assert client.get("/api/cache").json()["entries"] == 0


def test_unsupported_backend_rejects_batching_before_any_translation(batch_client):
    client, engine = batch_client
    engine.supported_batch_sizes = (1,)
    assert client.get("/api/setup").json()["supported_batch_sizes"] == [1]
    assert send(client, request_body(["First."], batch_size=2)).status_code == 422
    assert not engine.calls and not engine.serial


@pytest.mark.parametrize("broken_callback", [False, True])
def test_per_uid_streaming_detokenizers_are_independent_and_callbacks_cannot_break_final(
    batched, monkeypatch, broken_callback
):
    tokenizer = batched.engine._tokenizer
    decoders, previews = [], {"a": [], "b": []}

    class Decoder:
        def __init__(self):
            self.tokens = []
            decoders.append(self)

        def add_token(self, token):
            self.tokens.append(token)

        @property
        def text(self):
            return tokenizer.decode(self.tokens)

    monkeypatch.setattr(
        type(tokenizer), "detokenizer", property(lambda _: Decoder()), raising=False
    )
    clock = [1.0]
    monkeypatch.setattr("plamo_cp.engine.time.monotonic", lambda: clock[0])
    batched.on_next = lambda: clock.__setitem__(0, clock[0] + 0.1)
    batched.steps = [
        ([], [event(41, 100)]),
        ([], [event(41, 101), event(7, 102)]),
        ([], [event(41, 1, "stop"), event(7, 2, "stop")]),
    ]

    def first(text):
        previews["a"].append(text)
        if broken_callback:
            raise RuntimeError("An optional display callback failed")

    parameters = {"source_lang": "English", "target_lang": "Japanese"}
    works = deque(
        [
            TranslationWork("a", "First.", parameters, progress=first),
            TranslationWork("b", "Second.", parameters, progress=previews["b"].append),
        ]
    )
    results = list(
        batched.engine.iter_work(
            "0.8b",
            lambda count: [works.popleft() for _ in range(min(count, len(works)))],
            lambda: 2,
        )
    )
    assert [(work.key, text) for work, text in results] == [
        ("a", "訳文0。訳文1。"),
        ("b", "訳文2。"),
    ]
    assert previews["a"] == (["訳文0。"] if broken_callback else ["訳文0。", "訳文0。訳文1。"])
    assert previews["b"] == ["訳文2。"]
    assert decoders[0].tokens == ([100] if broken_callback else [100, 101])
    assert decoders[1].tokens == [102]
    assert batched.generators[0].closed


def test_draft_text_is_decoded_at_most_twenty_times_per_second(batched, monkeypatch):
    tokenizer = batched.engine._tokenizer
    reads, published = [], []

    class Decoder:
        def __init__(self):
            self.tokens = []

        def add_token(self, token):
            self.tokens.append(token)

        @property
        def text(self):
            reads.append(tuple(self.tokens))
            return tokenizer.decode(self.tokens)

    monkeypatch.setattr(
        type(tokenizer), "detokenizer", property(lambda _: Decoder()), raising=False
    )
    clock = [1.0]
    times = iter([1.0, 1.01, 1.02, 1.06, 1.07])
    monkeypatch.setattr("plamo_cp.engine.time.monotonic", lambda: clock[0])
    batched.on_next = lambda: clock.__setitem__(0, next(times))
    batched.steps = [([], [event(41, token)]) for token in (100, 101, 102, 103)] + [
        ([], [event(41, 1, "stop")])
    ]
    work = TranslationWork(
        "a",
        "First.",
        {"source_lang": "English", "target_lang": "Japanese"},
        progress=published.append,
    )
    waiting = [work]
    result = list(
        batched.engine.iter_work(
            "0.8b", lambda count: [waiting.pop()] if waiting else [], lambda: 1
        )
    )
    assert reads == [(100,), (100, 101, 102, 103)]
    assert published == ["訳文0。", "訳文0。訳文1。訳文2。訳文3。"]
    assert result[0][1] == "訳文0。訳文1。訳文2。訳文3。"


def test_wire_stream_previews_are_canonical_and_uid_mappings_never_cross(batched, monkeypatch):
    pieces = {10: '<span id="plm-a">訳文', 11: "</span><math-", 12: "a/>。", 13: "別の<math-a/>。"}
    monkeypatch.setattr(
        batched.engine._tokenizer,
        "decode",
        lambda tokens: "".join(pieces[token] for token in tokens),
    )
    clock = [1.0]
    monkeypatch.setattr("plamo_cp.engine.time.monotonic", lambda: clock[0])
    batched.on_next = lambda: clock.__setitem__(0, clock[0] + 0.1)
    batched.steps = [
        ([], [event(41, 10)]),
        ([], [event(41, 11), event(7, 13)]),
        ([], [event(41, 12), event(7, 2, "stop")]),
        ([], [event(41, 1, "stop")]),
    ]
    previews = {"first": [], "second": []}
    parameters = {"source_lang": "en", "target_lang": "ja"}
    waiting = deque(
        [
            TranslationWork(
                "first",
                "<plm-a>All</plm-a> [[PLM_19]].",
                parameters,
                progress=previews["first"].append,
            ),
            TranslationWork(
                "second", "Other [[PLM_83]].", parameters, progress=previews["second"].append
            ),
        ]
    )
    results = list(
        batched.engine.iter_work(
            "2b",
            lambda count: [waiting.popleft() for _ in range(min(count, len(waiting)))],
            lambda: 2,
        )
    )
    assert [(work.key, result) for work, result in results] == [
        ("second", "別の[[PLM_83]]。"),
        ("first", "<plm-a>訳文</plm-a>[[PLM_19]]。"),
    ]
    assert previews["first"] == [
        "<plm-a>訳文",
        "<plm-a>訳文</plm-a>",
        "<plm-a>訳文</plm-a>[[PLM_19]]。",
    ]
    assert previews["second"] == ["別の[[PLM_83]]。"]
    assert all(
        "<math-" not in text and "<span" not in text
        for values in previews.values()
        for text in values
    )


def test_model_wire_is_decoded_and_verified_before_api_cache_write(batched, monkeypatch, tmp_path):
    monkeypatch.setattr(batched.engine, "model_available", lambda model: True)
    mapping = {10: '<span id="plm-a">すべて</span><math-a/>。', 11: "<math-z/>"}
    monkeypatch.setattr(
        batched.engine._tokenizer,
        "decode",
        lambda tokens: "".join(mapping[token] for token in tokens),
    )
    batched.steps = [
        ([], [event(41, 10)]),
        ([], [event(41, 1, "stop"), event(7, 11)]),
        ([], [event(7, 2, "stop")]),
    ]
    app = create_app(batched.engine, tmp_path / "verified.sqlite3", tmp_path / "settings.json")
    headers = {"X-Plamo-Client": "userscript"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/translate",
            headers=headers,
            json={
                "batch_size": 2,
                "segments": [
                    {"id": "good", "text": "<plm-a>All</plm-a> [[PLM_9]]."},
                    {"id": "bad", "text": "Find [[PLM_82]]."},
                ],
            },
        )
        assert response.status_code == 200
        assert response.json()["translations"] == [
            {"id": "good", "text": "<plm-a>すべて</plm-a>[[PLM_9]]。"}
        ]
        assert response.json()["errors"][0]["id"] == "bad"
        assert client.get("/api/cache").json()["entries"] == 1
