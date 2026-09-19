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


def test_scan_job_report_carries_skipped_archives_through_json_round_trip(tmp_tree: Path, tmp_path: Path):
    """Finding A1: the report the CLI writes to disk (and later reloads for
    `quarantine`) must keep the skipped-archives list, not just the
    in-memory ScanReport.
    """
    import json

    from dupecleaner.models import ScanReport

    db = tmp_path / "index.db"
    job = ScanJob(roots=[str(tmp_tree)], db_path=db, include_archives=False)
    job.run()

    assert job.progress.status == "done"
    assert job.report is not None
    assert len(job.report.skipped_archives) >= 2  # backup.zip and archive_only.zip

    data = json.loads(json.dumps(job.report.to_dict(), ensure_ascii=False))
    reloaded = ScanReport.from_dict(data)
    assert len(reloaded.skipped_archives) == len(job.report.skipped_archives)
    assert {a.path for a in reloaded.skipped_archives} == {a.path for a in job.report.skipped_archives}


def test_scan_job_with_archives_has_no_skipped_archives(tmp_tree: Path, tmp_path: Path):
    db = tmp_path / "index.db"
    job = ScanJob(roots=[str(tmp_tree)], db_path=db, include_archives=True)
    job.run()
    assert job.report.skipped_archives == []


def test_scan_job_hashes_one_tar_archive_in_a_single_pass(tmp_path: Path, monkeypatch):
    """Finding A2, exercised through the real background-job path: scanning
    a folder with one tar archive containing several duplicate-size
    members must open that archive a small, constant number of times
    (enumeration once, hashing once) — never once per candidate member.
    """
    import io
    import tarfile

    root = tmp_path / "data"
    root.mkdir()

    pair_a = b"first duplicate pair inside the tar " * 20
    pair_b = b"second duplicate pair, different bytes " * 15
    archive = root / "dump.tar"
    with tarfile.open(archive, "w") as tf:
        for name, content in [
            ("a1.jpg", pair_a),
            ("a2.jpg", pair_a),
            ("b1.jpg", pair_b),
            ("b2.jpg", pair_b),
        ]:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))

    real_open = tarfile.open
    open_calls = []

    def _counting_open(*args, **kwargs):
        open_calls.append(1)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(tarfile, "open", _counting_open)

    db = tmp_path / "index.db"
    job = ScanJob(roots=[str(root)], db_path=db, include_archives=True)
    job.run()

    assert job.progress.status == "done"
    assert len(job.report.groups) == 2
    # One open to list members during enumeration, one more to hash all
    # four candidates in a single sequential pass — not four (one per
    # member, the pre-fix behaviour).
    assert len(open_calls) == 2
