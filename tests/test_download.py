"""Download limits are tested without fetching model weights."""

import json
from contextlib import nullcontext

import httpx
import pytest

from plamo_cp import download


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


def limiter(rate=0.5):
    clock = FakeClock()
    return download.BandwidthLimiter(rate, clock=clock, sleep=clock.sleep), clock


def test_default_and_environment_rate(monkeypatch):
    monkeypatch.delenv("PLAMO_DOWNLOAD_MIBPS", raising=False)
    assert download.resolve_rate() is None
    monkeypatch.setenv("PLAMO_DOWNLOAD_MIBPS", "0.25")
    assert download.resolve_rate() == 0.25
    assert download.resolve_rate(0.125) == 0.125


def test_product_default_uses_normal_network_and_reports_actual_bytes(monkeypatch):
    monkeypatch.delenv("PLAMO_DOWNLOAD_MIBPS", raising=False)
    events = []
    clock = FakeClock()
    budget = download.BandwidthLimiter(None, clock=clock, sleep=clock.sleep, progress=events.append)
    transport = download.RateLimitedTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"model"))),
        budget,
    )
    with httpx.Client(transport=transport) as client:
        assert client.get("https://example.test/model").content == b"model"
    assert clock.sleeps == []
    assert events[-1]["bytes_received"] == 5


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), "unlimited", ""])
def test_no_unlimited_or_invalid_rate(value):
    with pytest.raises(ValueError, match="positive"):
        download.resolve_rate(value)


def test_half_mib_per_second_and_no_idle_burst():
    budget, clock = limiter()
    for _ in range(8):
        budget.wait_for(64 * 1024)
    assert clock.now == pytest.approx(1.0)
    clock.now += 100
    budget.wait_for(64 * 1024)
    assert clock.now == pytest.approx(101.125)
    assert clock.sleeps == pytest.approx([0.125] * 9)


def test_transport_waits_before_network_reads_and_closes_stream():
    budget, clock = limiter()
    reads = []
    closed = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(2):
                reads.append(clock.now)
                yield b"x" * download.READ_QUANTUM

        def close(self):
            closed.append(True)

    transport = download.RateLimitedTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())), budget
    )
    with httpx.Client(transport=transport) as client:
        response = client.get("https://example.test/model")
    assert len(response.content) == 128 * 1024
    assert reads == pytest.approx([0.125, 0.25])
    assert closed == [True]


def test_default_and_proxy_transports_are_all_limited(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    budget, _ = limiter()
    with download.limited_client_factory(budget) as client:
        assert isinstance(client._transport, download.RateLimitedTransport)
        assert all(
            transport is None or isinstance(transport, download.RateLimitedTransport)
            for transport in client._mounts.values()
        )
        assert isinstance(
            client._transport_for_url(httpx.URL("https://huggingface.co")),
            download.RateLimitedTransport,
        )
        pool = client._transport._transport._pool
        assert any(option[-1] == download.READ_QUANTUM for option in pool._socket_options)


def test_snapshot_single_worker_disables_xet_and_reuses_ready_manifest(monkeypatch, tmp_path):
    import huggingface_hub
    from huggingface_hub import constants

    calls = []
    factories = []
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text("{}")
    spec = {"repo": "pfnet/example", "revision": "1" * 40}

    def fake_snapshot(**kwargs):
        calls.append(kwargs)
        if kwargs.get("local_files_only") and not (snapshot / download.READY_FILE).exists():
            raise FileNotFoundError("not ready")
        return str(snapshot)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    monkeypatch.setattr(huggingface_hub, "set_client_factory", factories.append)
    monkeypatch.setattr(download, "_download_lock", nullcontext)
    monkeypatch.setattr(constants, "HF_HUB_DISABLE_XET", False)
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "0")
    assert download.download_snapshot(spec, allow_patterns=["config.json"]) == str(snapshot)
    assert all(call["max_workers"] == 1 for call in calls)
    assert all(call["revision"] == spec["revision"] for call in calls)
    assert constants.HF_HUB_DISABLE_XET is True
    assert len(factories) == 2
    assert [call.get("local_files_only", False) for call in calls] == [True, False]
    assert download.download_snapshot(spec, allow_patterns=["config.json"]) == str(snapshot)
    assert len(calls) == 3
    assert calls[-1]["local_files_only"] is True
    assert len(factories) == 2
    # A damaged cached file cannot pass the readiness marker.
    (snapshot / "config.json").write_text('{"changed":true}')
    download.download_snapshot(spec, allow_patterns=["config.json"])
    assert len(calls) == 5
    assert len(factories) == 4
    assert factories[1] is factories[3]


def test_complete_existing_model_cache_needs_no_network(monkeypatch, tmp_path):
    import huggingface_hub

    names = [
        "config.json",
        "generation_config.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "tokenizer.jsonl",
        "chat_template.jinja",
        "modeling_plamo_mlx.py",
        "tokenization_plamo.py",
        "model.safetensors",
    ]
    for name in names:
        (tmp_path / name).write_text("cached")
    calls = []

    def cached_snapshot(**kwargs):
        calls.append(kwargs)
        assert kwargs["local_files_only"] is True
        return str(tmp_path)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", cached_snapshot)
    monkeypatch.setattr(
        huggingface_hub,
        "set_client_factory",
        lambda factory: pytest.fail("network client should not be created"),
    )
    monkeypatch.setattr(download, "_download_lock", nullcontext)
    spec = {"repo": "pfnet/example", "revision": "1" * 40}
    assert download.download_snapshot(spec) == str(tmp_path)
    assert len(calls) == 1


