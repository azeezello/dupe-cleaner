"""Tests for the task-9 quality metrics.

The bar these have to clear is set by what the metrics are *for*: Р2 says
they may only ever rank copies of the same photograph against each other
(task 17), never condemn a file on their own. So the assertions below are
almost all about **ordering between two versions of one image**, not about
absolute values — an absolute Laplacian variance means nothing, while "the
blurred copy scores lower than the sharp one" means exactly what the
ranking will need.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter

from dupecleaner import quality, thumbnails
from dupecleaner.storage import ScanIndex


def _detailed_photo(size=(1600, 1200)) -> Image.Image:
    """An image with real high-frequency content in it.

    A flat colour would be pointless here: it has no detail to lose, so
    blurring it changes nothing and every sharpness assertion below would
    pass for the wrong reason.
    """
    width, height = size
    img = Image.new("RGB", size, (30, 30, 40))
    draw = ImageDraw.Draw(img)
    for x in range(0, width, 13):
        draw.line([(x, 0), (x + 60, height)], fill=(230, 200 - x % 200, x % 256), width=2)
    for y in range(0, height, 19):
        draw.line([(0, y), (width, y + 40)], fill=(40, 220, (y * 3) % 256), width=1)
    return img


def _preview_sized(img: Image.Image) -> Image.Image:
    """The image as `thumbnails.generate` would hand it to `quality.measure`."""
    copy = img.copy()
    copy.thumbnail((thumbnails.THUMBNAIL_MAX_DIMENSION, thumbnails.THUMBNAIL_MAX_DIMENSION))
    return copy


def _save_jpeg(path: Path, img: Image.Image, jpeg_quality: int) -> None:
    img.save(path, format="JPEG", quality=jpeg_quality)


# --- resolution --------------------------------------------------------


def test_resolution_is_the_source_photo_not_the_thumbnail(tmp_path: Path):
    """The distinction task 8's schema comment got wrong. `width`/`height`
    in `content_previews` are the preview's; the metric has to be the
    photo's, or every 12 MP original in the index reads as 240 px wide."""
    src = tmp_path / "big.jpg"
    _save_jpeg(src, _detailed_photo((1600, 1200)), 92)

    preview = thumbnails.generate(src)

    assert preview.width <= thumbnails.THUMBNAIL_MAX_DIMENSION
    assert preview.metrics is not None
    assert (preview.metrics.source_width, preview.metrics.source_height) == (1600, 1200)
    assert preview.metrics.megapixels == pytest.approx(1.92, abs=0.01)


# --- sharpness ---------------------------------------------------------


def test_blurring_the_same_photo_lowers_its_sharpness(tmp_path: Path):
    """The one thing the metric must get right, since it is the whole
    reason task 17 will look at it."""
    original = _detailed_photo()
    blurred = original.filter(ImageFilter.GaussianBlur(4))

    sharp_score = quality.measure_sharpness(_preview_sized(original))
    blurred_score = quality.measure_sharpness(_preview_sized(blurred))

    assert blurred_score < sharp_score / 2


def test_sharpness_does_not_simply_track_resolution(tmp_path: Path):
    """Without the normalisation, a 1600 px file would beat its own 500 px
    downscale for having more pixels to disagree about — and then sharpness
    would be resolution wearing a different name, which resolution already
    covers. Normalised, two downscales of one sharp photo land close
    together, and far above a blurred version of either.
    """
    original = _detailed_photo((1600, 1200))
    smaller = original.resize((500, 375), Image.LANCZOS)

    big_score = quality.measure_sharpness(_preview_sized(original))
    small_score = quality.measure_sharpness(_preview_sized(smaller))
    blurred_score = quality.measure_sharpness(
        _preview_sized(original.filter(ImageFilter.GaussianBlur(4)))
    )

    assert small_score == pytest.approx(big_score, rel=0.35)
    assert blurred_score < min(big_score, small_score) / 2


def test_sharpness_ignores_the_unfiltered_border(tmp_path: Path):
    """Pillow copies the 1 px border from the source instead of filtering
    it, so those pixels carry raw brightness, not edge response. Left in,
    a bright flat image would score as though it were full of detail.
    """
    flat_dark = Image.new("RGB", (240, 180), (10, 10, 10))
    flat_bright = Image.new("RGB", (240, 180), (250, 250, 250))

    # Both are featureless; neither may score as detailed, and the bright
    # one must not outscore the dark one just for being bright.
    assert quality.measure_sharpness(flat_dark) < 1.0
    assert quality.measure_sharpness(flat_bright) < 1.0


# --- recompression -----------------------------------------------------


@pytest.mark.parametrize("jpeg_quality", [95, 90, 85, 75, 60, 50])
def test_jpeg_quality_is_recovered_from_the_files_own_tables(tmp_path: Path, jpeg_quality: int):
    """Not inferred from the pixels — read back out of the quantization
    tables the encoder wrote into the file."""
    src = tmp_path / f"q{jpeg_quality}.jpg"
    _save_jpeg(src, _detailed_photo((800, 600)), jpeg_quality)

    preview = thumbnails.generate(src)
    assert preview.metrics is not None
    assert preview.metrics.recompression_basis == quality.BASIS_JPEG_QUANT
    assert preview.metrics.jpeg_quality == pytest.approx(jpeg_quality, abs=3)


