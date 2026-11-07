"""Cache management reports translated-text size and keeps storage reusable."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from plamo_cp.cache import JOURNAL_RETAINED_BYTES, MAX_CACHE_BYTES, TranslationCache


def content_stats(cache):
    stats = cache.stats()
    return {"entries": stats["entries"], "bytes": stats["bytes"]}


def test_empty_stats_and_clear_are_idempotent():
    cache = TranslationCache(":memory:")
    try:
        assert content_stats(cache) == {"entries": 0, "bytes": 0}
        assert cache.clear() == 0
        assert cache.clear() == 0
        assert content_stats(cache) == {"entries": 0, "bytes": 0}
    finally:
        cache.close()


def test_stats_count_utf8_translation_bytes_and_replacements():
    cache = TranslationCache(":memory:")
    try:
        values = {"first": "日本語", "second": "abc🦊"}
        for key, value in values.items():
            cache.put(key, value)
        assert content_stats(cache) == {
            "entries": 2,
            "bytes": sum(len(value.encode("utf-8")) for value in values.values()),
        }
        cache.put("first", "短")
        assert content_stats(cache) == {"entries": 2, "bytes": len("短abc🦊".encode())}
    finally:
        cache.close()


def test_clear_returns_deleted_count_and_database_can_be_reused():
    cache = TranslationCache(":memory:")
    try:
        cache.put("before", "前の訳文")
        cache.put("another", "別の訳文")
        assert cache.clear() == 2
        assert cache.get("before") is None
        assert cache.get("another") is None
        cache.put("after", "新しい訳文")
        assert cache.get("after") == "新しい訳文"
        assert content_stats(cache) == {"entries": 1, "bytes": len("新しい訳文".encode())}
    finally:
        cache.close()


def test_clear_is_persistent_after_reopening(tmp_path):
    path = tmp_path / "nested" / "translations.sqlite"
    first = TranslationCache(path)
    first.put("persistent", "保存済みの訳文")
    first.close()
    second = TranslationCache(path)
    assert second.stats()["entries"] == 1
    assert second.clear() == 1
    second.close()
    third = TranslationCache(path)
    try:
        assert third.get("persistent") is None
        assert content_stats(third) == {"entries": 0, "bytes": 0}
        third.put("reused", "再利用")
        assert third.get("reused") == "再利用"
    finally:
        third.close()


def test_stats_and_clear_can_share_connection_with_workers():
    cache = TranslationCache(":memory:")
    try:

        def use_cache(index):
            cache.put(str(index), "訳文")
            snapshot = cache.stats()
            assert snapshot["bytes"] == snapshot["entries"] * len("訳文".encode())
            if index % 7 == 0:
                cache.clear()

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(use_cache, range(40)))
        final = cache.stats()
        assert cache.clear() == final["entries"]
        assert content_stats(cache) == {"entries": 0, "bytes": 0}
    finally:
        cache.close()


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 5, tzinfo=UTC).timestamp()

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_default_policy_is_7_days_and_1_mib():
    cache = TranslationCache(":memory:")
    try:
        assert cache.stats() == {
            "entries": 0,
            "bytes": 0,
            "max_bytes": MAX_CACHE_BYTES,
            "retention_days": 7,
            "eviction_policy": "lru",
            "database_max_bytes": MAX_CACHE_BYTES,
        }
    finally:
        cache.close()


def test_expiry_is_from_creation_and_a_read_does_not_extend_it():
    clock = Clock()
    cache = TranslationCache(":memory:", clock=clock)
    try:
        cache.put("a", "訳文")
        clock.advance(6 * 86400)
        assert cache.get("a") == "訳文"
        clock.advance(86400)
        assert cache.get("a") is None
        assert content_stats(cache) == {"entries": 0, "bytes": 0}
    finally:
        cache.close()


def test_lru_evicts_the_least_recently_read_translation():
    clock = Clock()
    cache = TranslationCache(":memory:", max_bytes=8, clock=clock)
    try:
        cache.put("a", "aaaa")
        clock.advance(1)
        cache.put("b", "bbbb")
        clock.advance(1)
        assert cache.get("a") == "aaaa"
        clock.advance(1)
        cache.put("c", "cccc")
        assert cache.get("b") is None
        assert cache.get("a") == "aaaa"
        assert cache.get("c") == "cccc"
        assert content_stats(cache) == {"entries": 2, "bytes": 8}
    finally:
        cache.close()


def test_oversized_utf8_translation_is_not_stored_and_does_not_break_later_writes():
    cache = TranslationCache(":memory:", max_bytes=8)
    try:
        assert cache.put("a", "日本語") is False  # Nine UTF-8 bytes, not three.
        assert cache.get("a") is None
        assert cache.put("small", "ok") is True
        assert cache.get("small") == "ok"
        assert cache.put("small", "日本語") is False
        assert cache.get("small") is None
    finally:
        cache.close()


def test_ttl_and_lru_metadata_survive_restarting_the_cache(tmp_path):
    path, clock = tmp_path / "persistent.sqlite", Clock()
    first = TranslationCache(path, max_bytes=8, clock=clock)
    first.put("a", "aaaa")
    clock.advance(1)
    first.put("b", "bbbb")
    clock.advance(1)
    first.get("a")
    first.close()
    second = TranslationCache(path, max_bytes=8, clock=clock)
    try:
        clock.advance(1)
        second.put("c", "cccc")
        assert second.get("b") is None
        assert second.get("a") == "aaaa"
        clock.advance(30 * 86400)
        assert content_stats(second) == {"entries": 0, "bytes": 0}
    finally:
        second.close()


def legacy_database(path, rows):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE translations(cache_key TEXT PRIMARY KEY, translation TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.executemany("INSERT INTO translations VALUES(?,?,?)", rows)
    connection.commit()
    connection.close()


def test_legacy_utc_created_at_is_migrated_without_refreshing_or_losing_valid_japanese(tmp_path):
    path, clock = tmp_path / "legacy.sqlite", Clock()
    recent = datetime.fromtimestamp(clock() - 5 * 86400, UTC).strftime("%Y-%m-%d %H:%M:%S")
    expired = datetime.fromtimestamp(clock() - 31 * 86400, UTC).strftime("%Y-%m-%d %H:%M:%S")
    legacy_database(path, [("recent", "有効な日本語🦊", recent), ("expired", "古い訳文", expired)])
    cache = TranslationCache(path, clock=clock)
    try:
        assert cache.get("recent") == "有効な日本語🦊"
        assert cache.get("expired") is None
        assert (
            cache._connection.execute(
                "SELECT created_at FROM translations WHERE cache_key='recent'"
            ).fetchone()[0]
            == recent
        )
        clock.advance(2 * 86400)
        assert cache.get("recent") is None
    finally:
        cache.close()


def test_main_database_and_retained_journal_are_bounded_and_clear_reclaims_pages(tmp_path):
    path, clock = tmp_path / "bounded.sqlite", Clock()
    capacity = 128 * 1024
    cache = TranslationCache(path, max_bytes=capacity, max_database_bytes=capacity, clock=clock)
    try:
        for index in range(30):
            clock.advance(1)
            cache.put(str(index), "x" * 8192)
            assert path.stat().st_size <= capacity
        assert cache.stats()["bytes"] <= capacity
        assert cache.get("29") == "x" * 8192
        wal = path.with_name(path.name + "-wal")
        assert not wal.exists() or wal.stat().st_size <= JOURNAL_RETAINED_BYTES
        before = path.stat().st_size
        assert cache.clear() > 0
        assert path.stat().st_size < before
        assert content_stats(cache) == {"entries": 0, "bytes": 0}
    finally:
        cache.close()


def test_single_value_that_cannot_fit_sqlite_physical_limit_is_skipped(tmp_path):
    cache = TranslationCache(
        tmp_path / "physical.sqlite", max_bytes=1024 * 1024, max_database_bytes=64 * 1024
    )
    try:
        assert cache.put("large", "x" * 100000) is False
        assert cache.get("large") is None
        assert cache.put("small", "ok") is True
        assert cache.get("small") == "ok"
    finally:
        cache.close()


def test_oversized_legacy_database_is_compacted_but_a_recent_small_row_survives(tmp_path):
    path, clock = tmp_path / "oversized-legacy.sqlite", Clock()
    older = datetime.fromtimestamp(clock() - 86400, UTC).strftime("%Y-%m-%d %H:%M:%S")
    recent = datetime.fromtimestamp(clock() - 1, UTC).strftime("%Y-%m-%d %H:%M:%S")
    legacy_database(path, [("big", "x" * (256 * 1024), older), ("small", "残す訳文", recent)])
    cache = TranslationCache(path, max_bytes=128 * 1024, clock=clock)
    try:
        assert cache.get("small") == "残す訳文"
        assert cache.get("big") is None
        assert path.stat().st_size <= 128 * 1024
    finally:
        cache.close()


def test_shorter_policy_expires_old_text_immediately():
    clock = Clock()
    cache = TranslationCache(":memory:", clock=clock)
    try:
        cache.put("old", "前の訳文")
        clock.advance(2 * 86400)
        cache.put("fresh", "新しい訳文")
        state = cache.set_policy(retention_days=1, max_bytes=MAX_CACHE_BYTES)
        assert state["retention_days"] == 1
        assert cache.get("old") is None
        assert cache.get("fresh") == "新しい訳文"
    finally:
        cache.close()


def test_smaller_policy_evicts_lru_and_shrinks_database_immediately(tmp_path):
    path, clock = tmp_path / "policy-shrink.sqlite", Clock()
    cache = TranslationCache(path, max_bytes=3 * 1024 * 1024, clock=clock)
    try:
        for key in ("a", "b", "c"):
            cache.put(key, key * (400 * 1024))
            clock.advance(1)
        assert cache.get("a") is not None
        state = cache.set_policy(retention_days=7, max_bytes=1024 * 1024)
        assert state["max_bytes"] == 1024 * 1024
        assert state["bytes"] <= 1024 * 1024
        assert path.stat().st_size <= 1024 * 1024
        assert cache.get("b") is None
        assert cache.get("a") is not None and cache.get("c") is not None
    finally:
        cache.close()


def test_user_policy_is_persistent_and_clear_does_not_reset_it(tmp_path):
    path = tmp_path / "saved-policy.sqlite"
    first = TranslationCache(path)
    assert (
        first.set_policy(retention_days=30, max_bytes=25 * 1024 * 1024)["max_bytes"]
        == 25 * 1024 * 1024
    )
    first.put("a", "訳文")
    first.clear()
    first.close()
    second = TranslationCache(path)
    try:
        state = second.stats()
        assert state["retention_days"] == 30
        assert state["max_bytes"] == 25 * 1024 * 1024
        assert state["entries"] == 0
    finally:
        second.close()


def test_longer_policy_extends_unexpired_rows_from_creation_but_does_not_revive_expired_rows():
    clock = Clock()
    cache = TranslationCache(":memory:", clock=clock)
    try:
        cache.put("expired", "復活させない")
        clock.advance(6 * 86400)
        cache.put("recent", "まだ有効")
        clock.advance(2 * 86400)
        cache.set_policy(retention_days=30, max_bytes=MAX_CACHE_BYTES)
        assert cache.get("expired") is None
        clock.advance(27 * 86400)
        assert cache.get("recent") == "まだ有効"
        clock.advance(86400)
        assert cache.get("recent") is None  # Creation day6 +30, not policy change day8 +30.
    finally:
        cache.close()


def test_reapplying_same_policy_does_not_refresh_expiry():
    clock = Clock()
    cache = TranslationCache(":memory:", clock=clock)
    try:
        cache.put("a", "訳文")
        clock.advance(6 * 86400)
        cache.set_policy(retention_days=7, max_bytes=MAX_CACHE_BYTES)
        clock.advance(86400)
        assert cache.get("a") is None
    finally:
        cache.close()


def test_legacy_v1_default_policy_migrates_to_7_days_1_mib(tmp_path):
    path, clock = tmp_path / "v1-policy.sqlite", Clock()
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE translations(cache_key TEXT PRIMARY KEY,translation TEXT NOT NULL,created_at TEXT NOT NULL,expires_at REAL NOT NULL,last_access REAL NOT NULL,size_bytes INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE cache_meta(id INTEGER PRIMARY KEY,entries INTEGER NOT NULL,total_bytes INTEGER NOT NULL,retention_days INTEGER NOT NULL)"
    )
    for key, age in (("expired-under-new-policy", 8), ("recent", 1)):
        created = clock() - age * 86400
        connection.execute(
            "INSERT INTO translations VALUES(?,?,?,?,?,?)",
            (
                key,
                "日本語",
                datetime.fromtimestamp(created, UTC).isoformat(),
                created + 30 * 86400,
                created,
                len("日本語".encode()),
            ),
        )
    connection.execute("INSERT INTO cache_meta VALUES(1,2,?,30)", (2 * len("日本語".encode()),))
    connection.commit()
    connection.close()
    cache = TranslationCache(path, clock=clock)
    try:
        assert cache.stats()["retention_days"] == 7
        assert cache.stats()["max_bytes"] == 1024 * 1024
        assert cache.get("expired-under-new-policy") is None
        assert cache.get("recent") == "日本語"
    finally:
        cache.close()


def test_explicit_constructor_overrides_remain_supported_for_developer_tests(tmp_path):
    path = tmp_path / "override.sqlite"
    first = TranslationCache(path)
    first.set_policy(retention_days=30, max_bytes=25 * 1024 * 1024)
    first.close()
    second = TranslationCache(path, retention_days=3, max_bytes=8)
    try:
        assert second.stats()["retention_days"] == 3
        assert second.stats()["max_bytes"] == 8
    finally:
        second.close()


def test_invalid_user_policy_is_rejected_without_changing_saved_values():
    cache = TranslationCache(":memory:")
    try:
        original = cache.stats()
        for days, size in (
            (0, MAX_CACHE_BYTES),
            (31, MAX_CACHE_BYTES),
            (7, 128 * 1024),
            (7, 65 * 1024 * 1024),
            (True, MAX_CACHE_BYTES),
        ):
            with pytest.raises(ValueError):
                cache.set_policy(days, size)
            assert cache.stats() == original
    finally:
        cache.close()


@pytest.mark.parametrize("capacity", [256 * 1024, 512 * 1024, 64 * 1024 * 1024])
def test_new_policy_bounds_are_accepted_and_persisted(tmp_path, capacity):
    path = tmp_path / "capacity.sqlite"
    cache = TranslationCache(path)
    cache.put("existing", "既存の訳文")
    assert cache.set_policy(7, capacity)["max_bytes"] == capacity
    cache.close()
    cache = TranslationCache(path)
    try:
        assert cache.stats()["max_bytes"] == capacity
        assert cache.get("existing") == "既存の訳文"
    finally:
        cache.close()


def v2_policy_database(path, capacity, clock):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE translations(cache_key TEXT PRIMARY KEY,translation TEXT NOT NULL,created_at TEXT NOT NULL,expires_at REAL NOT NULL,last_access REAL NOT NULL,size_bytes INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE cache_meta(id INTEGER PRIMARY KEY,entries INTEGER NOT NULL,total_bytes INTEGER NOT NULL,retention_days INTEGER NOT NULL,max_bytes INTEGER NOT NULL,policy_version INTEGER NOT NULL)"
    )
    value = "以前の有効な訳文。"
    created = clock() - 86400
    connection.execute(
        "INSERT INTO translations VALUES(?,?,?,?,?,?)",
        (
            "existing",
            value,
            datetime.fromtimestamp(created, UTC).isoformat(),
            created + 7 * 86400,
            created,
            len(value.encode()),
        ),
    )
    connection.execute(
        "INSERT INTO cache_meta VALUES(1,1,?,7,?,2)", (len(value.encode()), capacity)
    )
    connection.commit()
    connection.close()
    return value


@pytest.mark.parametrize(
    "old_capacity,new_capacity",
    [
        (10 * 1024 * 1024, 1024 * 1024),
        (25 * 1024 * 1024, 25 * 1024 * 1024),
        (100 * 1024 * 1024, 64 * 1024 * 1024),
        (128 * 1024, 256 * 1024),
    ],
)
def test_v2_capacity_migration_changes_old_default_preserves_custom_and_clamps_bounds(
    tmp_path, old_capacity, new_capacity
):
    path, clock = tmp_path / "v2-policy.sqlite", Clock()
    value = v2_policy_database(path, old_capacity, clock)
    for _ in range(2):
        cache = TranslationCache(path, clock=clock)
        try:
            assert cache.stats()["max_bytes"] == new_capacity
            assert cache.get("existing") == value
            assert path.stat().st_size <= new_capacity
            version, custom = cache._connection.execute(
                "SELECT policy_version,capacity_custom FROM cache_meta"
            ).fetchone()
            assert version == 3 and custom == (old_capacity != 10 * 1024 * 1024)
        finally:
            cache.close()


def test_explicit_ten_mib_selection_after_migration_is_not_mistaken_for_old_default(tmp_path):
    path, clock = tmp_path / "selected-ten.sqlite", Clock()
    v2_policy_database(path, 10 * 1024 * 1024, clock)
    cache = TranslationCache(path, clock=clock)
    assert cache.stats()["max_bytes"] == 1024 * 1024
    cache.set_policy(7, 10 * 1024 * 1024)
    cache.close()
    cache = TranslationCache(path, clock=clock)
    try:
        assert cache.stats()["max_bytes"] == 10 * 1024 * 1024
        assert cache.get("existing") == "以前の有効な訳文。"
    finally:
        cache.close()
