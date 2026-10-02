r"""The Р0/Р2 guarantee for near-duplicates, held down by tests.

Р0 gives axis A — byte equality — the sole right to move a file, and Р2
says in as many words that quality and resemblance rank copies inside a
group and never justify removing one. Near-duplicate detection is the
first feature in this project that produces *groups* without producing
proof, so this file exists to make the boundary something that fails
loudly rather than something everyone remembers.

The argument is structural, and these tests check the structure rather
than a guard clause:

1. `SimilarGroup` is not a `DuplicateGroup`, and no quarantine entry point
   accepts one — passing it in raises, it does not silently half-work.
2. A `SimilarGroup` has no keeper: there is no copy here the others are
   provably redundant against, so the field that would be believed does
   not exist.
3. Similar groups never travel on `ScanReport`, which is the only object
   the quarantine layer reads. There is no field for them to arrive in.
4. At `max_distance=PHASH_BITS` — where every photograph in a library is
   "similar" to every other — a quarantine run over the same files still
   moves exactly what byte equality alone would have moved, and not one
   file more.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from dupecleaner import similar
from dupecleaner.models import DuplicateGroup, FileRecord, MediaKind, ScanReport
from dupecleaner.quarantine import (
    quarantine_reviewed_groups,
    run_quarantine,
)


def _entry(content_hash: str, bits: int, *paths: str) -> similar.PhashEntry:
    return similar.PhashEntry(
        content_hash=content_hash, bits=bits, aspect=1.5, paths=tuple(paths), size=10
    )


def _photo(path: Path, seed: int, size=(400, 300)) -> None:
    """A small image with actual low-frequency structure, saved as a real
    JPEG. Not noise: `similar.phash_image` refuses flat images and a
    noise field has no structure a perceptual hash is supposed to see, so
    a test built on either would be testing nothing."""
    img = Image.new("RGB", (8, 6))
    img.putdata([((seed * 37 + i * 29) % 256, (seed * 11 + i * 7) % 256, (i * 53) % 256)
                 for i in range(48)])
    img.resize(size, Image.Resampling.BICUBIC).save(path, "JPEG", quality=90)


def test_similar_group_is_not_a_duplicate_group():
    group = similar.SimilarGroup(members=[_entry("a", 1, "x.jpg"), _entry("b", 3, "y.jpg")])
    assert not isinstance(group, DuplicateGroup)
    # ...and carries none of the fields that would let it be mistaken for one.
    for forbidden in ("keeper_display_path", "keeper_reason", "wasted_bytes"):
        assert not hasattr(group, forbidden), forbidden
    assert "keeper" not in group.to_dict()
    assert "wasted_bytes" not in group.to_dict()


def test_quarantine_refuses_a_similar_group_outright(tmp_path: Path):
    """Not "moves nothing" — raises. A silent no-op would mean a future
    caller could pass one of these in and read the empty result as
    "nothing needed moving"."""
    group = similar.SimilarGroup(members=[_entry("a", 1, "x.jpg"), _entry("b", 3, "y.jpg")])
    with pytest.raises(AttributeError):
        run_quarantine([group], tmp_path / "q", confirm_media=True)  # type: ignore[list-item]
    with pytest.raises(AttributeError):
        quarantine_reviewed_groups(
            [group],  # type: ignore[list-item]
            tmp_path / "q",
            decisions={"a": None},
            confirm_media=True,
        )
    assert not (tmp_path / "q").exists() or not list((tmp_path / "q").glob("**/*.jpg"))


def test_scan_report_has_nowhere_to_put_similar_groups():
    """The quarantine layer reads a `ScanReport` and nothing else. If
    similar groups could ride along on one, every argument above would
    rest on a caller's restraint instead of on the type."""
    report = ScanReport(scanned_roots=["/x"], total_files_seen=0, groups=[])
    assert not hasattr(report, "similar_groups")
    assert "similar_groups" not in report.to_dict()
    assert "similar" not in ScanReport.to_dict(report)


def test_everything_similar_to_everything_still_moves_only_byte_equal_copies(
    tmp_path: Path,
):
    """The load-bearing test.

    Four photographs: `orig.jpg` and `copy.jpg` are byte-identical, so Р0
    allows one of them to move. `resave.jpg` is the same picture saved
    again — a near-duplicate, different bytes. `other.jpg` is a different
    picture. Grouping runs at `max_distance=PHASH_BITS`, i.e. everything
    is similar to everything, and the quarantine still moves exactly the
    one redundant byte-identical copy.
    """
    root = tmp_path / "photos"
    root.mkdir()
    _photo(root / "orig.jpg", seed=1)
    (root / "copy.jpg").write_bytes((root / "orig.jpg").read_bytes())
    with Image.open(root / "orig.jpg") as img:
        img.convert("RGB").resize((200, 150)).save(root / "resave.jpg", "JPEG", quality=45)
    _photo(root / "other.jpg", seed=200)

    # Every photo is "similar" to every other at this threshold...
    entries = []
    for i, name in enumerate(("orig.jpg", "copy.jpg", "resave.jpg", "other.jpg")):
        with Image.open(root / name) as img:
            img.thumbnail((240, 240))
            ph = similar.phash_image(img)
        assert ph.bits is not None
        entries.append(_entry(f"h{i}", ph.bits, str(root / name)))
    clustering = similar.find_similar_groups(
        entries,
        similar.SimilarThresholds(
            max_distance=similar.PHASH_BITS, max_aspect_log_ratio=None
        ),
    )
    assert len(clustering.groups) == 1
    assert clustering.groups[0].size == 4  # all four, deliberately

    # ...and the quarantine, which never sees them, still moves exactly one file.
    payload = (root / "orig.jpg").read_bytes()
    byte_group = DuplicateGroup(
        content_hash="bytes",
        records=[
            FileRecord(
                display_path=str(root / name),
                real_path=str(root / name),
                size=len(payload),
                mtime=0.0,
                media_kind=MediaKind.PHOTO,
            )
            for name in ("orig.jpg", "copy.jpg")
        ],
    )
    result = run_quarantine([byte_group], tmp_path / "q", confirm_media=True)
    assert len(result.moved) == 1
    assert (root / "resave.jpg").exists()
    assert (root / "other.jpg").exists()
    moved = {Path(m["original"]).name for m in result.moved}
    assert moved <= {"orig.jpg", "copy.jpg"}


def test_no_quarantine_path_imports_the_similarity_module():
    """`quarantine.py` does not know this feature exists, and should not
    start to. A grep-level check, which is the level the guarantee lives
    at: the day someone wires the two together, this says so."""
    from dupecleaner import quarantine as quarantine_module

    source = Path(quarantine_module.__file__).read_text(encoding="utf-8")
    assert "import similar" not in source
    assert "SimilarGroup" not in source
