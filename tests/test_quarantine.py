from __future__ import annotations

import hashlib
import json
import re
import shutil
import textwrap
from pathlib import Path

import pytest

from dupecleaner import quarantine as quarantine_module
from dupecleaner.dedupe import find_duplicate_groups
from dupecleaner.quarantine import (
    JOURNAL_FILENAME,
    journal_summary,
    quarantine_review_drops,
    quarantine_reviewed_groups,
    restore_from_journal,
    run_quarantine,
)
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


# --------------------------------------------------------------------------
# `quarantine_reviewed_groups` (задача 12, half two): the batch path pilot
# finding P1.1 asked for, between "nothing moves" (no --confirm-media) and
# "everything moves" (--confirm-media on the whole report). Only groups a
# human actually queued in `decisions` may move; everything else -- decided
# or not -- must be left exactly where it was.
# --------------------------------------------------------------------------

def _leaf(record) -> str:
    # For an archive member (`archive.zip::inner/name.ext`) the meaningful
    # leaf is the member name, not the archive's own filename.
    if record.is_archive_member:
        return Path(record.member_name).name
    return Path(record.display_path).name


def _find_group(groups, *leaf_names: str):
    wanted = set(leaf_names)
    for g in groups:
        if {_leaf(r) for r in g.records} == wanted:
            return g
    raise AssertionError(f"no group with leaves {wanted} in {[{_leaf(r) for r in g.records} for g in groups]}")


