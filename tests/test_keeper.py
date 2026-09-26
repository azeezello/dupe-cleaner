# -*- coding: utf-8 -*-
"""Р8: which copy of a byte-identical group stays where it is.

The cases below are the real ones from the pilot report, with the real
folder names, because the rule exists to fix a specific measured failure
(62% of groups kept the flat `Pictures\\` dump) and a synthetic `dir_a` vs
`dir_b` test would not have caught it.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from dupecleaner.archive_classify import member_twins
from dupecleaner.dedupe import find_duplicate_groups
from dupecleaner.keeper import (
    SegmentKind,
    choose_keeper,
    classify_segment,
    keeper_reason,
    rank_keepers,
)
from dupecleaner.models import DuplicateGroup, FileRecord, MediaKind, ScanReport
from dupecleaner.quarantine import run_quarantine
from dupecleaner.scanner import Scanner

ROOT = r"D:\Photos\Photos"


def rec(relative: str, mtime: float = 1000.0, media: bool = True) -> FileRecord:
    path = f"{ROOT}\\{relative}"
    return FileRecord(
        display_path=path,
        real_path=path,
        size=4096,
        mtime=mtime,
        media_kind=MediaKind.PHOTO if media else MediaKind.NONE,
    )


def member(archive: str, name: str, mtime: float = 1000.0) -> FileRecord:
    return FileRecord(
        display_path=f"{archive}::{name}",
        real_path=archive,
        size=4096,
        mtime=mtime,
        media_kind=MediaKind.PHOTO,
        is_archive_member=True,
        archive_path=archive,
        member_name=name,
    )


# --------------------------------------------------------------------------
# Segment classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("Pictures", SegmentKind.GENERIC),
        ("pictures", SegmentKind.GENERIC),
        ("Photos", SegmentKind.GENERIC),
        ("DCIM", SegmentKind.GENERIC),
        ("Camera Roll", SegmentKind.GENERIC),
        ("Загрузки", SegmentKind.GENERIC),
        ("Новая папка (2)", SegmentKind.GENERIC),
        ("New Folder", SegmentKind.GENERIC),
        ("2020", SegmentKind.DATED),
        ("2010 -2020", SegmentKind.DATED),
        ("2022-2023", SegmentKind.DATED),
        ("13 May 2018", SegmentKind.DATED),
        ("2019-05-04", SegmentKind.DATED),
        ("Краснодар", SegmentKind.NAMED),
        ("Wedding Day", SegmentKind.NAMED),
        ("Pamir 2016", SegmentKind.NAMED),
        ("Грузия 2022-2023", SegmentKind.NAMED),
        ("Wedding 16042017", SegmentKind.NAMED),
        # A bare month word has no digit, so it stays NAMED — "Мая" and
        # "May" are names of people at least as often as months, and NAMED
        # is the class that errs towards keeping the copy.
        ("Мая", SegmentKind.NAMED),
        # "Photos from 2022" is not generic just because it starts with a
        # generic word: the list is matched against the whole segment.
        ("Photos from 2022", SegmentKind.NAMED),
    ],
)
def test_segment_classification(name: str, expected: SegmentKind):
    assert classify_segment(name) is expected


# --------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------

def test_named_folder_beats_the_flat_dump():
    """P1.2, the case the whole decision exists for: 914 files from
    `Краснодар` used to be quarantined in favour of `Pictures\\`."""
    dump = rec(r"Pictures\20180926_120940.jpg")
    named = rec(r"Краснодар\20180926_120940.jpg")
    assert choose_keeper([dump, named]) is named
    assert choose_keeper([named, dump]) is named


def test_dated_folder_also_beats_the_dump():
    dump = rec(r"Pictures\IMG_1.jpg")
    dated = rec(r"2010 -2020\2020\IMG_1.jpg")
    assert choose_keeper([dump, dated]) is dated


def test_named_folder_beats_a_year_folder():
    """A year is recoverable from EXIF; a name the person invented is not
    (Р4, fourth link of the naming chain)."""
    dated = rec(r"2010 -2020\2016\IMG_2.jpg")
    named = rec(r"Pictures\Pamir 2016\IMG_2.jpg")
    assert choose_keeper([dated, named]) is named


def test_fewer_generic_ancestors_wins_between_two_named_folders():
    """`Wedding 16042017` sits at the top level; `Wedding Day` sits inside
    the dump. Both are human names, so the one that is not buried in
    `Pictures\\` is the less accidental location."""
    in_dump = rec(r"Pictures\Wedding Day\20170416_145106.jpg")
    top_level = rec(r"Wedding 16042017\20170416_145106.jpg")
    assert choose_keeper([in_dump, top_level]) is top_level


def test_more_named_segments_wins_a_more_precise_filing():
    coarse = rec(r"Грузия 2022-2023\IMG_3.jpg")
    precise = rec(r"Грузия 2022-2023\Батуми\IMG_3.jpg")
    assert choose_keeper([coarse, precise]) is precise


def test_the_old_shortest_path_rule_still_breaks_remaining_ties():
    """The Р8 keys are a prefix in front of the old ones, so anything they
    cannot separate answers exactly as it did before Р8 existed."""
    short = rec(r"Pictures\Tbt\IMG_4.jpg", mtime=2000.0)
    long = rec(r"Pictures\Pamir 2016\IMG_4.jpg", mtime=1000.0)
    assert choose_keeper([long, short]) is short  # shorter path first


def test_mtime_is_the_last_structural_tiebreaker_not_the_first():
    """Measured on the pilot report: in 3878 of 6000 groups the *oldest*
    copy is the one in the dump, so "oldest is the original" would have
    reproduced most of the bug it was meant to fix."""
    old_in_dump = rec(r"Pictures\IMG_5.jpg", mtime=1.0)
    new_in_album = rec(r"Краснодар\IMG_5.jpg", mtime=9_999.0)
    assert choose_keeper([old_in_dump, new_in_album]) is new_in_album


def test_result_does_not_depend_on_input_order():
    records = [
        rec(r"Pictures\IMG_6.jpg"),
        rec(r"2010 -2020\2019\IMG_6.jpg"),
        rec(r"Краснодар\IMG_6.jpg"),
        rec(r"Pictures\Wedding Day\IMG_6.jpg"),
        rec(r"Wedding 16042017\IMG_6.jpg"),
    ]
    expected = choose_keeper(records)
    rng = random.Random(20260920)
    for _ in range(20):
        shuffled = records[:]
        rng.shuffle(shuffled)
        assert choose_keeper(shuffled) is expected
        assert [r.display_path for r in rank_keepers(shuffled)] == [
            r.display_path for r in rank_keepers(records)
        ]


# --------------------------------------------------------------------------
# Archive members and the degenerate cases
# --------------------------------------------------------------------------

def test_a_plain_file_always_beats_an_archive_member():
    """Even when the member's path inside the archive looks more
    meaningful — an archive member can never be quarantined (Р1), so
    making it the keeper would mean moving the only copy that *can* be
    moved."""
    inside = member(r"D:\Takeouts\takeout.tgz", "Takeout/Краснодар/IMG_7.jpg")
    dump = rec(r"Pictures\IMG_7.jpg")
    assert choose_keeper([inside, dump]) is dump


def test_single_disk_copy_beside_an_archive_member_is_the_keeper():
    """2812 of the 8814 groups in the combined real run look exactly like
    this: one file on disk, one member inside the Google takeout. There is
    nothing to choose, and the rule must not try."""
    inside = member(r"D:\Takeouts\takeout.tgz", "Takeout/Google Photos/Photos from 2022/x.jpg")
    only = rec(r"Pictures\x.jpg")
    assert choose_keeper([inside, only]) is only


def test_all_copies_inside_archives_still_returns_one_deterministically():
    a = member(r"D:\a.zip", "one.bin")
    b = member(r"D:\b.zip", "two.bin")
    assert choose_keeper([a, b]) in (a, b)
    assert choose_keeper([a, b]) is choose_keeper([b, a])


def test_empty_candidate_list_is_a_named_error():
    with pytest.raises(ValueError):
        choose_keeper([])


# --------------------------------------------------------------------------
# The two consumers must not drift apart
# --------------------------------------------------------------------------

def test_member_twins_orders_twins_by_the_same_rule():
    """Задача 4 re-verifies an archive's twins in this order right before
    moving the archive. If the order disagreed with `choose_keeper`, the
    first copy it asked about would be the one a file-level quarantine had
    already taken away."""
    inside = member(r"D:\Takeouts\takeout.tgz", "Takeout/IMG_8.jpg")
    dump = rec(r"Pictures\IMG_8.jpg")
    named = rec(r"Краснодар\IMG_8.jpg")
    report = ScanReport(
        scanned_roots=[ROOT],
        total_files_seen=3,
        groups=[DuplicateGroup(content_hash="h8", records=[inside, dump, named])],
    )
    twins = member_twins(report)[r"D:\Takeouts\takeout.tgz"]
    assert len(twins) == 1
    assert twins[0].twin_paths[0] == named.real_path
    assert twins[0].twin_paths[0] == choose_keeper([inside, dump, named]).real_path


def test_quarantine_keeps_the_named_folder_and_moves_the_dump(tmp_path: Path):
    """End to end on real files: the named folder survives in place."""
    root = tmp_path / "Photos"
    (root / "Pictures").mkdir(parents=True)
    (root / "Краснодар").mkdir()
    photo = b"\xff\xd8\xff" + b"same bytes in both places " * 40
    dump_copy = root / "Pictures" / "20180926_120940.jpg"
    album_copy = root / "Краснодар" / "20180926_120940.jpg"
    dump_copy.write_bytes(photo)
    album_copy.write_bytes(photo)

    groups = find_duplicate_groups(list(Scanner().iter_records([str(root)])))
    assert len(groups) == 1

    result = run_quarantine(groups, tmp_path / "quarantine", confirm_media=True)

    assert album_copy.exists(), "the folder the person named must stay in place"
    assert not dump_copy.exists()
    assert [Path(m["original"]).parent.name for m in result.moved] == ["Pictures"]
    assert Path(next(iter(result.kept.values()))).parent.name == "Краснодар"


def test_the_serialized_report_carries_the_keeper_so_the_ui_cannot_guess():
    """`web/static/app.js` drew its "(оставить)" badge by re-deriving the
    keeper as the shortest path. That was a second implementation of the
    rule, and Р8 made it a wrong one — so the answer now travels with the
    group, and a JSON round trip keeps working."""
    dump = rec(r"Pictures\IMG_9.jpg")
    named = rec(r"Краснодар\IMG_9.jpg")
    report = ScanReport(
        scanned_roots=[ROOT],
        total_files_seen=2,
        groups=[DuplicateGroup(content_hash="h9", records=[dump, named])],
    )
    payload = report.to_dict()
    assert payload["groups"][0]["keeper_display_path"] == named.display_path
    # The extra field must not break reading a report back (Р7: a report
    # outlives the process and is re-read by `quarantine` days later).
    assert ScanReport.from_dict(payload).groups[0].keeper_display_path == (
        named.display_path
    )


# --------------------------------------------------------------------------
# `keeper_reason` (задача 12): the UI must show not just the choice but why.
# --------------------------------------------------------------------------

def test_keeper_reason_names_the_folder_for_a_named_win():
    dump = rec(r"Pictures\20180926_120940.jpg")
    named = rec(r"Краснодар\20180926_120940.jpg")
    text, kind = keeper_reason([dump, named])
    assert kind == "named"
    assert "Краснодар" in text


def test_keeper_reason_names_the_folder_for_a_dated_win():
    dump = rec(r"Pictures\IMG_1.jpg")
    dated = rec(r"2010 -2020\2020\IMG_1.jpg")
    text, kind = keeper_reason([dump, dated])
    assert kind == "dated"
    assert "2020" in text


def test_keeper_reason_falls_back_to_generic_count_when_specificity_ties():
    """Both copies sit under an equally-named ancestor (a real path always
    has one — the scan root itself), so the tiebreak that actually fires
    is 'fewer junk folders above it', and the reason must say that, not
    fabricate a folder-name explanation that doesn't apply."""
    shallow = rec(r"Pamir 2016\IMG_2.jpg")
    buried = rec(r"Pamir 2016\Pictures\IMG_2.jpg")
    text, kind = keeper_reason([buried, shallow])
    assert kind == "named"
    assert "папок-свалок" in text


