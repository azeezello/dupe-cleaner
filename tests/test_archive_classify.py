"""Р1 — an archive is classified, and acted on, as a whole (task 4).

Every test here drives a real `ScanJob` over real archives on disk rather
than hand-building a report, because half of what task 4 had to get right
lives in the plumbing: whether the scanner counts members at all, whether a
member that fails to decrypt is noticed rather than silently dropped, and
whether "could not read" reaches the report as its own list instead of a
pile of warning strings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dupecleaner.archive_classify import (
    classify_archives,
    member_twins,
    verify_member,
    unread_skipped_entries,
    verify_members,
)
from dupecleaner.jobs import ScanJob
from dupecleaner.models import ArchiveClass, ArchiveStat, ScanReport


def _scan(root: Path, tmp_path: Path) -> ScanReport:
    job = ScanJob(roots=[str(root)], db_path=str(tmp_path / "index.db"))
    report = job.run()
    assert report is not None, f"скан не дошёл до конца: {job.progress.error}"
    return report


def _verdicts(report: ScanReport) -> dict[str, ArchiveClass]:
    return {Path(v.path).name: v.verdict for v in classify_archives(report)}


def test_all_four_classes_are_distinguished(archive_tree: Path, tmp_path: Path):
    """The headline: four archives, four different verdicts, from one scan."""
    verdicts = _verdicts(_scan(archive_tree, tmp_path))

    assert verdicts["fully.zip"] is ArchiveClass.FULLY_REDUNDANT
    assert verdicts["partial.zip"] is ArchiveClass.PARTIALLY_REDUNDANT
    assert verdicts["unique.zip"] is ArchiveClass.UNIQUE
    assert verdicts["locked.7z"] is ArchiveClass.UNREAD
    assert verdicts["broken.zip"] is ArchiveClass.UNREAD


def test_password_protected_archive_is_unread_not_unique(
    archive_tree: Path, tmp_path: Path
):
    """The class the pilot never covered.

    An encrypted 7z opens and lists like any other: names and sizes are
    right there. Only reading the bytes fails. Nothing stops such an
    archive from looking *unique* — its members can't be hashed, so they
    join no duplicate group, so nothing in the report mentions them — and
    "unique" is a verdict, while the truth is that we never saw inside.
    """
    report = _scan(archive_tree, tmp_path)
    stat = next(a for a in report.archives if Path(a.path).name == "locked.7z")

    assert stat.opened is True, "7z-каталог читается без пароля — архив открылся"
    assert stat.members_total == 1
    assert stat.members_unreadable == 1

    verdict = next(v for v in classify_archives(report) if v.path == stat.path)
    assert verdict.verdict is ArchiveClass.UNREAD
    assert "не прочитано участников" in (verdict.reason or "")
    # And it says *why* in words a person can act on, rather than handing
    # them py7zr's several-hundred-character codec-chain repr.
    assert "пароль" in (verdict.reason or "")
    assert len(stat.error or "") <= 160


def test_unread_archives_land_in_skipped_archives_not_only_warnings(
    archive_tree: Path, tmp_path: Path
):
    """Task 3 built one list for "not checked"; unread archives belong in
    it. A per-member warning line is not a substitute: warnings are free
    text nobody reads to the end, and the archive itself never appears.
    """
    report = _scan(archive_tree, tmp_path)
    skipped = {Path(s.path).name: s.reason for s in report.skipped_archives}

    assert skipped["broken.zip"] == "unreadable"
    assert skipped["locked.7z"] == "unreadable_members"
    assert len({s.path for s in report.skipped_archives}) == len(report.skipped_archives)


def test_unread_entries_are_not_duplicated_on_repeat(archive_tree: Path, tmp_path: Path):
    report = _scan(archive_tree, tmp_path)
    again = unread_skipped_entries(classify_archives(report), report.skipped_archives)
    assert again == []


def test_archives_that_only_mirror_each_other_are_never_fully_redundant(
    archive_tree: Path, tmp_path: Path
):
    """The mutual-vouching trap.

    `mirror_a.zip` and `mirror_b.zip` hold the same bytes and nothing on
    disk does. If an archive member counted as a twin, each would vouch for
    the other, both would be called fully redundant, both would go to
    quarantine — and the day the user empties the quarantine folder the
    content is gone from everywhere it ever was.
    """
    report = _scan(archive_tree, tmp_path)
    verdicts = _verdicts(report)

    assert verdicts["mirror_a.zip"] is ArchiveClass.UNIQUE
    assert verdicts["mirror_b.zip"] is ArchiveClass.UNIQUE

    # The pair *is* found as a duplicate group — it just grants no permission.
    mirrored = [
        g for g in report.groups
        if g.only_archive_members and len(g.records) == 2
        and all("mirror_" in r.display_path for r in g.records)
    ]
    assert len(mirrored) == 1, "группа дублей между архивами всё равно находится"
    assert member_twins(report).get(str(archive_tree / "mirror_a.zip")) is None


def test_partial_archive_reports_how_partial_it_is(archive_tree: Path, tmp_path: Path):
    report = _scan(archive_tree, tmp_path)
    verdict = next(
        v for v in classify_archives(report) if Path(v.path).name == "partial.zip"
    )
    assert verdict.members_total == 2
    assert verdict.members_redundant == 1
    assert verdict.redundant_bytes > 0


def test_empty_archive_is_unique_not_vacuously_redundant(tmp_path: Path):
    """"Every member has a twin" is true of an archive with no members.
    Acting on that would move a file for no reason at all.
    """
    import zipfile

    root = tmp_path / "data"
    root.mkdir()
    with zipfile.ZipFile(root / "empty.zip", "w"):
        pass

    verdicts = _verdicts(_scan(root, tmp_path))
    assert verdicts["empty.zip"] is ArchiveClass.UNIQUE


def test_quick_mode_archive_is_unread_not_unique(archive_tree: Path, tmp_path: Path):
    """--no-archives must not turn into a verdict either (finding A1)."""
    job = ScanJob(
        roots=[str(archive_tree)],
        db_path=str(tmp_path / "index.db"),
        include_archives=False,
    )
    report = job.run()
    assert report is not None

    verdicts = _verdicts(report)
    assert set(verdicts.values()) == {ArchiveClass.UNREAD}
    assert all(not a.opened for a in report.archives)


def test_one_hopeless_archive_does_not_kill_the_scan(
    archive_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A library that gives up mid-pass used to take the whole scan with
    it: the exception escaped the per-archive loop, and every other root
    went with it. It must cost exactly one archive, classified unread.
    """
    from dupecleaner import jobs

    real = jobs.hash_archive_members
    target = str(archive_tree / "fully.zip")

    def exploding(index, archive_path, members, on_start=None):
        if archive_path == target:
            raise RuntimeError("архив внезапно кончился")
        return real(index, archive_path, members, on_start=on_start)

    monkeypatch.setattr(jobs, "hash_archive_members", exploding)

    report = _scan(archive_tree, tmp_path)
    verdicts = _verdicts(report)

    assert verdicts["fully.zip"] is ArchiveClass.UNREAD
    assert verdicts["partial.zip"] is ArchiveClass.PARTIALLY_REDUNDANT
    assert verdicts["unique.zip"] is ArchiveClass.UNIQUE


