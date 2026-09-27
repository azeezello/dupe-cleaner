"""Events: cutting a photo library into the occasions a person remembers.

Р4 decided that an album is an **event**, and that an event is "a gap in
time plus a change of place". This module is that sentence turned into
code, with no model, no training data and nothing that cannot be
explained in one line to the person whose photos it is rearranging.

Why time is the backbone and geography only an assistant
--------------------------------------------------------
Measured on `D:\\Photos`, 30 100 media files: **49% carry no position at all**
(14 699 of 29 987 images) — messengers strip it, the older cameras here
never wrote it, scans never had it, and Picasa cleaned out what was left.
The other half does have EXIF GPS, which is more than this was written to
expect, and it still cannot be the backbone: a rule that needs two
coordinates would abstain on half the library, and abstaining is not an
option when the answer decides which folder a photo ends up in.

So the rules are arranged so that **time alone always produces an answer**,
and geography only ever adds boundaries time did not already find. On the
real library that is 109 extra boundaries out of 2032 — five per cent,
which is the shape of the claim as well as its size: useful, never
load-bearing. A photo with no position is not a special case to be handled;
it is half the library.

The three rules, and what each is for
--------------------------------------
1. **A long enough gap is a new event.** Unconditional. Nothing about a
   position can glue two occasions together across a night, because the
   alternative — "same place, so one event" — would merge every evening at
   home into one album that never ends.
2. **A shorter gap plus a real change of place is also a new event.** A
   morning in one town and an afternoon in the next are two events even
   though only three hours separate them. This rule needs both halves: the
   gap, so that the movement had time to happen, and the distance, so that
   GPS noise and a walk around the block do not split a birthday party.
3. **The same day in the same place is not cut, even over a long gap.**
   The one place geography *removes* a boundary rather than adding one: a
   wedding with five hours between the registry office and the dinner is
   one event, and the photos say so by being in the same square kilometre
   on the same date. Only applies when both sides actually have a
   position — which, per the paragraph above, is the rare case, so this
   rule is a refinement and never load-bearing.

A pair whose implied speed is impossible (a 500 km jump in four minutes)
is not an event boundary — it is a broken coordinate or a broken clock.
Such a pair is recorded in `warnings` and decided on time alone, because
inventing a boundary out of bad data is worse than missing one.

One clock, and why that matters more than it looks
--------------------------------------------------
The timestamps come from four different sources and only two of them are
instants:

- EXIF `DateTimeOriginal` — the camera's **local wall clock**, no zone
  (93.3% of this library);
- a filename like `20170416_145106` — the same, written by the same device
  (3.5%, and the only capture time left on the photos Google stripped);
- a Google sidecar `photoTakenTime` — a true **UTC epoch** (finding A3);
- the file's `mtime` — a true UTC epoch, and **off by default**: see
  `MomentPolicy.use_mtime` for the measurement that decided that.

Mixing the two kinds silently shifts half the library by the local UTC
offset — seven hours here — which is larger than any threshold below, so
it would not merely blur boundaries, it would invent and erase them. This
module therefore keeps **one scale**: wall-clock seconds, as the camera
would have printed them. Epoch sources are converted by adding a single
declared offset (`MomentPolicy.utc_offset_seconds`, the machine's own zone
by default, `--utc-offset-hours` in the CLI). The residual error is named
rather than hidden: a photo taken abroad carries the camera's local time
while the offset applied to its neighbours is the home one, so an event
spanning a flight can be off by the travel delta. That is hours at worst,
it affects only events that straddle a border, and `time_source` is stored
per photo so such an event can be argued with instead of merely believed.

What is deliberately kept out
------------------------------
**Screenshots and document scans** (Р3, task 15). Not because they are
worthless but because there are thousands of them and they dissolve the
structure they are mixed into. `storage.ScanIndex.screenshot_paths` hands
over the list in one query; `OriginClass.excluded_from_albums` is the
slightly wider set (it adds scans, on Р5's authority — those already get
their own `_documents/` tree).

**Anything that moves a file.** An event is axis C in Р0 — organisation —
and axis C has no authority over the filesystem. This module returns
clusters; task 21 builds a plan from them and shows it before touching
anything.

**The choice of grain.** Whether a five-day trip is one album or five is a
product decision that changes what Р5 materialises on disk, so it is not
made here. `cluster_events` answers the fine question (sessions, split by
the rules above) and `merge_into_trips` answers the coarse one on top of
the same data; which one becomes a folder is Aziz's call, and the report
for task 16 states it as open.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import PurePath

from .origin import GoogleSidecar, OriginSignals


class TimeSource(str, Enum):
    """Where a photo's timestamp came from, in descending trustworthiness.

    Kept per photo rather than collapsed into one number for the library,
    because it is the only way an odd-looking event can be explained
    afterwards: a cluster of four hundred photos "taken" within one minute
    is not a party, it is a folder someone copied, and the source column
    is what says so.
    """

    EXIF = "exif"                    # the camera's own DateTimeOriginal
    SIDECAR = "sidecar"              # Google's photoTakenTime (finding A3)
    FILENAME = "filename"            # 20170416_145106 — device-generated
    FILENAME_DATE = "filename_date"  # a date with no time of day
    MTIME = "mtime"                  # the filesystem's idea, the weak one
    NONE = "none"

    @property
    def rank(self) -> int:
        return _TIME_SOURCE_RANK[self]

    @property
    def is_exact(self) -> bool:
        """Whether the source gives a time of day at all.

        FILENAME_DATE does not: it is placed at local noon, so it can be
        out by up to twelve hours. That is fine for putting a photo on the
        right day and useless for deciding a boundary inside one, and the
        clustering below treats it accordingly.
        """
        return self in (TimeSource.EXIF, TimeSource.SIDECAR, TimeSource.FILENAME, TimeSource.MTIME)


_TIME_SOURCE_RANK = {
    TimeSource.EXIF: 0,
    TimeSource.SIDECAR: 1,
    TimeSource.FILENAME: 2,
    TimeSource.FILENAME_DATE: 3,
    TimeSource.MTIME: 4,
    TimeSource.NONE: 5,
}


class GeoSource(str, Enum):
    EXIF = "exif"
    SIDECAR = "sidecar"
    NONE = "none"


class EventConfidence(str, Enum):
    """How much the event's own boundaries are worth.

    HIGH  — most of its photos are timed by the camera itself.
    MEDIUM— mostly filenames or sidecars: a device wrote them, but after
            the fact.
    LOW   — mostly `mtime`. On this corpus that is usually still the
            camera's time (Windows preserves it when copying, which Р8
            measured), but a single bulk copy re-stamps thousands of files
            to one instant, and the result would look exactly like a very
            busy afternoon. A LOW event is a suggestion, not a finding.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class MomentPolicy:
    """The assumptions turning four kinds of timestamp into one scale.

    `utc_offset_seconds=None` means "use this machine's own zone at that
    instant", which is right when the tool runs where the photos live and
    wrong in a container in UTC — hence the explicit override.
    """

    utc_offset_seconds: float | None = None
    # `mtime` is off by default, and that is a measurement rather than
    # caution. On `D:\Photos` the file modification time disagrees with the
    # camera's own EXIF by a **median of 2288 days**: the folder was copied
    # in September 2026, and every mtime now says September 2026. Trusting
    # it put all 889 photos that have no other timestamp into one phantom
    # event — 889 photos "taken" within 36 minutes on 2026-09-13, the
    # largest single artefact in the whole run. 377 of them sit in a folder
    # the owner himself named `No date`, which is the same verdict reached
    # by hand.
    #
    # Р5 already has the honest destination for a photo with no date:
    # `_unsorted/`. A wrong date is worse than none, because a photo filed
    # into an event that never happened is one nobody will look for again.
    # On a library that was never bulk-copied mtime is the best remaining
    # fallback, so the switch stays — `--use-mtime` in the CLI.
    use_mtime: bool = False
    use_filename: bool = True

    def to_wall_clock(self, epoch_seconds: float) -> float:
        """A true instant -> the wall clock a person would have read."""
        if self.utc_offset_seconds is not None:
            return epoch_seconds + self.utc_offset_seconds
        # DST-correct by construction: ask the platform what the offset was
        # at that instant rather than what it is now.
        moment = dt.datetime.fromtimestamp(epoch_seconds)
        return float(calendar.timegm(moment.timetuple())) + moment.microsecond / 1e6