def test_recompression_orders_a_squeezed_copy_above_a_clean_one(tmp_path: Path):
    original = _detailed_photo((1200, 900))
    clean = tmp_path / "clean.jpg"
    squeezed = tmp_path / "squeezed.jpg"
    _save_jpeg(clean, original, 95)
    _save_jpeg(squeezed, original, 45)

    clean_metrics = thumbnails.generate(clean).metrics
    squeezed_metrics = thumbnails.generate(squeezed).metrics

    assert clean_metrics is not None and squeezed_metrics is not None
    assert clean_metrics.recompression == 0.0
    assert squeezed_metrics.recompression == 1.0
    assert squeezed_metrics.recompression > clean_metrics.recompression


def test_a_lossless_file_says_lossless_rather_than_scoring_zero_silently(tmp_path: Path):
    """The basis is the point. A PNG scores 0.0, but "0.0 because nothing
    lossy happened in this file" and "0.0 because it was saved at quality
    97" are different claims, and task 17 must not average them together.
    """
    src = tmp_path / "shot.png"
    _detailed_photo((600, 400)).save(src, format="PNG")

    metrics = thumbnails.generate(src).metrics
    assert metrics is not None
    assert metrics.recompression_basis == quality.BASIS_LOSSLESS
    assert metrics.recompression == 0.0
    assert metrics.jpeg_quality is None


def test_bits_per_pixel_is_the_fallback_when_there_are_no_tables():
    """The HEIC path, exercised directly so it is covered even where
    libheif cannot encode a test file."""
    thin, basis, jpeg_quality = quality.measure_recompression(
        image_format="HEIF", quantization=None, pixels=1_000_000, file_bytes=20_000
    )
    fat, _, _ = quality.measure_recompression(
        image_format="HEIF", quantization=None, pixels=1_000_000, file_bytes=500_000
    )
    assert basis == quality.BASIS_BITS_PER_PIXEL
    assert jpeg_quality is None
    assert thin == 1.0          # 0.16 bpp — squeezed hard
    assert fat < thin           # 4 bpp — nothing to complain about
    assert fat == 0.0


def test_estimate_jpeg_quality_returns_none_without_a_luminance_table():
    assert quality.estimate_jpeg_quality({}) is None
    assert quality.estimate_jpeg_quality({1: [16] * 64}) is None
    assert quality.estimate_jpeg_quality({0: [0] * 64}) is None


# --- storage round-trip ------------------------------------------------


def test_metrics_reach_the_index_and_come_back_batched(tmp_path: Path):
    db_path = tmp_path / "index.db"
    with ScanIndex(db_path) as index:
        one = tmp_path / "one.jpg"
        two = tmp_path / "two.jpg"
        _save_jpeg(one, _detailed_photo((1600, 1200)), 95)
        _save_jpeg(two, _detailed_photo((640, 480)), 45)

        thumbnails.maybe_generate(index, "hash-one", one)
        thumbnails.maybe_generate(index, "hash-two", two)

        found = index.quality_for_hashes(["hash-one", "hash-two", "hash-never-seen"])

        assert set(found) == {"hash-one", "hash-two"}
        assert found["hash-one"]["source_width"] == 1600
        assert found["hash-one"]["megapixels"] == pytest.approx(1.92, abs=0.01)
        assert found["hash-one"]["recompression"] < found["hash-two"]["recompression"]
        # The preview's own dimensions stay available and stay separate.
        assert found["hash-one"]["thumbnail_width"] <= thumbnails.THUMBNAIL_MAX_DIMENSION
        assert index.count_quality_metrics() == 2


def test_a_photo_that_will_not_decode_gets_no_metrics_rather_than_zeroes(tmp_path: Path):
    """UX-BRIEF's "честность в цифрах": an unmeasurable photo is absent
    from the result, not present with a sharpness of 0."""
    db_path = tmp_path / "index.db"
    with ScanIndex(db_path) as index:
        broken = tmp_path / "broken.jpg"
        broken.write_bytes(b"not an image at all")
        thumbnails.maybe_generate(index, "hash-broken", broken)

        assert index.quality_for_hashes(["hash-broken"]) == {}
        assert index.count_quality_metrics() == 0


def test_a_known_file_size_is_used_instead_of_stating_the_file(tmp_path: Path):
    """The scan already knows every file's size from the walk that found
    it. Statting again is not free — through the session's mount to
    D:\\Photos it measured 2.3 ms, against 0.4 ms for all three metrics
    together — so the caller passes it in and `generate` must actually use
    what it is given.
    """
    src = tmp_path / "shot.heic.png"  # lossless: bpp is not consulted
    _detailed_photo((400, 300)).save(src, format="PNG")

    # A deliberately wrong size, on a format whose score *does* read it.
    lying, basis, _ = quality.measure_recompression(
        image_format="HEIF", quantization=None, pixels=400 * 300, file_bytes=1_000_000
    )
    honest, _, _ = quality.measure_recompression(
        image_format="HEIF", quantization=None, pixels=400 * 300, file_bytes=5_000
    )
    assert basis == quality.BASIS_BITS_PER_PIXEL
    assert lying != honest  # the parameter is load-bearing, not decoration

    # And the real path: the file is not stat'ed at all when its size was
    # supplied. Patching pathlib.Path.stat rather than os.stat on purpose —
    # Path.stat resolves os.stat at import time, so patching the latter
    # catches nothing and the test would pass against the very code it is
    # supposed to forbid.
    seen: list[str] = []
    real_stat = Path.stat

    def counting_stat(self, *args, **kwargs):
        seen.append(str(self))
        return real_stat(self, *args, **kwargs)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(Path, "stat", counting_stat)
        thumbnails.generate(src, file_bytes=12345)
        assert str(src) not in seen

        seen.clear()
        thumbnails.generate(src)  # ...and it still stats when nobody said
        assert str(src) in seen
    finally:
        monkey.undo()
