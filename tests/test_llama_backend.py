"""Cross-platform contract tests; no Windows executable or weights are fetched."""

from __future__ import annotations

import hashlib
import json
import threading
import zipfile

import httpx
import pytest

from plamo_cp import llama_backend as backend


def test_prompt_has_one_bos_and_translation_metadata():
    prompt = backend.build_prompt(
        "Find the <plm-a>second smallest</plm-a> integer [[PLM_0]].", "English", "Japanese"
    )
    assert prompt.startswith("<|plamo:bos|><|plamo:tag|>dataset<|plamo:msg|>translation")
    assert prompt.count("<|plamo:bos|>") == 1
    assert "<|plamo:tag|>context<|plamo:msg|>" in prompt
    assert '<span id="plm-a">second smallest</span> integer <math-a/>.' in prompt
    assert "<|plamo:index|><math-a/><|plamo:item|><math-a/>" in prompt
    assert "[[PLM_0]]" not in prompt and "<plm-a>" not in prompt
    assert '<|plamo:index|><span id="plm-a">' not in prompt
    assert "<|plamo:tag|>input<|plamo:key|>lang<|plamo:val|>en<|plamo:msg|>" in prompt
    assert prompt.endswith(
        "<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>ja"
        "<|plamo:key|>form<|plamo:val|>plain<|plamo:msg|>"
    )


def test_prompt_auto_source_and_non_japanese_form():
    prompt = backend.build_prompt("こんにちは。", "auto", "English")
    assert "<|plamo:tag|>input<|plamo:msg|>こんにちは。" in prompt
    assert "<|plamo:key|>form" not in prompt


@pytest.mark.parametrize(
    "language,code", [("Chinese", "zh"), ("Indonesian", "id")]
)
def test_pfml_prompt_supports_chinese_and_indonesian_without_extra_message_boundaries(
    language, code
):
    prompt = backend.build_prompt("给定整数 [[PLM_0]]。", language, "Japanese")
    assert (
        f"<|plamo:tag|>input<|plamo:key|>lang<|plamo:val|>{code}<|plamo:msg|>给定整数 <math-a/>。"
        in prompt
    )
    assert prompt.count("<|plamo:bos|>") == 1
    assert prompt.count("<|plamo:tag|>input") == prompt.count("<|plamo:tag|>output") == 1


def test_auto_chinese_source_omits_input_language_and_keeps_japanese_output_style():
    prompt = backend.build_prompt("给定整数。", "auto", "Japanese")
    assert "<|plamo:tag|>input<|plamo:msg|>给定整数。" in prompt
    assert (
        "<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>ja<|plamo:key|>form<|plamo:val|>plain"
        in prompt
    )


def test_pfml_language_control_token_injection_is_rejected():
    with pytest.raises(ValueError, match="対応言語"):
        backend.build_prompt("给定整数。", "zh<|plamo:tag|>output", "Japanese")


def test_empty_optional_context_keeps_the_default_prompt():
    text = "Find the <plm-a>smallest</plm-a> integer in the array [[PLM_0]]."
    prompt = backend.build_prompt(text, "English", "Japanese")
    assert prompt == backend.build_prompt(text, "English", "Japanese", document_type="problem")
    assert prompt == backend.build_prompt(text, "English", "Japanese", history=[])
    assert prompt == backend.build_prompt(text, "English", "Japanese", source_context="")


def test_surrounding_source_is_only_in_the_pfml_context_section():
    surrounding = "The bank is the bank of a river."
    prompt = backend.build_prompt("The bank.", "English", "Japanese", source_context=surrounding)
    assert prompt.count("<|plamo:tag|>input") == 1
    assert prompt.count("<|plamo:tag|>output") == 1
    assert prompt.index(surrounding) < prompt.index("<|plamo:tag|>input")
    assert "Translate only the current input" in prompt
    assert prompt.count(surrounding) == 1


