from __future__ import annotations

import json
from pathlib import Path

from dupecleaner.dedupe import find_duplicate_groups
from dupecleaner.quarantine import run_quarantine
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
