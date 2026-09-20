from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

from dupecleaner import thumbnails
from dupecleaner.storage import ScanIndex


def _make_photo(path: Path, size=(800, 600), color=(200, 60, 60)) -> None:
    Image.new("RGB", size, color).save(path, format="JPEG", quality=95)


def test_generate_produces_a_bounded_jpeg(tmp_path: Path):
    src = tmp_path / "photo.jpg"
    _make_photo(src)

    preview = thumbnails.generate(src)

    assert preview.width <= thumbnails.THUMBNAIL_MAX_DIMENSION
    assert preview.height <= thumbnails.THUMBNAIL_MAX_DIMENSION
    assert len(preview.data) <= thumbnails.THUMBNAIL_MAX_BYTES_PER_FILE

    with Image.open(__import__("io").BytesIO(preview.data)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (preview.width, preview.height)


def test_generate_raises_on_unreadable_input(tmp_path: Path):
    bogus = tmp_path / "not_really_a_photo.jpg"
    bogus.write_bytes(b"this is not image data")
    with pytest.raises(Exception):
        thumbnails.generate(bogus)


def test_maybe_generate_is_a_noop_for_an_already_cached_hash(tmp_path: Path):
    """The cache key is the content hash, not the path: once a hash is
    cached, a second call with a *different* (even nonexistent) source path
    must not touch disk again — this is what lets N duplicate copies share
    one decode.
    """
    db_path = tmp_path / "index.db"
    with ScanIndex(db_path) as index:
        real_photo = tmp_path / "a.jpg"
        _make_photo(real_photo)

        thumbnails.maybe_generate(index, "contenthash1", real_photo)
        meta_before = index.get_thumbnail_meta("contenthash1")
        assert meta_before is not None
        # Both halves of the decode landed, which is what makes the second
        # call below a true no-op rather than a silent re-measurement (see
        # maybe_generate: a thumbnail without metrics is unfinished work).
        assert meta_before["recompression_basis"] is not None

        # A path that would raise if actually opened — proves the second
        # call short-circuits on the cache check and never calls generate().
        thumbnails.maybe_generate(index, "contenthash1", tmp_path / "does-not-exist.jpg")

        meta_after = index.get_thumbnail_meta("contenthash1")
        assert meta_after["thumbnail_bytes"] == meta_before["thumbnail_bytes"]


def test_maybe_generate_skips_unreadable_files_without_raising(tmp_path: Path):
    db_path = tmp_path / "index.db"
    with ScanIndex(db_path) as index:
        bogus = tmp_path / "broken.jpg"
        bogus.write_bytes(b"not a photo")
        thumbnails.maybe_generate(index, "brokenhash", bogus)
        assert index.get_thumbnail_meta("brokenhash") is None
        assert thumbnails.get_cached_path(index, "brokenhash") is None


def test_get_cached_path_serves_bytes_and_touches_access_time(tmp_path: Path):
    db_path = tmp_path / "index.db"
    with ScanIndex(db_path) as index:
        src = tmp_path / "a.jpg"
        _make_photo(src)
        thumbnails.maybe_generate(index, "hash-x", src)

        before = index.get_thumbnail_meta("hash-x")["accessed_at"]
        time.sleep(0.01)
        path = thumbnails.get_cached_path(index, "hash-x")
        assert path is not None and path.is_file()
        after = index.get_thumbnail_meta("hash-x")["accessed_at"]
        assert after >= before


def test_quarantine_move_does_not_invalidate_cached_thumbnail(tmp_path: Path):
    """The design decision this encodes: the cache key is content, not
    location, so moving the source file into "quarantine" (or back) must
    not affect whether the cached thumbnail still serves.
    """
    db_path = tmp_path / "index.db"
    with ScanIndex(db_path) as index:
        original = tmp_path / "photo.jpg"
        _make_photo(original)
        thumbnails.maybe_generate(index, "stable-hash", original)
        cached_before = thumbnails.get_cached_path(index, "stable-hash")
        assert cached_before is not None
        cached_bytes = cached_before.read_bytes()

        quarantine_dir = tmp_path / "quarantine"
        quarantine_dir.mkdir()
        quarantined = quarantine_dir / "photo.jpg"
        shutil.move(str(original), str(quarantined))

        # No re-generation call happens here on purpose: the point is that
        # nothing needs to.
        cached_after = thumbnails.get_cached_path(index, "stable-hash")
        assert cached_after is not None
        assert cached_after.read_bytes() == cached_bytes

        shutil.move(str(quarantined), str(original))
        cached_restored = thumbnails.get_cached_path(index, "stable-hash")
        assert cached_restored is not None
        assert cached_restored.read_bytes() == cached_bytes


def test_eviction_removes_least_recently_accessed_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """LRU, not FIFO: an entry that was re-served recently must outlive an
    equally-old, never-revisited entry once the cache is over its size cap
    and something has to go.
    """
    import dupecleaner.storage as storage_module

    db_path = tmp_path / "index.db"
    photo = tmp_path / "a.jpg"
    _make_photo(photo, size=(200, 200))

    # Measure the real encoded size once, so the cap/target below force
    # exactly one eviction instead of guessing at byte counts.
    entry_size = len(thumbnails.generate(photo)[0])
    monkeypatch.setattr(thumbnails, "THUMBNAIL_CACHE_MAX_BYTES", int(2.5 * entry_size))
    monkeypatch.setattr(thumbnails, "THUMBNAIL_EVICT_HEADROOM", 0.9)  # target ~= 2.25 * entry_size

    # A fully controlled clock, so access-time ordering is deterministic
    # instead of depending on real wall-clock deltas between fast calls.
    fake_now = [1_000_000.0]

    def fake_time() -> float:
        fake_now[0] += 1.0
        return fake_now[0]

    monkeypatch.setattr(storage_module.time, "time", fake_time)

    with ScanIndex(db_path) as index:
        thumbnails.maybe_generate(index, "hash-untouched", photo)  # accessed_at = 2
        thumbnails.maybe_generate(index, "hash-kept", photo)       # accessed_at = 3
        thumbnails.get_cached_path(index, "hash-kept")             # re-touched -> accessed_at = 4

        # total is now 2 entries (~2x entry_size), under the ~2.5x cap.
        assert index.total_thumbnail_bytes() <= thumbnails.THUMBNAIL_CACHE_MAX_BYTES

        # A third entry pushes total to 3x, over the cap -> exactly one
        # eviction is needed to get back under the ~2.25x target, and it
        # must be the least-recently-accessed one.
        thumbnails.maybe_generate(index, "hash-filler", photo)     # accessed_at = 5

        assert index.total_thumbnail_bytes() <= thumbnails.THUMBNAIL_CACHE_MAX_BYTES
        assert index.get_thumbnail_meta("hash-untouched") is None
        assert index.get_thumbnail_meta("hash-kept") is not None
        assert index.get_thumbnail_meta("hash-filler") is not None


def test_heic_decodes_when_pillow_heif_is_available(tmp_path: Path):
    pillow_heif = pytest.importorskip("pillow_heif")
    heic_path = tmp_path / "photo.heic"
    img = Image.new("RGB", (400, 300), (10, 200, 10))
    try:
        img.save(heic_path, format="HEIF", quality=80)
    except Exception as exc:  # pragma: no cover - depends on libheif build
        pytest.skip(f"pillow-heif build here can't encode HEIF: {exc}")

    preview = thumbnails.generate(heic_path)
    assert preview.width <= thumbnails.THUMBNAIL_MAX_DIMENSION
    assert preview.height <= thumbnails.THUMBNAIL_MAX_DIMENSION
    with Image.open(__import__("io").BytesIO(preview.data)) as decoded:
        assert decoded.format == "JPEG"
    # HEIC carries no readable quantization tables, so the recompression
    # score has to fall back to bits-per-pixel rather than claim a JPEG
    # quality it cannot know (quality.py).
    assert preview.metrics is not None
    assert preview.metrics.source_width == 400
    assert preview.metrics.jpeg_quality is None


def test_metrics_are_backfilled_for_a_preview_cached_before_task_9(tmp_path: Path):
    """An index written by task 8 holds thumbnails with no metrics beside
    them. The cheap "already cached?" check would call those done forever —
    on the only index that matters, the one on Aziz's machine, that is every
    photo already scanned. So a row with a thumbnail and no metrics counts
    as work: decode once more, write the numbers, leave the JPEG alone.
    """
    db_path = tmp_path / "index.db"
    with ScanIndex(db_path) as index:
        photo = tmp_path / "a.jpg"
        _make_photo(photo, size=(900, 600))
        thumbnails.maybe_generate(index, "legacy-hash", photo)

        # Rewind this row to exactly what task 8 would have left behind.
        index._conn.execute(
            "UPDATE content_previews SET source_width = NULL, source_height = NULL, "
            "sharpness_score = NULL, recompression_score = NULL, "
            "recompression_basis = NULL, jpeg_quality = NULL "
            "WHERE content_hash = ?",
            ("legacy-hash",),
        )
        stale = index.get_thumbnail_meta("legacy-hash")
        cached_jpeg = thumbnails.get_cached_path(index, "legacy-hash")
        assert cached_jpeg is not None
        jpeg_before = cached_jpeg.read_bytes()

        thumbnails.maybe_generate(index, "legacy-hash", photo)

        filled = index.get_thumbnail_meta("legacy-hash")
        assert filled["recompression_basis"] == "jpeg_quant_tables"
        assert filled["source_width"] == 900 and filled["source_height"] == 600
        # The thumbnail itself was neither re-encoded nor re-written: same
        # bytes, same recorded size. Identical content, identical preview.
        assert cached_jpeg.read_bytes() == jpeg_before
        assert filled["thumbnail_bytes"] == stale["thumbnail_bytes"]


def test_store_refuses_a_metrics_only_preview(tmp_path: Path):
    """`generate(encode=False)` exists for the backfill path above and
    returns no JPEG. Handing that to `store` would write a zero-byte
    thumbnail over a good one, so it is refused rather than tolerated."""
    db_path = tmp_path / "index.db"
    with ScanIndex(db_path) as index:
        photo = tmp_path / "a.jpg"
        _make_photo(photo)
        preview = thumbnails.generate(photo, encode=False)
        assert preview.data == b""
        assert preview.metrics is not None
        with pytest.raises(ValueError):
            thumbnails.store(index, "hash-y", preview)
