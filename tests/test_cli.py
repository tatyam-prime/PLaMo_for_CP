"""CLI model selection is checked with fake engines/downloaders only."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from plamo_cp import cli


@pytest.mark.parametrize("explicit,expected", [(False, "2b"), (True, "0.8b")])
def test_cli_translation_defaults_to_2b_and_preserves_explicit_small_model(
    monkeypatch, capsys, explicit, expected
):
    calls = []
    monkeypatch.setattr(
        cli,
        "make_engine",
        lambda: SimpleNamespace(translate=lambda *args: calls.append(args) or "訳文"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["plamo-cp", "translate", "Find the answer.", *(["--model", "0.8b"] if explicit else [])],
    )
    cli.main()
    assert calls == [("Find the answer.", expected, "English", "Japanese")]
    assert capsys.readouterr().out.strip() == "訳文"


def test_cli_download_command_selects_2b_without_real_network(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        cli,
        "make_engine",
        lambda: SimpleNamespace(download=lambda model: calls.append(model) or "/fake/cache"),
    )
    monkeypatch.setattr(sys, "argv", ["plamo-cp", "download"])
    cli.main()
    assert calls == ["2b"] and capsys.readouterr().out.strip() == "/fake/cache"


def test_cli_models_uses_the_selected_runtime_specs(monkeypatch, capsys):
    from plamo_cp.llama_backend import GGUF_MODELS

    monkeypatch.setattr(cli, "make_engine", lambda: SimpleNamespace(model_specs=GGUF_MODELS))
    monkeypatch.setattr(sys, "argv", ["plamo-cp", "models"])
    cli.main()
    specs = json.loads(capsys.readouterr().out)
    assert set(specs) == {"0.8b", "2b", "8b"}
    assert all(item["repo"].endswith("-GGUF") for item in specs.values())


def test_cli_download_reports_an_actionable_error_without_traceback(monkeypatch, capsys):
    def unavailable(model):
        raise RuntimeError("保存先に書き込めません。")

    monkeypatch.setattr(cli, "make_engine", lambda: SimpleNamespace(download=unavailable))
    monkeypatch.setattr(sys, "argv", ["plamo-cp", "download"])
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 1
    assert "保存先に書き込めません。" in capsys.readouterr().err


@pytest.mark.parametrize("port_args,port", [([], cli.DEFAULT_PORT), (["--port", "18765"], 18765)])
def test_cli_server_stays_on_loopback_and_does_not_log_page_access(monkeypatch, port_args, port):
    import uvicorn

    import plamo_cp.api

    sentinel = object()
    calls = []
    monkeypatch.setattr(plamo_cp.api, "create_app", lambda: sentinel)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(sys, "argv", ["plamo-cp", "serve", *port_args])
    cli.main()
    assert calls == [((sentinel,), {"host": "127.0.0.1", "port": port, "access_log": False})]