def test_only_decided_groups_move_undecided_ones_are_untouched(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    groups = find_duplicate_groups(list(scanner.iter_records([str(tmp_tree)])))

    plain_group = _find_group(groups, "a1.txt", "a2.txt", "a_copy.txt")
    media_group = _find_group(groups, "photo1.jpg", "photo2.jpg")

    quarantine_dir = tmp_tree.parent / "q1"
    result = quarantine_reviewed_groups(
        groups, quarantine_dir, {plain_group.content_hash: None}, confirm_media=False
    )

    assert len(result.moved) == 1
    # The media group was never in `decisions` at all -- not even deferred
    # to pending_media_review, because nobody asked about it.
    assert result.pending_media_review == []
    assert (tmp_tree / "photo1.jpg").exists()
    assert (tmp_tree / "photo2.jpg").exists()


def test_a_decided_media_group_without_confirm_media_is_deferred_not_dropped(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    groups = find_duplicate_groups(list(scanner.iter_records([str(tmp_tree)])))
    media_group = _find_group(groups, "photo1.jpg", "photo2.jpg")

    quarantine_dir = tmp_tree.parent / "q2"
    result = quarantine_reviewed_groups(
        groups, quarantine_dir, {media_group.content_hash: None}, confirm_media=False
    )
    assert result.moved == []
    assert len(result.pending_media_review) == 1
    assert (tmp_tree / "photo1.jpg").exists()
    assert (tmp_tree / "photo2.jpg").exists()

    # The same call again, this time confirmed, actually moves it -- the
    # exact "second explicit step" apply_decisions's docstring promises.
    result2 = quarantine_reviewed_groups(
        groups, quarantine_dir, {media_group.content_hash: None}, confirm_media=True
    )
    assert len(result2.moved) == 1


def test_keeper_override_is_honoured_in_the_batch_path(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    groups = find_duplicate_groups(list(scanner.iter_records([str(tmp_tree)])))
    plain_group = _find_group(groups, "a1.txt", "a2.txt", "a_copy.txt")

    # Р8's default keeper would be whichever plain-file copy sorts first;
    # override it to the *other* plain copy so the archive member (which
    # can never be the keeper anyway) isn't the only thing left standing.
    non_archive = [r for r in plain_group.records if not r.is_archive_member]
    override_path = non_archive[1].display_path if len(non_archive) > 1 else non_archive[0].display_path

    quarantine_dir = tmp_tree.parent / "q3"
    result = quarantine_reviewed_groups(
        groups, quarantine_dir, {plain_group.content_hash: override_path}, confirm_media=True
    )
    assert override_path in result.kept.values() or override_path == list(result.kept.values())[0]
    assert Path(override_path).exists()


def test_unknown_or_archive_only_hashes_in_decisions_are_skipped_not_errors(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    groups = find_duplicate_groups(list(scanner.iter_records([str(tmp_tree)])))
    archive_only = next(g for g in groups if g.only_archive_members)

    quarantine_dir = tmp_tree.parent / "q4"
    # A hash that isn't in this report's groups at all, plus a real
    # archive-only group's hash -- neither has a `quarantine_group` call
    # that could possibly succeed, and both must be silently skipped.
    result = quarantine_reviewed_groups(
        groups,
        quarantine_dir,
        {"not-a-real-hash": None, archive_only.content_hash: None},
        confirm_media=True,
    )
    assert result.moved == []
    assert result.failed == []


# --------------------------------------------------------------------------
# `journal_summary` -- the read side of the journal/restore screen.
# --------------------------------------------------------------------------

def test_journal_summary_reports_moved_then_restored(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    groups = find_duplicate_groups(list(scanner.iter_records([str(tmp_tree)])))
    plain_group = _find_group(groups, "a1.txt", "a2.txt", "a_copy.txt")

    quarantine_dir = tmp_tree.parent / "q5"
    quarantine_reviewed_groups(
        groups, quarantine_dir, {plain_group.content_hash: None}, confirm_media=True
    )

    entries = journal_summary(quarantine_dir)
    assert len(entries) == 1
    assert entries[0]["status"] == "moved"

    restore_from_journal(quarantine_dir)
    entries_after = journal_summary(quarantine_dir)
    assert entries_after[0]["status"] == "restored"


# --- задача 24: «убрать» ведёт в карантин, и только туда -------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _library(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Три настоящих файла в «собранной библиотеке» и их хэши до всего."""
    album = tmp_path / "Library" / "2018" / "2018 Novosibirsk"
    album.mkdir(parents=True)
    digests = {}
    for name, payload in (
        ("IMG_1.jpg", b"\xff\xd8\xff" + b"first photo " * 400),
        ("IMG_2.jpg", b"\xff\xd8\xff" + b"second photo " * 300),
        ("IMG_3.jpg", b"\xff\xd8\xff" + b"third photo " * 200),
    ):
        (album / name).write_bytes(payload)
        digests[name] = _sha(album / name)
    return album, digests


def test_a_dropped_photo_is_moved_not_deleted(tmp_path: Path):
    """Несущий тест пункта 24. На настоящих файлах, с побайтной сверкой:
    файла нет на исходном пути, он лежит в карантине, и это **те же
    байты**. Удаления не происходит ни на одном шаге."""
    album, digests = _library(tmp_path)
    quarantine_root = tmp_path / "Quarantine"

    result = quarantine_review_drops(
        [{"path": str(album / "IMG_2.jpg"), "content_hash": "h2", "size": 0}],
        quarantine_root,
        confirm=True,
    )

    assert len(result.moved) == 1
    assert result.failed == []
    assert not (album / "IMG_2.jpg").exists()
    moved_to = Path(result.moved[0]["quarantined"])
    assert moved_to.is_file()
    assert _sha(moved_to) == digests["IMG_2.jpg"]
    # Соседи не тронуты: двигается ровно помеченное.
    assert _sha(album / "IMG_1.jpg") == digests["IMG_1.jpg"]
    assert _sha(album / "IMG_3.jpg") == digests["IMG_3.jpg"]


def test_a_dropped_photo_comes_back_with_the_same_bytes(tmp_path: Path):
    """Откат — тот же `restore_from_journal`, что у задач 5, 4 и 12, без
    единой новой строчки в нём. Это и есть проверка того, что «убрать»
    пользуется существующей механикой, а не второй своей."""
    album, digests = _library(tmp_path)
    quarantine_root = tmp_path / "Quarantine"
    quarantine_review_drops(
        [{"path": str(album / "IMG_1.jpg"), "content_hash": "h1"}],
        quarantine_root,
        confirm=True,
    )
    assert not (album / "IMG_1.jpg").exists()

    restored = restore_from_journal(quarantine_root)

    assert len(restored.restored) == 1
    assert restored.skipped == []
    assert (album / "IMG_1.jpg").is_file()
    assert _sha(album / "IMG_1.jpg") == digests["IMG_1.jpg"]


def test_without_confirmation_nothing_moves_and_no_quarantine_is_created(tmp_path: Path):
    """Тот же приём, которым задача 7 проверяет отказ быстрого режима:
    не только «не переместил», но и «папку даже не создал». Решения
    остаются в очереди, а не теряются."""
    album, digests = _library(tmp_path)
    quarantine_root = tmp_path / "Quarantine"

    result = quarantine_review_drops(
        [
            {"path": str(album / "IMG_1.jpg"), "content_hash": "h1"},
            {"path": str(album / "IMG_2.jpg"), "content_hash": "h2"},
        ],
        quarantine_root,
    )

    assert result.moved == []
    assert len(result.pending_media_review) == 2
    assert not quarantine_root.exists()
    for name, digest in digests.items():
        assert _sha(album / name) == digest


def test_a_photo_missing_from_its_library_path_is_reported(tmp_path: Path):
    album, _ = _library(tmp_path)
    result = quarantine_review_drops(
        [{"path": str(album / "gone.jpg"), "content_hash": "hX"}],
        tmp_path / "Quarantine",
        confirm=True,
    )
    assert result.moved == []
    assert len(result.failed) == 1
    assert "нет по этому пути" in result.failed[0]["error"]


def test_a_drop_writes_the_intent_before_the_move(tmp_path: Path):
    """Р5 целиком: строка намерения на диске раньше, чем произошло
    перемещение. Проверяется по журналу, который пишет тот же
    `journalled_move`, что и остальные два пути в карантин."""
    album, _ = _library(tmp_path)
    quarantine_root = tmp_path / "Quarantine"
    quarantine_review_drops(
        [{"path": str(album / "IMG_3.jpg"), "content_hash": "h3"}],
        quarantine_root,
        confirm=True,
    )
    lines = [
        json.loads(line)
        for line in (quarantine_root / JOURNAL_FILENAME).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    events = [entry["event"] for entry in lines]
    assert events == ["move_pending", "move_done"]
    assert lines[0]["origin"] == "album_review"
    assert lines[0]["group_hash"] == "h3"


def test_the_journal_screen_sees_a_drop_like_any_other_move(tmp_path: Path):
    album, _ = _library(tmp_path)
    quarantine_root = tmp_path / "Quarantine"
    quarantine_review_drops(
        [{"path": str(album / "IMG_1.jpg"), "content_hash": "h1"}],
        quarantine_root,
        confirm=True,
    )
    rows = journal_summary(quarantine_root)
    assert len(rows) == 1
    assert rows[0]["status"] == "moved"


def test_the_drop_path_contains_no_deletion(tmp_path: Path):
    """Единственное место в проекте, где файл удаляется, — сверенная копия
    при переезде между томами (пункт 22). Разбор альбома к этому списку не
    добавляется, и это проверяется по исходнику функции, а не на доверии:
    `quarantine.py` законно импортирует `shutil` и `os` ради перемещения,
    поэтому проверять надо тело именно этой функции."""
    import ast
    import inspect

    source = inspect.getsource(quarantine_review_drops)
    tree = ast.parse(textwrap.dedent(source))
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                calls.add(target.attr)
            elif isinstance(target, ast.Name):
                calls.add(target.id)

    for forbidden in ("remove", "unlink", "rmtree", "rmdir", "truncate", "open"):
        assert forbidden not in calls, forbidden
    # И наоборот: перемещение идёт через общий journalled-путь, а не своим.
    assert "_move_one" in calls