DEFAULT_POLICY = MomentPolicy()


@dataclass(frozen=True)
class PhotoMoment:
    """One photo reduced to what an event is made of: when, and where.

    Deliberately not a subset of `FileRecord`: clustering has no business
    knowing a file's size, hash or archive membership, and keeping the
    input this thin is what makes the rules testable on a table of
    timestamps with no filesystem in sight.
    """

    display_path: str
    taken_at: float | None = None          # wall-clock seconds; see module docstring
    time_source: TimeSource = TimeSource.NONE
    latitude: float | None = None
    longitude: float | None = None
    geo_source: GeoSource = GeoSource.NONE

    @property
    def has_time(self) -> bool:
        return self.taken_at is not None

    @property
    def has_geo(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def folder(self) -> str:
        parts = PurePath(self.display_path.replace("\\", "/")).parts
        return parts[-2] if len(parts) >= 2 else ""

    def local_date(self) -> dt.date | None:
        if self.taken_at is None:
            return None
        return dt.datetime.utcfromtimestamp(self.taken_at).date()


# --- filenames that carry a date -------------------------------------------
#
# Every shape below exists in `D:\Photos` or in the Takeout archive; none
# was invented. A filename date is not a fallback of last resort here: for
# the 1156 photos Google stripped the EXIF out of (task 15), it is the only
# capture time that survived.

_RE_NAME_DATETIME = re.compile(
    r"(?<!\d)(20\d{2}|19\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2})"   # 2017 04 16
    # Separator. `at` is in here because WhatsApp Desktop spells it out —
    # `WhatsApp Image 2024-01-02 at 10.11.12.jpeg` — and without it those
    # files fall back to a date with no time of day, which is a twelve-hour
    # error on a file that states the second.
    r"[ _tT\-]+(?:at[ ]+|в[ ]+)?"
    # 14 51 06, plus the milliseconds Android and Google Photos append:
    # `IMG_20170426_132143114`. Without the optional tail those names fell
    # through to the date-only branch and were placed at noon — a photo that
    # states the second, filed five hours off. Found by reading the
    # singleton events the real run produced, not by thinking about it.
    r"(\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2})(?:\d{1,3})?(?!\d)"
)
# `IMG-20240101-WA0001`, `photo_5@10-11-2024`, `2019-06-15.jpg`: a day with
# no time of day.
_RE_NAME_DATE = re.compile(r"(?<!\d)(20\d{2}|19\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2})(?!\d)")
_RE_NAME_DATE_DMY = re.compile(r"(?<!\d)(\d{2})[-_.](\d{2})[-_.](20\d{2}|19\d{2})(?!\d)")

_PLAUSIBLE_YEARS = (1990, 2035)
# A date-only filename is placed at local noon: the error is then at most
# twelve hours in either direction instead of a guaranteed twelve if it
# were placed at midnight, and midnight additionally drags the photo into
# the previous evening's event.
_NOON_SECONDS = 12 * 3600


def _wall_seconds(year: int, month: int, day: int, hour: int, minute: int, second: int) -> float | None:
    if not _PLAUSIBLE_YEARS[0] <= year <= _PLAUSIBLE_YEARS[1]:
        return None
    try:
        stamp = dt.datetime(year, month, day, hour, minute, min(second, 59))
    except ValueError:
        return None
    return float(calendar.timegm(stamp.timetuple()))


def parse_filename_time(name: str) -> tuple[float | None, TimeSource]:
    """Pull a capture time out of a filename, or admit there is none.

    Validation does the heavy lifting: a 14-digit CDN hash matches the
    shape of a timestamp, so every candidate has to survive being turned
    into a real calendar date inside a plausible range. A date a file does
    not really have is worse than no date — it would file the photo into an
    event that never happened.
    """
    stem = PurePath(name.replace("\\", "/")).stem
    match = _RE_NAME_DATETIME.search(stem)
    if match:
        year, month, day, hour, minute, second = (int(g) for g in match.groups())
        wall = _wall_seconds(year, month, day, hour, minute, second)
        if wall is not None:
            return wall, TimeSource.FILENAME
    match = _RE_NAME_DATE.search(stem)
    if match:
        year, month, day = (int(g) for g in match.groups())
        wall = _wall_seconds(year, month, day, 0, 0, 0)
        if wall is not None:
            return wall + _NOON_SECONDS, TimeSource.FILENAME_DATE
    match = _RE_NAME_DATE_DMY.search(stem)
    if match:
        day, month, year = (int(g) for g in match.groups())
        wall = _wall_seconds(year, month, day, 0, 0, 0)
        if wall is not None:
            return wall + _NOON_SECONDS, TimeSource.FILENAME_DATE
    return None, TimeSource.NONE


# --- building a moment -----------------------------------------------------


def _assemble(
    display_path: str,
    *,
    exif_taken_at: float | None,
    sidecar: GoogleSidecar | None,
    gps: tuple[float | None, float | None],
    mtime: float | None,
    policy: MomentPolicy,
) -> PhotoMoment:
    taken_at: float | None = None
    source = TimeSource.NONE

    if exif_taken_at is not None:
        taken_at, source = exif_taken_at, TimeSource.EXIF
    elif sidecar is not None and sidecar.taken_at is not None:
        taken_at, source = policy.to_wall_clock(sidecar.taken_at), TimeSource.SIDECAR
    elif policy.use_filename:
        taken_at, source = parse_filename_time(display_path)

    if taken_at is None and policy.use_mtime and mtime:
        taken_at, source = policy.to_wall_clock(mtime), TimeSource.MTIME

    latitude, longitude = gps
    geo_source = GeoSource.EXIF if latitude is not None and longitude is not None else GeoSource.NONE
    if geo_source is GeoSource.NONE and sidecar is not None and sidecar.latitude is not None:
        latitude, longitude, geo_source = sidecar.latitude, sidecar.longitude, GeoSource.SIDECAR

    return PhotoMoment(
        display_path=display_path,
        taken_at=taken_at,
        time_source=source,
        latitude=latitude,
        longitude=longitude,
        geo_source=geo_source,
    )


def moment_from_signals(
    signals: OriginSignals,
    *,
    mtime: float | None = None,
    policy: MomentPolicy = DEFAULT_POLICY,
) -> PhotoMoment:
    """A moment out of the header read `origin.read_signals` already did.

    Pure, like `origin.classify`, and over the same input: one file open
    answers both "where did this come from" and "when and where was it
    taken". Task 15 pays about 8 ms per photo for that read; task 16 adds
    nothing to it.
    """
    return _assemble(
        signals.path,
        exif_taken_at=signals.exif_taken_at,
        sidecar=signals.sidecar,
        gps=(signals.gps_latitude, signals.gps_longitude),
        mtime=mtime,
        policy=policy,
    )


def moment_without_header(
    display_path: str,
    *,
    mtime: float | None = None,
    policy: MomentPolicy = DEFAULT_POLICY,
) -> PhotoMoment:
    """A moment for a file nobody opened — a video, or an unreadable image.

    Videos belong in events (a clip shot at the party is part of the
    party), but Pillow cannot read their metadata and a container parser
    is a dependency task 16 does not need: `VID_20190101_120000.mp4` and
    the filesystem's mtime answer the question at zero cost. The 113
    videos in `D:\\Photos` all carry a camera-style name.
    """
    return _assemble(
        display_path,
        exif_taken_at=None,
        sidecar=None,
        gps=(None, None),
        mtime=mtime,
        policy=policy,
    )


def moments_from_rows(rows, *, policy: MomentPolicy = DEFAULT_POLICY) -> list[PhotoMoment]:
    """Rebuild moments from what `ScanIndex.moments` stored.

    The mtime fallback is applied *here*, not when the row was written:
    `MomentPolicy.use_mtime` is a judgement about one library (see the
    comment on it), and a judgement that can be re-made without re-reading
    30 000 headers should be. A row that already carries a real capture
    time is untouched.
    """
    out: list[PhotoMoment] = []
    for row in rows:
        display_path, taken_at, time_source, latitude, longitude, geo_source, mtime = row
        source = TimeSource(time_source or "none")
        if taken_at is None and policy.use_mtime and mtime:
            taken_at, source = policy.to_wall_clock(mtime), TimeSource.MTIME
        elif taken_at is None:
            source = TimeSource.NONE
        out.append(
            PhotoMoment(
                display_path=display_path,
                taken_at=taken_at,
                time_source=source,
                latitude=latitude,
                longitude=longitude,
                geo_source=GeoSource(geo_source or "none"),
            )
        )
    return out


# --- geography -------------------------------------------------------------

_EARTH_RADIUS_M = 6_371_000.0


def haversine_m(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance in metres.

    A sphere, not an ellipsoid: the error is a few tenths of a percent,
    and the thresholds below are round numbers in kilometres. Pretending
    to more precision than the GPS in a phone has would be theatre.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def distance_between(a: PhotoMoment, b: PhotoMoment) -> float | None:
    if not (a.has_geo and b.has_geo):
        return None
    return haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)  # type: ignore[arg-type]


# --- thresholds ------------------------------------------------------------


@dataclass(frozen=True)
class EventThresholds:
    """Every number the clustering depends on, in one place, with the
    reason it has that value written next to it.

    The defaults are measured rather than chosen: see
    `claude/task-16-events-report.md` for the gap histogram and the
    sensitivity table they come from, and `gap_sensitivity` below for the
    function that produced it. They are arguments, not constants, because
    the measurement was taken on one library and the next one will differ.
    """

    # Rule 1. A gap at least this long is a new event, whatever the
    # geography says. Nine hours, and three independent measurements on
    # `D:\Photos` picked the number rather than taste (the tables are in
    # `claude/task-16-events-report.md`):
    #
    # 1. **The gap histogram bottoms out between 9 and 12 hours.** Gaps
    #    inside an occasion are minutes — 81% of all gaps are under one —
    #    and the hourly density falls from 291 pairs in the 1–2 h bucket to
    #    41 in 9–10 h, 41 in 10–11 h and 42 in 11–12 h, then climbs again
    #    (53, 46, 47, …) because a twelve-to-twenty-four-hour gap is the
    #    next day rather than a longer break in the same one. Nine hours is
    #    the left edge of that floor: the least populated place to cut, so
    #    the fewest photos depend on the exact value.
    # 2. **It agrees with the owner's own folders 96% of the time.** Where
    #    two neighbouring photos sit in the same person-named folder on the
    #    same day — the closest thing to ground truth this project has —
    #    nine hours wrongly cuts 6 of those 3367 pairs (0.18%). Six hours
    #    cuts 28, four hours 48. Above twelve the count reaches zero, but
    #    by then the threshold is inside the next-day band and has started
    #    merging occasions instead of respecting them.
    # 3. **The answer is insensitive there.** Between 8 h and 10 h the
    #    number of events moves by under 2% per hour, against 6–8% per
    #    hour at 4 h and at 16 h. A threshold on a steep slope is a
    #    threshold that was guessed.
    session_gap_seconds: float = 9 * 3600

    # Rule 2. Below `session_gap_seconds`, a move of at least
    # `place_radius_m` also starts a new event, provided at least
    # `geo_gap_seconds` passed — without that second condition, two photos
    # taken a minute apart from a moving car would be two events.
    geo_gap_seconds: float = 30 * 60
    # Three kilometres, taken from between two measured distributions
    # rather than from intuition. Inside one occasion (a person-named
    # folder on one day) the largest step between neighbouring photos is
    # 0.52 km at the median and 2.58 km at the 75th percentile; between two
    # consecutive occasions it is 4.77 km at the 90th. Three kilometres
    # sits just above the first and below the second.
    #
    # And this is the weakest claim in the file, so it is stated rather
    # than buried: on this library the rule can barely be validated at all.
    # Where the owner's folders say two neighbouring photos belong to
    # different occasions, the two are almost always in the *same* place —
    # the 90th percentile of that distance is 20 m — so the cases this rule
    # is for barely occur inside the part of the library that can be
    # checked. Its 109 boundaries land mostly on pairs where at least one
    # side sits in a dump folder, where there is no ground truth to compare
    # against. The rule earns its place on a library with travel in it;
    # here it is close to inert, and that is measured rather than assumed.
    place_radius_m: float = 3_000.0

    # Rule 3. A long gap is *not* cut when both ends sit within this
    # radius on the same calendar day — the wedding-with-a-dinner-break
    # case. Deliberately small: same square kilometre, same day.
    keep_same_day_same_place: bool = True
    same_place_radius_m: float = 1_000.0

    # Above this implied speed the pair is not travel, it is a broken
    # coordinate or a broken clock. Faster than an airliner, so a real
    # flight still reads as movement.
    max_speed_kmh: float = 1_200.0

    # Stage 2 (`merge_into_trips`), off by default: consecutive events
    # within 50 km of each other and no more than 30 hours apart are days
    # of one trip. Both numbers are stated for what they are — a proposal
    # attached to an open product question, not a measurement.
    trip_radius_m: float = 50_000.0
    trip_gap_seconds: float = 30 * 3600

    def describe(self) -> list[str]:
        """The thresholds as sentences, for `dupecleaner events`.

        A number a person cannot see is a number they cannot argue with,
        and UX-BRIEF asks for the evidence rather than the verdict.
        """
        lines = [
            f"Разрыв во времени ≥ {self.session_gap_seconds / 3600:.1f} ч — новое событие, всегда.",
            (
                f"Разрыв ≥ {self.geo_gap_seconds / 60:.0f} мин при смене места на "
                f"≥ {self.place_radius_m / 1000:.1f} км — тоже новое событие."
            ),
        ]
        if self.keep_same_day_same_place:
            lines.append(
                f"Тот же день в пределах {self.same_place_radius_m / 1000:.1f} км — "
                "не разрезаем, даже при длинном разрыве."
            )
        lines.append(
            f"Скорость выше {self.max_speed_kmh:.0f} км/ч — координаты не верим, "
            "решаем по времени."
        )
        return lines


DEFAULT_THRESHOLDS = EventThresholds()


# --- clustering ------------------------------------------------------------


@dataclass(frozen=True)
class Boundary:
    """Why one event ends and the next begins. Always attached to the
    event that *starts* at it, so an event can explain its own existence.
    """

    rule: str                       # "time_gap" | "place_change"
    gap_seconds: float
    distance_m: float | None
    reason: str                     # human-readable, shown as-is

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "gap_seconds": round(self.gap_seconds, 1),
            "distance_m": round(self.distance_m, 1) if self.distance_m is not None else None,
            "reason": self.reason,
        }


