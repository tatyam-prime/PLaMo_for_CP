"""First-run preparation, started only by an explicit download button."""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .engine import DEFAULT_MODEL_ID, MODEL_SPECS, SUPPORTED_MODEL_IDS, default_glossary
from .performance import PerformanceSettings
from .settings import DEFAULT_SETTINGS_PATH, SettingsStore

logger = logging.getLogger(__name__)


def device_info() -> dict:
    system, machine = platform.system(), platform.machine().lower()
    memory = 0
    try:
        if system == "Darwin":
            memory = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]))
        elif system == "Windows":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("load", ctypes.c_ulong),
                    *[
                        (name, ctypes.c_ulonglong)
                        for name in (
                            "total",
                            "available",
                            "page_total",
                            "page_available",
                            "virtual_total",
                            "virtual_available",
                            "extended",
                        )
                    ],
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                memory = status.total
        elif hasattr(os, "sysconf"):
            memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError, AttributeError):
        logger.debug("Could not detect memory", exc_info=True)
    supported = (system == "Darwin" and machine == "arm64") or (
        system == "Windows" and machine in {"amd64", "x86_64", "arm64", "aarch64"}
    )
    return {
        "system": system,
        "machine": machine,
        "memory_gb": round(memory / 1024**3, 1),
        "supported": supported,
    }


