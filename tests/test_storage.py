from __future__ import annotations

from pathlib import Path

import pytest

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


def _create_v2_database(db_path: Path) -> None:
    """A database as task 8 left it: `content_previews` exists with the two
    pre-created metric columns, but none of task 9's four. Stands in for
    Aziz's real ~/.dupecleaner/index.db, which is in exactly this shape.
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE content_previews (
            content_hash         TEXT PRIMARY KEY,
            thumbnail_bytes      INTEGER NOT NULL,
            width                INTEGER,
            height               INTEGER,
            sharpness_score      REAL,
            recompression_score  REAL,
            created_at           REAL NOT NULL,
            accessed_at          REAL NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
    conn.execute(
        "INSERT INTO content_previews (content_hash, thumbnail_bytes, width, "
        "height, created_at, accessed_at) VALUES ('oldhash', 4096, 240, 180, 1.0, 2.0)"
    )
    conn.commit()
    conn.close()


def test_opening_a_v2_database_adds_the_quality_columns_in_place(tmp_path: Path):
    from dupecleaner.storage import SCHEMA_VERSION

    db_path = tmp_path / "v2.db"
    _create_v2_database(db_path)

    with ScanIndex(db_path) as index:
        version = index._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(version["value"]) == SCHEMA_VERSION

        meta = index.get_thumbnail_meta("oldhash")
        # The cached thumbnail is untouched — the migration adds columns,
        # it does not re-measure anything. Backfilling is a scan's job
        # (thumbnails.maybe_generate), not an open's.
        assert meta["thumbnail_bytes"] == 4096
        assert meta["width"] == 240 and meta["height"] == 180
        assert meta["source_width"] is None
        assert meta["recompression_basis"] is None
        # ...and it is therefore not yet reported as measured.
        assert index.quality_for_hashes(["oldhash"]) == {}
        assert index.count_quality_metrics() == 0


def test_the_v3_migration_is_safe_to_run_again(tmp_path: Path):
    """SQLite has no `ADD COLUMN IF NOT EXISTS`, so v3 is a Python step
    that checks first. If it ever stopped checking, a crash between the
    ALTER and the version bump would leave a database that can never be
    opened again — which is the failure the whole migration mechanism
    exists to avoid.
    """
    from dupecleaner.storage import _migrate_v3_quality_metrics

    db_path = tmp_path / "v2.db"
    _create_v2_database(db_path)
    with ScanIndex(db_path) as index:
        _migrate_v3_quality_metrics(index._conn)  # already applied on open
        _migrate_v3_quality_metrics(index._conn)  # and again, for good measure
        assert index.get_thumbnail_meta("oldhash")["source_width"] is None


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


# --- origin verdicts (task 15) ---------------------------------------------


def _origin_record(path: str, size: int = 10, mtime: float = 1.0) -> FileRecord:
    return FileRecord(
        display_path=path,
        real_path=path,
        size=size,
        mtime=mtime,
        media_kind=MediaKind.PHOTO,
    )


def test_v4_migration_adds_origin_columns_to_a_v3_index(tmp_path):
    """An index Aziz already has must gain the columns rather than be
    rebuilt — the hashes in it are the expensive part."""
    import sqlite3

    db = tmp_path / "old.db"
    with ScanIndex(db) as index:
        index.upsert_files([_origin_record("a.jpg")], "s1")
        index.commit()

    conn = sqlite3.connect(str(db))
    for column in (
        "origin_class", "origin_confidence", "origin_evidence",
        "origin_stamp_size", "origin_stamp_mtime",
    ):
        conn.execute(f"ALTER TABLE files DROP COLUMN {column}")
    conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with ScanIndex(db) as index:
        columns = {
            row["name"] for row in index._conn.execute("PRAGMA table_info(files)")
        }
        assert {"origin_class", "origin_confidence", "origin_evidence"} <= columns
        # The row that was already there still needs a verdict, and asking
        # for one does not crash on a column that only just appeared.
        assert [r.display_path for r in index.needs_origin("s1")] == ["a.jpg"]


def test_v4_migration_is_idempotent(tmp_path):
    db = tmp_path / "i.db"
    with ScanIndex(db) as index:
        index.upsert_files([_origin_record("a.jpg")], "s1")
        index.set_origin("a.jpg", "camera", "high", ["EXIF"])
    with ScanIndex(db) as index:  # reopening re-runs nothing and loses nothing
        assert index.get_origin("a.jpg")["origin_class"] == "camera"


def test_origin_round_trip_and_needs_origin_stops_asking(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_files([_origin_record("a.jpg"), _origin_record("b.jpg")], "s1")
        assert {r.display_path for r in index.needs_origin("s1")} == {"a.jpg", "b.jpg"}

        index.set_origin("a.jpg", "screenshot_phone", "high", ["имя снимка экрана"])
        assert [r.display_path for r in index.needs_origin("s1")] == ["b.jpg"]

        row = index.get_origin("a.jpg")
        assert row["origin_class"] == "screenshot_phone"
        assert row["origin_confidence"] == "high"
        assert "имя снимка экрана" in row["origin_evidence"]


def test_changed_bytes_invalidate_the_origin_verdict(tmp_path):
    """Half the evidence is EXIF, so replacing the file must re-open the
    question — the same rule the hash cache lives by."""
    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_files([_origin_record("a.jpg", mtime=1.0)], "s1")
        index.set_origin("a.jpg", "camera", "high", [])
        assert index.needs_origin("s1") == []

        index.upsert_files([_origin_record("a.jpg", mtime=2.0)], "s2")
        assert [r.display_path for r in index.needs_origin("s2")] == ["a.jpg"]


def test_archive_members_are_never_asked_for_an_origin(tmp_path):
    """Reading EXIF from inside an archive is finding A2 again."""
    member = FileRecord(
        display_path="a.zip::x.jpg",
        real_path="a.zip",
        size=10,
        mtime=1.0,
        media_kind=MediaKind.PHOTO,
        is_archive_member=True,
        archive_path="a.zip",
        member_name="x.jpg",
    )
    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_files([member, _origin_record("loose.jpg")], "s1")
        assert [r.display_path for r in index.needs_origin("s1")] == ["loose.jpg"]


def test_origin_breakdown_and_screenshot_list(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_files(
            [_origin_record(n) for n in ("a.jpg", "b.jpg", "c.jpg", "d.jpg")], "s1"
        )
        index.set_origin("a.jpg", "screenshot_phone", "high", [])
        index.set_origin("b.jpg", "screenshot_desktop", "medium", [])
        index.set_origin("c.jpg", "camera", "high", [])
        # d.jpg deliberately left unclassified.

        assert index.origin_breakdown("s1") == {
            ("screenshot_phone", "high"): 1,
            ("screenshot_desktop", "medium"): 1,
            ("camera", "high"): 1,
        }
        assert index.screenshot_paths("s1") == ["a.jpg", "b.jpg"]


def test_origin_verdicts_do_not_leak_between_scans(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_files([_origin_record("a.jpg")], "s1")
        index.set_origin("a.jpg", "camera", "high", [])
        index.upsert_files([_origin_record("b.jpg")], "s2")
        index.set_origin("b.jpg", "messenger", "high", [])
        assert index.screenshot_paths("s1") == []
        assert index.origin_breakdown("s2") == {("messenger", "high"): 1}


# --------------------------------------------------------------------------
# `review_decisions` (задача 12, "the main question of the session"):
# decisions are keyed by content_hash, not scan_id, precisely so they
# survive a server restart and a re-scan of the same roots. These tests
# exercise that guarantee directly rather than trusting the migration
# comment.
# --------------------------------------------------------------------------

def test_record_decision_round_trips_through_decisions_for_hashes(tmp_path: Path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.record_decision("h1", "quarantine", keeper_path="D:\\Краснодар\\a.jpg")
        index.record_decision("h2", "keep")
        got = index.decisions_for_hashes(["h1", "h2", "h3-never-decided"])

    assert got["h1"]["action"] == "quarantine"
    assert got["h1"]["keeper_path"] == "D:\\Краснодар\\a.jpg"
    assert got["h1"]["applied_at"] is None
    assert got["h2"]["action"] == "keep"
    assert got["h2"]["keeper_path"] is None
    assert "h3-never-decided" not in got


def test_record_decision_rejects_an_unknown_action(tmp_path: Path):
    with ScanIndex(tmp_path / "i.db") as index:
        with pytest.raises(ValueError):
            index.record_decision("h1", "delete-it-please")


def test_redeciding_a_group_resets_its_applied_state(tmp_path: Path):
    """A human who changes their mind about an already-applied group must
    have the new decision actually re-considered by the next apply, not
    silently ignored because `applied_at` was still set from the old one."""
    with ScanIndex(tmp_path / "i.db") as index:
        index.record_decision("h1", "quarantine")
        index.mark_decisions_applied(["h1"])
        assert index.decisions_for_hashes(["h1"])["h1"]["applied_at"] is not None

        index.record_decision("h1", "keep")
        got = index.decisions_for_hashes(["h1"])["h1"]
        assert got["action"] == "keep"
        assert got["applied_at"] is None


def test_clear_decision_removes_it(tmp_path: Path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.record_decision("h1", "quarantine")
        index.clear_decision("h1")
        assert index.decisions_for_hashes(["h1"]) == {}
        # Clearing something that was never decided is not an error.
        index.clear_decision("never-existed")


def test_mark_decisions_applied_is_batched_and_leaves_others_untouched(tmp_path: Path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.record_decision("h1", "quarantine")
        index.record_decision("h2", "quarantine")
        index.record_decision("h3", "keep")
        index.mark_decisions_applied(["h1", "h3"])
        got = index.decisions_for_hashes(["h1", "h2", "h3"])
        assert got["h1"]["applied_at"] is not None
        assert got["h2"]["applied_at"] is None
        assert got["h3"]["applied_at"] is not None


def test_pending_decision_count_counts_only_unapplied_quarantine_decisions(tmp_path: Path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.record_decision("h1", "quarantine")
        index.record_decision("h2", "quarantine")
        index.record_decision("h3", "keep")
        assert index.pending_decision_count() == 2
        index.mark_decisions_applied(["h1"])
        assert index.pending_decision_count() == 1


def test_decisions_for_hashes_is_batched_past_the_sqlite_variable_limit(tmp_path: Path):
    """`decisions_for_hashes` chunks its query -- a real report has up to
    8814 groups, and SQLite refuses a single query with that many bound
    parameters."""
    hashes = [f"h{i}" for i in range(1200)]
    with ScanIndex(tmp_path / "i.db") as index:
        for h in hashes[:5]:
            index.record_decision(h, "quarantine")
        got = index.decisions_for_hashes(hashes)
    assert len(got) == 5


# --------------------------------------------------------------------------
# The actual guarantee this table exists for: surviving a restart, and
# surviving a re-scan (same content_hash, new scan_id / new report object).
# --------------------------------------------------------------------------

def test_decisions_survive_reopening_the_same_database_file(tmp_path: Path):
    """This is P3 from the pilot report made concrete: after a server
    restart, `registry.get(scan_id)` returns nothing -- but the decisions
    must still be there, because they were never stored in that registry
    to begin with."""
    db = tmp_path / "index.db"
    with ScanIndex(db) as index:
        index.record_decision("stable-hash-1", "quarantine", keeper_path="D:\\a.jpg")

    # A brand new ScanIndex instance against the same file -- the closest
    # thing to "the process restarted" this test can simulate.
    with ScanIndex(db) as index:
        got = index.decisions_for_hashes(["stable-hash-1"])
    assert got["stable-hash-1"]["action"] == "quarantine"
    assert got["stable-hash-1"]["keeper_path"] == "D:\\a.jpg"


def test_opening_a_v4_database_adds_review_decisions_in_place(tmp_path: Path):
    """An index Aziz already has (with its expensive hashes already in it)
    must gain the table rather than be rebuilt, exactly like the v3->v4
    origin migration above."""
    import sqlite3

    from dupecleaner.storage import SCHEMA_VERSION

    db = tmp_path / "old.db"
    with ScanIndex(db):
        pass  # created fresh, at the current (>=5) schema version

    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE review_decisions")
    conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with ScanIndex(db) as index:
        tables = {
            row["name"]
            for row in index._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "review_decisions" in tables
        version = index._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(version["value"]) == SCHEMA_VERSION
        # And it's actually usable, not just present.
        index.record_decision("h1", "quarantine")
        assert index.decisions_for_hashes(["h1"])["h1"]["action"] == "quarantine"


def test_the_v5_migration_is_safe_to_run_again(tmp_path: Path):
    db = tmp_path / "i.db"
    with ScanIndex(db) as index:
        index.record_decision("h1", "keep")
    with ScanIndex(db) as index:  # reopening at the current version is a no-op
        assert index.decisions_for_hashes(["h1"])["h1"]["action"] == "keep"
