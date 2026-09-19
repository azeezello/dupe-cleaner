"""Thumbnail cache: a small JPEG preview generated once per unique file
*content* and served straight off disk, so the duplicate-review grid never
has to decode a full-size photo on every request (see docs/UX-BRIEF.md —
"решение по группе за секунду" only holds if a preview is a file read, not
a JPEG decode).

Why the cache key is the content hash, not a path
---------------------------------------------------
A duplicate group's whole reason for existing is that its records share
the exact same bytes, so they'd render an *identical* thumbnail. Keying
the cache by `full_hash` — the same content hash `dedupe.py`/`storage.py`
already compute for grouping — instead of by path means:

- generating N copies of the same photo costs one decode, not N. On the
  pilot's `D:\\Photos` (6000 groups, 12299 files in them, 5725 groups of
  exactly 2 copies) that alone is roughly half the decodes a per-path
  cache would do, before any eviction.
- quarantining or restoring a file is a no-op for this cache: the bytes
  haven't changed, only where they currently live, so there is nothing to
  invalidate, move, or regenerate. A thumbnail made before a file was
  quarantined is still the correct thumbnail after it's restored — see
  `test_quarantine_move_does_not_invalidate_cached_thumbnail` in
  test_thumbnails.py.
- it can't collide across genuinely different photos the way a
  name+size shortcut could (pilot finding: `2021-07-10 08-06-58.JPG` vs
  `20210710_120657~2.jpg`, byte-identical despite unrelated names — the
  reverse risk, two different files sharing a weak key, is just as real).
  `quick_hash` (head+tail sample only) is deliberately NOT used here
  either, even though it's cheaper to obtain and already present earlier
  in the funnel: two different files can share a quick_hash before the
  full read rules them out — that's the entire point of the three-stage
  funnel in dedupe.py — and a wrong thumbnail is a worse failure mode than
  a slow or missing one.

Where the bytes live
---------------------
One JPEG per content hash, on disk, sharded two hex characters deep
(`<cache_dir>/<hash[:2]>/<hash>.jpg`) so no single directory ever holds
more than a few hundred entries even at tens of thousands of unique
photos. Metadata (byte size, dimensions, access time, and — from task 9 —
quality metrics) lives in the `content_previews` table of the same SQLite
index used for everything else (see storage.py), not inside the image
files, so "how big is the cache" or "what hasn't been touched in months"
never requires walking the filesystem.

Growth is bounded, not unbounded-with-hope
--------------------------------------------
Two separate caps, because they answer two different questions:

- **Per file** (THUMBNAIL_MAX_BYTES_PER_FILE): every thumbnail is capped
  at THUMBNAIL_MAX_DIMENSION on its long side and encoded at
  THUMBNAIL_JPEG_QUALITY; if that still doesn't fit under the cap
  (pathological input that resists JPEG compression) quality is stepped
  down until it does. This bounds one entry.
- **Total** (THUMBNAIL_CACHE_MAX_BYTES): bounded by LRU eviction. The
  least-recently-*served* thumbnail is removed first (`accessed_at`,
  touched on every read via `ScanIndex.touch_thumbnail`, not only on
  generation) — a photo from a six-month-old scan nobody has revisited is
  the correct thing to reclaim before one the user opened five minutes
  ago. Eviction runs right after each write, so the cache never overshoots
  the cap between scans waiting for some later cleanup pass.

What is NOT thumbnailed in this task
--------------------------------------
- **Archive members.** `dedupe.hash_archive_members` deliberately never
  calls into this module — see its docstring. The web UI has never
  requested a thumbnail for an archive member either (`recordEl` in
  app.js only does it for `!record.is_archive_member`), and Р1 treats
  archive contents as cold storage. Generating previews for them would be
  pure waste today.
- **Video.** No frame-extraction dependency (ffmpeg/opencv) is in scope
  for this task; video groups render without a preview, exactly as before.
- **RAW formats** (.cr2/.nef/.arw/.dng) still fail to decode — Pillow
  can't read them without an additional plugin. This task only fixes the
  HEIC gap (pilot finding P2.8); RAW support is a separate, unscoped
  problem with the same symptom.
"""

