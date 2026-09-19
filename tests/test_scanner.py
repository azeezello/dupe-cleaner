from __future__ import annotations

from pathlib import Path

from dupecleaner.models import MediaKind
from dupecleaner.scanner import Scanner, classify_media


def test_classify_media():
    assert classify_media("photo.JPG") is MediaKind.PHOTO
    assert classify_media("clip.mp4") is MediaKind.VIDEO
    assert classify_media("notes.txt") is MediaKind.NONE


def test_scanner_finds_plain_files_and_archive_members(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    records = list(scanner.iter_records([str(tmp_tree)]))

    plain_paths = {r.real_path for r in records if not r.is_archive_member}
    assert str(tmp_tree / "a1.txt") in plain_paths
    assert str(tmp_tree / "sub" / "a2.txt") in plain_paths
    assert str(tmp_tree / "unique.txt") in plain_paths

    archive_members = [r for r in records if r.is_archive_member]
    member_names = {r.member_name for r in archive_members}
    assert "inner/a_copy.txt" in member_names
    assert "inner/note.txt" in member_names
    assert "one.bin" in member_names
    assert "two.bin" in member_names

    assert scanner.total_files_seen == len(records)
    assert scanner.warnings == []


def test_scanner_without_archives_skips_zip_contents(tmp_tree: Path):
    scanner = Scanner(include_archives=False)
    records = list(scanner.iter_records([str(tmp_tree)]))
    assert not any(r.is_archive_member for r in records)
    # The zip files themselves are still seen as opaque regular files.
    assert any(r.real_path.endswith("backup.zip") for r in records)


def test_missing_root_path_is_warned_not_raised(tmp_path: Path):
    scanner = Scanner()
    missing = tmp_path / "does_not_exist"
    records = list(scanner.iter_records([str(missing)]))
    assert records == []
    assert any("не найдена" in w or "not found" in w.lower() for w in scanner.warnings)


def test_no_archives_mode_records_skipped_archives(tmp_tree: Path):
    """Finding A1: --no-archives must not silently treat an archive as an
    ordinary, fully-examined file. It's still counted as "seen" (unchanged
    behaviour), but it must also show up in its own skipped-archives list.
    """
    scanner = Scanner(include_archives=False)
    list(scanner.iter_records([str(tmp_tree)]))

    skipped_paths = {a.path for a in scanner.skipped_archives}
    assert any(p.endswith("backup.zip") for p in skipped_paths)
    assert any(p.endswith("archive_only.zip") for p in skipped_paths)
    assert all(a.reason == "excluded_by_mode" for a in scanner.skipped_archives)
    assert all(a.size > 0 for a in scanner.skipped_archives)


def test_include_archives_mode_has_no_skipped_archives(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    list(scanner.iter_records([str(tmp_tree)]))
    assert scanner.skipped_archives == []


def test_unreadable_archive_is_recorded_as_skipped(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    broken = root / "broken.zip"
    broken.write_bytes(b"this is not a real zip file at all")

    scanner = Scanner(include_archives=True)
    records = list(scanner.iter_records([str(root)]))

    # An archive that fails to open produces no FileRecord at all (nothing
    # inside it could be enumerated), but it's still counted as "seen" and
    # explained, rather than just vanishing from the totals.
    assert records == []
    assert scanner.total_files_seen == 1
    assert any("Не удалось открыть архив" in w for w in scanner.warnings)
    assert len(scanner.skipped_archives) == 1
    assert scanner.skipped_archives[0].path.endswith("broken.zip")
    assert scanner.skipped_archives[0].reason == "unreadable"
