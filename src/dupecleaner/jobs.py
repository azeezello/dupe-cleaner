"""Runs a scan as a cancellable background job that writes its work to disk
as it goes.

The previous version ran the whole scan inside one HTTP request and kept
results in memory: no progress, no cancel, and a crash meant starting from
zero. This module fixes all three. The job:

- reports which phase it is in, what it is currently reading, and — once a
  phase's denominator is known — a real percentage, throughput and ETA;
- can be stopped at any moment without losing completed work;
- persists every computed hash to the SQLite index immediately (batched
  commits), so an interrupted scan resumes from the index rather than
  re-reading terabytes.

Resuming is deliberately "re-run the scan and let the cache answer" rather
than "restore the exact previous run". Enumeration is the cheap phase;
hashing is the expensive one, and hashing is what the cache eliminates.
The `files_from_cache` counter shows exactly how much was skipped.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from .dedupe import group_by_archive, hash_archive_members, run_full_stage, run_quick_stage
from .models import FileRecord, ScanReport
from .progress import ScanProgress
from .scanner import Scanner
from .storage import DEFAULT_DB_PATH, ScanIndex

# How often work in progress is flushed to SQLite. A crash loses at most
# this much: a few seconds of hashing, never the whole scan.
COMMIT_EVERY_FILES = 200
COMMIT_EVERY_SECONDS = 5.0


class ScanCancelled(Exception):
    pass


class ScanJob:
    """One scan. Start it, poll `progress`, optionally `cancel()`, then read
    `report` once the status is "done".
    """

    def __init__(
        self,
        roots: list[str],
        db_path: Path | str = DEFAULT_DB_PATH,
        include_archives: bool = True,
        scan_id: str | None = None,
    ) -> None:
        self.scan_id = scan_id or str(uuid.uuid4())
        self.roots = roots
        self.db_path = db_path
        self.include_archives = include_archives

        self.progress = ScanProgress(scan_id=self.scan_id, roots=roots)
        self.report: ScanReport | None = None

        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    # --- control -----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name=f"scan-{self.scan_id[:8]}", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise ScanCancelled()

    # --- the actual work ---------------------------------------------------

    def run(self) -> ScanReport | None:
        index = ScanIndex(self.db_path)
        scanner = Scanner(include_archives=self.include_archives)
        try:
            self._enumerate(index, scanner)

            # Measured after enumeration but before any hashing happens in
            # this run, so every hash counted here necessarily came from an
            # earlier run. This is the number that answers "do I have to
            # start over?" — on a resumed scan it is large.
            self.progress.files_from_cache = index.count_cached_hashes(self.scan_id)

            self._quick_hash_phase(index)
            self._full_hash_phase(index)

            self.progress.enter_phase("grouping")
            groups = index.duplicate_groups(self.scan_id)
            self.progress.groups_found = len(groups)

            files_total, _ = index.scan_totals(self.scan_id)
            self.report = ScanReport(
                scanned_roots=self.roots,
                total_files_seen=files_total,
                groups=groups,
                warnings=scanner.warnings,
                skipped_archives=scanner.skipped_archives,
            )
            self.progress.warnings = scanner.warnings
            self.progress.finish("done")
            return self.report

        except ScanCancelled:
            index.commit()
            self.progress.warnings = scanner.warnings
            self.progress.finish("cancelled")
            return None
        except Exception as exc:  # noqa: BLE001 - a scan must never take the server down
            index.commit()
            self.progress.warnings = scanner.warnings
            self.progress.finish("failed", error=f"{type(exc).__name__}: {exc}")
            return None
        finally:
            index.close()

    def _enumerate(self, index: ScanIndex, scanner: Scanner) -> None:
        """Phase 1: walk the tree and record what exists. No file contents are
        read here, so this is fast even on huge disks — which is why an
        interrupted scan can afford to redo it.
        """
        self.progress.enter_phase("enumerating")

        batch: list[FileRecord] = []
        last_commit = time.time()

        for record in scanner.iter_records(self.roots):
            self._check_cancelled()
            batch.append(record)
            self.progress.note_seen(record.size, record.display_path)

            if len(batch) >= COMMIT_EVERY_FILES or (time.time() - last_commit) > COMMIT_EVERY_SECONDS:
                index.upsert_files(batch, self.scan_id)
                index.commit()
                batch.clear()
                last_commit = time.time()

        if batch:
            index.upsert_files(batch, self.scan_id)
        index.commit()
        self.progress.warnings = list(scanner.warnings)

    def _quick_hash_phase(self, index: ScanIndex) -> None:
        """Phase 2: cheap head+tail fingerprint, but only for files that share
        a size with something else. Files with a unique size are never read.

        Archive members are hashed separately from plain files, grouped by
        their containing archive: `hash_archive_members` reads each archive
        once for every member that needs hashing, rather than opening it
        again per member (pilot finding A2 — see dedupe.hash_archive_members
        for the mechanism and measurements).
        """
        candidates = index.needs_quick_hash(self.scan_id)
        # Reading min(size, 2 * sample) per file is what this phase actually
        # costs, so that — not the files' full size — is the denominator.
        from .config import QUICK_HASH_SAMPLE_BYTES

        bytes_total = sum(min(r.size, 2 * QUICK_HASH_SAMPLE_BYTES) for r in candidates)
        self.progress.enter_phase("quick_hashing", files_total=len(candidates), bytes_total=bytes_total)

        plain, by_archive = group_by_archive(candidates)

        cost = lambda record: min(record.size, 2 * QUICK_HASH_SAMPLE_BYTES)  # noqa: E731

        self._hash_loop(index, plain, work=run_quick_stage, cost=cost)
        self._hash_archives_loop(index, by_archive, cost=cost)

    def _full_hash_phase(self, index: ScanIndex) -> None:
        """Phase 3: read in full, but only files that matched another file on
        both size and quick hash. This is the only phase that touches whole
        file contents, and its denominator is exact.
        """
        candidates = index.needs_full_hash(self.scan_id)
        bytes_total = sum(r.size for r in candidates)
        self.progress.enter_phase("full_hashing", files_total=len(candidates), bytes_total=bytes_total)

        self._hash_loop(
            index,
            candidates,
            work=run_full_stage,
            cost=lambda record: record.size,
        )

    def _hash_loop(self, index: ScanIndex, candidates, work, cost) -> None:
        last_commit = time.time()
        processed_since_commit = 0

        for record in candidates:
            self._check_cancelled()
            # Set the path *before* reading it: if the read hangs on a dead
            # network share, this is the path the stall indicator shows.
            self.progress.advance(current_path=record.display_path)

            try:
                work(index, record)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the scan
                self.progress.warnings.append(
                    f"Не удалось прочитать {record.display_path}: {exc}"
                )

            self.progress.advance(files=1, size_bytes=cost(record))
            processed_since_commit += 1

            if processed_since_commit >= COMMIT_EVERY_FILES or (time.time() - last_commit) > COMMIT_EVERY_SECONDS:
                index.commit()
                processed_since_commit = 0
                last_commit = time.time()

        index.commit()

    def _hash_archives_loop(self, index: ScanIndex, by_archive: dict, cost) -> None:
        """Same progress/commit/warning bookkeeping as `_hash_loop`, but one
        `hash_archive_members` call per archive — a single sequential pass —
        instead of one record-at-a-time call per member.
        """
        last_commit = time.time()
        processed_since_commit = 0

        for archive_path, members in by_archive.items():
            def _on_start(record: FileRecord) -> None:
                # Raises ScanCancelled (via _check_cancelled) if a cancel
                # came in mid-archive, so a long archive doesn't have to
                # finish before Ctrl+C takes effect.
                self._check_cancelled()
                self.progress.advance(current_path=record.display_path)

            errors = hash_archive_members(index, archive_path, members, on_start=_on_start)
            error_by_member = {record.member_name: exc for record, exc in errors}

            for record in members:
                exc = error_by_member.get(record.member_name)
                if exc is not None:
                    self.progress.warnings.append(
                        f"Не удалось прочитать {record.display_path}: {exc}"
                    )
                self.progress.advance(files=1, size_bytes=cost(record))
                processed_since_commit += 1

                if processed_since_commit >= COMMIT_EVERY_FILES or (time.time() - last_commit) > COMMIT_EVERY_SECONDS:
                    index.commit()
                    processed_since_commit = 0
                    last_commit = time.time()

        index.commit()


class ScanRegistry:
    """Keeps running and finished jobs addressable by id for the web layer."""

    def __init__(self) -> None:
        self._jobs: dict[str, ScanJob] = {}
        self._lock = threading.Lock()

    def create(self, roots: list[str], db_path: Path | str, include_archives: bool = True) -> ScanJob:
        job = ScanJob(roots=roots, db_path=db_path, include_archives=include_archives)
        with self._lock:
            self._jobs[job.scan_id] = job
        job.start()
        return job

    def get(self, scan_id: str) -> ScanJob | None:
        with self._lock:
            return self._jobs.get(scan_id)

    def list_jobs(self) -> list[ScanJob]:
        with self._lock:
            return list(self._jobs.values())