from __future__ import annotations

import io
import logging
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .storage import ScanIndex

logger = logging.getLogger(__name__)

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - pillow-heif is a declared dependency
    logger.warning(
        "pillow-heif is not installed — HEIC/HEIF photos will fail to "
        "thumbnail exactly as before (pilot finding P2.8 not fixed)."
    )

# Long side of the generated thumbnail, in pixels. Matches the size the
# old inline /api/thumbnail implementation used, so existing UI layout
# assumptions don't change.
THUMBNAIL_MAX_DIMENSION = 240
THUMBNAIL_JPEG_QUALITY = 80
# Defensive per-file ceiling. Ordinary photos land far under this (a
# couple dozen KB at most at 240x240/q80) — this only bites on
# pathological input that resists JPEG compression.
THUMBNAIL_MAX_BYTES_PER_FILE = 64 * 1024
# Total cache size before LRU eviction kicks in. At a realistic ~10-15 KB
# per thumbnail this comfortably holds several full scans' worth of
# duplicate groups (tens of thousands of entries) before anything is
# reclaimed.
THUMBNAIL_CACHE_MAX_BYTES = 512 * 1024 * 1024
# Eviction brings the cache down to this fraction of the cap, so the very
# next write doesn't immediately re-trigger it.
THUMBNAIL_EVICT_HEADROOM = 0.9
_EVICTION_BATCH = 200


def cache_dir_for(db_path: Path | str) -> Path:
    """Thumbnails live next to the index they're described in, not inside
    it — see the module docstring for why the bytes and the metadata are
    kept apart."""
    if str(db_path) == ":memory:":
        # Tests and one-shot in-memory scans (find_duplicate_groups) have
        # no real db file to sit next to; fall back to a predictable spot
        # under the system temp dir rather than the current directory.
        import tempfile

        return Path(tempfile.gettempdir()) / "dupecleaner-thumbnails-memory"
    return Path(db_path).parent / "thumbnails"


def _thumbnail_path(cache_dir: Path, content_hash: str) -> Path:
    return cache_dir / content_hash[:2] / f"{content_hash}.jpg"


def _encode_bounded(img: Image.Image) -> bytes:
    rgb = img.convert("RGB")
    quality = THUMBNAIL_JPEG_QUALITY
    while True:
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= THUMBNAIL_MAX_BYTES_PER_FILE or quality <= 20:
            return data
        quality -= 15


def generate(source_path: Path) -> tuple[bytes, int, int]:
    """Decode `source_path` and produce a bounded JPEG thumbnail.

    Returns `(jpeg_bytes, width, height)` — width/height are the
    *thumbnail's* dimensions (post-resize), stored for the UI's benefit and
    as the "resolution" half of task 9's quality metrics is expected to
    want the source's, but that's a task-9 concern; this task only needs
    something to show.

    Raises `OSError`/`UnidentifiedImageError`/`ValueError` on anything
    Pillow can't open — callers treat that exactly like any other
    unreadable file (log and move on), never let it fail a scan.
    """
    with Image.open(source_path) as img:
        # For JPEG (the vast majority of real photos — ~96% in sampled
        # passes over D:\Photos, see decisions.md Р9 for the measurement),
        # this asks libjpeg to decode directly at a reduced resolution from
        # the DCT coefficients instead of a full-resolution IDCT followed by
        # a software downscale. Measured on the same photos: average
        # decode+encode time roughly halved (down to 54-90 ms depending on
        # the run, median 30-44 ms, from ~191 ms without it), because a
        # 12+ MP photo no longer has to be fully decoded to produce a 240px
        # preview. A no-op for formats that don't support it (PNG, HEIF via
        # pillow-heif) — Pillow silently ignores draft() when the plugin
        # doesn't implement it; HEIC stays ~520-540 ms regardless.
        img.draft("RGB", (THUMBNAIL_MAX_DIMENSION, THUMBNAIL_MAX_DIMENSION))
        img.load()
        img.thumbnail((THUMBNAIL_MAX_DIMENSION, THUMBNAIL_MAX_DIMENSION))
        data = _encode_bounded(img)
        return data, img.width, img.height