class SetupController:
    def __init__(self, engine, executor: ThreadPoolExecutor, settings_path=None):
        self.engine, self.executor = engine, executor
        self.settings_path = Path(settings_path or DEFAULT_SETTINGS_PATH)
        self.settings = SettingsStore(self.settings_path)
        self.performance = PerformanceSettings(engine, self.settings)
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._future = None
        self._state = {"stage": "idle", "model": None, "bytes_received": 0, "error": None}
        self._download_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="plamo-download"
        )
        self._download_future = None
        self._download_cancelled = threading.Event()
        self._download_state = {"stage": "idle", "model": None, "bytes_received": 0, "error": None}
        self.selected_model = DEFAULT_MODEL_ID
        selected = self.settings.read().get("selected_model")
        if isinstance(selected, str) and selected in SUPPORTED_MODEL_IDS:
            self.selected_model = selected
        else:
            # Recommend the small model only when RAM detection positively
            # establishes that the default does not fit. Preserve saved choices.
            memory = device_info()["memory_gb"]
            required = self.model_specs.get(DEFAULT_MODEL_ID, MODEL_SPECS[DEFAULT_MODEL_ID])[
                "min_memory_gb"
            ]
            if memory > 0 and memory + 0.5 < required:
                self.selected_model = "0.8b"

    @property
    def model_specs(self):
        return {
            model: spec
            for model, spec in getattr(self.engine, "model_specs", MODEL_SPECS).items()
            if model in SUPPORTED_MODEL_IDS
        }

    def model_available(self, model: str) -> bool:
        if self.is_downloading(model):
            return False
        check = getattr(self.engine, "model_available", None)
        return bool(check(model)) if check else False

    def snapshot(self) -> dict:
        device = device_info()
        models = []
        for model_id, spec in self.model_specs.items():
            item = dict(spec)
            compatible = device["supported"] and device["memory_gb"] + 0.5 >= spec["min_memory_gb"]
            item.update(
                id=model_id, available=self.model_available(model_id), compatible=compatible
            )
            models.append(item)
        with self._lock:
            preparation = dict(self._state)
            download = dict(self._download_state)
        return {
            "application": "plamo-for-cp",
            "selected_model": self.selected_model,
            **self.performance.snapshot(),
            "device": device,
            "models": models,
            "preparation": preparation,
            "download": download,
            "supported_batch_sizes": list(getattr(self.engine, "supported_batch_sizes", (1,))),
            "supported_translation_jobs": True,
            "supported_translation_previews": bool(
                getattr(self.engine, "supported_translation_previews", False)
            ),
            "default_glossary": default_glossary(),
        }

    def start(self, model: str) -> dict:
        if model not in self.model_specs:
            raise ValueError("モデルを選択してください。")
        device = device_info()
        if not device["supported"]:
            raise RuntimeError(
                "このパソコンは対応していません。Windows または Apple Silicon Mac をお使いください。"
            )
        if device["memory_gb"] + 0.5 < self.model_specs[model]["min_memory_gb"]:
            raise ValueError("このモデルにはメモリが足りません。小さいモデルを選択してください。")
        if not self.model_available(model):
            raise RuntimeError("先にモデルをダウンロードしてください。")
        with self._lock:
            if self._future is not None and not self._future.done():
                raise RuntimeError("モデルを準備しています。完了するまでお待ちください。")
            self._cancelled.clear()
            self._state = {"stage": "queued", "model": model, "bytes_received": 0, "error": None}
            self._future = self.executor.submit(self._prepare, model)
            return dict(self._state)

    def _progress(self, event: dict) -> None:
        if self._cancelled.is_set():
            raise InterruptedError("準備を中断しました。次回、続きから取得できます。")
        with self._lock:
            if "downloaded_bytes" in event:
                event = {**event, "bytes_received": event["downloaded_bytes"]}
            self._state.update(event)

    def _prepare(self, model: str) -> None:
        try:
            self._progress({"stage": "checking"})
            self.engine.prepare(model, progress=self._progress)
            self.settings.update(selected_model=model)
            with self._lock:
                self.selected_model = model
                self._state.update(stage="ready", error=None)
        except Exception as error:
            logger.exception("Model preparation failed")
            with self._lock:
                self._state.update(stage="error", error=str(error))

    def start_download(self, model: str) -> dict:
        if model not in self.model_specs:
            raise ValueError("モデルを選択してください。")
        device = device_info()
        if not device["supported"]:
            raise RuntimeError("このパソコンは対応していません。")
        if device["memory_gb"] + 0.5 < self.model_specs[model]["min_memory_gb"]:
            raise ValueError("このモデルにはメモリが足りません。小さいモデルを選択してください。")
        with self._lock:
            if self._download_future is not None and not self._download_future.done():
                raise RuntimeError(
                    "別のモデルをダウンロードしています。完了してからお試しください。"
                )
            self._download_cancelled.clear()
            self._download_state = {
                "stage": "queued",
                "model": model,
                "bytes_received": 0,
                "error": None,
            }
            self._download_future = self._download_executor.submit(self._download, model)
            return dict(self._download_state)

    def _download_progress(self, event: dict) -> None:
        if self._download_cancelled.is_set():
            raise InterruptedError("ダウンロードを中断しました。次回、続きから取得できます。")
        with self._lock:
            if "downloaded_bytes" in event:
                event = {**event, "bytes_received": event["downloaded_bytes"]}
            self._download_state.update(event)

    def _download(self, model: str) -> None:
        try:
            self._download_progress({"stage": "checking"})
            self.engine.download(
                model, progress=self._download_progress, cancelled=self._download_cancelled
            )
            self._download_progress({"stage": "ready", "error": None})
        except InterruptedError as error:
            with self._lock:
                self._download_state.update(stage="cancelled", error=str(error))
        except Exception as error:
            logger.exception("Model download failed")
            with self._lock:
                self._download_state.update(stage="error", error=str(error))

    def cancel_download(self) -> dict:
        self._download_cancelled.set()
        with self._lock:
            if (
                self._download_future is not None
                and not self._download_future.done()
                and self._download_state["stage"] not in {"ready", "error", "cancelled"}
            ):
                if self._download_future.cancel():
                    self._download_state.update(stage="cancelled", error=None)
                else:
                    self._download_state["stage"] = "cancelling"
            return dict(self._download_state)

    def cancel(self) -> None:
        self._cancelled.set()
        self._download_cancelled.set()

    def close(self) -> None:
        self.cancel()
        self._download_executor.shutdown(wait=True, cancel_futures=True)

    def is_downloading(self, model: str) -> bool:
        with self._lock:
            return (
                self._download_future is not None
                and not self._download_future.done()
                and self._download_state["model"] == model
            )

    @property
    def downloading(self) -> bool:
        with self._lock:
            return self._download_future is not None and not self._download_future.done()

    @property
    def preparing(self) -> bool:
        with self._lock:
            return self._future is not None and not self._future.done()

    @property
    def busy(self) -> bool:
        return self.preparing or self.downloading

    def forget_model(self, model: str) -> None:
        with self._lock:
            if self._state["model"] == model:
                self._state = {"stage": "idle", "model": None, "bytes_received": 0, "error": None}
            if self._download_state["model"] == model and (
                self._download_future is None or self._download_future.done()
            ):
                self._download_state = {
                    "stage": "idle",
                    "model": None,
                    "bytes_received": 0,
                    "error": None,
                }

    def reset_preparation(self) -> None:
        """A new destination must not inherit the previous folder's ready state."""
        with self._lock:
            self._state = {"stage": "idle", "model": None, "bytes_received": 0, "error": None}
            self._download_state = {
                "stage": "idle",
                "model": None,
                "bytes_received": 0,
                "error": None,
            }