def _format_gap(seconds: float) -> str:
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} мин"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} ч"
    return f"{seconds / 86400:.1f} сут"


@dataclass
class EventCluster:
    """One event: the photos in it, and why it starts where it starts."""

    moments: list[PhotoMoment] = field(default_factory=list)
    boundary: Boundary | None = None

    @property
    def size(self) -> int:
        return len(self.moments)

    @property
    def start(self) -> float:
        return self.moments[0].taken_at  # type: ignore[return-value]

    @property
    def end(self) -> float:
        return self.moments[-1].taken_at  # type: ignore[return-value]

    @property
    def duration_seconds(self) -> float:
        return self.end - self.start

    @property
    def date_range(self) -> tuple[dt.date, dt.date]:
        return (
            dt.datetime.utcfromtimestamp(self.start).date(),
            dt.datetime.utcfromtimestamp(self.end).date(),
        )

    @property
    def paths(self) -> list[str]:
        return [m.display_path for m in self.moments]

    @property
    def time_source_counts(self) -> Counter:
        return Counter(m.time_source.value for m in self.moments)

    @property
    def geo_known(self) -> int:
        return sum(1 for m in self.moments if m.has_geo)

    @property
    def centroid(self) -> tuple[float, float] | None:
        """Mean position of the photos that have one.

        An arithmetic mean of degrees, which is wrong at the poles and
        across the date line and right everywhere a holiday happens. The
        alternative (vector mean) would be more correct and less readable,
        and nothing downstream needs sub-kilometre accuracy: this feeds
        "are these two events in the same city".
        """
        located = [m for m in self.moments if m.has_geo]
        if not located:
            return None
        return (
            sum(m.latitude for m in located) / len(located),  # type: ignore[misc]
            sum(m.longitude for m in located) / len(located),  # type: ignore[misc]
        )

    @property
    def radius_m(self) -> float | None:
        """Distance from the centroid to the furthest located photo."""
        centre = self.centroid
        if centre is None:
            return None
        return max(
            haversine_m(centre[0], centre[1], m.latitude, m.longitude)  # type: ignore[arg-type]
            for m in self.moments
            if m.has_geo
        )

    @property
    def confidence(self) -> EventConfidence:
        counts = self.time_source_counts
        total = max(1, self.size)
        exif_like = counts[TimeSource.EXIF.value]
        device_like = exif_like + counts[TimeSource.SIDECAR.value] + counts[TimeSource.FILENAME.value]
        if exif_like / total >= 0.5:
            return EventConfidence.HIGH
        if device_like / total >= 0.5:
            return EventConfidence.MEDIUM
        return EventConfidence.LOW

    @property
    def folders(self) -> Counter:
        """Which folders the photos came from.

        Not used by the clustering — it would make the result depend on
        the very structure task 21 is about to replace. It is here because
        those folder names are the closest thing to ground truth this
        project has: where a person filed photos themselves is where they
        thought one occasion ended (Р8 leans on the same fact).
        """
        return Counter(m.folder for m in self.moments)

    def to_dict(self) -> dict:
        start, end = self.date_range
        centre = self.centroid
        return {
            "size": self.size,
            "start": self.start,
            "end": self.end,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "duration_seconds": round(self.duration_seconds, 1),
            "confidence": self.confidence.value,
            "time_sources": dict(self.time_source_counts),
            "geo_known": self.geo_known,
            "centroid": list(centre) if centre else None,
            "radius_m": round(self.radius_m, 1) if self.radius_m is not None else None,
            "boundary": self.boundary.to_dict() if self.boundary else None,
            "paths": self.paths,
        }


