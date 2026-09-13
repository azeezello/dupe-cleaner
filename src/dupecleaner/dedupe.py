"""Groups FileRecords into DuplicateGroups of byte-identical content.

Three-stage funnel, each stage only paying for what the previous stage
couldn't already rule out:

  1. group by size            — free (already known from the scan)
  2. group by quick_hash       — cheap (head+tail sample), plain files only
  3. group by full_hash        — expensive (full read), only within groups
                                  that already survived stages 1 and 2

Archive members skip stage 2: several archive backends only give us a
sequential (non-seekable) or reconstructed stream, so a "cheap partial
read" isn't actually cheap for them. They still benefit fully from stage 1.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import BinaryIO, Iterable

from . import archives
from .hashing import full_hash, quick_hash
from .config import QUICK_HASH_MIN_FILE_SIZE
from .models import DuplicateGroup, FileRecord


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


def _quick_key(record: FileRecord, size: int, warnings: list[str]) -> str:
    if record.is_archive_member or size < QUICK_HASH_MIN_FILE_SIZE:
        # No cheap prefilter available/worthwhile — defer entirely to the
        # full-hash stage by putting everything in one bucket.
        return "_all_"
    try:
        with open(record.real_path, "rb") as stream:
            return quick_hash(stream, size)
    except OSError as exc:
        warnings.append(f"Не удалось прочитать {record.real_path}: {exc}")
        return f"_unreadable_:{record.real_path}"


def _full_key(record: FileRecord, warnings: list[str]) -> str | None:
    try:
        with open_record_stream(record) as stream:
            return full_hash(stream)
    except Exception as exc:  # noqa: BLE001 - archive libs raise assorted errors
        warnings.append(f"Не удалось прочитать содержимое {record.display_path}: {exc}")
        return None


def find_duplicate_groups(
    records: Iterable[FileRecord],
    warnings: list[str] | None = None,
) -> list[DuplicateGroup]:
    if warnings is None:
        warnings = []

    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for record in records:
        if record.size == 0:
            continue  # empty files: nothing to reclaim, and rarely meaningful
        by_size[record.size].append(record)

    groups: list[DuplicateGroup] = []

    for size, size_bucket in by_size.items():
        if len(size_bucket) < 2:
            continue

        by_quick: dict[str, list[FileRecord]] = defaultdict(list)
        for record in size_bucket:
            by_quick[_quick_key(record, size, warnings)].append(record)

        for quick_bucket in by_quick.values():
            if len(quick_bucket) < 2:
                continue

            by_full: dict[str, list[FileRecord]] = defaultdict(list)
            for record in quick_bucket:
                full = _full_key(record, warnings)
                if full is not None:
                    by_full[full].append(record)

            for content_hash, full_bucket in by_full.items():
                if len(full_bucket) >= 2:
                    groups.append(DuplicateGroup(content_hash=content_hash, records=full_bucket))

    return groups