def test_sharded_readiness_requires_every_weight_file(tmp_path):
    for name in [
        "config.json",
        "generation_config.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "tokenizer.jsonl",
        "chat_template.jinja",
        "modeling_plamo_mlx.py",
        "tokenization_plamo.py",
        "one.safetensors",
    ]:
        (tmp_path / name).write_text("cached")
    (tmp_path / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"a": "one.safetensors", "b": "two.safetensors"}})
    )
    assert not download._inference_files_ready(tmp_path)
    (tmp_path / "two.safetensors").write_text("cached")
    assert download._inference_files_ready(tmp_path)


def test_ready_manifest_never_includes_itself_when_rewritten(tmp_path):
    spec = {"repo": "pfnet/example", "revision": "1" * 40}
    (tmp_path / "config.json").write_text("{}")
    for _ in range(2):
        download._mark_ready(tmp_path, spec, download.ALLOW_PATTERNS)
        assert download._ready(tmp_path, spec, download.ALLOW_PATTERNS)
        manifest = json.loads((tmp_path / download.READY_FILE).read_text())
        assert download.READY_FILE not in manifest["files"]


def test_cancelled_transfer_never_reaches_network_and_releases_writer_lock(monkeypatch, tmp_path):
    import threading

    import huggingface_hub

    monkeypatch.setattr(download.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        huggingface_hub, "snapshot_download", lambda **_: pytest.fail("cancelled network")
    )
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError):
        download.download_snapshot(
            {"repo": "official/test", "revision": "pinned"}, cancelled=cancelled
        )
    assert download.DOWNLOAD_LOCK.acquire(blocking=False)
    download.DOWNLOAD_LOCK.release()


def test_cancel_interrupts_wait_for_another_process_download_budget(monkeypatch, tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(download.Path, "home", lambda: tmp_path)
    cancelled, started = threading.Event(), threading.Event()

    def waiting():
        started.set()
        with download._download_lock(cancelled):
            pytest.fail("Cancelled waiter must not acquire the writer lock")

    download.DOWNLOAD_LOCK.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(waiting)
            assert started.wait(1)
            cancelled.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=1)
    finally:
        download.DOWNLOAD_LOCK.release()


def test_cancelled_response_headers_close_transport_before_any_body_reads():
    import threading

    cancelled = threading.Event()
    closed = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("body must not be read")

        def close(self):
            closed.append(True)

    def headers(request):
        cancelled.set()
        return httpx.Response(200, stream=Body())

    budget = download.BandwidthLimiter(None, cancelled=cancelled)
    transport = download.RateLimitedTransport(httpx.MockTransport(headers), budget)
    with httpx.Client(transport=transport) as client, pytest.raises(InterruptedError):
        client.get("https://example.test/model")
    assert closed == [True]


def test_snapshot_failure_restores_previous_factory_instead_of_retaining_cancel_callback(
    monkeypatch,
):
    import huggingface_hub
    from huggingface_hub.utils import _http

    factories = []
    previous = _http._GLOBAL_CLIENT_FACTORY

    def interrupted(**options):
        if options.get("local_files_only"):
            raise FileNotFoundError
        raise InterruptedError("cancelled")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", interrupted)
    monkeypatch.setattr(huggingface_hub, "set_client_factory", factories.append)
    monkeypatch.setattr(download, "_download_lock", nullcontext)
    with pytest.raises(InterruptedError):
        download.download_snapshot({"repo": "official/test", "revision": "pinned"})
    assert len(factories) == 2 and factories[-1] is previous


@pytest.mark.parametrize("status,headers", [(429, {}), (503, {"Retry-After": "3600"})])
def test_cancellable_download_never_enters_uninterruptible_server_rate_limit_sleep(status, headers):
    import threading

    response = httpx.Response(status, headers=headers, stream=httpx.ByteStream(b"busy"))
    budget = download.BandwidthLimiter(None, cancelled=threading.Event())
    transport = download.RateLimitedTransport(httpx.MockTransport(lambda request: response), budget)
    with httpx.Client(transport=transport) as client, pytest.raises(RuntimeError, match="配布元"):
        client.get("https://example.test/model")
    assert response.is_closed


def test_cancellable_transfer_caps_even_explicit_unlimited_read_timeout():
    import threading

    seen = []

    def network(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, content=b"cached bytes")

    transport = download.RateLimitedTransport(
        httpx.MockTransport(network), download.BandwidthLimiter(None, cancelled=threading.Event())
    )
    with httpx.Client(transport=transport) as client:
        assert client.get("https://example.test/model", timeout=None).content == b"cached bytes"
    assert seen == [dict.fromkeys(("connect", "read", "write", "pool"), 10.0)]
