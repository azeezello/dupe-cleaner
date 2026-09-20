from __future__ import annotations

from pathlib import Path

from dupecleaner.models import FileRecord, MediaKind
from dupecleaner.storage import ScanIndex


def _record(path: Path, size: int, mtime: float = 1000.0) -> FileRecord:
    return FileRecord(
        display_path=str(path),
        real_path=str(path),
        size=size,
        mtime=mtime,
    )


def test_unique_sizes_are_never_candidates(tmp_path: Path):
    with ScanIndex(":memory:") as index:
        index.upsert_files(
            [_record(tmp_path / "a", 10), _record(tmp_path / "b", 20)], "scan1"
        )
        index.commit()
        assert index.needs_quick_hash("scan1") == []


def test_same_size_files_become_candidates(tmp_path: Path):
    with ScanIndex(":memory:") as index:
        index.upsert_files(
            [_record(tmp_path / "a", 10), _record(tmp_path / "b", 10)], "scan1"
        )
        index.commit()
        assert len(index.needs_quick_hash("scan1")) == 2


def test_cached_hash_is_reused_while_size_and_mtime_are_unchanged(tmp_path: Path):
    with ScanIndex(":memory:") as index:
        records = [_record(tmp_path / "a", 10), _record(tmp_path / "b", 10)]
        index.upsert_files(records, "scan1")
        for record in records:
            index.set_quick_hash(record.display_path, "qh")
            index.set_full_hash(record.display_path, "fh")
        index.commit()

        # A second scan of unchanged files must not ask for any re-hashing.
        index.upsert_files(records, "scan2")
        index.commit()
        assert index.needs_quick_hash("scan2") == []
        assert index.needs_full_hash("scan2") == []
        assert index.count_cached_hashes("scan2") == 2


def test_changed_mtime_invalidates_cached_hash(tmp_path: Path):
    with ScanIndex(":memory:") as index:
        record = _record(tmp_path / "a", 10, mtime=1000.0)
        other = _record(tmp_path / "b", 10, mtime=1000.0)
        index.upsert_files([record, other], "scan1")
        index.set_quick_hash(record.display_path, "qh")
        index.set_full_hash(record.display_path, "fh")
        index.commit()

        touched = _record(tmp_path / "a", 10, mtime=2000.0)
        index.upsert_files([touched, other], "scan2")
        index.commit()

        needs = {r.display_path for r in index.needs_quick_hash("scan2")}
        assert str(tmp_path / "a") in needs


def test_changed_archive_invalidates_its_members(tmp_path: Path):
    archive = tmp_path / "backup.zip"

    def member(name: str, archive_mtime: float) -> FileRecord:
        return FileRecord(
            display_path=f"{archive}::{name}",
            real_path=str(archive),
            size=100,
            mtime=1.0,
            is_archive_member=True,
            archive_path=str(archive),
            member_name=name,
            source_size=5000,
            source_mtime=archive_mtime,
        )

    with ScanIndex(":memory:") as index:
        members = [member("one.txt", 1000.0), member("two.txt", 1000.0)]
        index.upsert_files(members, "scan1")
        for m in members:
            index.set_quick_hash(m.display_path, "qh")
            index.set_full_hash(m.display_path, "fh")
        index.commit()

        # Repack the archive: every cached hash taken from inside it must go.
        repacked = [member("one.txt", 2000.0), member("two.txt", 2000.0)]
        index.upsert_files(repacked, "scan2")
        index.commit()
        assert len(index.needs_quick_hash("scan2")) == 2


def test_groups_are_scoped_to_the_scan_that_found_them(tmp_path: Path):
    with ScanIndex(":memory:") as index:
        records = [_record(tmp_path / "a", 10), _record(tmp_path / "b", 10)]
        index.upsert_files(records, "scan1")
        for record in records:
            index.set_full_hash(record.display_path, "same")
        index.commit()
        assert len(index.duplicate_groups("scan1")) == 1

        # A later scan that only saw one of them must not report a pair —
        # this is what stops deleted files resurfacing as phantom duplicates.
        index.upsert_files([records[0]], "scan2")
        index.commit()
        assert index.duplicate_groups("scan2") == []


# --- schema migration (v1 -> v2: content_previews) --------------------------


