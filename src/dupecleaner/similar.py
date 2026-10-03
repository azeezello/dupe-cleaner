r"""Near-duplicate photos: the same picture twice, not the same bytes twice.

What this is for, and the line it may never cross
--------------------------------------------------
Everything else in this project that groups files does it on **byte
equality**, and that is what gives Р0 its teeth: a group exists because a
hash proved it, so moving the extra copies cannot lose a photograph. This
module groups on *resemblance*, which proves nothing of the kind. A
messenger's re-encode of a wedding photo and the original are one picture
to a person and two different files to a hash; two frames of the same
burst are two different pictures that any perceptual hash will call
identical.

So by Р0 and Р2 a similar-group is **a list to look at, never a licence to
move anything**, in any mode and at any threshold. That is not enforced by
care here but by shape:

- similar groups are `SimilarGroup`, a separate type from
  `models.DuplicateGroup`, and nothing in `quarantine.py` accepts one;
- they never travel on `ScanReport`, which is the only object
  `quarantine.run_quarantine` / `quarantine_archives` /
  `quarantine_reviewed_groups` read. There is no field for them to arrive
  in, so there is no path — not a check that could be removed by accident,
  an absence;
- grouping is a *query* over stored hashes (`find_similar_groups`), run
  after a scan with a threshold in hand, rather than something the scan
  bakes into its report.

`tests/test_similar_never_quarantines.py` holds that down, including at
`max_distance=63`, where every photo in the library is "similar" to every
other one and a quarantine still moves nothing.

The threshold is deliberately not settled
------------------------------------------
`DEFAULT_THRESHOLDS` is conservative on purpose: it would rather miss a
resized copy than put two different photographs in one group, because the
two mistakes do not cost the same. A missed pair is a duplicate left on
disk — the situation before this module existed. A wrong pair is two
photographs shown side by side as if one were redundant, and the person
reviewing 8000 groups at three seconds each is exactly the person who will
believe it.

The numbers measured over `D:\Photos` at several thresholds are in
`claude/task-13-nearduplicate-report.md`. The choice of which one to run
with is Aziz's, on his own photographs, which is why every one of them is
a flag rather than a constant.

The hash: DCT-based pHash, 63 bits, computed in-house
-----------------------------------------------------
Of the usual family, three candidates and the reason for this one:

- **aHash** (compare each cell to the image mean) is the cheapest and much
  the weakest: it is a 64-cell thumbnail with the brightness thrown away,
  so it collapses any two photographs with a similar overall layout — a
  false-merge machine on a library of holiday snapshots.
- **dHash** (compare each cell to its right-hand neighbour) is a real
  improvement and nearly free. It is however a purely local gradient
  measure: a JPEG re-encode at low quality perturbs exactly the local
  differences it is made of.
- **pHash** takes the 8x8 lowest-frequency block of a 2-D DCT over a 32x32
  greyscale reduction. Recompression and rescaling are high-frequency
  events; the bottom-left corner of the DCT is where they are not. This is
  the standard choice for "the same picture, saved again smaller" and it is
  what task 13 was asked to detect.

`imagehash` — named in `media.py` as the intended implementation and still
sitting in the `[media]` extra — would bring numpy and scipy along for
this one 8x8 block, and Р11 has already established what an optional heavy
dependency costs this project (117 MB of OpenCV behind its own install
command). It is not needed: only 64 of the 1024 DCT coefficients are ever
read, so the transform is not a 32x32 DCT at all but two narrow
projections — 8x32x32 multiply-adds for the first, 8x8x32 for the second,
about ten thousand in total, well under a millisecond in plain Python
against the 54-90 ms the decode already costs (задача 8). Computing the
full transform and throwing 94% of it away is what would have needed
numpy.

**Why 63 bits and not 64.** The DC coefficient is the image's mean
brightness scaled up by 1024; it is larger than every other coefficient in
essentially every real photograph, so as a bit it is a constant, and as a
member of the median it drags the threshold that the other 63 are compared
against. Both standard implementations keep it anyway. Here it is dropped
from the median and from the hash, which leaves 63 informative bits in a
64-bit integer instead of 64 bits of which one says nothing. Distances are
therefore in 0..63.

**Flat images get no hash at all, on purpose.** pHash on a picture with no
structure — a solid colour, a blank screenshot, a scan of an empty page —
is a hash of the noise in its low frequencies, and such hashes cluster
with each other for no reason whatsoever. `phash_image` returns
`structure` alongside the hash (the mean absolute deviation of the 63
coefficients, in grey levels), and anything under `FLAT_MIN_STRUCTURE` is
recorded as "looked at, no usable hash" rather than given a hash that
would be actively wrong. That row still exists in the index — the same
lesson as `content_face_scans` in Р11: "we looked and found nothing" has
to be distinguishable from "we never looked", or every rescan does the
work again.

Where it is computed
--------------------
In `jobs.ScanJob`, next to the two phases that already decode or read
these same files:

- `_preview_phase` gets it for free. It is already decoding one photo per
  duplicate group for the thumbnail and the task-9 metrics
  (`thumbnails.generate`), and the hash comes out of the same 240 px image
  that is about to be encoded as a JPEG.
- `_similar_phase` covers the rest of the library, which is most of it and
  is not optional: two photographs that are *similar* are by definition
  **not** in a duplicate group, so a near-duplicate pass that only looked
  at duplicate groups would be looking in the one place its answers cannot
  be.

Both write through the same `content_phashes` table, keyed by content
hash, for the same reason faces and previews are (Р9, Р11): a perceptual
hash is a property of pixels, so four filed copies of one photograph share
one answer and one decode. Archive members are excluded — decoding pixels
out of an archive member is pilot finding A2 again, which task 3 removed.

One thing the hash normalises before anything else: the EXIF orientation
flag. See `phash_image` — on this library it decides more than half the
answers.

Measuring on the 240 px image rather than the original is deliberate and
is the same argument `quality.measure_sharpness` makes: every copy goes
through the same reduction, so the comparison is between pictures rather
than between resolutions. It is not free of consequences —
`Image.draft()` reduces by powers of two, so an original and a
half-size copy of it reach 240 px by slightly different routes and land a
few bits apart. That distance is measured rather than assumed; see the
report.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from PIL import Image, ImageOps

# Stamped into every stored row, so an index whose hashes were computed by
# a different definition of "the hash" can be told apart from one whose
# were not. Bump it when anything above the bit level changes: the resize,
# the block, the bit order, the DC decision. A stored hash from another
# algorithm is not comparable to one from this one, and a silent mix would
# show up as near-duplicates that are not.
PHASH_ALGO = "phash-dct8-63-v1"

# The greyscale square the DCT runs over. 32 is the conventional choice and
# the reason is the block below: at 32 the 8x8 low-frequency block covers
# the bottom quarter of each axis, which is the band that survives
# rescaling.
PHASH_RESIZE_SIDE = 32
PHASH_BLOCK = 8
# 8*8 coefficients minus the DC term. See the module docstring.
PHASH_BITS = 63

# cos(pi * (2n+1) * k / 2N) for the eight k we actually need. Built once at
# import; this is the whole of the "DCT dependency".
_BASIS: tuple[tuple[float, ...], ...] = tuple(
    tuple(
        math.cos(math.pi * (2 * n + 1) * k / (2 * PHASH_RESIZE_SIDE))
        for n in range(PHASH_RESIZE_SIDE)
    )
    for k in range(PHASH_BLOCK)
)

# Unnormalised DCT-II sums N/2 per axis, so a coefficient is about
# (N/2)^2 = 256 times the amplitude of the pattern it measures. Dividing by
# it puts `structure` back into grey levels, where a threshold can be
# argued about.
_COEFF_SCALE = (PHASH_RESIZE_SIDE / 2.0) ** 2

# Below this much variation across the 63 coefficients (in grey levels,
# mean absolute deviation) there is no picture to hash — see the module
# docstring. Half a grey level: far below anything a photograph produces
# (the real distribution is in the report) and far above the rounding in a
# flat fill.
FLAT_MIN_STRUCTURE = 0.5


@dataclass(frozen=True)
class PerceptualHash:
    """One photograph's perceptual fingerprint, or the honest absence of one.

    `bits` is None exactly when `structure < FLAT_MIN_STRUCTURE`: the image
    was decoded and measured, and it has no low-frequency structure to
    fingerprint. Callers store the row either way.
    """

    bits: int | None
    structure: float
    aspect: float
    algo: str = PHASH_ALGO

    @property
    def hex(self) -> str | None:
        return None if self.bits is None else f"{self.bits:016x}"


def phash_image(img: Image.Image) -> PerceptualHash:
    r"""Perceptual hash of an already-decoded image.

    Takes an `Image`, not a path, because every caller already has one open
    — that is the entire cost saving this module is built around (see the
    module docstring). The image is not modified.

    **The EXIF orientation flag is applied first, and this is not a
    nicety.** A phone writes its sensor's pixels in the sensor's own
    orientation and adds a tag saying which way up the picture is; on
    `D:\Photos` that tag is not the identity on **56% of photographs**
    (2224 of a random 4000: 2001 rotated 90°, 156 rotated 270°, 67 by
    180°). Anything that re-encodes a photo — a messenger, an editor, a
    resize by any sane tool — bakes the rotation into the pixels and drops
    the tag, which is the correct thing to do and which leaves the copy's
    pixels at ninety degrees to the original's. Hashing the raw pixels
    would therefore put an original and its WhatsApp copy on opposite
    sides of the library: measured over 25 such pairs, median distance
    **32 of 63**, and not one of them inside any usable threshold. With
    the flag applied the same 25 pairs come out at median 0 and worst 2.

    In other words the whole feature turns on this line for more than half
    the photographs, and it is measured rather than assumed because the
    failure it prevents is silent: the groups simply would not be there.
    """
    img = ImageOps.exif_transpose(img) or img
    aspect = img.width / img.height if img.height else 1.0
    small = img.convert("L").resize(
        (PHASH_RESIZE_SIDE, PHASH_RESIZE_SIDE), Image.Resampling.LANCZOS
    )
    # `tobytes()` rather than `getdata()`: for mode "L" it is the same
    # row-major sequence of 0..255 values, it is not deprecated, and it
    # avoids building a list of a thousand Python ints.
    pixels = small.tobytes()

    side = PHASH_RESIZE_SIDE
    basis = _BASIS

    # Pass 1: DCT along x, keeping only the 8 lowest frequencies.
    # rows_dct[kx][y] — 8 x 32.
    rows_dct = [[0.0] * side for _ in range(PHASH_BLOCK)]
    for y in range(side):
        row = pixels[y * side : (y + 1) * side]
        for kx in range(PHASH_BLOCK):
            b = basis[kx]
            total = 0.0
            for n in range(side):
                total += row[n] * b[n]
            rows_dct[kx][y] = total

    # Pass 2: DCT along y over those, again only 8 frequencies. The result
    # is the 8x8 low-frequency block, DC first, row-major.
    coeffs: list[float] = []
    for ky in range(PHASH_BLOCK):
        b = basis[ky]
        for kx in range(PHASH_BLOCK):
            column = rows_dct[kx]
            total = 0.0
            for n in range(side):
                total += column[n] * b[n]
            coeffs.append(total)

    ac = coeffs[1:]  # drop DC
    ordered = sorted(ac)
    median = ordered[len(ordered) // 2]
    structure = sum(abs(c - median) for c in ac) / (len(ac) * _COEFF_SCALE)

    if structure < FLAT_MIN_STRUCTURE:
        return PerceptualHash(bits=None, structure=structure, aspect=aspect)

    bits = 0
    for i, c in enumerate(ac):
        if c > median:
            bits |= 1 << i
    return PerceptualHash(bits=bits, structure=structure, aspect=aspect)


def parse_phash(text: str | None) -> int | None:
    """Hex form back to bits. `None` in, `None` out — a stored NULL means
    "measured, no usable hash", not "missing"."""
    return None if text is None else int(text, 16)


def distance(a: int, b: int) -> int:
    """Hamming distance, 0..PHASH_BITS."""
    return (a ^ b).bit_count()


def aspect_log_ratio(a: float, b: float) -> float:
    """How differently two images are shaped, as |ln(a/b)| — symmetric, and
    additive in a way a plain ratio is not. 4:3 against 3:2 is 0.12, 4:3
    against 16:9 is 0.29, and 4:3 against 3:4 (a landscape against a
    portrait) is 0.58."""
    if a <= 0 or b <= 0:
        return 0.0
    return abs(math.log(a / b))


# --- thresholds ------------------------------------------------------------


@dataclass(frozen=True)
class SimilarThresholds:
    """Every number the grouping depends on, in one place, with why it has
    that value next to it — and with the loud caveat that the first one is
    **not settled**.
    """

    # Hamming distance at which two photographs are called the same
    # picture. Conservative by decision, not by measurement: the report
    # tabulates 2, 4, 6, 8, 10 and 12 over the real library, and 6 is
    # offered rather than imposed. At 6 the guarantee is roughly "no more
    # than one bit in ten of the fingerprint differs", which covers a
    # re-encode and a rescale of one photograph and does not reach as far
    # as the next frame of a burst.
    #
    # Deliberately the first flag on `dupecleaner similar`, because it is
    # the one Aziz is expected to change.
    max_distance: int = 6

    # Two images shaped differently enough are not one picture however
    # their fingerprints compare — a 32x32 square resize throws the shape
    # away, so this puts a little of it back. 0.35 admits a 16:9 crop of a
    # 4:3 original (0.29) and refuses a landscape paired with a portrait
    # (0.58). Set to None to switch the guard off entirely.
    max_aspect_log_ratio: float | None = 0.35

    # A bucket in the candidate index this large is not telling us anything
    # — it means thousands of photographs share those bits — and expanding
    # it costs a quadratic number of comparisons. Hitting the cap is
    # reported rather than silently absorbed (`SimilarClustering.warnings`),
    # because a skipped bucket means pairs that were not examined, and that
    # is exactly the kind of quiet gap finding A1 is about.
    max_bucket: int = 4000

    def describe(self) -> list[str]:
        """The thresholds as sentences, for `dupecleaner similar`."""
        lines = [
            f"Похожими считаются снимки на расстоянии Хэмминга ≤ {self.max_distance} "
            f"из {PHASH_BITS} бит "
            f"({100.0 * self.max_distance / PHASH_BITS:.0f}% отпечатка).",
        ]
        if self.max_aspect_log_ratio is None:
            lines.append("Форма кадра не учитывается.")
        else:
            lines.append(
                "Форма кадра должна совпадать: |ln(соотношение сторон)| ≤ "
                f"{self.max_aspect_log_ratio:.2f} "
                "(кроп 16:9 из 4:3 проходит, портрет с пейзажем — нет)."
            )
        lines.append(
            "Группы похожих — только список на просмотр: ни в одном режиме и "
            "ни при каком пороге они не дают права перемещать файлы (Р0, Р2)."
        )
        return lines


DEFAULT_THRESHOLDS = SimilarThresholds()


# --- grouping --------------------------------------------------------------


@dataclass(frozen=True)
class PhashEntry:
    """One unique piece of photo content, as the grouping sees it.

    Keyed by content hash and carrying every path that holds those bytes,
    because that is what the index stores: four filed copies of one
    photograph are one entry here, and they were one decode too.

    `paths` is only along for the ride — nothing in the grouping reads it —
    so this module never has to know how a path is spelled or which of them
    Р8 would keep.
    """

    content_hash: str
    bits: int
    aspect: float
    paths: tuple[str, ...] = ()
    size: int = 0


@dataclass
class SimilarGroup:
    """A set of >=2 pieces of content that look like the same picture.

    Pointedly **not** a `models.DuplicateGroup`, and with no `keeper_*`
    anything: there is no copy here that the others are provably redundant
    against, so offering a keeper would be offering a lie in the one place
    it would be believed. Р8 answers "which of these identical files stays"
    — that question does not exist here (Р2).
    """

    members: list[PhashEntry] = field(default_factory=list)
    # Largest Hamming distance between any two members, i.e. how far the
    # group is stretched. Single-linkage clustering builds chains: A is
    # within the threshold of B and B of C, so A, B and C are one group
    # even when A and C are twice the threshold apart. This number is how
    # a person sees that happening instead of guessing at it. None when the
    # group is too large to measure exactly (see MAX_EXACT_SPREAD).
    spread: int | None = None

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def file_count(self) -> int:
        return sum(max(1, len(m.paths)) for m in self.members)

    @property
    def content_hashes(self) -> list[str]:
        return [m.content_hash for m in self.members]

    @property
    def display_paths(self) -> list[str]:
        return [p for m in self.members for p in m.paths]

    def to_dict(self) -> dict:
        return {
            "spread": self.spread,
            "size": self.size,
            "file_count": self.file_count,
            # No "wasted_bytes", and no "keeper": both would be claims this
            # group is not entitled to make. Р2 allows ranking copies by
            # quality inside such a group (task 17); it does not allow
            # calling any of them redundant.
            "members": [
                {
                    "content_hash": m.content_hash,
                    "phash": f"{m.bits:016x}",
                    "size": m.size,
                    "paths": list(m.paths),
                }
                for m in self.members
            ],
        }


# Above this many members, the exact spread would cost more pairs than it
# is worth (a 64-member group is 2016 comparisons; a chained 5000-member
# one would be twelve million). Such a group is already a finding in
# itself, so it is reported with spread=None rather than measured.
MAX_EXACT_SPREAD = 64


@dataclass
class SimilarClustering:
    """The whole answer for one threshold, with what it cost and what it
    could not see."""

    groups: list[SimilarGroup]
    thresholds: SimilarThresholds
    entries_considered: int
    # Content with a row but no usable hash — flat images (see
    # FLAT_MIN_STRUCTURE). Counted, because "nothing found" and "not
    # fingerprintable" are different answers.
    entries_without_hash: int = 0
    # Candidate pairs the index proposed and the loop looked at. Includes
    # repeats: one pair can surface in several blocks (see
    # `_iter_candidate_pairs`).
    pairs_examined: int = 0
    # Unions actually performed — i.e. how many times a pair within the
    # threshold joined two groups that were not yet one. Not "how many
    # similar pairs exist": a pair whose ends are already in one group is
    # skipped before its distance is computed, deliberately.
    pairs_joined: int = 0
    # Distinct pairs inside the distance threshold that the shape guard
    # refused. Counted per pair rather than per candidate block: the same
    # pair surfaces in several blocks, and a diagnostic that answers "five
    # pairs refused" about two photographs is worse than no diagnostic.
    # Still not a census — a pair whose ends are already in one group never
    # reaches the guard.
    pairs_rejected_by_aspect: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        sizes = [g.size for g in self.groups]
        spreads = [g.spread for g in self.groups if g.spread is not None]
        return {
            "groups": len(self.groups),
            "contents_in_groups": sum(sizes),
            "files_in_groups": sum(g.file_count for g in self.groups),
            "largest_group": max(sizes) if sizes else 0,
            "max_spread": max(spreads) if spreads else 0,
            "entries_considered": self.entries_considered,
            "entries_without_hash": self.entries_without_hash,
            "pairs_examined": self.pairs_examined,
            "pairs_joined": self.pairs_joined,
            "pairs_rejected_by_aspect": self.pairs_rejected_by_aspect,
        }


def _block_masks(n_blocks: int) -> list[tuple[int, int]]:
    """`(shift, mask)` for `n_blocks` near-equal contiguous slices of the
    PHASH_BITS-bit fingerprint."""
    n_blocks = max(1, min(PHASH_BITS, n_blocks))
    out: list[tuple[int, int]] = []
    start = 0
    for i in range(n_blocks):
        width = (PHASH_BITS - start) // (n_blocks - i)
        out.append((start, (1 << width) - 1))
        start += width
    return out


def _iter_candidate_pairs(
    entries: Sequence[PhashEntry], thresholds: SimilarThresholds
) -> tuple[list[tuple[int, int]], list[str]]:
    """Index the fingerprints so that no pair within `max_distance` is
    missed, without comparing all 430 million pairs of a 30 000-photo
    library.

    Pigeonhole: split each fingerprint into `max_distance + 1` contiguous
    blocks. Two fingerprints differing in at most `max_distance` bits
    cannot differ in *every* block, so they must agree exactly on at least
    one — which makes "equal in some block" an exact filter rather than a
    heuristic. Bucketing by each block and pairing within buckets therefore
    finds every real pair, and merely also proposes some that the distance
    check then throws out.

    Returned as a plain list of buckets rather than a de-duplicated set of
    pairs, and that is not a detail: the set was the first version and it
    ran the machine out of memory at `max_distance=16`. The blocks get
    narrower as the threshold rises (63 bits over d+1 of them), buckets get
    correspondingly fatter, and the number of *candidate* pairs grows
    roughly with the square of the bucket size while the number of real
    ones does not. Streaming the pairs costs some repeats — one pair can
    surface in several blocks — which `find_similar_groups` absorbs with a
    union-find check that is cheaper than the distance it skips.

    The cost is still quadratic in bucket size, which is what
    `max_bucket` is for, and skipping a bucket means pairs that were never
    examined, so it says so in the returned warnings rather than quietly
    returning fewer groups.
    """
    masks = _block_masks(thresholds.max_distance + 1)
    buckets: dict[tuple[int, int], list[int]] = {}
    for i, entry in enumerate(entries):
        for b, (shift, mask) in enumerate(masks):
            buckets.setdefault((b, (entry.bits >> shift) & mask), []).append(i)

    usable: list[tuple[int, int]] = []
    warnings: list[str] = []
    oversized = 0
    skipped_entries = 0
    for members in buckets.values():
        if len(members) < 2:
            continue
        if len(members) > thresholds.max_bucket:
            oversized += 1
            skipped_entries += len(members)
            continue
        usable.append(members)  # type: ignore[arg-type]
    if oversized:
        warnings.append(
            f"Пропущено {oversized} корзин индекса, в каждой больше "
            f"{thresholds.max_bucket} отпечатков ({skipped_entries} записей "
            "суммарно): пары внутри них не проверялись. Понизьте порог или "
            "поднимите --max-bucket."
        )
    return usable, warnings  # type: ignore[return-value]


def entries_from_rows(rows: Iterable[tuple]) -> list[PhashEntry]:
    """Turn `storage.ScanIndex.phash_rows` output into entries.

    The conversion lives here rather than in `storage.py` for the layering
    reason `events.moments_from_rows` exists: `storage` must stay
    importable without Pillow, and this module is not. A row is
    `(content_hash, phash_hex, aspect, size, [display_path, ...])`.
    """
    out: list[PhashEntry] = []
    for content_hash, phash, aspect, size, paths in rows:
        bits = parse_phash(phash)
        if bits is None:
            continue
        out.append(
            PhashEntry(
                content_hash=content_hash,
                bits=bits,
                aspect=float(aspect),
                paths=tuple(paths),
                size=int(size or 0),
            )
        )
    return out


def find_similar_groups(
    entries: Iterable[PhashEntry],
    thresholds: SimilarThresholds = DEFAULT_THRESHOLDS,
    entries_without_hash: int = 0,
) -> SimilarClustering:
    """Group content that looks like the same picture.

    Single-linkage: a pair within the threshold joins two groups. The
    alternative (every member within the threshold of every other) would
    refuse the case this exists for — an original, a messenger's re-encode
    of it and a re-encode of *that* are a chain by construction, and the
    ends are further apart than either link. The cost of single linkage is
    that chains can run away, which is why every group carries its
    `spread`: the failure mode is reported per group rather than argued
    about in the abstract.

    Returns groups sorted by size then by first content hash, so the same
    input gives the same order — a review screen that reshuffles between
    reloads is unusable.
    """
    items = [e for e in entries]
    if len(items) < 2:
        return SimilarClustering(
            groups=[],
            thresholds=thresholds,
            entries_considered=len(items),
            entries_without_hash=entries_without_hash,
        )

    buckets, warnings = _iter_candidate_pairs(items, thresholds)

    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    examined = 0
    matched = 0
    rejected_pairs: set[tuple[int, int]] = set()
    limit = thresholds.max_aspect_log_ratio
    max_distance = thresholds.max_distance
    for members in buckets:
        for a_pos in range(len(members)):
            ia = members[a_pos]
            a = items[ia]
            for b_pos in range(a_pos + 1, len(members)):
                ib = members[b_pos]
                examined += 1
                ra, rb = find(ia), find(ib)
                if ra == rb:
                    # Already in one group: whatever this pair would say,
                    # it cannot change the answer. Skipping it here is what
                    # makes the repeats the streaming index produces cheap.
                    continue
                b = items[ib]
                if (a.bits ^ b.bits).bit_count() > max_distance:
                    continue
                if limit is not None and aspect_log_ratio(a.aspect, b.aspect) > limit:
                    rejected_pairs.add((ia, ib) if ia < ib else (ib, ia))
                    continue
                matched += 1
                parent[ra] = rb

    clusters: dict[int, list[int]] = {}
    for i in range(len(items)):
        clusters.setdefault(find(i), []).append(i)

    groups: list[SimilarGroup] = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        chosen = [items[i] for i in members]
        chosen.sort(key=lambda e: e.content_hash)
        spread: int | None = None
        if len(chosen) <= MAX_EXACT_SPREAD:
            spread = max(
                distance(chosen[i].bits, chosen[j].bits)
                for i in range(len(chosen))
                for j in range(i + 1, len(chosen))
            )
        groups.append(SimilarGroup(members=chosen, spread=spread))

    groups.sort(key=lambda g: (-g.size, g.members[0].content_hash))
    return SimilarClustering(
        groups=groups,
        thresholds=thresholds,
        entries_considered=len(items),
        entries_without_hash=entries_without_hash,
        pairs_examined=examined,
        pairs_joined=matched,
        pairs_rejected_by_aspect=len(rejected_pairs),
        warnings=warnings,
    )


def distance_sensitivity(
    entries: Iterable[PhashEntry],
    distances: Sequence[int],
    *,
    base: SimilarThresholds | None = None,
) -> list[dict]:
    """The same library grouped at several thresholds, as one table.

    This is what makes the threshold arguable instead of asserted, and it
    is the function that produced the table in
    `claude/task-13-nearduplicate-report.md`. Mirrors
    `events.gap_sensitivity`, which exists for the same reason.
    """
    items = list(entries)
    base = base or DEFAULT_THRESHOLDS
    rows: list[dict] = []
    for d in distances:
        from dataclasses import replace

        clustering = find_similar_groups(items, replace(base, max_distance=d))
        row = {"max_distance": d}
        row.update(clustering.summary)
        rows.append(row)
    return rows
