"""All settings are temporary; the user's saved app preferences stay untouched."""

import json
from concurrent.futures import ThreadPoolExecutor

from plamo_cp.settings import SettingsStore


def test_settings_updates_preserve_other_keys_and_save_utf8_atomically(tmp_path):
    path = tmp_path / "設定/settings.json"
    first, second = SettingsStore(path), SettingsStore(path)
    first.update(selected_model="2b", unrelated={"name": "既存"})
    second.update(model_folder=str(tmp_path / "モデル保存先"))
    first.update(selected_model="0.8b")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "selected_model": "0.8b",
        "model_folder": str(tmp_path / "モデル保存先"),
        "unrelated": {"name": "既存"},
    }
    assert "モデル保存先" in path.read_text(encoding="utf-8")
    # filelock keeps its marker on Unix but may remove it on Windows.
    assert sorted(
        item.name for item in path.parent.iterdir() if item.name != "settings.json.lock"
    ) == ["settings.json"]


def test_independent_settings_writers_do_not_lose_fields(tmp_path):
    path = tmp_path / "settings.json"
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(SettingsStore(path).update, **{f"key_{i}": i}) for i in range(16)
        ]
        for future in futures:
            future.result()
    assert SettingsStore(path).read() == {f"key_{i}": i for i in range(16)}


def test_missing_or_invalid_settings_are_readonly_and_recoverable(tmp_path):
    path = tmp_path / "settings.json"
    assert SettingsStore(path).read() == {} and not path.exists()
    path.write_text("[]", encoding="utf-8")
    assert SettingsStore(path).read() == {}
    SettingsStore(path).update(selected_model="2b")
    assert SettingsStore(path).read() == {"selected_model": "2b"}
