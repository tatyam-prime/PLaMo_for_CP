"""Persist inference duty presets only after the live runtime setting is applied."""

from __future__ import annotations

import threading

from .settings import SettingsStore

MAX_QUIET_LEVEL = 7
LEGACY_QUIET_LEVEL = 3


class PerformanceSettings:
    def __init__(self, engine, settings: SettingsStore):
        self._engine = engine
        self._settings = settings
        self._lock = threading.Lock()
        self._quiet_level = 0
        # A JSON string/number must not accidentally activate a resource limit.
        # Restoring the runtime flag does not prepare or download a model.
        saved = settings.read()
        level = saved.get("quiet_level")
        if type(level) is not int or not 0 <= level <= MAX_QUIET_LEVEL:
            level = LEGACY_QUIET_LEVEL if saved.get("quiet_mode") is True else 0
        if level:
            self._apply(level)
            self._quiet_level = level

    def _apply(self, level: int) -> None:
        apply = getattr(self._engine, "set_quiet_level", None)
        if apply is not None:
            apply(level)
            return
        legacy_apply = getattr(self._engine, "set_quiet_mode", None)
        if legacy_apply is not None and level in {0, LEGACY_QUIET_LEVEL}:
            legacy_apply(level > 0)
            return
        raise RuntimeError("この推論エンジンは指定した稼働率に対応していません。")

    def snapshot(self) -> dict:
        with self._lock:
            return {"quiet_level": self._quiet_level, "quiet_mode": self._quiet_level > 0}

    @property
    def quiet_level(self) -> int:
        with self._lock:
            return self._quiet_level

    @property
    def quiet_mode(self) -> bool:
        with self._lock:
            return self._quiet_level > 0

    def set_quiet_mode(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("静音モードの有効・無効を指定してください。")
        return self.set_quiet_level(LEGACY_QUIET_LEVEL if enabled else 0)

    def set_quiet_level(self, level: int) -> dict:
        if type(level) is not int or not 0 <= level <= MAX_QUIET_LEVEL:
            raise ValueError("推論の稼働率を選択してください。")
        # This lock is independent of the inference worker. Concurrent toggles
        # are serialized, while a running translation continues on that worker.
        with self._lock:
            previous = self._quiet_level
            if level != previous:
                self._apply(level)
                self._quiet_level = level
            try:
                # Merge this key atomically with model/folder settings, which
                # have their own independent writers and file lock.
                self._settings.update(quiet_level=level, quiet_mode=level > 0)
            except OSError as error:
                if level != previous:
                    try:
                        self._apply(previous)
                    except Exception as rollback_error:
                        raise RuntimeError(
                            "稼働率の設定を保存できず、動作を元に戻せませんでした。"
                            "管理画面の現在の状態を確認してから、もう一度切り替えてください。"
                        ) from rollback_error
                    self._quiet_level = previous
                raise RuntimeError(
                    "稼働率の設定を保存できませんでした。保存先の権限や空き容量を確認してください。"
                ) from error
            return {"quiet_level": self._quiet_level, "quiet_mode": self._quiet_level > 0}
