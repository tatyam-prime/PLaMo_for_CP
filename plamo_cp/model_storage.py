"""Inspect and remove only this application's known repos in the shared HF cache."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from huggingface_hub import constants, scan_cache_dir

from .download import _download_lock, _inference_files_ready
from .engine import MODEL_SPECS
from .settings import SettingsStore


class ModelStorage:
    def __init__(
        self,
        engine,
        cache_dir: str | Path | None = None,
        *,
        settings_path: str | Path | None = None,
    ):
        self.engine = engine
        self.model_specs = getattr(
            engine, "storage_model_specs", getattr(engine, "model_specs", MODEL_SPECS)
        )
        self.settings = SettingsStore(settings_path)
        saved = self.settings.read().get("model_folder")
        if cache_dir is None and isinstance(saved, str) and saved:
            candidate = Path(saved).expanduser()
            if candidate.is_absolute():
                cache_dir = candidate
        if cache_dir is None:
            cache_dir = getattr(engine, "cache_dir", None)
        self.cache_dir = Path(cache_dir or constants.HF_HUB_CACHE).expanduser().resolve()
        self._lock = threading.Lock()
        self._apply_engine_folder(self.cache_dir)

    def _apply_engine_folder(self, folder: Path) -> None:
        change = getattr(self.engine, "set_cache_dir", None)
        if change is not None:
            change(folder)
        else:
            self.engine.cache_dir = folder

    def _repo_path(self, model: str) -> Path:
        if model not in self.model_specs:
            raise ValueError("モデルを選択してください。")
        repo = self.model_specs[model]["repo"]
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("モデルの保存先設定が不正です。")
        if any(part in {".", ".."} for part in repo.split("/")):
            raise ValueError("モデルの保存先設定が不正です。")
        return self.cache_dir / ("models--" + repo.replace("/", "--"))

    @staticmethod
    def _under(path: Path, parent: Path) -> bool:
        return Path(os.path.abspath(path)).is_relative_to(parent)

    def _inspect_repo(self, repo: Path) -> tuple[dict[Path, tuple[int, int]], bool]:
        """Stat one repo only, deduplicating links to shared payloads, never reading them."""
        if repo.is_symlink():
            return {}, True
        if not repo.exists():
            return {}, False
        files, seen, unsafe = {}, set(), False
        for folder, directories, names in os.walk(repo, followlinks=False):
            for name in list(directories):
                if (Path(folder) / name).is_symlink():
                    unsafe = True
                    directories.remove(name)
            for name in names:
                path = Path(folder) / name
                physical = path.resolve()
                if not self._under(physical, self.cache_dir):
                    unsafe = True
                    continue
                try:
                    info = path.stat()
                except OSError:
                    continue
                identity = (info.st_dev, info.st_ino)
                if identity not in seen:
                    seen.add(identity)
                    files[physical] = (info.st_size, info.st_nlink)
        return files, unsafe

    def _available_path(self, model: str, repo: Path) -> Path | None:
        try:
            return self._available_path_checked(model, repo)
        except OSError:
            # A download can atomically replace a link between is_file/stat.
            return None

    def _available_path_checked(self, model: str, repo: Path) -> Path | None:
        spec = self.model_specs[model]
        revision = str(spec["revision"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", revision) or revision in {".", ".."}:
            raise ValueError("モデルのリビジョン設定が不正です。")
        snapshot = repo / "snapshots" / revision
        if not snapshot.is_dir():
            return None
        if filename := spec.get("filename"):
            if Path(filename).name != filename:
                raise ValueError("モデルのファイル設定が不正です。")
            path = snapshot / filename
            if (
                path.is_file()
                and path.stat().st_size > 0
                and (
                    spec.get("weight_bytes") is None or path.stat().st_size == spec["weight_bytes"]
                )
            ):
                return path
            return None
        return snapshot if _inference_files_ready(snapshot) else None

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        models = []
        loaded = self.engine.status.get("loaded_model")
        for model, spec in self.model_specs.items():
            repo = self._repo_path(model)
            files, unsafe = self._inspect_repo(repo)
            available = None if unsafe else self._available_path(model, repo)
            models.append(
                {
                    "id": model,
                    "name": spec["name"],
                    "weight_gb": spec["weight_gb"],
                    "downloaded": available is not None,
                    "path": str(available or repo)
                    if available is not None or repo.exists()
                    else None,
                    "on_disk_bytes": sum(size for size, _ in files.values()),
                    "loaded": loaded == model,
                }
            )
        return {
            "folder": str(self.cache_dir),
            "shared": self.cache_dir == Path(constants.HF_HUB_CACHE).expanduser().resolve(),
            "models": models,
        }

    def set_folder(self, folder: str | Path) -> dict:
        """Choose a future destination; never move or remove any previous files."""
        if not isinstance(folder, (str, Path)) or not str(folder).strip():
            raise ValueError("モデルの保存先フォルダーを選択してください。")
        try:
            selected = Path(folder).expanduser()
            if not selected.is_absolute():
                raise ValueError("保存先にはフォルダーの完全な場所を指定してください。")
            selected = selected.resolve()
            selected.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=selected):
                pass
        except (OSError, RuntimeError) as error:
            raise ValueError(
                "このフォルダーに保存できません。書き込み可能なフォルダーを選択してください。"
            ) from error
        with self._lock, _download_lock():
            previous = self.cache_dir
            if selected == previous:
                return self._snapshot_locked()
            if not hasattr(self.engine, "set_cache_dir"):
                unload = getattr(self.engine, "unload", None)
                if unload is not None:
                    unload(None)
            self._apply_engine_folder(selected)
            try:
                self.settings.update(model_folder=str(selected))
            except OSError:
                self._apply_engine_folder(previous)
                raise
            self.cache_dir = selected
            return self._snapshot_locked()

    def choose_folder(self) -> dict:
        from .folder_picker import choose_folder

        selected = choose_folder(self.cache_dir)
        return {"cancelled": True} if selected is None else self.set_folder(selected)

    def open_folder(self) -> None:
        """Open the fixed model-cache directory, never a client-supplied path."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if platform.system() == "Windows":
            os.startfile(str(self.cache_dir))
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(self.cache_dir)])
        else:
            raise RuntimeError(
                "このOSでは保存フォルダーを開けません。表示された場所を確認してください。"
            )

    def _validate_plan(self, strategy, repo: Path) -> None:
        plan_cache = getattr(strategy, "cache_dir", None)
        if plan_cache is not None and Path(plan_cache).resolve() != self.cache_dir:
            raise ValueError("モデル以外のキャッシュを含む削除はできません。")
        for path in getattr(strategy, "repos", ()):
            if Path(path).resolve() != repo:
                raise ValueError("ほかのモデルを含む削除はできません。")
        for field in ("snapshots", "refs", "files"):
            for path in getattr(strategy, field, ()):
                path = Path(path)
                if not self._under(path, repo) or not self._under(path.resolve(), self.cache_dir):
                    raise ValueError("モデルの保存先以外を含む削除はできません。")
        for path in getattr(strategy, "blobs", ()):
            path = Path(path)
            if (
                not self._under(path, self.cache_dir)
                or not self._under(path.resolve(), self.cache_dir)
                or not (self._under(path, repo) or self._under(path, self.cache_dir / "blobs"))
            ):
                raise ValueError("ほかのモデルのファイルを含む削除はできません。")

    def delete(self, model: str) -> dict:
        repo = self._repo_path(model)
        with self._lock, _download_lock():
            before, unsafe = self._inspect_repo(repo)
            if unsafe:
                raise ValueError("安全に削除できないリンクを含む保存先です。")
            unload = getattr(self.engine, "unload", None)
            if unload is not None:
                unload(model)
            if not repo.exists():
                return {"deleted": False, "freed_bytes": 0}
            information = scan_cache_dir(self.cache_dir)
            selected = [
                item
                for item in information.repos
                if item.repo_type == "model" and item.repo_id == self.model_specs[model]["repo"]
            ]
            if len(selected) > 1:
                raise ValueError("モデルの保存情報が重複しています。")
            if selected:
                cached_repo = selected[0]
                if Path(cached_repo.repo_path).resolve() != repo:
                    raise ValueError("モデルの保存先が一致しません。")
                revisions = [item.commit_hash for item in cached_repo.revisions]
                if revisions:
                    strategy = information.delete_revisions(*revisions)
                    self._validate_plan(strategy, repo)
                    strategy.execute()
            # Interrupted downloads may have no valid snapshot at all. Remove
            # only their repo-local directory; never sweep global blobs ourselves.
            if repo.exists():
                shutil.rmtree(repo)
            freed = sum(
                size for path, (size, links) in before.items() if links <= 1 and not path.exists()
            )
            return {"deleted": not repo.exists(), "freed_bytes": freed}
