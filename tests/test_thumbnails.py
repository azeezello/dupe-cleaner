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

    data, width, height = thumbnails.generate(src)

    assert width <= thumbnails.THUMBNAIL_MAX_DIMENSION
    assert height <= thumbnails.THUMBNAIL_MAX_DIMENSION
    assert len(data) <= thumbnails.THUMBNAIL_MAX_BYTES_PER_FILE

    with Image.open(__import__("io").BytesIO(data)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (width, height)


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

    data, width, height = thumbnails.generate(heic_path)
    assert width <= thumbnails.THUMBNAIL_MAX_DIMENSION
    assert height <= thumbnails.THUMBNAIL_MAX_DIMENSION
    with Image.open(__import__("io").BytesIO(data)) as decoded:
        assert decoded.format == "JPEG"
