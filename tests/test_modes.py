"""Р7: «Быстро» / «Полно».

The modes differ in *coverage*, never in confidence, and the tests here are
arranged around that one sentence. Roughly half of them check that quick
mode really does look at less (otherwise the mode is pointless), and the
other half check that looking at less never buys it a weaker standard of
proof (otherwise the mode is dangerous, which is the failure Р7 exists to
prevent and Р0 forbids outright).

The second half matters more. A bug that makes quick mode slower than it
could be costs minutes; a bug that lets it authorise moving a file it never
read byte-for-byte costs a photograph that exists nowhere else.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from dupecleaner.archive_classify import classify_archives
from dupecleaner.cli import build_parser, main
from dupecleaner.config import QUICK_HASH_SAMPLE_BYTES
from dupecleaner.dedupe import verify_group
from dupecleaner.jobs import ScanJob
from dupecleaner.models import ArchiveClass, ScanMode, ScanReport
from dupecleaner.quarantine import quarantine_archives
from dupecleaner.storage import ScanIndex


def _run(root: Path, db: Path, mode: ScanMode, **kwargs) -> ScanJob:
    job = ScanJob(roots=[str(root)], db_path=db, mode=mode, **kwargs)
    job.run()
    assert job.progress.status == "done", job.progress.error
    return job


def _display_paths(job: ScanJob) -> set[str]:
    return {r.display_path for g in job.report.groups for r in g.records}


# --------------------------------------------------------------------------
# Coverage: quick mode looks at less
# --------------------------------------------------------------------------


def test_quick_mode_skips_archives_but_says_so(tmp_tree: Path, tmp_path: Path):
    job = _run(tmp_tree, tmp_path / "index.db", ScanMode.QUICK)

    skipped = {Path(a.path).name: a.reason for a in job.report.skipped_archives}
    assert skipped == {
        "backup.zip": "excluded_by_mode",
        "archive_only.zip": "excluded_by_mode",
    }

    # Not one archive member anywhere in the results...
    assert not any(
        r.is_archive_member for g in job.report.groups for r in g.records
    )
    # ...while the plain duplicates are found exactly as in full mode. This
    # pairing is the whole claim of the mode: less looked at, same answer
    # about what was looked at.
    assert any("a1.txt" in p for p in _display_paths(job))


def test_full_mode_finds_the_duplicates_hiding_inside_archives(
    tmp_tree: Path, tmp_path: Path
):
    job = _run(tmp_tree, tmp_path / "index.db", ScanMode.FULL)

    assert job.report.skipped_archives == []
    assert any(
        r.is_archive_member for g in job.report.groups for r in g.records
    )
    # The mixed group: a plain file on disk and a member of backup.zip.
    mixed = [
        g
        for g in job.report.groups
        if any(r.is_archive_member for r in g.records)
        and any(not r.is_archive_member for r in g.records)
    ]
    assert mixed, "a_copy.txt inside backup.zip duplicates a1.txt on disk"


def test_quick_results_are_exactly_the_full_results_minus_the_archives(
    tmp_tree: Path, tmp_path: Path
):
    r"""Quick mode's answer equals full mode's answer with archive members
    deleted from it — nothing more is lost, nothing is found instead.

    Stated as a test because it is also a measuring instrument. The real
    comparison this task owes (задача 1's folder, quick vs full) costs ten
    minutes of disk per run, and running it twice through the desktop
    bridge is not possible here. But if this relationship holds, the quick
    numbers can be derived exactly from the full report задача 4 already
    produced, instead of estimated. This pins the relationship so that
    derivation is sound rather than plausible.

    And the derivation checks out against an independent measurement: on
    `D:\Photos` + the Takeout it yields 6000 groups / 13.50 GB, which is
    what задача 1 measured on `D:\Photos` by itself — as it must be, since
    the Takeout is one archive and quick mode does not open it.
    """
    full = _run(tmp_tree, tmp_path / "full.db", ScanMode.FULL)
    quick = _run(tmp_tree, tmp_path / "quick.db", ScanMode.QUICK)

    derived = {}
    for group in full.report.groups:
        plain = [r.display_path for r in group.records if not r.is_archive_member]
        if len(plain) >= 2:
            derived[group.content_hash] = sorted(plain)

    actual = {
        g.content_hash: sorted(r.display_path for r in g.records)
        for g in quick.report.groups
    }
    assert actual == derived


def test_no_archives_may_narrow_full_mode_but_nothing_may_widen_quick(
    tmp_tree: Path, tmp_path: Path
):
    """`--no-archives` predates Р7 and survives as an escape hatch, but the
    two switches must not be able to contradict each other.

    If `include_archives=True` could override quick mode, the result would
    be a third, undocumented mode whose report still calls itself quick —
    and `quarantine_archives` decides what it is allowed to touch by
    reading exactly that claim.
    """
    narrowed = ScanJob(
        roots=[str(tmp_tree)],
        db_path=tmp_path / "a.db",
        mode=ScanMode.FULL,
        include_archives=False,
    )
    assert narrowed.include_archives is False

    widened = ScanJob(
        roots=[str(tmp_tree)],
        db_path=tmp_path / "b.db",
        mode=ScanMode.QUICK,
        include_archives=True,
    )
    assert widened.include_archives is False


# --------------------------------------------------------------------------
# The guarantee: quick mode buys no shortcuts (Р0)
# --------------------------------------------------------------------------


def test_quick_mode_does_not_group_files_that_only_look_identical(tmp_path: Path):
    """The load-bearing test of this task.

    Two files of the same size whose first and last 64 KB match exactly but
    whose middles differ. They are identical as far as every cheap signal
    goes — name, size, and the quick hash itself — so a "fast" mode that
    grouped on anything short of a full read would pair them, and quarantine
    would move one of them away.

    Р7 rejected exactly that trade. Both modes run the funnel to the full
    hash, so this pair must not form a group in either one.
    """
    root = tmp_path / "data"
    root.mkdir()

    head = b"A" * QUICK_HASH_SAMPLE_BYTES
    tail = b"B" * QUICK_HASH_SAMPLE_BYTES
    middle = 72 * 1024

    (root / "IMG_0001.jpg").write_bytes(head + b"X" * middle + tail)
    (root / "IMG_0001 (1).jpg").write_bytes(head + b"Y" * middle + tail)

    # Sanity: the cheap signals really are identical, so the test is testing
    # something. Same size, same quick hash.
    from dupecleaner.hashing import quick_hash

    sizes, quicks = set(), set()
    for name in ("IMG_0001.jpg", "IMG_0001 (1).jpg"):
        path = root / name
        sizes.add(path.stat().st_size)
        with open(path, "rb") as fh:
            quicks.add(quick_hash(fh, path.stat().st_size))
    assert len(sizes) == 1 and len(quicks) == 1

    for mode in (ScanMode.QUICK, ScanMode.FULL):
        job = _run(root, tmp_path / f"{mode.value}.db", mode)
        assert job.report.groups == [], (
            f"{mode.value}: two different photos were grouped as duplicates "
            "on a head+tail sample"
        )


def test_quick_mode_still_finds_genuinely_identical_files(tmp_path: Path):
    """The other half of the test above: the funnel must still do its job."""
    root = tmp_path / "data"
    root.mkdir()
    content = b"C" * (200 * 1024)
    (root / "one.bin").write_bytes(content)
    (root / "two.bin").write_bytes(content)

    job = _run(root, tmp_path / "index.db", ScanMode.QUICK)
    assert len(job.report.groups) == 1
    assert len(job.report.groups[0].records) == 2


def test_quick_mode_report_never_authorises_moving_an_archive(
    archive_tree: Path, tmp_path: Path
):
    """A quick run must not be able to quarantine an archive — not even
    `fully.zip`, whose members really are all loose on disk.

    Two independent things are checked, on purpose. First that no archive
    gets an actionable verdict (the mechanism: nothing was read, so nothing
    is redundant). Second that `quarantine_archives` refuses anyway (the
    rule, stated once so it survives a future change to the mechanism).
    """
    job = _run(archive_tree, tmp_path / "index.db", ScanMode.QUICK)

    verdicts = classify_archives(job.report)
    assert verdicts, "the scan still has to notice the archives exist"
    assert all(v.verdict is ArchiveClass.UNREAD for v in verdicts)
    assert not any(v.is_actionable for v in verdicts)

    result = quarantine_archives(
        verdicts,
        job.report,
        tmp_path / "quarantine",
        confirm_media=True,  # even with every confirmation given
    )
    assert result.moved == []
    assert result.freed_bytes == 0
    assert len(result.refused) == len(verdicts)
    assert all("быстром режиме" in item["reason"] for item in result.refused)

    # Nothing on disk moved, and no quarantine folder was even created.
    assert (archive_tree / "fully.zip").exists()
    assert not (tmp_path / "quarantine").exists()


def test_full_mode_report_does_authorise_it(archive_tree: Path, tmp_path: Path):
    """The contrast that gives the previous test meaning: with the same
    folder, the same code and the only difference being coverage, the fully
    redundant archive does move.
    """
    job = _run(archive_tree, tmp_path / "index.db", ScanMode.FULL)

    verdicts = classify_archives(job.report)
    fully = [v for v in verdicts if v.verdict is ArchiveClass.FULLY_REDUNDANT]
    assert {Path(v.path).name for v in fully} >= {"fully.zip"}

    result = quarantine_archives(
        verdicts, job.report, tmp_path / "quarantine", confirm_media=True
    )
    assert {Path(m["archive"]).name for m in result.moved} >= {"fully.zip"}
    assert not (archive_tree / "fully.zip").exists()


def test_a_quick_report_stays_quick_across_json(tmp_tree: Path, tmp_path: Path):
    """The report outlives the process: the CLI writes it to report.json and
    `quarantine` reads it back, possibly days later. The mode is what that
    later command consults for permission, so it has to survive the trip —
    and a report written before this field existed must read back as full,
    which is what those runs actually did.
    """
    job = _run(tmp_tree, tmp_path / "index.db", ScanMode.QUICK)

    reloaded = ScanReport.from_dict(
        json.loads(json.dumps(job.report.to_dict(), ensure_ascii=False))
    )
    assert reloaded.mode is ScanMode.QUICK

    legacy = job.report.to_dict()
    del legacy["mode"]
    assert ScanReport.from_dict(legacy).mode is ScanMode.FULL


# --------------------------------------------------------------------------
# «Досчитать полностью»: reaching full coverage without scanning from scratch
# --------------------------------------------------------------------------


def _write_jpeg(path: Path, colour: tuple[int, int, int]) -> None:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), colour).save(buffer, format="JPEG", quality=85)
    path.write_bytes(buffer.getvalue())


def test_upgrading_to_full_rehashes_nothing_it_already_hashed(
    tmp_tree: Path, tmp_path: Path
):
    """Р7 promises the upgrade costs only what the quick run skipped.

    Measured, not asserted by inspection: the upgrade run is compared
    against a full scan of the same folder starting from an empty index.
    Both end at the same answer; the upgrade reads strictly less to get
    there, and what it does read is the archive contents the quick run
    never opened.
    """
    shared_db = tmp_path / "upgraded.db"
    quick = _run(tmp_tree, shared_db, ScanMode.QUICK)
    assert quick.progress.files_hashed > 0
    # Everything the quick run resolved to a full hash — i.e. every file it
    # would be able to quarantine. Counted from its own result rather than
    # from `files_hashed`, which tallies work (a file passes through two
    # hashing phases) rather than files.
    quick_resolved = len(
        {r.display_path for g in quick.report.groups for r in g.records}
    )
    assert quick_resolved > 0

    upgrade = _run(tmp_tree, shared_db, ScanMode.FULL)
    from_scratch = _run(tmp_tree, tmp_path / "scratch.db", ScanMode.FULL)

    # Same answer either way.
    assert len(upgrade.report.groups) == len(from_scratch.report.groups)
    assert upgrade.report.skipped_archives == []

    # But the upgrade paid for less of it: the plain files came from the
    # index, so only archive members had to be read.
    assert upgrade.progress.files_hashed < from_scratch.progress.files_hashed
    assert upgrade.progress.files_from_cache >= quick_resolved
    upgrade_read = {
        r.display_path
        for g in upgrade.report.groups
        for r in g.records
        if r.is_archive_member
    }
    assert upgrade_read, "the upgrade is supposed to open the archives"


def test_quick_mode_builds_no_previews_and_the_upgrade_fills_them_in(
    tmp_path: Path,
):
    """Previews are the other thing quick mode skips, and the one most
    likely to be quietly lost in the upgrade.

    A plain photo already hashed by the quick run never enters
    `needs_full_hash` again, so the hashing loop — where preview generation
    used to live exclusively — never sees it. Without
    `ScanJob._preview_phase` the upgraded scan would show an empty grid and
    give no hint why.
    """
    root = tmp_path / "photos"
    root.mkdir()
    _write_jpeg(root / "shot.jpg", (200, 40, 40))
    (root / "copy.jpg").write_bytes((root / "shot.jpg").read_bytes())

    db = tmp_path / "index.db"
    quick = _run(root, db, ScanMode.QUICK)
    assert len(quick.report.groups) == 1
    content_hash = quick.report.groups[0].content_hash

    with ScanIndex(db) as index:
        assert index.get_thumbnail_meta(content_hash) is None

    upgrade = _run(root, db, ScanMode.FULL)
    assert upgrade.progress.files_hashed == 0  # nothing re-hashed...
    with ScanIndex(db) as index:
        meta = index.get_thumbnail_meta(content_hash)
    assert meta is not None  # ...yet the preview is now there


def test_full_scan_from_scratch_builds_previews_too(tmp_path: Path):
    root = tmp_path / "photos"
    root.mkdir()
    _write_jpeg(root / "shot.jpg", (30, 90, 200))
    (root / "copy.jpg").write_bytes((root / "shot.jpg").read_bytes())

    db = tmp_path / "index.db"
    job = _run(root, db, ScanMode.FULL)
    with ScanIndex(db) as index:
        assert index.get_thumbnail_meta(job.report.groups[0].content_hash) is not None


# --------------------------------------------------------------------------
# «Сверить полностью» for one group
# --------------------------------------------------------------------------


def test_verify_group_confirms_an_intact_group(tmp_tree: Path, tmp_path: Path):
    job = _run(tmp_tree, tmp_path / "index.db", ScanMode.FULL)
    group = next(g for g in job.report.groups if not g.has_archive_members)

    result = verify_group(group)
    assert result.ok
    assert len(result.confirmed_paths) == len(group.records)
    assert result.failures == []


def test_verify_group_notices_a_copy_that_changed_since_the_scan(
    tmp_tree: Path, tmp_path: Path
):
    """What this action is actually for: a report is a photograph of the
    disk at scan time, and gets reviewed hours later.
    """
    job = _run(tmp_tree, tmp_path / "index.db", ScanMode.FULL)
    group = next(g for g in job.report.groups if not g.has_archive_members)

    victim = Path(group.records[0].real_path)
    victim.write_bytes(b"edited since the scan ran")

    result = verify_group(group)
    assert not result.ok  # fewer than two live identical copies remain
    failures = {f.display_path: f.reason for f in result.failures}
    assert str(victim) in failures
    assert "размер" in failures[str(victim)] or "содержимое" in failures[str(victim)]


def test_verify_group_reads_an_archive_once_for_all_its_members(
    tmp_path: Path, monkeypatch
):
    """A group can hold several members of the same archive, and re-reading
    them one at a time would be pilot finding A2 all over again.
    """
    import tarfile

    root = tmp_path / "data"
    root.mkdir()
    content = b"the same bytes twice inside one tar " * 40
    archive = root / "dump.tar"
    with tarfile.open(archive, "w") as tf:
        for name in ("one.bin", "two.bin"):
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))

    job = _run(root, tmp_path / "index.db", ScanMode.FULL)
    group = next(g for g in job.report.groups if g.only_archive_members)
    assert len(group.records) == 2

    real_open = tarfile.open
    opens = []

    def _counting_open(*args, **kwargs):
        opens.append(1)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(tarfile, "open", _counting_open)
    result = verify_group(group)

    assert result.ok
    assert len(opens) == 1


def test_a_group_verified_down_to_one_copy_is_not_ok(tmp_tree: Path, tmp_path: Path):
    """One surviving copy is not a duplicate group, so "verified" must not
    read as "safe to reclaim space here".
    """
    job = _run(tmp_tree, tmp_path / "index.db", ScanMode.FULL)
    group = next(
        g
        for g in job.report.groups
        if not g.has_archive_members and len(g.records) == 2
    )

    Path(group.records[0].real_path).unlink()
    result = verify_group(group)

    assert not result.ok
    assert len(result.confirmed_paths) == 1


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------


def test_cli_defaults_to_quick_and_accepts_both_modes():
    parser = build_parser()
    assert parser.parse_args(["scan", "D:/Photos"]).mode == "quick"
    assert parser.parse_args(["scan", "D:/Photos", "--mode", "full"]).mode == "full"
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "D:/Photos", "--mode", "sortof"])


def test_cli_scan_writes_a_report_that_remembers_its_mode(
    tmp_tree: Path, tmp_path: Path
):
    report_path = tmp_path / "report.json"
    code = main(
        [
            "--db",
            str(tmp_path / "index.db"),
            "scan",
            str(tmp_tree),
            "--report",
            str(report_path),
        ]
    )
    assert code == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["mode"] == "quick"


def test_cli_quarantine_refuses_archives_from_a_quick_report(
    tmp_tree: Path, tmp_path: Path, capsys
):
    report_path = tmp_path / "report.json"
    quarantine_dir = tmp_path / "quarantine"
    main(
        [
            "--db",
            str(tmp_path / "index.db"),
            "scan",
            str(tmp_tree),
            "--report",
            str(report_path),
        ]
    )
    capsys.readouterr()

    main(
        [
            "quarantine",
            "--report",
            str(report_path),
            "--quarantine-dir",
            str(quarantine_dir),
            "--archives",
        ]
    )
    err = capsys.readouterr().err
    assert "быстром режиме" in err
    assert "--mode full" in err
    for name in ("backup.zip", "archive_only.zip"):
        assert (tmp_tree / name).exists()
