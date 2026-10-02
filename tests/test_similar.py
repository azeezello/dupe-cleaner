r"""Perceptual hashing and near-duplicate grouping (task 13).

On what these are measured against
-----------------------------------
The numbers that chose the default threshold were measured on Aziz's own
`D:\Photos` — 29 984 images, and 550 recompressed/resized copies built
from 50 of his photographs — and they are written up in
`claude/task-13-nearduplicate-report.md`. None of that can live in a test:
the photographs are his and the repository is not the place for them.

So this file does two things instead.

Most tests build their source image *procedurally but not randomly* —
a low-resolution colour field upscaled bicubically, which is what a
photograph is at the scale a 32x32 perceptual hash looks at — and then put
it through **real** JPEG recompression and real resampling. The copies
under test are genuinely recompressed copies; only the subject is drawn
rather than photographed. A hash tested against added noise would be
testing nothing, since noise has no low-frequency structure to preserve.

`test_real_photographs_survive_recompression` is the other half: point
`DUPECLEANER_PHOTO_DIR` at a folder of real photographs and it builds the
same set of copies from the first few it finds and asserts each one lands
within the default threshold of its original. Skipped when the variable is
unset, in the same spirit as the face tests that need model files. It was
run against `D:\Photos` while task 13 was written.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image, ImageEnhance, ImageOps

from dupecleaner import similar
from dupecleaner.storage import ScanIndex


# --- helpers ---------------------------------------------------------------


def photo_like(seed: int, size=(1200, 900)) -> Image.Image:
    """An image with photographic *structure*: smooth low-frequency
    variation, which is precisely the band a DCT perceptual hash reads.
    Deterministic, so a failure is reproducible."""
    small = Image.new("RGB", (12, 9))
    small.putdata(
        [
            (
                (seed * 37 + i * 29) % 256,
                (seed * 61 + i * 17) % 256,
                (seed * 13 + i * 53) % 256,
            )
            for i in range(12 * 9)
        ]
    )
    return small.resize(size, Image.Resampling.BICUBIC)


def hash_of(img: Image.Image) -> similar.PerceptualHash:
    """Through exactly the reduction the scan uses (thumbnails.generate)."""
    work = img.copy()
    work.thumbnail((240, 240))
    return similar.phash_image(work)


def hash_file(path: Path) -> similar.PerceptualHash:
    with Image.open(path) as img:
        img.draft("RGB", (240, 240))
        img.load()
        img.thumbnail((240, 240))
        return similar.phash_image(img)


def variants(img: Image.Image, directory: Path) -> dict[str, Path]:
    """The copies a photograph actually acquires in life: re-saved at a
    lower quality, resized by a messenger, exported as PNG, brightened."""
    directory.mkdir(parents=True, exist_ok=True)
    rgb = img.convert("RGB")
    out: dict[str, Path] = {}

    def save(name: str, image: Image.Image, fmt="JPEG", **kw) -> None:
        path = directory / name
        image.convert("RGB").save(path, fmt, **kw)
        out[name] = path

    def resized(long_side: int) -> Image.Image:
        w, h = rgb.size
        if max(w, h) <= long_side:
            return rgb.copy()
        scale = long_side / max(w, h)
        return rgb.resize(
            (max(1, round(w * scale)), max(1, round(h * scale))),
            Image.Resampling.LANCZOS,
        )

    save("q40.jpg", rgb, quality=40)
    save("w1024_q70.jpg", resized(1024), quality=70)
    save("w320_q50.jpg", resized(320), quality=50)
    save("half.png", resized(max(rgb.size) // 2 or 1), fmt="PNG")
    save("bright110.jpg", ImageEnhance.Brightness(rgb).enhance(1.10), quality=85)
    return out


# --- the hash itself -------------------------------------------------------


def test_hash_has_exactly_31_bits_set_and_is_63_bits_wide():
    """The median is a real element of the 63 coefficients, so `> median`
    is true for exactly 31 of them. Two consequences worth pinning: the
    top bit is never used, and every Hamming distance between two hashes
    is even — which is why `--max-distance 7` is the same threshold as 6.
    """
    hashes = [hash_of(photo_like(seed)).bits for seed in range(1, 12)]
    for bits in hashes:
        assert bits is not None
        assert bits.bit_count() == 31
        assert bits < (1 << similar.PHASH_BITS)
    for a in hashes:
        for b in hashes:
            assert similar.distance(a, b) % 2 == 0


def test_flat_image_gets_no_hash_but_is_still_an_answer():
    """A blank frame has no low-frequency structure, so a fingerprint of
    it would be a fingerprint of rounding noise — and those cluster with
    each other. The hash is refused; the measurement is still returned, so
    a caller can record "looked at, nothing to hash"."""
    result = similar.phash_image(Image.new("RGB", (400, 300), (128, 128, 128)))
    assert result.bits is None
    assert result.hex is None
    assert result.structure < similar.FLAT_MIN_STRUCTURE

    real = similar.phash_image(photo_like(3))
    assert real.bits is not None
    assert real.structure > similar.FLAT_MIN_STRUCTURE


def test_exif_orientation_is_applied_before_hashing():
    """More than half of `D:\\Photos` carries a rotation flag, and anything
    that re-encodes a photo bakes the rotation into the pixels and drops
    the flag. Without this the original and its copy would be ninety
    degrees apart — measured at a median distance of 32 of 63, i.e. every
    such pair missed. See `similar.phash_image`.
    """
    upright = photo_like(5, size=(600, 400))
    # Tag 6 means "rotate 90 CW to display", so the stored pixels are the
    # upright picture turned 90 CCW. Turning it the other way and then
    # tagging it 6 describes a 180 rotation, which is a different test and
    # one the hash is entitled to fail.
    sideways = upright.rotate(90, expand=True)  # what the sensor wrote
    exif = sideways.getexif()
    exif[274] = 6  # "rotate 90° CW to display"
    tagged = Image.open(_saved(sideways, exif))

    assert similar.distance(hash_of(upright).bits, hash_of(tagged).bits) <= 2
    # ...and without the flag the same pixels are a different picture.
    assert similar.distance(hash_of(upright).bits, hash_of(sideways).bits) > 16


_TMP: list[Path] = []


def _saved(img: Image.Image, exif) -> Path:
    import tempfile

    path = Path(tempfile.mkdtemp()) / "tagged.jpg"
    img.convert("RGB").save(path, "JPEG", quality=92, exif=exif)
    _TMP.append(path)
    return path


def test_recompressed_and_resized_copies_stay_within_the_default_threshold(
    tmp_path: Path,
):
    """The task's acceptance criterion, first half: a recompressed and
    shrunk copy lands in the same group as its original."""
    original = photo_like(7)
    original_hash = hash_of(original)
    assert original_hash.bits is not None

    for name, path in variants(original, tmp_path / "copies").items():
        copy_hash = hash_file(path)
        assert copy_hash.bits is not None, name
        gap = similar.distance(original_hash.bits, copy_hash.bits)
        assert gap <= similar.DEFAULT_THRESHOLDS.max_distance, (name, gap)


def test_two_different_pictures_are_not_similar():
    """The acceptance criterion's second half, and the one that matters:
    the answer "everything is similar" would satisfy the first half
    perfectly."""
    hashes = [hash_of(photo_like(seed)) for seed in (11, 47, 91, 143, 205)]
    for i, a in enumerate(hashes):
        for b in hashes[i + 1 :]:
            assert similar.distance(a.bits, b.bits) > 16


def test_rotation_by_ninety_degrees_is_not_detected_and_says_so():
    """A named limit rather than a surprise: a perceptual hash of a
    rotated picture is a different hash, and this one does not try to
    pretend otherwise (see the report)."""
    original = photo_like(9)
    rotated = original.rotate(90, expand=True)
    assert similar.distance(hash_of(original).bits, hash_of(rotated).bits) > 16


# --- grouping --------------------------------------------------------------


def _entry(name: str, bits: int, aspect: float = 1.5, paths=None) -> similar.PhashEntry:
    return similar.PhashEntry(
        content_hash=name, bits=bits, aspect=aspect, paths=tuple(paths or (name,))
    )


def test_grouping_finds_every_pair_the_threshold_allows():
    """The block index is a pigeonhole filter, not a heuristic: it must
    never lose a pair. Checked against brute force over every pair."""
    import random

    rng = random.Random(1313)
    entries = []
    for i in range(300):
        bits = rng.getrandbits(similar.PHASH_BITS)
        entries.append(_entry(f"h{i:03d}", bits))
        if i % 3 == 0:  # a near copy, a few bits away
            flipped = bits
            for shift in rng.sample(range(similar.PHASH_BITS), 4):
                flipped ^= 1 << shift
            entries.append(_entry(f"h{i:03d}n", flipped))

    for max_distance in (2, 4, 6):
        thresholds = similar.SimilarThresholds(
            max_distance=max_distance, max_aspect_log_ratio=None
        )
        clustering = similar.find_similar_groups(entries, thresholds)
        grouped = {
            frozenset(m.content_hash for m in g.members) for g in clustering.groups
        }

        brute: dict[str, set[str]] = {e.content_hash: {e.content_hash} for e in entries}
        for i, a in enumerate(entries):
            for b in entries[i + 1 :]:
                if similar.distance(a.bits, b.bits) <= max_distance:
                    merged = brute[a.content_hash] | brute[b.content_hash]
                    for name in merged:
                        brute[name] = merged
        expected = {frozenset(v) for v in brute.values() if len(v) > 1}
        assert grouped == expected, max_distance


def test_spread_exposes_chaining():
    """Single linkage builds chains: A joins B, B joins C, and A and C are
    twice the threshold apart. The group reports how far it is stretched
    rather than leaving that to be discovered by eye."""
    a = 0
    b = 0b111
    c = 0b111111
    clustering = similar.find_similar_groups(
        [_entry("a", a), _entry("b", b), _entry("c", c)],
        similar.SimilarThresholds(max_distance=3, max_aspect_log_ratio=None),
    )
    assert len(clustering.groups) == 1
    group = clustering.groups[0]
    assert group.size == 3
    assert group.spread == 6  # twice the threshold: a chain, and visible as one


def test_shape_guard_refuses_a_portrait_paired_with_a_landscape():
    bits = 0b101010
    clustering = similar.find_similar_groups(
        [_entry("landscape", bits, aspect=4 / 3), _entry("portrait", bits, aspect=3 / 4)],
        similar.SimilarThresholds(max_distance=4),
    )
    assert clustering.groups == []
    assert clustering.pairs_rejected_by_aspect == 1

    # ...while a 16:9 crop of a 4:3 original still passes (0.29 < 0.35).
    clustering = similar.find_similar_groups(
        [_entry("wide", bits, aspect=16 / 9), _entry("classic", bits, aspect=4 / 3)],
        similar.SimilarThresholds(max_distance=4),
    )
    assert len(clustering.groups) == 1


def test_one_group_per_content_not_per_file():
    """Four filed copies of one photograph are one fingerprint and one
    decode. A group counts contents and files separately, because the
    reviewer needs both numbers."""
    bits = 0b1100110011
    clustering = similar.find_similar_groups(
        [
            _entry("x", bits, paths=("a.jpg", "b.jpg", "c.jpg")),
            _entry("y", bits ^ 0b11, paths=("d.jpg",)),
        ],
        similar.SimilarThresholds(max_distance=4, max_aspect_log_ratio=None),
    )
    group = clustering.groups[0]
    assert group.size == 2
    assert group.file_count == 4
    assert sorted(group.display_paths) == ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]


def test_oversized_bucket_is_reported_not_swallowed():
    """Skipping work quietly is pilot finding A1. If the candidate index
    gives up on a bucket, the clustering says so."""
    entries = [_entry(f"h{i}", 0b101) for i in range(50)]
    clustering = similar.find_similar_groups(
        entries, similar.SimilarThresholds(max_distance=2, max_bucket=10)
    )
    assert clustering.warnings
    assert "корзин" in clustering.warnings[0]


def test_sensitivity_table_is_monotone():
    """Raising the threshold can only add pairs, so groups can only merge
    — the number of grouped contents never falls."""
    import random

    rng = random.Random(7)
    entries = [_entry(f"h{i}", rng.getrandbits(similar.PHASH_BITS)) for i in range(400)]
    rows = similar.distance_sensitivity(
        entries,
        (0, 2, 4, 6, 8),
        base=similar.SimilarThresholds(max_aspect_log_ratio=None),
    )
    grouped = [r["contents_in_groups"] for r in rows]
    assert grouped == sorted(grouped)


def test_entries_from_rows_skips_content_with_no_hash():
    rows = [
        ("a", "00ff00ff00ff00ff", 1.5, 100, ["x.jpg", "y.jpg"]),
        ("b", None, 1.5, 100, ["z.jpg"]),
    ]
    entries = similar.entries_from_rows(rows)
    assert [e.content_hash for e in entries] == ["a"]
    assert entries[0].paths == ("x.jpg", "y.jpg")


# --- the index -------------------------------------------------------------


def test_index_records_a_missing_hash_as_an_answer(tmp_path: Path):
    """A row with a NULL hash means "decoded, nothing to fingerprint".
    Without it every rescan would decode the same blank frames again to
    rediscover the same nothing — `content_face_scans`' lesson, one
    feature over."""
    with ScanIndex(tmp_path / "index.db") as index:
        assert not index.has_phash("flat")
        index.set_phash("flat", None, 0.01, 1.33, similar.PHASH_ALGO)
        index.commit()
        assert index.has_phash("flat")
        assert index.has_phash("flat", similar.PHASH_ALGO)
        assert not index.has_phash("flat", "some-other-algo")


def test_index_rows_carry_every_path_for_one_content(tmp_path: Path):
    from dupecleaner.models import FileRecord, MediaKind

    with ScanIndex(tmp_path / "index.db") as index:
        records = [
            FileRecord(
                display_path=f"/photos/{name}",
                real_path=f"/photos/{name}",
                size=100,
                mtime=1.0,
                media_kind=MediaKind.PHOTO,
            )
            for name in ("a.jpg", "b.jpg", "other.jpg")
        ]
        index.upsert_files(records, "scan1")
        index.set_full_hash("/photos/a.jpg", "same")
        index.set_full_hash("/photos/b.jpg", "same")
        index.set_full_hash("/photos/other.jpg", "different")
        index.set_phash("same", "0f0f0f0f0f0f0f0f", 8.0, 1.5, similar.PHASH_ALGO)
        index.set_phash("different", None, 0.0, 1.5, similar.PHASH_ALGO)
        index.commit()

        rows = index.phash_rows("scan1", similar.PHASH_ALGO)
        assert len(rows) == 1  # the NULL-hash content is not groupable
        assert rows[0][0] == "same"
        assert sorted(rows[0][4]) == ["/photos/a.jpg", "/photos/b.jpg"]

        stats = index.phash_stats("scan1", similar.PHASH_ALGO)
        assert stats == {
            "photos": 3,
            "with_phash": 2,      # both copies of `same`
            "without_phash": 1,   # `other.jpg`: looked at, nothing to hash
            "not_looked": 0,
        }


def test_needs_phash_covers_the_library_not_only_duplicate_groups(tmp_path: Path):
    """The point of the phase. Two photographs that merely look alike are
    not byte-identical, so they are not in a duplicate group — a pass
    restricted to duplicate groups would search the one place its answers
    cannot be."""
    from dupecleaner.models import FileRecord, MediaKind

    with ScanIndex(tmp_path / "index.db") as index:
        index.upsert_files(
            [
                FileRecord(
                    display_path="/photos/lonely.jpg",
                    real_path="/photos/lonely.jpg",
                    size=999,  # a size nothing else shares: never enters the funnel
                    mtime=1.0,
                    media_kind=MediaKind.PHOTO,
                ),
                FileRecord(
                    display_path="/photos/inside.zip::x.jpg",
                    real_path="/photos/inside.zip",
                    size=999,
                    mtime=1.0,
                    media_kind=MediaKind.PHOTO,
                    is_archive_member=True,
                    archive_path="/photos/inside.zip",
                    member_name="x.jpg",
                ),
            ],
            "scan1",
        )
        index.commit()
        wanted = [r.display_path for r in index.needs_phash("scan1", similar.PHASH_ALGO)]

    # the unhashed loose photo is wanted; the archive member never is
    # (decoding pixels out of an archive is finding A2 again, and Р1 calls
    # archive contents cold storage)
    assert wanted == ["/photos/lonely.jpg"]


def test_a_hash_from_another_algorithm_is_work_to_redo(tmp_path: Path):
    from dupecleaner.models import FileRecord, MediaKind

    with ScanIndex(tmp_path / "index.db") as index:
        index.upsert_files(
            [
                FileRecord(
                    display_path="/photos/a.jpg",
                    real_path="/photos/a.jpg",
                    size=10,
                    mtime=1.0,
                    media_kind=MediaKind.PHOTO,
                )
            ],
            "scan1",
        )
        index.set_full_hash("/photos/a.jpg", "content")
        index.set_phash("content", "0f0f0f0f0f0f0f0f", 8.0, 1.5, "phash-something-else")
        index.commit()
        assert [r.display_path for r in index.needs_phash("scan1", similar.PHASH_ALGO)] == [
            "/photos/a.jpg"
        ]
        assert index.phash_rows("scan1", similar.PHASH_ALGO) == []


# --- real photographs, when there are any ----------------------------------


@pytest.mark.skipif(
    not os.environ.get("DUPECLEANER_PHOTO_DIR"),
    reason="нужна папка с настоящими снимками: DUPECLEANER_PHOTO_DIR=...",
)
def test_real_photographs_survive_recompression(tmp_path: Path):
    r"""The acceptance criterion on real material.

    Run it against a real photo folder:

        DUPECLEANER_PHOTO_DIR='D:\Photos' python -m pytest tests/test_similar.py

    It builds the same recompressed/resized copies the synthetic test uses
    from the first few photographs it finds, and asserts each lands within
    the default threshold of its original. Read-only: originals are opened
    and nothing else, the copies go to pytest's temp directory.
    """
    root = Path(os.environ["DUPECLEANER_PHOTO_DIR"])
    sources: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() in (".jpg", ".jpeg") and path.is_file():
            sources.append(path)
        if len(sources) >= 5:
            break
    assert sources, f"в {root} не нашлось ни одного JPEG"

    checked = 0
    for i, source in enumerate(sources):
        with Image.open(source) as img:
            img.load()
            # what a person sees, which is what a copy of it will contain
            upright = ImageOps.exif_transpose(img) or img
            original_hash = hash_of(upright)
            if original_hash.bits is None:
                continue  # a blank frame has no fingerprint by design
            copies = variants(upright, tmp_path / f"src{i}")
        for name, path in copies.items():
            copy_hash = hash_file(path)
            gap = similar.distance(original_hash.bits, copy_hash.bits)
            assert gap <= similar.DEFAULT_THRESHOLDS.max_distance, (
                source.name,
                name,
                gap,
            )
            checked += 1
    assert checked >= 5