def test_keeper_reason_for_a_single_copy_has_nothing_to_compare_to():
    only = rec(r"Краснодар\solo.jpg")
    text, kind = keeper_reason([only])
    assert "сравнивать не с чем" in text
    assert kind == "named"


def test_keeper_reason_for_a_plain_file_beating_an_archive_member():
    inside = member(r"D:\Takeouts\takeout.tgz", "Takeout/Краснодар/IMG_7.jpg")
    dump = rec(r"Pictures\IMG_7.jpg")
    text, kind = keeper_reason([inside, dump])
    assert "архив" in text
    assert kind == "generic"


def test_keeper_reason_kind_matches_keeper_key_specificity_ordering():
    """The kind label is read off the *keeper's own* segments, independent
    of which tiebreak level actually decided the group -- so it must never
    disagree with the plain rule (named > dated > generic)."""
    dump = rec(r"Pictures\IMG_9.jpg")
    dated = rec(r"2010 -2020\2016\IMG_9.jpg")
    named = rec(r"Pictures\Pamir 2016\IMG_9.jpg")
    _, dated_alone_kind = keeper_reason([dump, dated])
    assert dated_alone_kind == "dated"
    _, named_wins_kind = keeper_reason([dated, named])
    assert named_wins_kind == "named"


def test_keeper_reason_is_exposed_through_the_serialized_group():
    """Задача 12's whole point: the reason travels with the group, exactly
    like `keeper_display_path` already does (see the round-trip test
    above for that one)."""
    dump = rec(r"Pictures\IMG_10.jpg")
    named = rec(r"Краснодар\IMG_10.jpg")
    report = ScanReport(
        scanned_roots=[ROOT],
        total_files_seen=2,
        groups=[DuplicateGroup(content_hash="h10", records=[dump, named])],
    )
    payload = report.to_dict()
    group_payload = payload["groups"][0]
    assert "Краснодар" in group_payload["keeper_reason"]
    assert group_payload["keeper_reason_kind"] == "named"
    # And a round trip through from_dict must not lose it either.
    restored = ScanReport.from_dict(payload).groups[0]
    assert restored.keeper_reason == group_payload["keeper_reason"]
    assert restored.keeper_reason_kind == "named"
