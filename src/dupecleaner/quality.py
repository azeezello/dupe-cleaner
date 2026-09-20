"""Quality metrics for one unique piece of photo content: how big it is,
how much detail it actually carries, and how hard it was squeezed.

Why this exists, and what it is explicitly *not* allowed to do
---------------------------------------------------------------
Р2 is unusually blunt about quality: of the five things people mean by
"плохое качество", exactly two justify moving a file (it does not decode
at all; there is a better copy of the same shot), and low resolution,
softness and exposure never do. A photo from 2005 is irreplaceable at
1.3 MP, and a deliberately shallow-focus portrait is not a defect.

So these numbers are **evidence for a choice between copies**, not a
verdict on a file. Today nothing consumes them at all: they are computed,
stored and served, and that is the whole of task 9. The ranking that uses
them is task 17, and it only ever ranks *within* a near-duplicate group,
where the alternative is another copy of the same photograph. Nothing
here may ever reach `quarantine`, which is the Р0 invariant: axis B has
no authority to move files.

They are also the reason the review screen can be fast. UX-BRIEF asks for
a decision per group in about a second, which rules out answering "which
of these is the better copy?" by opening files from the browser. The
answer has to be in the index by the time the grid renders — same
argument, and same storage, as the thumbnails in `thumbnails.py`.

The three metrics
------------------
**Resolution** — the source image's pixel dimensions, read from the
decoder header *before* any downscaling. Free: no pixels are touched.
Note that `content_previews.width/height` are the *thumbnail's*
dimensions and always were (task 8 stored them that way); the source
resolution is a separate pair of columns rather than a re-reading of
those, because re-interpreting existing rows would silently turn every
240×180 preview already in Aziz's index into a "240×180 photo".

**Sharpness** — variance of a 3×3 Laplacian response, measured on the
image normalised to a fixed long side (`NORMALISED_LONG_SIDE`). The
normalisation is the entire point: without it a 12 MP file outscores its
own 2 MP resave simply for having more pixels to disagree about, and
resolution is already its own metric. Normalised, the comparison asks the
question that actually matters between two copies of one photograph — for
the same framing at the same display size, which one carries more real
detail? A resave that went through an extra resample and an extra JPEG
generation loses here, which is exactly what task 17 wants to see.

It is a *relative* number, comparable between files that went through
this same pipeline, and not an absolute optical measurement. Two
deliberate approximations are baked in: the measurement is taken on the
preview-sized image (so it inherits `draft()`'s DCT-domain downscale, see
thumbnails.py), and the Laplacian is evaluated in 8-bit with an offset,
so responses beyond ±256 saturate. Both are uniform across files, so
ordering survives; absolute values are meaningless on their own.

**Recompression** — how hard the file was compressed, on a 0..1 scale
where 1 means "heavily squeezed". The honest name for what is measured is
compression *strength*, not the number of JPEG generations a file has
been through: counting generations needs DCT coefficient histogram
analysis, which is a much bigger and much less reliable thing than this
task has any use for. Compression strength is the usable proxy, because
the practical case — an original next to a messenger's re-encode of it —
separates on it cleanly.

It is derived three different ways depending on what the file offers, and
the basis is stored alongside the score:

- `jpeg_quant_tables` — a JPEG carries its own quantization tables, which
  are the compressor's settings written into the file. Inverting
  libjpeg's scaling recovers the quality factor it was saved at (see
  `estimate_jpeg_quality`). This is the strongest of the three: it is
  read from the file rather than inferred from it.
- `bits_per_pixel` — for lossy formats with no readable tables (HEIC),
  file size per pixel. Weaker, and **format-dependent**: HEIC at 0.5 bpp
  is a good-looking image while JPEG at 0.5 bpp is not, because HEVC
  intra coding is roughly twice as efficient.
- `lossless` — PNG/BMP/TIFF and friends score 0.0, because no lossy
  compression happened *in this file*. That is not the same as "this is
  an original": a PNG can perfectly well be a screenshot of a badly
  compressed JPEG. The score answers what the file can prove, and the
  basis says how much that is worth.

Scores from different bases must never be compared numerically against
each other — which is why the basis is a stored column and not a comment
here. Task 17 has to read it.

No new dependencies: everything here runs on Pillow, which is already
required for the thumbnails this rides along with.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from PIL import Image, ImageFilter, ImageStat

# Long side the image is brought to before sharpness is measured. Equal to
# THUMBNAIL_MAX_DIMENSION on purpose: the preview pipeline has already
# produced an image of exactly this size by the time metrics are taken, so
# in the overwhelmingly common case (any photo larger than 240 px, i.e.
# essentially all of them) normalisation is already done and costs nothing.
NORMALISED_LONG_SIDE = 240

# 3x3 Laplacian. `scale=2` halves the response and `offset=128` recentres
# it, because Pillow evaluates kernels in the image's 8-bit domain and
# would otherwise clip every negative value to zero — which would turn a
# signed edge response into a half-wave-rectified one and flatter blurry
# images. Together they give an effective range of roughly ±256 before
# saturation; `_LAPLACIAN_VARIANCE_RESTORE` undoes the halving afterwards
# (variance scales with the square of the factor).
_LAPLACIAN = ImageFilter.Kernel(
    size=(3, 3),
    kernel=(0, -1, 0, -1, 4, -1, 0, -1, 0),
    scale=2,
    offset=128,
)
_LAPLACIAN_VARIANCE_RESTORE = 4.0

BASIS_JPEG_QUANT = "jpeg_quant_tables"
BASIS_BITS_PER_PIXEL = "bits_per_pixel"
BASIS_LOSSLESS = "lossless"

# Formats whose bytes are stored without loss. A file in one of these has
# not been squeezed *by this file*; see the module docstring for why that
# is a weaker statement than it looks.
_LOSSLESS_FORMATS = frozenset({"PNG", "BMP", "TIFF", "GIF", "PPM", "TGA", "ICO"})

# Standard JPEG luminance quantization table (ITU T.81 Annex K, table K.1)
# and its sum. Only the sum is used: quantization tables are stored in the
# file in zig-zag order, and comparing sums is order-independent, so this
# cannot be silently wrong about which coefficient is which.
_STD_LUMA_TABLE: tuple[int, ...] = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)
_STD_LUMA_SUM = sum(_STD_LUMA_TABLE)  # 3688

# Where the 0..1 recompression scale is pinned, per basis. Everything
# between the two ends is interpolated; everything outside is clamped.
_QUALITY_CLEAN = 92.0   # saved at this JPEG quality or better -> 0.0
_QUALITY_SQUEEZED = 50.0  # at or below this -> 1.0
_BPP_CLEAN = 2.0        # bits per pixel at or above this -> 0.0
_BPP_SQUEEZED = 0.25    # at or below this -> 1.0


@dataclass(frozen=True)
class QualityMetrics:
    """What one decode learned about one unique piece of photo content.

    Keyed, like the thumbnail it is produced with, by content hash — every
    copy in a duplicate group holds identical bytes and therefore identical
    metrics, so one measurement answers for all of them.
    """

    source_width: int
    source_height: int
    sharpness: float
    recompression: float
    recompression_basis: str
    jpeg_quality: int | None = None

    @property
    def pixels(self) -> int:
        return self.source_width * self.source_height

    @property
    def megapixels(self) -> float:
        return round(self.pixels / 1_000_000, 2)

    def to_dict(self) -> dict:
        return {
            "source_width": self.source_width,
            "source_height": self.source_height,
            "megapixels": self.megapixels,
            "sharpness": round(self.sharpness, 2),
            "recompression": round(self.recompression, 3),
            "recompression_basis": self.recompression_basis,
            "jpeg_quality": self.jpeg_quality,
        }


def estimate_jpeg_quality(tables: Mapping[int, Sequence[int]]) -> int | None:
    """Recover the quality factor a JPEG was saved at from its quantization
    tables, by inverting libjpeg's scaling.

    libjpeg builds a table as `q[i] = clamp(round(std[i] * scale / 100), 1,
    255)`, where `scale` comes from the quality setting: `5000 / quality`
    below 50, `200 - 2 * quality` at 50 and above. Summing over the whole
    table makes `scale` recoverable without caring about coefficient order
    (they are stored zig-zagged) and averages out the per-coefficient
    rounding.

    Returns `None` when there is no luminance table to read. The result is
    a good estimate for anything encoded by libjpeg or a library that
    copies its tables — the common case by a wide margin — and only an
    approximation for encoders that design their own tables (Photoshop's
    "Save for Web" scale, mozjpeg with trellis quantisation). It also
    saturates at the bottom: quantizer values clamp at 255, so everything
    below roughly quality 12 reads back as about the same number. None of
    that matters for the job, which is ordering two copies of one photo,
    but it does mean the number is a reading rather than a fact.
    """
    table = tables.get(0)
    if not table:
        return None
    total = sum(table)
    if total <= 0:
        return None

    scale = 100.0 * total / _STD_LUMA_SUM
    if scale <= 0:
        return None
    quality = 5000.0 / scale if scale > 100.0 else (200.0 - scale) / 2.0
    return int(round(min(100.0, max(1.0, quality))))


def _interpolate(value: float, clean: float, squeezed: float) -> float:
    """Map `value` onto 0..1, where `clean` is 0.0 and `squeezed` is 1.0.
    Works in either direction (both pin pairs here run downwards)."""
    if clean == squeezed:  # pragma: no cover - defensive
        return 0.0
    ratio = (clean - value) / (clean - squeezed)
    return min(1.0, max(0.0, ratio))


def measure_sharpness(img: Image.Image) -> float:
    """Variance of the Laplacian over `img`, normalised to a fixed long
    side first so the number does not simply track resolution."""
    gray = img.convert("L")
    width, height = gray.size
    longest = max(width, height)
    if longest != NORMALISED_LONG_SIDE and longest > 0:
        factor = NORMALISED_LONG_SIDE / longest
        target = (max(1, round(width * factor)), max(1, round(height * factor)))
        # LANCZOS going down (it is what the preview pipeline uses, so the
        # measurement matches the image the person will see); NEAREST going
        # up, because a genuinely small image should keep its per-pixel edge
        # contrast rather than be scored as blurry for being small — that is
        # what the resolution metric is for. The honest caveat on the NEAREST
        # branch: block edges it introduces can inflate the score a little.
        # It only ever runs for images under 240 px on the long side, which
        # in a photo archive means icons and thumbnails, not photographs.
        resample = Image.LANCZOS if factor < 1 else Image.NEAREST
        gray = gray.resize(target, resample)

    if gray.width < 3 or gray.height < 3:
        return 0.0

    filtered = gray.filter(_LAPLACIAN)
    # Pillow copies the 1 px border straight from the source instead of
    # filtering it, so those pixels hold raw brightness values while the
    # interior holds responses centred on 128. Left in, that border alone
    # dominates the variance of any smooth image. Crop it off.
    filtered = filtered.crop((1, 1, filtered.width - 1, filtered.height - 1))
    variance = ImageStat.Stat(filtered).var[0]
    return float(variance) * _LAPLACIAN_VARIANCE_RESTORE


def measure_recompression(
    image_format: str | None,
    quantization: Mapping[int, Sequence[int]] | None,
    pixels: int,
    file_bytes: int,
) -> tuple[float, str, int | None]:
    """Return `(score, basis, jpeg_quality_or_None)` — see the module
    docstring for what each basis is worth and why they must not be
    compared against each other."""
    if quantization:
        quality = estimate_jpeg_quality(quantization)
        if quality is not None:
            score = _interpolate(quality, _QUALITY_CLEAN, _QUALITY_SQUEEZED)
            return score, BASIS_JPEG_QUANT, quality

    if image_format and image_format.upper() in _LOSSLESS_FORMATS:
        return 0.0, BASIS_LOSSLESS, None

    if pixels > 0 and file_bytes > 0:
        bpp = file_bytes * 8.0 / pixels
        # Interpolated in log space: the distance from 2.0 to 1.0 bits per
        # pixel is the same *kind* of step as 1.0 to 0.5, which a linear
        # scale would not say.
        score = _interpolate(
            math.log2(bpp), math.log2(_BPP_CLEAN), math.log2(_BPP_SQUEEZED)
        )
        return score, BASIS_BITS_PER_PIXEL, None

    return 0.0, BASIS_BITS_PER_PIXEL, None


def measure(
    preview_img: Image.Image,
    *,
    source_size: tuple[int, int],
    image_format: str | None,
    quantization: Mapping[int, Sequence[int]] | None,
    file_bytes: int,
) -> QualityMetrics:
    """Compute all three metrics from work the preview pipeline has already
    done.

    `preview_img` is the already-downscaled image the thumbnail is encoded
    from, `source_size` its dimensions *before* that downscale, and
    `quantization`/`image_format`/`file_bytes` come from the same open
    file. Nothing here re-opens or re-reads anything: that is what keeps
    task 9's cost down to a Laplacian over a 240 px image on top of a
    decode task 8 was already paying for.
    """
    source_width, source_height = source_size
    sharpness = measure_sharpness(preview_img)
    recompression, basis, jpeg_quality = measure_recompression(
        image_format=image_format,
        quantization=quantization,
        pixels=source_width * source_height,
        file_bytes=file_bytes,
    )
    return QualityMetrics(
        source_width=source_width,
        source_height=source_height,
        sharpness=sharpness,
        recompression=recompression,
        recompression_basis=basis,
        jpeg_quality=jpeg_quality,
    )
