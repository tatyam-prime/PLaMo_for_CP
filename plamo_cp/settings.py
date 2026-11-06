"""Small, atomic per-app settings; never changes global Hugging Face defaults."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from filelock import FileLock

DEFAULT_SETTINGS_PATH = Path.home() / ".config" / "plamo-cp" / "settings.json"


class SettingsStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_SETTINGS_PATH

    def read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def update(self, **changes) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + ".lock"):
            value = {**self.read(), **changes}
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent, delete=False
                ) as output:
                    temporary = Path(output.name)
                    json.dump(value, output, ensure_ascii=False, sort_keys=True)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return value


def model_cache_dir(cache_dir: str | Path | None = None) -> Path:
    """Explicit folders win; otherwise use saved app choice or the shared HF cache."""
    from huggingface_hub import constants

    selected = cache_dir
    if selected is None:
        saved = SettingsStore().read().get("model_folder")
        if isinstance(saved, str) and saved and Path(saved).expanduser().is_absolute():
            selected = saved
    return Path(selected or constants.HF_HUB_CACHE).expanduser().resolve()