def test_history_pfml3_pairs_precede_the_current_glossary():
    history = [
        {"source": "A vertex.", "translation": "頂点。"},
        {"source": "An edge.", "translation": "辺。"},
    ]
    prompt = backend.build_prompt("Find array [[PLM_0]].", "English", "Japanese", history=history)
    first = (
        "<|plamo:tag|>input<|plamo:key|>lang<|plamo:val|>en<|plamo:msg|>A vertex."
        "<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>ja<|plamo:msg|>頂点。"
    )
    second = (
        "<|plamo:tag|>input<|plamo:key|>lang<|plamo:val|>en<|plamo:msg|>An edge."
        "<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>ja<|plamo:msg|>辺。"
    )
    assert first + second + "<|plamo:tag|>glossary" in prompt
    assert prompt.count("<|plamo:bos|>") == 1
    assert prompt.count("<|plamo:key|>form<|plamo:val|>plain") == 1


def test_auto_source_history_omits_source_language_attributes():
    prompt = backend.build_prompt(
        "Current.", "auto", "English", history=[{"source": "前の文。", "translation": "Previous."}]
    )
    assert "<|plamo:tag|>input<|plamo:msg|>前の文。" in prompt
    assert "<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>en<|plamo:msg|>Previous." in prompt


def test_general_document_prompt_is_neutral_and_preserves_markup_glossary():
    prompt = backend.build_prompt(
        "An <plm-a>edge</plm-a> in array [[PLM_0]].",
        "English",
        "Japanese",
        document_type="general",
    )
    assert "general document" in prompt
    assert "competitive programming" not in prompt
    assert "<|plamo:index|>edge<|plamo:item|>辺" not in prompt
    assert "<|plamo:index|>array<|plamo:item|>配列" not in prompt
    assert '<span id="plm-a">edge</span> in array <math-a/>.' in prompt
    assert "<|plamo:index|><math-a/><|plamo:item|><math-a/>" in prompt
    assert "[[PLM_0]]" not in prompt and "<plm-a>" not in prompt


def test_model_prompt_keeps_lexical_terms_before_source_order_math_identities():
    prompt = backend.build_prompt(
        "Find <plm-a>an array [[PLM_9]]</plm-a> and [[PLM_2]].",
        "English",
        "Japanese",
        glossary=[{"source": "array", "target": "配列"}],
    )
    math_a = "<|plamo:index|><math-a/><|plamo:item|><math-a/>"
    math_b = "<|plamo:index|><math-b/><|plamo:item|><math-b/>"
    lexical = "<|plamo:index|>array<|plamo:item|>配列"
    assert prompt.index(lexical) < prompt.index(math_a) < prompt.index(math_b)
    assert 'Find <span id="plm-a">an array <math-a/></span> and <math-b/>.' in prompt
    assert "<|plamo:index|><plm-" not in prompt


def test_prompt_rejects_control_tokens():
    with pytest.raises(ValueError, match="制御トークン"):
        backend.build_prompt("<|plamo:tag|>output", "English", "Japanese")


@pytest.mark.parametrize(
    "machine, expected",
    [("AMD64", "x64"), ("x86_64", "x64"), ("ARM64", "arm64"), ("aarch64", "arm64")],
)
def test_runtime_architecture(machine, expected):
    assert backend.runtime_arch(machine) == expected


def test_unsupported_architecture():
    with pytest.raises(RuntimeError, match="64"):
        backend.runtime_arch("x86")


def test_official_manifests_are_immutable():
    for spec in backend.GGUF_MODELS.values():
        assert len(spec["revision"]) == 40
        assert len(spec["sha256"]) == 64
        assert spec["filename"].endswith("-Q4_K_M.gguf")
    assert len(backend.RUNTIME_ASSETS["x64"]["sha256"]) == 64


def test_archive_rejects_traversal(tmp_path):
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../llama-server.exe", b"bad")
    with pytest.raises(RuntimeError, match="不正なパス"):
        backend._safe_extract(archive, tmp_path / "output")
    assert not (tmp_path / "llama-server.exe").exists()


