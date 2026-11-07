"""Keep tests independent of the desktop application's saved user settings."""

import pytest


@pytest.fixture(autouse=True)
def isolated_default_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("plamo_cp.settings.DEFAULT_SETTINGS_PATH", tmp_path / "app-settings.json")
    monkeypatch.setattr("plamo_cp.setup.DEFAULT_SETTINGS_PATH", tmp_path / "app-settings.json")
