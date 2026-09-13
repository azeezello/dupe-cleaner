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