def store(index: ScanIndex, content_hash: str, data: bytes, width: int | None, height: int | None) -> None:
    """Write already-encoded thumbnail bytes into the cache and record
    their metadata. Shared by `maybe_generate` (the scan-time path) and the
    web layer's on-the-fly fallback, so both go through the same eviction
    bookkeeping."""
    cache_dir = cache_dir_for(index.db_path)
    dest = _thumbnail_path(cache_dir, content_hash)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{content_hash}.{uuid.uuid4().hex}.tmp"
    tmp.write_bytes(data)
    tmp.replace(dest)  # atomic rename on both POSIX and Windows/NTFS

    index.upsert_thumbnail(content_hash, len(data), width, height)
    _evict_if_needed(index, cache_dir)


def maybe_generate(index: ScanIndex, content_hash: str, source_path: Path) -> None:
    """Generate and cache a thumbnail for `content_hash` if one isn't
    already cached. This is the scan-time entry point: `dedupe.run_full_stage`
    calls it right after computing a plain photo's full hash, so by the
    time a scan finishes, every unique piece of duplicated photo content
    already has a preview waiting — the review grid never decodes on
    request.

    Best-effort: a broken/unreadable image never raises out of here — one
    bad photo must not stop the scan, matching how jobs.py already treats
    a hashing failure (turn it into a warning, keep going).
    """
    if index.get_thumbnail_meta(content_hash) is not None:
        return  # an earlier copy in this group (or an earlier scan) already cached it

    try:
        data, width, height = generate(source_path)
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        logger.debug("Не удалось построить миниатюру для %s: %s", source_path, exc)
        return

    store(index, content_hash, data, width, height)


def get_cached_path(index: ScanIndex, content_hash: str) -> Path | None:
    """Return the on-disk thumbnail path for `content_hash`, touching its
    access time (LRU bookkeeping) on the way. `None` on a cache miss —
    never generated yet, or evicted since."""
    meta = index.get_thumbnail_meta(content_hash)
    if meta is None:
        return None
    path = _thumbnail_path(cache_dir_for(index.db_path), content_hash)
    if not path.is_file():
        # Metadata says cached but the file is gone (cache dir cleared by
        # hand, moved, etc.) — treat as a miss and drop the dangling row
        # rather than serving a 404 the caller has to special-case.
        index.delete_thumbnails([content_hash])
        return None
    index.touch_thumbnail(content_hash)
    return path


def _evict_if_needed(index: ScanIndex, cache_dir: Path) -> None:
    """Remove least-recently-accessed entries until the cache is back at
    THUMBNAIL_EVICT_HEADROOM of its cap. Stops as soon as that target is
    reached, even mid-batch — a batch is a fetch-size limit for the LRU
    query, not a fixed number of entries to delete regardless of need,
    otherwise a cache that's only slightly over the cap could evict far
    more (everything the batch happened to fetch) than necessary.
    """
    total = index.total_thumbnail_bytes()
    if total <= THUMBNAIL_CACHE_MAX_BYTES:
        return
    target = int(THUMBNAIL_CACHE_MAX_BYTES * THUMBNAIL_EVICT_HEADROOM)

    while total > target:
        batch = index.lru_thumbnail_hashes(_EVICTION_BATCH)
        if not batch:
            break  # metadata and reality disagree; nothing left to reclaim

        removed: list[str] = []
        for content_hash in batch:
            if total <= target:
                break
            path = _thumbnail_path(cache_dir, content_hash)
            freed = path.stat().st_size if path.exists() else 0
            path.unlink(missing_ok=True)
            removed.append(content_hash)
            total -= freed

        index.delete_thumbnails(removed)
        if not removed:
            break  # nothing left in this batch could be freed; avoid spinning
