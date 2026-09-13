"""Duplicate detection: turning discovered files into groups of byte-identical ones.

Three-stage funnel, each stage only paying for what the previous stage
couldn't rule out:

  1. group by size       — free (already known from the scan)
  2. quick hash          — cheap (head+tail sample), only for files whose
                            size matches at least one other file
  3. full hash           — expensive (whole file), only for files that
                            matched on size *and* quick hash

The grouping itself lives in SQL (`storage.py`), so it works identically
whether the index is a throwaway in-memory database (as in `find_duplicate_groups`
below, used by tests and small one-shot runs) or the persistent on-disk
index a long scan resumes from (`jobs.ScanJob`). One implementation, two
lifetimes.
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Iterable

from . import archives
from .hashing import full_hash, quick_and_full_hash, quick_hash
from .models import DuplicateGroup, FileRecord
from .storage import ScanIndex


def open_record_stream(record: FileRecord) -> BinaryIO:
    """Return a fresh, readable, closeable stream for a record's content,
    whether it's a plain file or lives inside an archive.
    """
    if record.is_archive_member:
        assert record.archive_path and record.member_name
        archive_path = Path(record.archive_path)
        kind = archives.archive_kind_for(archive_path)
        if kind is None:  # pragma: no cover - defensive, shouldn't happen
            raise ValueError(f"Unknown archive kind for {archive_path}")
        return archives.open_member(archive_path, kind, record.member_name)
    return open(record.real_path, "rb")


def compute_hashes(record: FileRecord) -> tuple[str, str | None]:
    """Quick-stage hashing for one record.

    Returns `(quick_hash, full_hash_or_None)`. For a plain file only the
    cheap head+tail sample is read, and the full hash is left for the next
    stage — which most files never reach. For an archive member the content
    has to be decompressed sequentially anyway, so both hashes come out of
    that single pass and the full-hash stage finds its answer already
    cached.

    The quick hash is computed by the same formula in both cases, so a file
    inside an archive and the same file loose on disk compare equal — that
    is what makes "вперемешку" detection work.
    """
    if record.is_archive_member:
        with open_record_stream(record) as stream:
            return quick_and_full_hash(stream, record.size)
    with open(record.real_path, "rb") as stream:
        return quick_hash(stream, record.size), None


def compute_full_hash(record: FileRecord) -> str:
    with open_record_stream(record) as stream:
        return full_hash(stream)


def run_quick_stage(index: ScanIndex, record: FileRecord) -> None:
    """Compute and store whatever the quick stage can determine for a record."""
    quick, full = compute_hashes(record)
    index.set_quick_hash(record.display_path, quick)
    if full is not None:
        index.set_full_hash(record.display_path, full)


def run_full_stage(index: ScanIndex, record: FileRecord) -> None:
    index.set_full_hash(record.display_path, compute_full_hash(record))


def find_duplicate_groups(
    records: Iterable[FileRecord],
    warnings: list[str] | None = None,
) -> list[DuplicateGroup]:
    """One-shot, in-memory duplicate detection.

    Convenient for small runs and tests. For anything large, use
    `jobs.ScanJob`, which does the same work against a persistent index and
    can report progress, be cancelled, and resume after a crash.
    """
    if warnings is None:
        warnings = []

    scan_id = "oneshot"
    with ScanIndex(":memory:") as index:
        index.upsert_files(records, scan_id)
        index.commit()

        for record in index.needs_quick_hash(scan_id):
            try:
                run_quick_stage(index, record)
            except Exception as exc:  # noqa: BLE001 - assorted OS/archive errors
                warnings.append(f"Не удалось прочитать {record.display_path}: {exc}")
        index.commit()

        for record in index.needs_full_hash(scan_id):
            try:
                run_full_stage(index, record)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Не удалось прочитать содержимое {record.display_path}: {exc}")
        index.commit()

        return index.duplicate_groups(scan_id)
