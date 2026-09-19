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
from typing import BinaryIO, Callable, Iterable

from . import archives, thumbnails
from .hashing import full_hash, quick_and_full_hash, quick_hash
from .models import DuplicateGroup, FileRecord, MediaKind
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

    This single-record path is a correctness-preserving fallback (e.g. for
    a lone archive-member record processed outside the normal batch flow).
    The normal, fast path for hashing many members of the same archive is
    `hash_archive_members`, which reads the archive once for all of them —
    see its docstring for why that matters (pilot finding A2).
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
    content_hash = compute_full_hash(record)
    index.set_full_hash(record.display_path, content_hash)

    # Thumbnail generation piggybacks on the full hash rather than a
    # separate pass: by the time a plain file gets here, it has already
    # matched another file on both size and quick hash (see the funnel
    # docstring at the top of this module), so it is — bar a rare quick-hash
    # collision the full hash itself is about to rule out — going to end up
    # in a duplicate group. Archive members are deliberately excluded here;
    # see thumbnails.py's module docstring and hash_archive_members below
    # for why.
    if record.media_kind is MediaKind.PHOTO and not record.is_archive_member:
        thumbnails.maybe_generate(index, content_hash, Path(record.real_path))


def group_by_archive(
    records: Iterable[FileRecord],
) -> tuple[list[FileRecord], dict[str, list[FileRecord]]]:
    """Split records needing the quick-hash stage into plain files and
    archive members grouped by their containing archive.

    This is the grouping that lets `hash_archive_members` read each
    archive exactly once for every member that needs hashing, instead of
    the old per-record loop opening the same archive again for every
    single member (pilot finding A2).
    """
    plain: list[FileRecord] = []
    by_archive: dict[str, list[FileRecord]] = {}
    for record in records:
        if record.is_archive_member:
            assert record.archive_path is not None
            by_archive.setdefault(record.archive_path, []).append(record)
        else:
            plain.append(record)
    return plain, by_archive


def hash_archive_members(
    index: ScanIndex,
    archive_path: str,
    records: list[FileRecord],
    on_start: Callable[[FileRecord], None] | None = None,
) -> list[tuple[FileRecord, Exception]]:
    """Quick+full-hash every given member of one archive in a single
    sequential pass over the archive, storing each result as it's found.

    This is the fix for pilot finding A2: the old code opened the whole
    archive again for every member (`open_record_stream` ->
    `archives.open_member`), which for a tar/gzip archive costs a full
    decompression pass *per member* rather than once total — measured at
    71 seconds per member against 73 seconds for the entire 44.9 GB
    archive. Here, `archives.open_members_sequential` walks the archive
    once and hands back a stream per requested member as it's reached, and
    `hashing.quick_and_full_hash` derives both hashes from that single
    read — so the whole batch, however many members it contains, costs one
    pass over the archive.

    `on_start(record)` fires right before each member's stream begins
    being read, so a caller can drive progress reporting the same way it
    would for a plain per-record loop (see `jobs.ScanJob._hash_archives`).

    Returns `(record, exception)` pairs for members that failed to read or
    were not found in the archive, so the caller can turn each into a
    per-file warning without losing the rest of the batch — matching how
    `find_duplicate_groups` and `ScanJob` already handle per-record
    hashing failures.

    Deliberately does not generate thumbnails the way `run_full_stage`
    does for plain files: the web UI has never requested a preview for an
    archive member (`recordEl` in app.js only does it for non-archive
    records), and Р1 treats archive contents as cold, read-only storage.
    Adding it here would mean decoding image bytes from a stream that's
    already been consumed for hashing (a second, non-sequential pass over
    a tar/gzip member — exactly the cost pilot finding A2 exists to avoid)
    for a preview nothing currently displays.
    """
    kind = archives.archive_kind_for(Path(archive_path))
    if kind is None:  # pragma: no cover - defensive, shouldn't happen
        raise ValueError(f"Unknown archive kind for {archive_path}")

    by_name = {r.member_name: r for r in records}
    errors: list[tuple[FileRecord, Exception]] = []
    found: set[str] = set()

    for name, stream in archives.open_members_sequential(Path(archive_path), kind, by_name.keys()):
        record = by_name[name]
        found.add(name)
        if on_start is not None:
            on_start(record)
        try:
            with stream:
                quick, full = quick_and_full_hash(stream, record.size)
            index.set_quick_hash(record.display_path, quick)
            index.set_full_hash(record.display_path, full)
        except Exception as exc:  # noqa: BLE001 - assorted archive/OS errors
            errors.append((record, exc))

    for name, record in by_name.items():
        if name not in found:
            errors.append((record, FileNotFoundError(name)))

    return errors


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

        plain, by_archive = group_by_archive(index.needs_quick_hash(scan_id))

        for record in plain:
            try:
                run_quick_stage(index, record)
            except Exception as exc:  # noqa: BLE001 - assorted OS/archive errors
                warnings.append(f"Не удалось прочитать {record.display_path}: {exc}")

        for archive_path, members in by_archive.items():
            for record, exc in hash_archive_members(index, archive_path, members):
                warnings.append(f"Не удалось прочитать {record.display_path}: {exc}")

        index.commit()

        for record in index.needs_full_hash(scan_id):
            try:
                run_full_stage(index, record)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Не удалось прочитать содержимое {record.display_path}: {exc}")
        index.commit()

        return index.duplicate_groups(scan_id)