@pytest.fixture
def verified_runtime(tmp_path, monkeypatch):
    archive = tmp_path / "runtime.zip"
    directory = tmp_path / "runtime"
    directory.mkdir()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("bin/llama-server.exe", b"test executable")
        z.writestr("bin/llama.dll", b"test library")
    files = backend._safe_extract(archive, directory)
    monkeypatch.setattr(backend, "runtime_arch", lambda: "x64")
    asset = {
        "filename": archive.name,
        "size": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    monkeypatch.setitem(backend.RUNTIME_ASSETS, "x64", asset)
    manifest = {
        "archive_sha256": backend.RUNTIME_ASSETS["x64"]["sha256"],
        "files": files,
        "executable": "bin/llama-server.exe",
    }
    (directory / ".runtime-manifest.json").write_text(json.dumps(manifest))
    return directory, archive, manifest


def test_archive_manifest_and_runtime_tamper_detection(verified_runtime):
    directory, _archive, _manifest = verified_runtime
    assert backend._runtime_executable(directory, verify=True) == directory / "bin/llama-server.exe"
    # Same-size mutation must still fail verification before an executable runs.
    (directory / "bin/llama-server.exe").write_bytes(b"evil executable")
    assert backend._runtime_executable(directory, verify=True) is None


def test_manifest_cannot_replace_the_pinned_archive_as_trust_anchor(verified_runtime):
    directory, _archive, manifest = verified_runtime
    executable = directory / manifest["executable"]
    executable.write_bytes(b"evil executable")
    # A forged sidecar can describe the modified payload, but cannot change the
    # separately pinned archive or derive a different trusted file inventory.
    manifest["files"][manifest["executable"]]["sha256"] = hashlib.sha256(
        executable.read_bytes()
    ).hexdigest()
    (directory / ".runtime-manifest.json").write_text(json.dumps(manifest))
    assert backend._runtime_executable(directory, verify=True) is None


@pytest.mark.parametrize(
    "name", ["../llama-server.exe", "/llama-server.exe", "C:/llama-server.exe"]
)
def test_manifest_executable_cannot_escape_runtime_directory(verified_runtime, name):
    directory, _archive, manifest = verified_runtime
    manifest["executable"] = name
    manifest["files"][name] = next(iter(manifest["files"].values()))
    (directory / ".runtime-manifest.json").write_text(json.dumps(manifest))
    assert backend._runtime_executable(directory, verify=True) is None


def test_unlisted_dll_cannot_enter_the_runtime_search_directory(verified_runtime):
    directory, _archive, _manifest = verified_runtime
    (directory / "bin/version.dll").write_bytes(b"injected DLL")
    assert backend._runtime_executable(directory, verify=True) is None


def test_runtime_requires_the_original_pinned_archive(verified_runtime):
    directory, archive, _manifest = verified_runtime
    archive.unlink()
    assert backend._runtime_executable(directory, verify=True) is None


@pytest.mark.parametrize("name", [".. /llama-server.exe", "bin/CON.dll", "bin/llama-server.exe."])
def test_archive_rejects_windows_path_aliases_before_writing(tmp_path, name):
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr(name, b"not executable")
    with pytest.raises(RuntimeError, match="不正なパス"):
        backend._safe_extract(archive, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_archive_rejects_case_insensitive_duplicate_paths(tmp_path):
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr("bin/llama-server.exe", b"first")
        source.writestr("BIN/LLAMA-SERVER.EXE", b"second")
    with pytest.raises(RuntimeError, match="重複"):
        backend._safe_extract(archive, tmp_path / "output")
    assert not (tmp_path / "output").exists()


class FakeProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return 1 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        return 1


class MockRequest:
    """Keep the existing mock HTTP fixtures on the request-owned transport seam."""

    def __init__(self, engine, cancelled):
        self.engine, self.cancelled = engine, cancelled
        self.aborted = threading.Event()
        self.response = None

    def check(self):
        if self.aborted.is_set() or self.cancelled.is_set():
            raise InterruptedError("翻訳を停止しました。")

    def cancel(self):
        self.aborted.set()
        if self.response is not None:
            self.response.close()

    def post_json(self, path, payload):
        self.check()
        response = self.engine._client.post(path, json=payload)
        response.raise_for_status()
        self.check()
        return response.json()

    def events(self, path, payload):
        with self.engine._client.stream("POST", path, json=payload) as response:
            self.response = response
            response.raise_for_status()
            for line in response.iter_lines():
                self.check()
                if line.startswith("data:") and line[5:].strip() not in {"", "[DONE]"}:
                    yield json.loads(line[5:])
            self.check()


def fake_engine(monkeypatch, *, content="<math-a/> 個の整数。", stop_type="eos", truncated=False):
    engine = backend.LlamaEngine()
    process = FakeProcess()
    engine._process = process
    engine._loaded = "2b"
    calls = []

    def handle(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2, 3]})
        chunks = content if isinstance(content, list) else [content]
        events = [
            {
                "content": chunk,
                "stop": index == len(chunks) - 1,
                "stop_type": stop_type,
                "truncated": truncated,
            }
            for index, chunk in enumerate(chunks)
        ]
        data = "".join(
            "data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events
        )
        return httpx.Response(200, text=data)

    engine._client = httpx.Client(
        base_url="http://127.0.0.1:12345", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(engine, "model_available", lambda model: True)
    monkeypatch.setattr(engine, "prepare", lambda model, progress=None: None)
    monkeypatch.setattr(engine, "_new_request", lambda cancelled: MockRequest(engine, cancelled))
    return engine, process, calls


def test_streaming_completion_and_generation_contract(monkeypatch):
    engine, process, calls = fake_engine(monkeypatch)
    try:
        assert engine.translate("An integer [[PLM_0]].") == "[[PLM_0]] 個の整数。"
        assert calls[0][1]["add_special"] is False
        assert calls[0][1]["parse_special"] is True
        payload = calls[-1][1]
        assert payload["prompt"] == [1, 2, 3]
        assert payload["temperature"] == 0
        assert payload["repeat_penalty"] == 1.0
        assert payload["cache_prompt"] is False
        assert payload["stop"] == ["<|plamo:tag|>", "<|plamo:bos|>"]
        assert not process.terminated
        assert engine.status["state"] == "idle"
    finally:
        engine.close()
    assert process.terminated


def test_windows_sse_decodes_split_wire_tags_to_canonical_nested_markup(monkeypatch):
    source = "<plm-a>Find <plm-b>the array</plm-b> [[PLM_9]]</plm-a> with [[PLM_2]]."
    chunks = [
        '<span id="plm-',
        'a">',
        '<span id="plm-b">配列</span><math-',
        "a/>",
        "</span>",
        " と <math-b/>。",
    ]
    engine, _, calls = fake_engine(monkeypatch, content=chunks)
    try:
        assert engine.translate(source) == (
            "<plm-a><plm-b>配列</plm-b>[[PLM_9]]</plm-a> と [[PLM_2]]。"
        )
        assert (
            '<span id="plm-a">Find <span id="plm-b">the array</span> <math-a/></span>'
            in calls[0][1]["content"]
        )
        assert engine.status["state"] == "idle"
    finally:
        engine.close()


@pytest.mark.parametrize(
    "content",
    [
        '<span id="plm-a">配列 <math-b/></span>。',  # Unknown mathematical alias.
        '<span id="plm-b">配列 <math-a/></span>。',  # Unknown formatting ID.
        '<span id="plm-a">配列 [[PLM_0]]</span>。',  # Mixed canonical/model protocols.
        "<plm-a>配列 <math-a/></plm-a>。",
        '<span id="plm-a">配列 <math-a/><math-a/></span>。',
        '<span id="plm-a">配列 <math-a/>。',
        '<span id="plm-a">配列 <math-a/></span><math-',
    ],
)
def test_windows_rejects_unknown_mixed_or_incomplete_model_markers(monkeypatch, content):
    engine, _, _ = fake_engine(monkeypatch, content=content)
    try:
        with pytest.raises(ValueError, match="識別子"):
            engine.translate("<plm-a>An array [[PLM_0]]</plm-a>.")
        assert engine.status["state"] == "idle"
    finally:
        engine.close()


def test_windows_literal_span_collision_preserves_the_canonical_protocol(monkeypatch):
    source = "Keep the literal <span> and [[PLM_0]]."
    engine, _, calls = fake_engine(monkeypatch, content="リテラル <span> と [[PLM_0]]。")
    try:
        assert engine.translate(source) == "リテラル <span> と [[PLM_0]]。"
        assert source in calls[0][1]["content"]
        assert "<|plamo:index|>[[PLM_0]]<|plamo:item|>[[PLM_0]]" in calls[0][1]["content"]
    finally:
        engine.close()


def test_general_document_type_reaches_the_llama_prompt(monkeypatch):
    engine, _, calls = fake_engine(monkeypatch, content="紙の端。")
    try:
        assert engine.translate("The edge of the paper.", document_type="general") == "紙の端。"
        prompt = calls[0][1]["content"]
        assert "general document" in prompt
        assert "competitive programming" not in prompt
        assert "<|plamo:index|>edge<|plamo:item|>辺" not in prompt
    finally:
        engine.close()


def test_user_glossary_reaches_the_owned_llama_engine(monkeypatch):
    engine, _, calls = fake_engine(monkeypatch, content="数列。")
    try:
        assert (
            engine.translate("An array.", glossary=[{"source": "array", "target": "数列"}])
            == "数列。"
        )
        prompt = calls[0][1]["content"]
        assert "<|plamo:index|>array<|plamo:item|>数列" in prompt
        assert "<|plamo:index|>array<|plamo:item|>配列" not in prompt
    finally:
        engine.close()


def test_llama_context_budget_drops_oldest_history_before_generating(monkeypatch):
    engine, _, _ = fake_engine(monkeypatch, content="現在の文章。")
    prompts = []
    completions = []

    def handle(request):
        payload = json.loads(request.content)
        if request.url.path == "/tokenize":
            prompt = payload["content"]
            if prompt.startswith("<|plamo:bos|>"):
                prompts.append(prompt)
                count = 32768 if prompt.count("<|plamo:tag|>input") > 2 else 10
                return httpx.Response(200, json={"tokens": list(range(count))})
            return httpx.Response(200, json={"tokens": [42]})
        completions.append(payload)
        return httpx.Response(
            200, text='data: {"content":"現在の文章。","stop":true,"stop_type":"eos"}\n\n'
        )

    engine._client.close()
    engine._client = httpx.Client(
        base_url="http://127.0.0.1:12345", transport=httpx.MockTransport(handle)
    )
    history = [{"source": f"source {index}", "translation": f"訳文{index}"} for index in range(3)]
    try:
        assert (
            engine.translate(
                "Current.",
                history=history,
                glossary=[{"source": "Current", "target": "現在"}],
                source_context="Reference English.",
            )
            == "現在の文章。"
        )
        assert [prompt.count("<|plamo:tag|>input") for prompt in prompts] == [4, 3, 2]
        assert "source 2" in prompts[-1]
        assert "source 0" not in prompts[-1] and "source 1" not in prompts[-1]
        assert all("<|plamo:index|>Current<|plamo:item|>現在" in prompt for prompt in prompts)
        assert len(completions[0]["prompt"]) == 10
        assert completions[0]["n_predict"] == 512
        assert len(history) == 3
        assert all("Reference English." in prompt for prompt in prompts)
    finally:
        engine.close()


@pytest.mark.parametrize("stop_type, truncated", [("limit", False), ("eos", True), ("none", False)])
def test_truncated_outputs_are_rejected(monkeypatch, stop_type, truncated):
    engine, _, _ = fake_engine(monkeypatch, stop_type=stop_type, truncated=truncated)
    try:
        with pytest.raises(RuntimeError, match="途中"):
            engine.translate("An integer [[PLM_0]].")
    finally:
        engine.close()


def test_corrupt_markers_rejected(monkeypatch):
    engine, _, _ = fake_engine(monkeypatch, content="整数。")
    try:
        with pytest.raises(ValueError, match="識別子"):
            engine.translate("An integer [[PLM_0]].")
    finally:
        engine.close()


def test_missing_model_never_starts_download(monkeypatch):
    engine = backend.LlamaEngine()
    monkeypatch.setattr(engine, "model_available", lambda model: False)
    monkeypatch.setattr(engine, "prepare", lambda *args: pytest.fail("prepare must not run"))
    with pytest.raises(RuntimeError, match="ダウンロード"):
        engine.translate("Hello.")


def test_cancelled_before_request(monkeypatch):
    engine, _, calls = fake_engine(monkeypatch)
    cancelled = threading.Event()
    cancelled.set()
    try:
        with pytest.raises(InterruptedError):
            engine.translate("Hello.", cancelled=cancelled)
        assert not calls
    finally:
        engine.close()


def test_cancellation_interrupts_tokenize_without_terminating_the_owned_process(monkeypatch):
    engine, process, _ = fake_engine(monkeypatch)
    cancelled = threading.Event()

    controls = []

    class BlockedRequest(MockRequest):
        def post_json(self, _path, _payload):
            cancelled.set()
            assert self.aborted.wait(1)
            self.check()

    def create(token):
        control = BlockedRequest(engine, token)
        controls.append(control)
        return control

    monkeypatch.setattr(engine, "_new_request", create)
    try:
        with pytest.raises(InterruptedError):
            engine.translate("Hello.", cancelled=cancelled)
        assert controls[0].aborted.is_set() and not process.terminated
        assert engine.status["loaded_model"] == "2b" and engine.status["state"] == "idle"
    finally:
        engine.close()


def test_context_budget_checked_before_completion(monkeypatch):
    engine, _, calls = fake_engine(monkeypatch)
    monkeypatch.setattr(backend, "CONTEXT_SIZE", 16)
    try:
        with pytest.raises(ValueError, match="長すぎ"):
            engine.translate("Hello.")
        assert all(path == "/tokenize" for path, _ in calls)
    finally:
        engine.close()


def test_windows_file_download_does_not_wait_for_or_stop_current_inference(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    engine, process, _ = fake_engine(monkeypatch)
    engine._state = "translating"
    weight = tmp_path / "small.gguf"
    weight.write_bytes(b"123")
    spec = {
        **backend.GGUF_MODELS["8b"],
        "weight_bytes": 3,
        "sha256": hashlib.sha256(b"123").hexdigest(),
    }
    engine.model_specs = {"8b": spec}
    monkeypatch.setattr(backend.platform, "system", lambda: "Windows")
    monkeypatch.setattr(backend, "ensure_runtime", lambda *_, **__: tmp_path / "runtime.exe")
    monkeypatch.setattr(engine, "_model_path", lambda model: weight)
    original_client = engine._client
    engine._lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(engine.download, "8b")
            try:
                assert future.result(timeout=1) == str(weight)
            finally:
                engine._lock.release()
        assert engine._loaded == "2b" and engine._process is process and not process.terminated
        assert engine._client is original_client and engine.status["state"] == "translating"
    finally:
        engine.close()


@pytest.fixture
def quiet_limits(monkeypatch):
    from types import SimpleNamespace

    from plamo_cp import windows_cpu_limit

    state = SimpleNamespace(instances=[], fail_on=None, closed=[])

    class Limit:
        def __init__(self, process, configured_threads):
            self.process, self.threads = process, configured_threads
            self.enabled, self.calls, self.level_calls = False, [], []
            self.level = 0
            state.instances.append(self)

        def set_level(self, level):
            enabled = level > 0
            self.calls.append(enabled)
            self.level_calls.append(level)
            if state.fail_on is enabled:
                raise RuntimeError("native CPU limit failed")
            self.enabled = enabled
            self.level = level

        def close(self):
            state.closed.append(self.process.poll())

    monkeypatch.setattr(windows_cpu_limit, "OwnedProcessCpuLimit", Limit)
    return state


def test_windows_quiet_mode_without_a_child_stores_only_future_intent(tmp_path, quiet_limits):
    engine = backend.LlamaEngine(cache_dir=tmp_path)
    try:
        assert engine.quiet_mode is False
        engine.set_quiet_mode(True)
        assert engine.quiet_mode is True and engine.status["loaded_model"] is None
        engine.set_quiet_mode(False)
        assert engine.quiet_mode is False and quiet_limits.instances == []
        with pytest.raises(ValueError):
            engine.set_quiet_mode(1)
    finally:
        engine.close()


def test_live_windows_quiet_toggle_caps_only_the_owned_child_without_the_inference_lock(
    monkeypatch, quiet_limits
):
    from concurrent.futures import ThreadPoolExecutor

    engine, process, calls = fake_engine(monkeypatch)
    engine._state = "translating"
    client = engine._client
    engine._lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(engine.set_quiet_mode, True).result(timeout=0.5)
        limit = quiet_limits.instances[0]
        assert limit.process is process and limit.enabled is True
        assert limit.threads == engine._configured_threads
        engine.set_quiet_mode(False)
        assert limit.calls == [True, False] and engine.quiet_mode is False
        assert engine._process is process and engine._client is client
        assert engine._loaded == "2b" and engine._state == "translating" and calls == []
    finally:
        engine._lock.release()
        engine.close()
    assert quiet_limits.closed == [1]  # Cleanup follows the owned child termination.


def test_failed_windows_toggle_preserves_the_applied_flag_and_can_recover(
    monkeypatch, quiet_limits
):
    engine, process, _ = fake_engine(monkeypatch)
    try:
        quiet_limits.fail_on = True
        with pytest.raises(RuntimeError, match="native"):
            engine.set_quiet_mode(True)
        assert engine.quiet_mode is False and not process.terminated
        quiet_limits.fail_on = None
        engine.set_quiet_mode(True)
        quiet_limits.fail_on = False
        with pytest.raises(RuntimeError, match="native"):
            engine.set_quiet_mode(False)
        assert engine.quiet_mode is True and engine._loaded == "2b"
        # A child that has exited must not prevent OFF from recovering intent.
        process.terminate()
        engine.set_quiet_mode(False)
        assert engine.quiet_mode is False and engine._cpu_limit is None
    finally:
        quiet_limits.fail_on = None
        engine.close()


def test_windows_quiet_prepare_aborts_its_child_if_the_cap_cannot_be_applied(
    monkeypatch, tmp_path, quiet_limits
):
    monkeypatch.setattr(backend.Path, "home", lambda: tmp_path)
    process = FakeProcess()
    monkeypatch.setattr(backend.subprocess, "Popen", lambda *_, **__: process)
    engine = backend.LlamaEngine(cache_dir=tmp_path)
    engine.set_quiet_mode(True)
    quiet_limits.fail_on = True
    try:
        with pytest.raises(RuntimeError, match="native"):
            engine._start(tmp_path / "llama-server.exe", tmp_path / "weights.gguf", "2b", None)
        assert process.terminated and quiet_limits.closed == [1]
        assert engine._process is None and engine._client is None and engine._loaded is None
        assert engine.quiet_mode is True  # Future intent remains explicit; no silent unlimited run.
        engine.set_quiet_mode(False)
        assert engine.quiet_mode is False
    finally:
        engine.close()


def test_windows_quiet_cap_is_applied_before_model_health_is_reported(
    monkeypatch, tmp_path, quiet_limits
):
    import io

    monkeypatch.setattr(backend.Path, "home", lambda: tmp_path)
    process = FakeProcess()
    process.stdout = io.BytesIO(b"")
    commands = []

    def popen(arguments, **_):
        commands.append(arguments)
        return process

    class Client:
        def get(self, *_args, **_kwargs):
            assert quiet_limits.instances[0].enabled is True
            return httpx.Response(200)

        def close(self):
            pass

    monkeypatch.setattr(backend.subprocess, "Popen", popen)
    monkeypatch.setattr(backend.httpx, "Client", lambda **_: Client())
    engine = backend.LlamaEngine(cache_dir=tmp_path)
    engine.set_quiet_mode(True)
    try:
        engine._start(tmp_path / "llama-server.exe", tmp_path / "weights.gguf", "2b", None)
        assert engine._loaded == "2b" and engine.quiet_mode is True
        assert quiet_limits.instances[0].threads == int(
            commands[0][commands[0].index("--threads") + 1]
        )
        for flag, expected in {
            "--parallel": "32",
            "--ctx-size": "32768",
            "--kv-unified-per-slot": "32768",
            "--batch-size": "256",
            "--ubatch-size": "256",
            "--cache-ram": "0",
            "--gpu-layers": "0",
        }.items():
            assert commands[0][commands[0].index(flag) + 1] == expected
        assert {"--kv-unified", "--cont-batching", "--no-context-shift"} <= set(commands[0])
    finally:
        engine.close()
    assert quiet_limits.closed == [1]


def test_live_windows_levels_apply_without_reload_and_fail_without_changing_the_level(
    monkeypatch, quiet_limits
):
    engine, process, _ = fake_engine(monkeypatch)
    try:
        engine.set_quiet_level(backend.MAX_QUIET_LEVEL)
        limit = quiet_limits.instances[0]
        assert engine.quiet_level == backend.MAX_QUIET_LEVEL and limit.level_calls == [
            backend.MAX_QUIET_LEVEL
        ]
        engine.set_quiet_level(2)
        assert engine.quiet_level == 2 and limit.level == 2
        quiet_limits.fail_on = True
        with pytest.raises(RuntimeError, match="native"):
            engine.set_quiet_level(3)
        assert engine.quiet_level == 2 and limit.level == 2
        quiet_limits.fail_on = None
        engine.set_quiet_mode(True)
        assert engine.quiet_level == backend.LEGACY_QUIET_LEVEL
        engine.set_quiet_mode(False)
        assert engine.quiet_level == 0 and engine.quiet_mode is False
        assert engine._process is process and not process.terminated and engine._loaded == "2b"
        with pytest.raises(ValueError):
            engine.set_quiet_level(True)
    finally:
        quiet_limits.fail_on = None
        engine.close()


def test_native_iterator_publishes_bounded_canonical_previews_and_a_strict_final(monkeypatch):
    from plamo_cp.engine import TranslationWork

    source = "<plm-a>An integer [[PLM_0]]</plm-a>."
    engine, process, _ = fake_engine(
        monkeypatch,
        content=['<span id="plm-a">整数 <math-', "a/>", "</span>。"],
    )
    previews = []
    work = TranslationWork("work", source, {}, progress=previews.append)
    pending = [work]
    try:
        results = list(
            engine.iter_work("2b", lambda _: [pending.pop(0)] if pending else [], lambda: 32)
        )
        assert results == [(work, "<plm-a>整数 [[PLM_0]]</plm-a>。")]
        assert previews == ["<plm-a>整数 "]  # Incomplete model tag is withheld.
        assert all("<math-" not in preview and "<span" not in preview for preview in previews)
        assert engine.supported_translation_previews is True
        assert engine.supported_batch_sizes == tuple(range(1, 33))
        assert engine.status["state"] == "idle" and not process.terminated
    finally:
        engine.close()


@pytest.mark.parametrize("broken_callback", [False, True])
def test_windows_preview_bound_or_callback_failure_never_changes_the_final(
    monkeypatch, broken_callback
):
    from plamo_cp.engine import TranslationWork

    translated = "訳" * 20000
    engine, _, _ = fake_engine(monkeypatch, content=[translated, "。"])
    previews = []

    def publish(text):
        previews.append(text)
        if broken_callback:
            raise RuntimeError("Browser preview callback failed")

    work = TranslationWork("large", "The source.", {}, progress=publish)
    pending = [work]
    try:
        assert list(
            engine.iter_work("2b", lambda _: [pending.pop(0)] if pending else [], lambda: 32)
        ) == [(work, translated + "。")]
        assert len(previews) == 1 and len(previews[0]) == 16384
    finally:
        engine.close()


def test_empty_native_cohort_does_not_prepare_and_always_restores_idle(monkeypatch):
    engine, process, calls = fake_engine(monkeypatch)
    monkeypatch.setattr(engine, "prepare", lambda *_: pytest.fail("Empty cohort must not load"))
    try:
        assert list(engine.iter_work("2b", lambda _: [], lambda: 32)) == []
        assert engine.status["state"] == "idle" and not process.terminated and calls == []
    finally:
        engine.close()
