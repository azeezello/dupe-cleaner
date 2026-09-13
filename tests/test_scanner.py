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