@dataclass
class EventClustering:
    """The whole answer: events, plus everything honestly left out of them."""

    events: list[EventCluster] = field(default_factory=list)
    # No usable timestamp at all -> Р5's `_unsorted/`. Never guessed into
    # an event: a photo placed in the wrong month is worse than one a
    # person has to file by hand.
    undated: list[PhotoMoment] = field(default_factory=list)
    # Screenshots and scans (Р3). Listed rather than dropped so the number
    # is visible.
    excluded: list[str] = field(default_factory=list)
    thresholds: EventThresholds = DEFAULT_THRESHOLDS
    warnings: list[str] = field(default_factory=list)

    @property
    def clustered_photos(self) -> int:
        return sum(e.size for e in self.events)

    def summary(self) -> dict:
        by_confidence = Counter(e.confidence.value for e in self.events)
        sizes = sorted(e.size for e in self.events)
        return {
            "events": len(self.events),
            "photos_in_events": self.clustered_photos,
            "undated": len(self.undated),
            "excluded": len(self.excluded),
            "median_event_size": sizes[len(sizes) // 2] if sizes else 0,
            "largest_event": sizes[-1] if sizes else 0,
            "singletons": sum(1 for s in sizes if s == 1),
            "by_confidence": dict(by_confidence),
            "warnings": len(self.warnings),
        }


def _boundary_between(
    previous: PhotoMoment, current: PhotoMoment, thresholds: EventThresholds
) -> tuple[Boundary | None, str | None]:
    """Decide whether an event ends between these two photos.

    Returns the boundary (or None) and an optional warning. Split out of
    the loop because this is the entire algorithm: everything else is
    bookkeeping, and this function is what the tests aim at.
    """
    gap = (current.taken_at or 0.0) - (previous.taken_at or 0.0)
    distance = distance_between(previous, current)
    warning: str | None = None

    if distance is not None and gap > 0:
        speed_kmh = (distance / 1000.0) / (gap / 3600.0)
        if speed_kmh > thresholds.max_speed_kmh:
            # Not travel: a stripped-and-refilled GPS block, a camera whose
            # clock was never set, two libraries merged. The position is
            # unusable for this pair, so fall back to time alone and say so.
            warning = (
                f"Невозможная скорость {speed_kmh:,.0f} км/ч между "
                f"{previous.display_path} и {current.display_path} — "
                "координаты для этой пары не учитываются"
            )
            distance = None

    # Rule 1: a long gap always cuts...
    if gap >= thresholds.session_gap_seconds:
        # ...except for the one case where geography removes a boundary:
        # same day, same place. Requires both positions, so on a library
        # without GPS this branch never fires and rule 1 stands alone.
        if (
            thresholds.keep_same_day_same_place
            and distance is not None
            and distance <= thresholds.same_place_radius_m
            and previous.local_date() == current.local_date()
        ):
            return None, warning
        return (
            Boundary(
                rule="time_gap",
                gap_seconds=gap,
                distance_m=distance,
                reason=f"разрыв во времени {_format_gap(gap)}",
            ),
            warning,
        )

    # Rule 2: a shorter gap plus a real move.
    if (
        distance is not None
        and gap >= thresholds.geo_gap_seconds
        and distance >= thresholds.place_radius_m
    ):
        return (
            Boundary(
                rule="place_change",
                gap_seconds=gap,
                distance_m=distance,
                reason=(
                    f"смена места: {distance / 1000:.1f} км за {_format_gap(gap)}"
                ),
            ),
            warning,
        )

    return None, warning


def cluster_events(
    moments,
    *,
    thresholds: EventThresholds = DEFAULT_THRESHOLDS,
    excluded_paths=(),
) -> EventClustering:
    """Cut a library into events. The only entry point that matters.

    `moments` may be any iterable in any order; `excluded_paths` is what
    Р3 keeps out of albums (`ScanIndex.screenshot_paths`, or the wider
    `OriginClass.excluded_from_albums` set). Photos with no usable
    timestamp land in `undated` rather than being guessed at.
    """
    excluded = set(excluded_paths)
    kept: list[PhotoMoment] = []
    skipped: list[str] = []
    undated: list[PhotoMoment] = []

    for moment in moments:
        if moment.display_path in excluded:
            skipped.append(moment.display_path)
        elif moment.has_time:
            kept.append(moment)
        else:
            undated.append(moment)

    # Sorted by time, then by path: two photos with the same timestamp
    # (a burst, or a copy) must not reorder between runs, or an event's
    # first photo — and therefore its date and its name — would wobble.
    kept.sort(key=lambda m: (m.taken_at, m.display_path))

    clustering = EventClustering(
        events=[], undated=undated, excluded=sorted(skipped), thresholds=thresholds
    )
    if not kept:
        return clustering

    current = EventCluster(moments=[kept[0]], boundary=None)
    for previous, moment in zip(kept, kept[1:]):
        boundary, warning = _boundary_between(previous, moment, thresholds)
        if warning:
            clustering.warnings.append(warning)
        if boundary is None:
            current.moments.append(moment)
            continue
        clustering.events.append(current)
        current = EventCluster(moments=[moment], boundary=boundary)
    clustering.events.append(current)
    return clustering


# --- stage 2: days of one trip (the open product question) -----------------


@dataclass
class TripCluster:
    """Consecutive events read as one journey.

    Exists so the product question — does a five-day trip become one
    album or five? — can be answered with numbers instead of intuition,
    not because it has been answered. `cluster_events` is the fine grain
    and is what everything downstream uses today.
    """

    events: list[EventCluster] = field(default_factory=list)
    reason: str = ""

    @property
    def size(self) -> int:
        return sum(e.size for e in self.events)

    @property
    def date_range(self) -> tuple[dt.date, dt.date]:
        return (self.events[0].date_range[0], self.events[-1].date_range[1])


def merge_into_trips(
    clustering: EventClustering, *, thresholds: EventThresholds | None = None
) -> list[TripCluster]:
    """Group consecutive events that are plainly the same trip.

    Requires geography on both sides on purpose. Without it, "consecutive
    and less than thirty hours apart" would merge every ordinary week at
    home into one endless album — which is exactly the failure mode Р4
    warns about for topic albums, arriving through a different door.
    """
    limits = thresholds or clustering.thresholds
    trips: list[TripCluster] = []
    for event in clustering.events:
        if not trips:
            trips.append(TripCluster(events=[event], reason="начало"))
            continue
        previous = trips[-1].events[-1]
        gap = event.start - previous.end
        here, there = event.centroid, previous.centroid
        if (
            here is not None
            and there is not None
            and gap <= limits.trip_gap_seconds
            and haversine_m(here[0], here[1], there[0], there[1]) <= limits.trip_radius_m
        ):
            trips[-1].events.append(event)
            trips[-1].reason = "та же местность, соседние дни"
            continue
        trips.append(TripCluster(events=[event], reason="начало"))
    return trips


# --- justifying the thresholds ---------------------------------------------


def gap_sensitivity(moments, gaps_seconds, *, base: EventThresholds | None = None) -> dict:
    """How many events each candidate time threshold would produce.

    This is the function that makes the defaults defensible instead of
    tasteful. A threshold sitting on a steep part of this curve is a
    threshold that was guessed; one sitting on a plateau means the data
    itself has a gap there, and any value inside the plateau gives the
    same answer.
    """
    template = base or DEFAULT_THRESHOLDS
    out: dict[float, int] = {}
    materialised = list(moments)
    for gap in gaps_seconds:
        clustering = cluster_events(
            materialised, thresholds=replace(template, session_gap_seconds=gap)
        )
        out[gap] = len(clustering.events)
    return out


def consecutive_gaps(moments) -> list[float]:
    """Sorted list of the time gaps between neighbouring photos.

    The raw material of the histogram in the task-16 report: the shape of
    this list is the argument for where the threshold goes.
    """
    timed = sorted(
        (m.taken_at for m in moments if m.has_time),
    )
    return [b - a for a, b in zip(timed, timed[1:])]