def test_archive_stats_survive_json_round_trip(archive_tree: Path, tmp_path: Path):
    """The verdict is recomputed from the saved report by `quarantine`, so
    the counts it needs have to survive being written to disk.
    """
    report = _scan(archive_tree, tmp_path)
    restored = ScanReport.from_dict(json.loads(json.dumps(report.to_dict())))

    assert _verdicts(restored) == _verdicts(report)
    assert {a.path for a in restored.archives} == {a.path for a in report.archives}


def test_verify_members_rejects_a_twin_whose_bytes_changed(
    archive_tree: Path, tmp_path: Path
):
    """Verification re-reads the twin rather than calling exists().

    Same path, same size, different content — the case a presence check
    cannot tell from a healthy twin, and the case where trusting it means
    quarantining the archive that held the only real copy.
    """
    report = _scan(archive_tree, tmp_path)
    archive = str(archive_tree / "fully.zip")
    twins = member_twins(report)[archive]

    assert verify_members(archive, twins).ok

    victim = Path(twins[0].twin_paths[0])
    original = victim.read_bytes()
    victim.write_bytes(b"x" * len(original))  # identical size, different bytes

    verification = verify_members(archive, twins)
    assert not verification.ok
    assert "содержимое изменилось" in verification.failure_summary


def test_verify_members_accepts_any_surviving_twin(archive_tree: Path, tmp_path: Path):
    """A file-level quarantine run may have moved one copy of a pair. The
    guarantee is "a readable copy is on disk", not "this exact one is".
    """
    report = _scan(archive_tree, tmp_path)
    archive = str(archive_tree / "fully.zip")
    twins = member_twins(report)[archive]

    extra = archive_tree / "loose" / "red1_second_copy.txt"
    extra.write_bytes(Path(twins[0].twin_paths[0]).read_bytes())
    report2 = _scan(archive_tree, tmp_path / "second")
    twins2 = [t for t in member_twins(report2)[archive] if len(t.twin_paths) > 1]
    assert twins2, "у участника должно быть больше одного двойника на диске"

    Path(twins2[0].twin_paths[0]).unlink()
    ok_twin, _ = verify_member(twins2[0])
    assert ok_twin == twins2[0].twin_paths[1]


def test_classify_is_pure_on_a_hand_built_stat():
    """Precedence check without touching the disk: an unreadable member
    outranks everything, because unknown content is never redundant.
    """
    from dupecleaner.archive_classify import classify_archive

    stat = ArchiveStat(path="X", size=10, members_total=5, members_unreadable=1)
    verdict = classify_archive(stat, [])
    assert verdict.verdict is ArchiveClass.UNREAD


def test_cli_prints_how_redundant_a_partial_archive_is(
    archive_tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture
):
    """"One archive, partially redundant" is true and nearly useless. On the
    real Google Takeout that line stood for "3727 of 6286 members, 9.4 GB,
    are already on disk" — the number that decides whether dissolving such
    an archive is worth building at all.
    """
    from dupecleaner.cli import _print_archive_verdicts

    _print_archive_verdicts(_scan(archive_tree, tmp_path))
    out = capsys.readouterr().out

    assert "Частично избыточен" in out
    assert "1 из 2" in out
    assert "Не прочитан" in out
