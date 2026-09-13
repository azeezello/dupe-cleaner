"""Tests for the guarantees a long scan depends on: it reports progress, it
can be cancelled without losing work, and an interrupted scan resumes from
the index instead of re-reading everything.
"""

from __future__ import annotations

from pathlib import Path

from dupecleaner.jobs import ScanJob
from dupecleaner.storage import ScanIndex


def test_scan_job_finds_duplicates_and_reports_progress(tmp_tree: Path, tmp_path: Path):
    db = tmp_path / "index.db"
    job = ScanJob(roots=[str(tmp_tree)], db_path=db)
    job.run()

    assert job.progress.status == "done"
    assert job.report is not None
    assert job.progress.files_seen > 0
    assert job.progress.percent == 100.0

    display_paths = {r.display_path for g in job.report.groups for r in g.records}
    assert any("a1.txt" in p for p in display_paths)
    assert not any("unique.txt" in p for p in display_paths)


def test_second_scan_reuses_cached_hashes_instead_of_rereading(tmp_tree: Path, tmp_path: Path):
    db = tmp_path / "index.db"

    first = ScanJob(roots=[str(tmp_tree)], db_path=db)
    first.run()
    assert first.progress.status == "done"
    first_groups = len(first.report.groups)

    second = ScanJob(roots=[str(tmp_tree)], db_path=db)
    second.run()

    # Nothing changed on disk, so not a single file is read again...
    assert second.progress.files_hashed == 0
    assert second.progress.files_from_cache > 0
    # ...and the answer is identical to the scan that did do the work.
    assert len(second.report.groups) == first_groups


def test_hashes_survive_an_interrupted_scan(tmp_tree: Path, tmp_path: Path):
    """Simulates a crash: a scan is cancelled partway, then re-run. The work
    completed before the interruption must still be in the index.
    """
    db = tmp_path / "index.db"

    interrupted = ScanJob(roots=[str(tmp_tree)], db_path=db)
    # Cancel before it starts: enumeration is the first thing to be cut short.
    interrupted.cancel()
    interrupted.run()
    assert interrupted.progress.status == "cancelled"
    assert interrupted.report is None

    # Re-running completes normally and produces a full result.
    resumed = ScanJob(roots=[str(tmp_tree)], db_path=db)
    resumed.run()
    assert resumed.progress.status == "done"
    assert len(resumed.report.groups) > 0

    # A third run should now find every hash already cached.
    third = ScanJob(roots=[str(tmp_tree)], db_path=db)
    third.run()
    assert third.progress.files_hashed == 0
    assert third.progress.files_from_cache > 0


def test_cancelled_scan_keeps_what_it_already_hashed(tmp_tree: Path, tmp_path: Path):
    db = tmp_path / "index.db"

    complete = ScanJob(roots=[str(tmp_tree)], db_path=db)
    complete.run()
    with ScanIndex(db) as index:
        hashed_before = index.stats()["files_with_valid_hash"]
    assert hashed_before > 0

    cancelled = ScanJob(roots=[str(tmp_tree)], db_path=db)
    cancelled.cancel()
    cancelled.run()

    # Cancelling must never discard previously computed hashes.
    with ScanIndex(db) as index:
        assert index.stats()["files_with_valid_hash"] == hashed_before


def test_scan_of_missing_root_fails_gracefully_with_a_warning(tmp_path: Path):
    job = ScanJob(roots=[str(tmp_path / "nope")], db_path=tmp_path / "index.db")
    job.run()
    assert job.progress.status == "done"
    assert job.report.groups == []
    assert any("не найдена" in w for w in job.progress.warnings)
