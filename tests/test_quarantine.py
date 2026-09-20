from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from dupecleaner import quarantine as quarantine_module
from dupecleaner.dedupe import find_duplicate_groups
from dupecleaner.quarantine import restore_from_journal, run_quarantine
from dupecleaner.scanner import Scanner


def test_plain_duplicates_are_moved_and_media_is_held_back(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    records = list(scanner.iter_records([str(tmp_tree)]))
    groups = find_duplicate_groups(records)

    quarantine_dir = tmp_tree.parent / "quarantine"
    result = run_quarantine(groups, quarantine_dir, confirm_media=False)

    # The plain a1.txt/a2.txt/zip-member group: one non-archive duplicate
    # (a2.txt or a1.txt, whichever isn't the keeper) should have moved.
    assert len(result.moved) == 1
    moved_original = Path(result.moved[0]["original"])
    assert moved_original.name in {"a1.txt", "a2.txt"}
    assert not moved_original.exists()
    assert Path(result.moved[0]["quarantined"]).exists()
    assert result.failed == []

    # Media duplicates (photo1.jpg/photo2.jpg) must NOT be moved without
    # explicit confirm_media=True, even though they were "confirmed" groups.
    assert len(result.pending_media_review) == 1
    for photo_name in ("photo1.jpg", "photo2.jpg"):
        assert (tmp_tree / photo_name).exists()

    # The archive-only group (one.bin/two.bin) must be reported, not touched.
    assert any("one.bin" in note["members"][0] or "two.bin" in note["members"][0]
               for note in result.archive_only_notes)

    manifest = json.loads((quarantine_dir / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(manifest, list) and len(manifest) == 1

    # The journal is written too, and is what restore actually reads.
    journal_path = quarantine_dir / "journal.jsonl"
    assert journal_path.exists()
    journal_lines = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    events = [entry["event"] for entry in journal_lines]
    assert events == ["move_pending", "move_done"]


def test_confirm_media_moves_media_duplicates_too(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    records = list(scanner.iter_records([str(tmp_tree)]))
    groups = find_duplicate_groups(records)

    quarantine_dir = tmp_tree.parent / "quarantine2"
    result = run_quarantine(groups, quarantine_dir, confirm_media=True)

    assert result.pending_media_review == []
    moved_names = {Path(m["original"]).name for m in result.moved}
    assert "photo1.jpg" in moved_names or "photo2.jpg" in moved_names


def test_group_hashes_filter_limits_which_groups_are_processed(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    records = list(scanner.iter_records([str(tmp_tree)]))
    groups = find_duplicate_groups(records)

    non_media_non_archive_only = [g for g in groups if not g.is_media and not g.only_archive_members]
    assert non_media_non_archive_only, "fixture should contain a plain duplicate group"
    target_hash = non_media_non_archive_only[0].content_hash

    quarantine_dir = tmp_tree.parent / "quarantine3"
    result = run_quarantine(groups, quarantine_dir, confirm_media=False, group_hashes={target_hash})

    assert len(result.moved) == 1
    assert result.moved[0]["group_hash"] == target_hash


def test_quarantine_mirrors_source_path_structure(tmp_tree: Path):
    """README promises the quarantine preserves path structure. Verify the
    moved file lands nested under a folder named after its original parent
    directory, not flattened into a `<hash16>/<basename>` bucket (the old
    layout pilot-findings.md problem P1.5 flagged as unnavigable at
    real-world scale, and inconsistent with what the docs say).
    """
    scanner = Scanner(include_archives=True)
    records = list(scanner.iter_records([str(tmp_tree)]))
    groups = find_duplicate_groups(records)

    quarantine_dir = tmp_tree.parent / "quarantine_mirror"
    result = run_quarantine(groups, quarantine_dir, confirm_media=False)

    assert len(result.moved) == 1
    original = Path(result.moved[0]["original"])
    quarantined = Path(result.moved[0]["quarantined"])

    assert quarantined.name == original.name
    assert quarantined.parent.name == original.parent.name

    hash16 = re.compile(r"^[0-9a-f]{16}$")
    mirrored_parts = quarantined.relative_to(quarantine_dir).parts
    assert not any(hash16.match(part) for part in mirrored_parts)


def test_restore_from_journal_moves_files_back(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    records = list(scanner.iter_records([str(tmp_tree)]))
    groups = find_duplicate_groups(records)

    quarantine_dir = tmp_tree.parent / "quarantine_restore"
    result = run_quarantine(groups, quarantine_dir, confirm_media=False)
    assert len(result.moved) == 1
    moved = result.moved[0]
    quarantined_path = Path(moved["quarantined"])
    original_path = Path(moved["original"])
    original_bytes = quarantined_path.read_bytes()
    assert quarantined_path.exists()
    assert not original_path.exists()

    restore_result = restore_from_journal(quarantine_dir)
    assert len(restore_result.restored) == 1
    assert restore_result.skipped == []
    assert original_path.exists()
    assert original_path.read_bytes() == original_bytes
    assert not quarantined_path.exists()

    # Idempotent: nothing left to restore, nothing to complain about either.
    again = restore_from_journal(quarantine_dir)
    assert again.restored == []
    assert again.skipped == []


def test_restore_refuses_to_overwrite_a_file_that_reappeared_at_original_path(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    records = list(scanner.iter_records([str(tmp_tree)]))
    groups = find_duplicate_groups(records)

    quarantine_dir = tmp_tree.parent / "quarantine_conflict"
    result = run_quarantine(groups, quarantine_dir, confirm_media=False)
    original_path = Path(result.moved[0]["original"])

    # Someone (or something else) put an unrelated file back at the
    # original path before restore ran.
    original_path.write_bytes(b"a completely different file now lives here")

    restore_result = restore_from_journal(quarantine_dir)
    assert restore_result.restored == []
    assert len(restore_result.skipped) == 1
    assert "не затираю" in restore_result.skipped[0]["reason"]
    assert original_path.read_bytes() == b"a completely different file now lives here"


def _make_duplicate_pairs(root: Path, n: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        content = f"payload-{i}-".encode() * 50
        (root / f"group{i}_a.txt").write_bytes(content)
        (root / f"group{i}_b.txt").write_bytes(content)


def test_journal_survives_a_crash_mid_run_and_restore_fully_recovers(tmp_path: Path, monkeypatch):
    """Reproduces pilot-findings.md P1.3: a failure partway through a large
    batch used to leave files quarantined with no journal at all, because
    manifest.json was only written once, at the very end. Here `shutil.move`
    is made to blow up (an exception that our per-file `except OSError`
    does *not* catch, standing in for the process dying mid-run) partway
    through several groups, and we verify that:

    - the final manifest.json is never written (proving the crash is real
      — the old source of truth is genuinely gone), yet
    - `journal.jsonl` alone is enough for `restore_from_journal` to bring
      every already-quarantined file back, and to correctly recognize that
      the file that was mid-flight when the crash hit was never actually
      moved (no action needed, not an error).
    """
    root = tmp_path / "data"
    _make_duplicate_pairs(root, 5)

    scanner = Scanner(include_archives=False)
    records = list(scanner.iter_records([str(root)]))
    groups = find_duplicate_groups(records)
    assert len(groups) == 5

    quarantine_dir = tmp_path / "quarantine"

    real_move = shutil.move
    call_count = {"n": 0}
    CRASH_AT = 3

    def flaky_move(src, dst):
        call_count["n"] += 1
        if call_count["n"] == CRASH_AT:
            raise RuntimeError("simulated crash mid-move")
        return real_move(src, dst)

    monkeypatch.setattr(quarantine_module.shutil, "move", flaky_move)

    with pytest.raises(RuntimeError):
        run_quarantine(groups, quarantine_dir, confirm_media=False)

    # The crash aborted the run before the final manifest.json write — this
    # is exactly the bug being fixed: the old summary file never gets
    # written, so it cannot be the thing restore depends on.
    assert not (quarantine_dir / "manifest.json").exists()
    journal_path = quarantine_dir / "journal.jsonl"
    assert journal_path.exists()

    # Two groups' files were fully moved before the crash; one group's file
    # was mid-flight (never actually moved, since flaky_move raises before
    # calling the real move); the remaining two groups were never reached.
    moved_groups = [
        i for i in range(5)
        if not (root / f"group{i}_a.txt").exists() or not (root / f"group{i}_b.txt").exists()
    ]
    assert len(moved_groups) == 2

    restore_result = restore_from_journal(quarantine_dir)

    assert len(restore_result.restored) == 2
    assert len(restore_result.skipped) == 1
    assert (
        "не завершилось" in restore_result.skipped[0]["reason"]
        or "восстанавливать нечего" in restore_result.skipped[0]["reason"]
    )

    # Every file, across all five groups, is back — nothing was lost.
    for i in range(5):
        a, b = root / f"group{i}_a.txt", root / f"group{i}_b.txt"
        assert a.exists() and a.read_bytes()
        assert b.exists() and b.read_bytes()

    # Idempotent: a second restore finds nothing left to do.
    second = restore_from_journal(quarantine_dir)
    assert second.restored == []


# --- Р1: quarantining a whole archive (task 4) -----------------------------
#
# The file-level tests above answer "is the copy safe to move?". These
# answer the harder question Р1 poses: is the *container* safe to move,
# given that whatever is inside it can no longer be reached individually
# once it is gone.

from dupecleaner.archive_classify import classify_archives  # noqa: E402
from dupecleaner.jobs import ScanJob  # noqa: E402
from dupecleaner.quarantine import quarantine_archives  # noqa: E402


def _scan(root: Path, tmp_path: Path):
    job = ScanJob(roots=[str(root)], db_path=str(tmp_path / "idx.db"))
    report = job.run()
    assert report is not None
    return report


def _run_archives(report, quarantine_dir: Path, confirm_media: bool = True):
    return quarantine_archives(
        classify_archives(report), report, quarantine_dir, confirm_media=confirm_media
    )


def test_fully_redundant_archive_is_moved_whole_and_restores(
    archive_tree: Path, tmp_path: Path
):
    """The happy path end to end: the archive moves as one file, and
    `restore` — which knows nothing about archives — brings it back,
    because an archive move is journalled exactly like a file move.
    """
    report = _scan(archive_tree, tmp_path)
    quarantine_dir = tmp_path / "quarantine"

    result = _run_archives(report, quarantine_dir)
    moved = {Path(item["archive"]).name for item in result.moved}

    assert "fully.zip" in moved
    assert not (archive_tree / "fully.zip").exists()
    assert result.freed_bytes > 0

    restored = restore_from_journal(quarantine_dir)
    assert (archive_tree / "fully.zip").exists()
    assert any(Path(r["original"]).name == "fully.zip" for r in restored.restored)


def test_only_fully_redundant_archives_are_touched(archive_tree: Path, tmp_path: Path):
    report = _scan(archive_tree, tmp_path)
    result = _run_archives(report, tmp_path / "quarantine")

    untouched = {"partial.zip", "unique.zip", "locked.7z", "broken.zip",
                 "mirror_a.zip", "mirror_b.zip"}
    for name in untouched:
        assert (archive_tree / name).exists(), f"{name} не должен был двигаться"

    reported = {Path(i["archive"]).name for i in result.not_actionable}
    assert untouched <= reported, "о каждом нетронутом архиве отчёт обязан сказать"


def test_intent_and_verification_are_journalled_before_the_move(
    archive_tree: Path, tmp_path: Path
):
    """Р5 says the journal line goes first. For an archive the line also
    carries what was verified, so a process that dies during the move
    leaves behind not just "we were moving X" but "we were moving X, and
    N twins had just been re-read successfully".
    """
    report = _scan(archive_tree, tmp_path)
    quarantine_dir = tmp_path / "quarantine"
    _run_archives(report, quarantine_dir)

    lines = [
        json.loads(line)
        for line in (quarantine_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pending = [
        entry for entry in lines
        if entry.get("event") == "move_pending" and entry.get("kind") == "archive"
    ]
    assert pending, "перемещение архива обязано быть записано в журнал"

    entry = next(e for e in pending if Path(e["original"]).name == "fully.zip")
    assert entry["twins_verified"] == entry["members_total"] == 2

    order = [e["event"] for e in lines if e["op_id"] == entry["op_id"]]
    assert order[0] == "move_pending" and "move_done" in order


def test_missing_twin_refuses_the_whole_archive(archive_tree: Path, tmp_path: Path):
    """The external-drive scenario Р1 names outright: the scan saw the
    twins, then the disk holding them went away.
    """
    report = _scan(archive_tree, tmp_path)
    (archive_tree / "loose" / "red2.txt").unlink()

    result = _run_archives(report, tmp_path / "quarantine")

    assert (archive_tree / "fully.zip").exists(), "архив остаётся на месте"
    refused = {Path(i["archive"]).name: i for i in result.refused}
    assert "fully.zip" in refused
    assert "двойники не подтвердились" in refused["fully.zip"]["reason"]


def test_silently_corrupted_twin_refuses_the_archive(archive_tree: Path, tmp_path: Path):
    """A twin of the right size holding the wrong bytes passes every
    existence check there is. Only re-hashing catches it — which is why
    verification reads the twin in full instead of stat()ing it.
    """
    report = _scan(archive_tree, tmp_path)
    victim = archive_tree / "loose" / "red1.txt"
    victim.write_bytes(b"\x00" * victim.stat().st_size)

    result = _run_archives(report, tmp_path / "quarantine")

    assert (archive_tree / "fully.zip").exists()
    assert any(Path(i["archive"]).name == "fully.zip" for i in result.refused)


def test_archive_of_photos_needs_confirm_media(archive_tree: Path, tmp_path: Path):
    """Photos inside a .zip are still photos. The safeguard that stops a
    batch run sweeping up family pictures must not be bypassable by the
    fact that they arrived wrapped in a container.
    """
    report = _scan(archive_tree, tmp_path)

    held = _run_archives(report, tmp_path / "q1", confirm_media=False)
    assert (archive_tree / "photos.zip").exists()
    assert {Path(i["archive"]).name for i in held.pending_media_review} == {"photos.zip"}

    confirmed = _run_archives(report, tmp_path / "q2", confirm_media=True)
    assert {Path(i["archive"]).name for i in confirmed.moved} >= {"photos.zip"}
    assert not (archive_tree / "photos.zip").exists()


def test_file_quarantine_then_archive_quarantine_still_verifies(
    archive_tree: Path, tmp_path: Path
):
    """The real command order: loose duplicates move first, archives after.
    Verification has to look at the disk as it is *then* — the keeper is
    what vouches for the archive, and the keeper never moves.
    """
    report = _scan(archive_tree, tmp_path)
    quarantine_dir = tmp_path / "quarantine"

    run_quarantine(report.groups, quarantine_dir, confirm_media=True)
    result = _run_archives(report, quarantine_dir)

    assert {Path(i["archive"]).name for i in result.moved} >= {"fully.zip"}
    assert not result.refused

    restored = restore_from_journal(quarantine_dir)
    assert (archive_tree / "fully.zip").exists()
    assert not restored.skipped, [s["reason"] for s in restored.skipped]
