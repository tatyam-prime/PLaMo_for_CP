"""Bounded translation-only cache with fixed expiry and indexed LRU eviction."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

MAX_CACHE_BYTES = 1024 * 1024
RETENTION_DAYS = 7
MIN_POLICY_BYTES = 256 * 1024
MAX_POLICY_BYTES = 64 * 1024 * 1024
POLICY_VERSION = 3
PREVIOUS_DEFAULT_BYTES = 10 * 1024 * 1024
JOURNAL_RETAINED_BYTES = 1024 * 1024


class TranslationCache:
    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int | None = None,
        retention_days: int | None = None,
        clock: Callable[[], float] = time.time,
        max_database_bytes: int | None = None,
    ):
        if (max_bytes is not None and int(max_bytes) <= 0) or (
            retention_days is not None and int(retention_days) <= 0
        ):
            raise ValueError("Cache capacity and retention must be positive.")
        self.max_bytes = int(MAX_CACHE_BYTES if max_bytes is None else max_bytes)
        self.retention_days = int(RETENTION_DAYS if retention_days is None else retention_days)
        self._requested_max_bytes = max_bytes
        self._capacity_custom = max_bytes is not None
        self._requested_retention_days = retention_days
        self._database_override = max_database_bytes
        self._clock = clock
        self._lock = threading.Lock()
        if str(path) != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(Path(path).expanduser()), check_same_thread=False)
        self._connection.execute("PRAGMA busy_timeout=5000")
        previous_vacuum = self._connection.execute("PRAGMA auto_vacuum").fetchone()[0]
        self._connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(f"PRAGMA journal_size_limit={JOURNAL_RETAINED_BYTES}")
        self._connection.execute("PRAGMA wal_autocheckpoint=128")
        self._initialize_schema()
        self.database_max_bytes = max(64 * 1024, int(max_database_bytes or self.max_bytes))
        self._purge_expired_locked(self._clock())
        excess = self._metadata_locked()[1] - self.max_bytes
        if excess > 0:
            self._evict_lru_locked(excess)
        # One migration VACUUM enables incremental reclamation for legacy DBs.
        # Run after expired/over-budget rows are removed, preserving valid rows.
        if previous_vacuum != 2:
            self._checkpoint_locked()
            self._connection.execute("VACUUM")
        self._page_size = self._connection.execute("PRAGMA page_size").fetchone()[0]
        self._set_database_limit_locked()

    def _set_database_limit_locked(self) -> None:
        self.database_max_bytes = max(64 * 1024, int(self._database_override or self.max_bytes))
        self._max_pages = max(16, self.database_max_bytes // self._page_size)
        self.database_max_bytes = self._max_pages * self._page_size
        while self._connection.execute("PRAGMA page_count").fetchone()[0] > self._max_pages:
            pages = self._connection.execute("PRAGMA page_count").fetchone()[0]
            if not self._evict_lru_locked((pages - self._max_pages) * self._page_size):
                break
            self._reclaim_locked()
        self._connection.execute(f"PRAGMA max_page_count={self._max_pages}")
        self._reclaim_locked()

    def _initialize_schema(self) -> None:
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS translations ("
            "cache_key TEXT PRIMARY KEY, translation TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "expires_at REAL NOT NULL DEFAULT 0, last_access REAL NOT NULL DEFAULT 0, "
            "size_bytes INTEGER NOT NULL DEFAULT 0)"
        )
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(translations)")}
        migrated = False
        for name, definition in (
            ("expires_at", "REAL NOT NULL DEFAULT 0"),
            ("last_access", "REAL NOT NULL DEFAULT 0"),
            ("size_bytes", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                self._connection.execute(f"ALTER TABLE translations ADD COLUMN {name} {definition}")
                migrated = True
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS cache_meta (id INTEGER PRIMARY KEY CHECK(id=1), "
            "entries INTEGER NOT NULL, total_bytes INTEGER NOT NULL, retention_days INTEGER NOT NULL, "
            "max_bytes INTEGER NOT NULL DEFAULT 0, policy_version INTEGER NOT NULL DEFAULT 1, "
            "capacity_custom INTEGER NOT NULL DEFAULT 0)"
        )
        meta_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(cache_meta)")}
        for name, default in (("max_bytes", 0), ("policy_version", 1), ("capacity_custom", 0)):
            if name not in meta_columns:
                self._connection.execute(
                    f"ALTER TABLE cache_meta ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}"
                )
        meta = self._connection.execute(
            "SELECT retention_days,max_bytes,policy_version,capacity_custom FROM cache_meta WHERE id=1"
        ).fetchone()
        if meta and meta[2] >= 2:
            if self._requested_retention_days is None:
                self.retention_days = int(meta[0])
            if self._requested_max_bytes is None:
                saved_capacity = int(meta[1])
                if meta[2] >= POLICY_VERSION:
                    self._capacity_custom = bool(meta[3])
                else:
                    # v2 did not distinguish an explicit 10MiB selection from
                    # its old default. Both migrate once to the new 1MiB default.
                    self._capacity_custom = saved_capacity != PREVIOUS_DEFAULT_BYTES
                capacity = saved_capacity if self._capacity_custom else MAX_CACHE_BYTES
                self.max_bytes = min(MAX_POLICY_BYTES, max(MIN_POLICY_BYTES, capacity))
        if migrated or (meta and meta[0] != self.retention_days):
            for key, created, translation in self._connection.execute(
                "SELECT cache_key, created_at, translation FROM translations"
            ).fetchall():
                try:
                    stamp = datetime.fromisoformat(created)
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=UTC)
                    timestamp = stamp.timestamp()
                    if not math.isfinite(timestamp):
                        raise ValueError("invalid timestamp")
                except (ValueError, TypeError, OverflowError):
                    timestamp = 0  # Invalid legacy metadata is expired, never made fresh.
                self._connection.execute(
                    "UPDATE translations SET expires_at=?, last_access=CASE WHEN ? THEN ? ELSE last_access END, size_bytes=? WHERE cache_key=?",
                    (
                        timestamp + self.retention_days * 86400,
                        migrated,
                        timestamp,
                        len(translation.encode("utf-8")),
                        key,
                    ),
                )
        if meta is None:
            count, size = self._connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes),0) FROM translations"
            ).fetchone()
            self._connection.execute(
                "INSERT INTO cache_meta(id,entries,total_bytes,retention_days,max_bytes,policy_version,capacity_custom) VALUES(1,?,?,?,?,?,?)",
                (
                    count,
                    size,
                    self.retention_days,
                    self.max_bytes,
                    POLICY_VERSION,
                    self._capacity_custom,
                ),
            )
        else:
            self._connection.execute(
                "UPDATE cache_meta SET retention_days=?,max_bytes=?,policy_version=?,capacity_custom=? WHERE id=1",
                (self.retention_days, self.max_bytes, POLICY_VERSION, self._capacity_custom),
            )
        self._connection.executescript("""
            CREATE INDEX IF NOT EXISTS translations_expiry ON translations(expires_at);
            CREATE INDEX IF NOT EXISTS translations_lru ON translations(last_access,created_at,cache_key);
            CREATE TRIGGER IF NOT EXISTS cache_count_insert AFTER INSERT ON translations BEGIN
              UPDATE cache_meta SET entries=entries+1,total_bytes=total_bytes+NEW.size_bytes WHERE id=1;
            END;
            CREATE TRIGGER IF NOT EXISTS cache_count_delete AFTER DELETE ON translations BEGIN
              UPDATE cache_meta SET entries=entries-1,total_bytes=total_bytes-OLD.size_bytes WHERE id=1;
            END;
            CREATE TRIGGER IF NOT EXISTS cache_count_update AFTER UPDATE ON translations BEGIN
              UPDATE cache_meta SET total_bytes=total_bytes+NEW.size_bytes-OLD.size_bytes WHERE id=1;
            END;
        """)
        self._connection.commit()

    @staticmethod
    def key(*parts: str) -> str:
        data = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def _metadata_locked(self) -> tuple[int, int]:
        row = self._connection.execute(
            "SELECT entries,total_bytes FROM cache_meta WHERE id=1"
        ).fetchone()
        return int(row[0]), int(row[1])

    def _checkpoint_locked(self) -> None:
        self._connection.commit()
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def _purge_expired_locked(self, now: float) -> int:
        deleted = 0
        while True:
            with self._connection:
                cursor = self._connection.execute(
                    "DELETE FROM translations WHERE cache_key IN "
                    "(SELECT cache_key FROM translations WHERE expires_at<=? LIMIT 32)",
                    (now,),
                )
            if not cursor.rowcount:
                break
            deleted += cursor.rowcount
            self._checkpoint_locked()
        return deleted

    def _evict_lru_locked(self, required_bytes: int) -> int:
        removed = 0
        while removed < required_bytes:
            candidates = self._connection.execute(
                "SELECT cache_key,size_bytes FROM translations ORDER BY last_access,created_at,cache_key LIMIT 32"
            ).fetchall()
            if not candidates:
                break
            with self._connection:
                for key, size in candidates:
                    self._connection.execute("DELETE FROM translations WHERE cache_key=?", (key,))
                    removed += size
                    if removed >= required_bytes:
                        break
            self._checkpoint_locked()
        return removed

    def _reclaim_locked(self) -> None:
        self._connection.commit()
        previous = self._connection.execute("PRAGMA freelist_count").fetchone()[0]
        if previous > 512:
            # A large cleanup is faster as one compacting copy than thousands
            # of single-page incremental-vacuum steps through Python's driver.
            self._connection.execute("VACUUM")
            self._checkpoint_locked()
            return
        while previous:
            self._connection.execute("PRAGMA incremental_vacuum(256)").fetchall()
            remaining = self._connection.execute("PRAGMA freelist_count").fetchone()[0]
            if remaining >= previous:
                break
            previous = remaining
        self._checkpoint_locked()

    def get(self, key: str) -> str | None:
        with self._lock:
            now = self._clock()
            if self._purge_expired_locked(now):
                self._reclaim_locked()
            row = self._connection.execute(
                "SELECT translation,expires_at FROM translations WHERE cache_key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            if row[1] <= now:
                with self._connection:
                    self._connection.execute("DELETE FROM translations WHERE cache_key=?", (key,))
                self._reclaim_locked()
                return None
            with self._connection:
                self._connection.execute(
                    "UPDATE translations SET last_access=? WHERE cache_key=?", (now, key)
                )
            self._checkpoint_locked()
            return row[0]

    def put(self, key: str, translation: str) -> bool:
        size = len(translation.encode("utf-8"))
        with self._lock:
            now = self._clock()
            self._purge_expired_locked(now)
            # Remove an old value too, so an uncacheable replacement cannot expose stale text.
            with self._connection:
                self._connection.execute("DELETE FROM translations WHERE cache_key=?", (key,))
            if size > self.max_bytes:
                self._reclaim_locked()
                return False
            excess = self._metadata_locked()[1] + size - self.max_bytes
            if excess > 0:
                self._evict_lru_locked(excess)
            created = datetime.fromtimestamp(now, UTC).isoformat()
            while True:
                try:
                    with self._connection:
                        self._connection.execute(
                            "INSERT INTO translations(cache_key,translation,created_at,expires_at,last_access,size_bytes) "
                            "VALUES(?,?,?,?,?,?)",
                            (
                                key,
                                translation,
                                created,
                                now + self.retention_days * 86400,
                                now,
                                size,
                            ),
                        )
                    self._reclaim_locked()
                    return True
                except sqlite3.DatabaseError as error:
                    self._connection.rollback()
                    if getattr(error, "sqlite_errorcode", None) != sqlite3.SQLITE_FULL:
                        raise
                    # SQLite overhead can reach the physical cap before the payload cap.
                    if not self._evict_lru_locked(max(size // 4, 4096)):
                        self._reclaim_locked()
                        return False
                    self._reclaim_locked()

    def _stats_locked(self) -> dict[str, int | str]:
        entries, size = self._metadata_locked()
        return {
            "entries": entries,
            "bytes": size,
            "max_bytes": self.max_bytes,
            "retention_days": self.retention_days,
            "eviction_policy": "lru",
            "database_max_bytes": self.database_max_bytes,
        }

    def stats(self) -> dict[str, int | str]:
        with self._lock:
            if self._purge_expired_locked(self._clock()):
                self._reclaim_locked()
            return self._stats_locked()

    def set_policy(self, retention_days: int, max_bytes: int) -> dict[str, int | str]:
        """Apply and persist real limits immediately, without reviving expired text."""
        if (
            not isinstance(retention_days, int)
            or isinstance(retention_days, bool)
            or not 1 <= retention_days <= 30
        ):
            raise ValueError("保存期間は1〜30日の整数で指定してください。")
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not MIN_POLICY_BYTES <= max_bytes <= MAX_POLICY_BYTES
        ):
            raise ValueError("保存容量は256KiB〜64MiBの範囲で指定してください。")
        with self._lock:
            now = self._clock()
            self._purge_expired_locked(now)
            excess = self._metadata_locked()[1] - max_bytes
            if excess > 0:
                self._evict_lru_locked(excess)
                self._reclaim_locked()
            difference = (retention_days - self.retention_days) * 86400
            if difference < 0:
                self._purge_expired_locked(now - difference)
                self._reclaim_locked()
            target_database = max(64 * 1024, int(self._database_override or max_bytes))
            current_limit = self._connection.execute("PRAGMA max_page_count").fetchone()[0]
            if target_database // self._page_size > current_limit:
                self._connection.execute(
                    f"PRAGMA max_page_count={target_database // self._page_size}"
                )
            with self._connection:
                if difference:
                    self._connection.execute(
                        "UPDATE translations SET expires_at=expires_at+?", (difference,)
                    )
                self._connection.execute(
                    "UPDATE cache_meta SET retention_days=?,max_bytes=?,policy_version=?,capacity_custom=1 WHERE id=1",
                    (retention_days, max_bytes, POLICY_VERSION),
                )
            self.retention_days, self.max_bytes = retention_days, max_bytes
            self._capacity_custom = True
            self._set_database_limit_locked()
            return self._stats_locked()

    def clear(self) -> int:
        with self._lock:
            count = self._metadata_locked()[0]
            # Batches keep an expired/cleared database from accumulating a huge WAL.
            while self._metadata_locked()[0]:
                self._evict_lru_locked(1024 * 1024)
            self._reclaim_locked()
            return count

    def close(self) -> None:
        with self._lock:
            self._checkpoint_locked()
            self._connection.close()