def _create_v1_database(db_path: Path) -> None:
    """Build a database shaped exactly like the pre-migration schema
    (SCHEMA_VERSION 1, no content_previews table), with one real row in
    `files` — standing in for a user's existing ~/.dupecleaner/index.db
    from before this task.
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE files (
            display_path        TEXT PRIMARY KEY,
            real_path           TEXT NOT NULL,
            size                INTEGER NOT NULL,
            mtime               REAL NOT NULL,
            media_kind          TEXT NOT NULL,
            is_archive_member   INTEGER NOT NULL,
            archive_path        TEXT,
            member_name         TEXT,
            source_size         INTEGER NOT NULL,
            source_mtime        REAL NOT NULL,
            quick_hash          TEXT,
            full_hash           TEXT,
            hashed_source_size  INTEGER,
            hashed_source_mtime REAL,
            last_scan_id        TEXT NOT NULL,
            seen_at             REAL NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', '1')"
    )
    conn.execute(
        """
        INSERT INTO files (
            display_path, real_path, size, mtime, media_kind,
            is_archive_member, archive_path, member_name,
            source_size, source_mtime, full_hash, last_scan_id, seen_at
        ) VALUES ('a.jpg', 'a.jpg', 10, 1000.0, 'photo', 0, NULL, NULL,
                   10, 1000.0, 'existinghash', 'scan1', 1000.0)
        """
    )
    conn.commit()
    conn.close()


def test_opening_a_v1_database_migrates_it_in_place(tmp_path: Path):
    from dupecleaner.storage import SCHEMA_VERSION

    db_path = tmp_path / "legacy.db"
    _create_v1_database(db_path)

    with ScanIndex(db_path) as index:
        row = index._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(row["value"]) == SCHEMA_VERSION

        # The new table exists and is usable...
        index.upsert_thumbnail("existinghash", 123, 240, 180)
        assert index.get_thumbnail_meta("existinghash") is not None

        # ...and the pre-existing data survived the migration untouched.
        files_row = index._conn.execute(
            "SELECT * FROM files WHERE display_path = 'a.jpg'"
        ).fetchone()
        assert files_row["full_hash"] == "existinghash"


def test_reopening_an_already_migrated_database_is_a_noop(tmp_path: Path):
    """Migrations must be safe to run again — opening the same db file
    twice (e.g. two `dupecleaner` invocations) shouldn't fail or duplicate
    anything."""
    db_path = tmp_path / "index.db"
    with ScanIndex(db_path) as index:
        index.upsert_thumbnail("h1", 100, 10, 10)

    with ScanIndex(db_path) as index:
        assert index.get_thumbnail_meta("h1") is not None
        index.upsert_thumbnail("h2", 200, 20, 20)
        assert index.get_thumbnail_meta("h2") is not None


# --- content_previews CRUD ---------------------------------------------------


def test_thumbnail_metadata_roundtrip(tmp_path: Path):
    with ScanIndex(tmp_path / "index.db") as index:
        assert index.get_thumbnail_meta("h") is None

        index.upsert_thumbnail("h", 555, 240, 180)
        meta = index.get_thumbnail_meta("h")
        assert meta["thumbnail_bytes"] == 555
        assert meta["width"] == 240
        assert meta["height"] == 180
        # Task 9's columns exist now and are NULL until that task fills
        # them in — this is the migration groundwork the task asked for.
        assert meta["sharpness_score"] is None
        assert meta["recompression_score"] is None

        index.upsert_thumbnail("h", 999, 240, 180)  # re-upsert overwrites
        assert index.get_thumbnail_meta("h")["thumbnail_bytes"] == 999

        assert index.total_thumbnail_bytes() == 999
        index.delete_thumbnails(["h"])
        assert index.get_thumbnail_meta("h") is None
        assert index.total_thumbnail_bytes() == 0


def test_lru_thumbnail_hashes_orders_oldest_first(tmp_path: Path, monkeypatch):
    import dupecleaner.storage as storage_module

    fake_now = [0.0]

    def fake_time() -> float:
        fake_now[0] += 1.0
        return fake_now[0]

    with ScanIndex(tmp_path / "index.db") as index:
        # `storage_module.time` is the real `time` module, so this patches a
        # clock the whole process shares. It has to be undone by monkeypatch
        # rather than by hand: the obvious manual restore —
        # `storage_module.time.time = __import__("time").time` — looks up
        # the attribute *after* it has been replaced and therefore reassigns
        # the fake over itself, leaving `time.time()` returning single-digit
        # values for every test that runs afterwards. That went unnoticed
        # until a later test tried to build a zip and hit "ZIP does not
        # support timestamps before 1980".
        monkeypatch.setattr(storage_module.time, "time", fake_time)

        index.upsert_thumbnail("first", 10, 1, 1)
        index.upsert_thumbnail("second", 10, 1, 1)
        index.upsert_thumbnail("third", 10, 1, 1)
        index.touch_thumbnail("first")  # now newest

        assert index.lru_thumbnail_hashes(2) == ["second", "third"]


def test_the_fake_clock_from_the_lru_test_does_not_leak(tmp_path: Path):
    """Guards the fix above rather than any production code.

    A leaked clock does not fail the test that leaks it — it fails
    something unrelated, later, in a way that reads as a bug in whatever
    ran next. Pinning it here means the next person to reach for a fake
    clock finds out immediately.
    """
    import time as real_time

    assert real_time.time() > 1_600_000_000


def test_resolve_content_hash_matches_either_path_form(tmp_path: Path):
    with ScanIndex(tmp_path / "index.db") as index:
        index.upsert_files(
            [_record(Path("a.jpg"), 10)], "scan1"
        )
        index.set_full_hash("a.jpg", "hash-a")
        index.commit()

        assert index.resolve_content_hash("a.jpg") == "hash-a"
        assert index.resolve_content_hash("does-not-exist.jpg") is None
