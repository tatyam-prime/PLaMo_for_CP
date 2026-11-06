"""Cross-platform contract tests; no Windows executable or weights are fetched."""

from __future__ import annotations

import hashlib
import json
import threading
import time
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
    assert "<|plamo:index|><plm-a><|plamo:item|><plm-a>" in prompt
    assert "<|plamo:index|>[[PLM_0]]<|plamo:item|>[[PLM_0]]" in prompt
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
    "language,code", [("Chinese", "zh"), ("zh", "zh"), ("Indonesian", "id"), ("id", "id")]
)
def test_pfml_prompt_supports_chinese_and_indonesian_without_extra_message_boundaries(
    language, code
):
    prompt = backend.build_prompt("给定整数 [[PLM_0]]。", language, "Japanese")
    assert (
        f"<|plamo:tag|>input<|plamo:key|>lang<|plamo:val|>{code}<|plamo:msg|>给定整数 [[PLM_0]]。"
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


def test_omitted_dictionary_keeps_the_compact_canonical_default_prompt():
    text = "Find the <plm-a>smallest</plm-a> integer in the array [[PLM_0]]."
    prompt = backend.build_prompt(text, "English", "Japanese")
    assert hashlib.sha256(prompt.encode()).hexdigest() == (
        "c18914a41605309d679dbb021ba04ad798bd8a14b6eb82bd70b5d8ad1f502aba"
    )
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
    assert "<|plamo:index|><plm-a><|plamo:item|><plm-a>" in prompt
    assert "<|plamo:index|>[[PLM_0]]<|plamo:item|>[[PLM_0]]" in prompt


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


def fake_engine(monkeypatch, *, content="[[PLM_0]] 個の整数。", stop_type="eos", truncated=False):
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
        event = {"content": content, "stop": True, "stop_type": stop_type, "truncated": truncated}
        data = "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        return httpx.Response(200, text=data)

    engine._client = httpx.Client(
        base_url="http://127.0.0.1:12345", transport=httpx.MockTransport(handle)
    )
    monkeypatch.setattr(engine, "model_available", lambda model: True)
    monkeypatch.setattr(engine, "prepare", lambda model, progress=None: None)
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


def test_cancellation_interrupts_prefill_owned_process(monkeypatch):
    engine, process, _ = fake_engine(monkeypatch)
    cancelled = threading.Event()

    def handle(request):
        cancelled.set()
        deadline = time.monotonic() + 2
        while not process.terminated and time.monotonic() < deadline:
            time.sleep(0.01)
        raise httpx.ReadError("owned process stopped")

    engine._client.close()
    engine._client = httpx.Client(
        base_url="http://127.0.0.1:12345", transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(InterruptedError):
            engine.translate("Hello.", cancelled=cancelled)
        assert process.terminated
        assert engine.status["loaded_model"] is None
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
