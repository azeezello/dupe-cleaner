from __future__ import annotations

from pathlib import Path

from dupecleaner.dedupe import find_duplicate_groups
from dupecleaner.models import MediaKind
from dupecleaner.scanner import Scanner


def test_finds_plain_and_mixed_and_archive_only_groups(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    records = list(scanner.iter_records([str(tmp_tree)]))
    warnings: list[str] = []
    groups = find_duplicate_groups(records, warnings=warnings)

    assert warnings == []

    # Group 1: a1.txt, sub/a2.txt, and backup.zip::inner/a_copy.txt — a
    # "mixed" group spanning plain files and an archive member.
    mixed = next(g for g in groups if any("a1.txt" in r.display_path for r in g.records))
    assert len(mixed.records) == 3
    assert mixed.has_archive_members
    assert not mixed.only_archive_members
    assert mixed.wasted_bytes == mixed.size * 2

    # Group 2: archive_only.zip::one.bin / two.bin — both archive members,
    # no plain-file counterpart anywhere.
    archive_only = next(g for g in groups if any("one.bin" in r.display_path for r in g.records))
    assert archive_only.only_archive_members
    assert len(archive_only.records) == 2

    # Group 3: photo1.jpg / photo2.jpg — identical media files.
    media_group = next(g for g in groups if g.is_media)
    assert len(media_group.records) == 2
    assert all(r.media_kind is MediaKind.PHOTO for r in media_group.records)

    # unique.txt and inner/note.txt must not appear in any group.
    all_display_paths = {r.display_path for g in groups for r in g.records}
    assert not any("unique.txt" in p for p in all_display_paths)
    assert not any("note.txt" in p for p in all_display_paths)


def test_no_false_positive_for_same_size_different_content(tmp_path: Path):
    from dupecleaner.models import FileRecord

    f1 = tmp_path / "one.bin"
    f2 = tmp_path / "two.bin"
    f1.write_bytes(b"AAAA")
    f2.write_bytes(b"BBBB")

    records = [
        FileRecord(display_path=str(f1), real_path=str(f1), size=4, mtime=0),
        FileRecord(display_path=str(f2), real_path=str(f2), size=4, mtime=0),
    ]
    groups = find_duplicate_groups(records)
    assert groups == []


def test_empty_files_are_ignored(tmp_path: Path):
    from dupecleaner.models import FileRecord

    f1 = tmp_path / "one.bin"
    f2 = tmp_path / "two.bin"
    f1.write_bytes(b"")
    f2.write_bytes(b"")

    records = [
        FileRecord(display_path=str(f1), real_path=str(f1), size=0, mtime=0),
        FileRecord(display_path=str(f2), real_path=str(f2), size=0, mtime=0),
    ]
    groups = find_duplicate_groups(records)
    assert groups == []
