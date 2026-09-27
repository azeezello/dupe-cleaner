"""Tests for event clustering (task 16, Р4).

Three things these tests are built around, in order of how much they
matter:

1. **A library with no GPS must cluster exactly as well as one with it.**
   Half of `D:\\Photos` has no position at all, so "geography optional" is
   not a graceful degradation path here, it is the main path. Several tests
   below assert that removing every coordinate from the input changes
   nothing.
2. **Every threshold is an argument.** The defaults were measured (see
   `claude/task-16-events-report.md`), and a measurement taken on one
   library is not a constant, so the tests pass explicit thresholds
   wherever the value is what is under test.
3. **A wrong date is worse than no date.** Anything that cannot be dated
   has to end up in `undated`, and never in a plausible-looking event.

Everything lives in this one file on purpose: task 12 is editing
`test_storage.py` on `main` while this branch is written, and a new file
merges without a conflict.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from dupecleaner import events as ev
from dupecleaner import origin as origin_module
from dupecleaner.jobs import ScanJob
from dupecleaner.models import FileRecord, MediaKind, ScanMode
from dupecleaner.storage import ScanIndex

HOUR = 3600.0


def wall(year=2020, month=6, day=15, hour=12, minute=0, second=0) -> float:
    """Wall-clock seconds, the scale everything in events.py lives on."""
    return float(calendar.timegm(dt.datetime(year, month, day, hour, minute, second).timetuple()))


def moment(path: str, when: float | None, lat=None, lon=None, source=ev.TimeSource.EXIF):
    return ev.PhotoMoment(
        display_path=path,
        taken_at=when,
        time_source=source if when is not None else ev.TimeSource.NONE,
        latitude=lat,
        longitude=lon,
        geo_source=ev.GeoSource.EXIF if lat is not None else ev.GeoSource.NONE,
    )


def strip_geo(moments):
    return [replace(m, latitude=None, longitude=None, geo_source=ev.GeoSource.NONE) for m in moments]


# --- rule 1: the time backbone --------------------------------------------


def test_short_gaps_stay_in_one_event():
    base = wall()
    moments = [moment(f"a{i}.jpg", base + i * 600) for i in range(5)]
    clustering = ev.cluster_events(moments)
    assert len(clustering.events) == 1
    assert clustering.events[0].size == 5
    assert clustering.events[0].boundary is None  # the first event starts for no reason


def test_gap_at_the_threshold_splits_and_just_below_it_does_not():
    """The boundary condition itself: `>=`, not `>`.

    Written as two clusterings over the same shape because an off-by-one
    here is invisible in aggregate numbers — it would move a few thousand
    photos on a real library and never show up as an error.
    """
    base = wall()
    gap = 9 * HOUR
    at = [moment("a.jpg", base), moment("b.jpg", base + gap)]
    below = [moment("a.jpg", base), moment("b.jpg", base + gap - 1)]
    assert len(ev.cluster_events(at).events) == 2
    assert len(ev.cluster_events(below).events) == 1


def test_time_rule_needs_no_coordinates_at_all():
    """The main path: no GPS anywhere, and the answer is unchanged.

    If this ever fails, the clusterer has started depending on geography,
    and half of the real library would stop being clustered.
    """
    base = wall()
    located = [
        moment("a.jpg", base, 55.0, 82.9),
        moment("b.jpg", base + 60, 55.0, 82.9),
        moment("c.jpg", base + 20 * HOUR, 55.0, 82.9),
    ]
    with_geo = ev.cluster_events(located)
    without_geo = ev.cluster_events(strip_geo(located))
    assert [e.size for e in with_geo.events] == [e.size for e in without_geo.events] == [2, 1]


def test_boundary_reason_is_attached_to_the_event_that_starts():
    base = wall()
    clustering = ev.cluster_events(
        [moment("a.jpg", base), moment("b.jpg", base + 30 * HOUR)]
    )
    first, second = clustering.events
    assert first.boundary is None
    assert second.boundary is not None
    assert second.boundary.rule == "time_gap"
    assert "разрыв" in second.boundary.reason
    assert second.boundary.gap_seconds == pytest.approx(30 * HOUR)


def test_equal_timestamps_do_not_reorder_between_runs():
    """A burst (or a copy) gives several photos the same timestamp. If the
    order of those wobbled, the event's first photo would wobble, and with
    it the date and the name task 20 derives from it."""
    base = wall()
    first = ev.cluster_events([moment("b.jpg", base), moment("a.jpg", base)])
    second = ev.cluster_events([moment("a.jpg", base), moment("b.jpg", base)])
    assert first.events[0].paths == second.events[0].paths == ["a.jpg", "b.jpg"]


# --- rule 2: a change of place --------------------------------------------


def test_moving_far_enough_splits_an_event_time_alone_would_keep():
    base = wall()
    moments = [
        moment("a.jpg", base, 55.00, 82.90),
        moment("b.jpg", base + 2 * HOUR, 55.20, 83.30),  # ~30 km away
    ]
    clustering = ev.cluster_events(moments)
    assert len(clustering.events) == 2
    boundary = clustering.events[1].boundary
    assert boundary.rule == "place_change"
    assert boundary.distance_m > 3000
    # ...and the same pair without coordinates stays one event, which is
    # what "geography only ever adds boundaries" means.
    assert len(ev.cluster_events(strip_geo(moments)).events) == 1


def test_a_short_hop_does_not_split():
    """GPS noise and a walk around the block are not a change of place."""
    base = wall()
    moments = [
        moment("a.jpg", base, 55.0000, 82.9000),
        moment("b.jpg", base + 2 * HOUR, 55.0040, 82.9040),  # ~500 m
    ]
    assert len(ev.cluster_events(moments).events) == 1


def test_distance_without_enough_time_does_not_split():
    """Two shots ten minutes apart from a moving car are one event, however
    far apart the coordinates say they are.

    Ten minutes and 30 km, not one minute and 30 km, and the difference
    matters: at one minute the pair is also travelling at 1800 km/h, so the
    implausible-speed guard would discard the distance and the test would
    pass without rule 2's time condition existing at all. Found by
    deliberately deleting that condition and watching every test still
    pass.
    """
    base = wall()
    moments = [
        moment("a.jpg", base, 55.00, 82.90),
        moment("b.jpg", base + 600, 55.20, 83.30),   # ~30 km at 180 km/h
    ]
    assert len(ev.cluster_events(moments).events) == 1
    # And with the geo gap lowered, the very same pair does split — so the
    # assertion above is about the threshold, not about the distance.
    permissive = replace(ev.DEFAULT_THRESHOLDS, geo_gap_seconds=300)
    assert len(ev.cluster_events(moments, thresholds=permissive).events) == 2


def test_place_rule_needs_both_positions():
    base = wall()
    moments = [
        moment("a.jpg", base, 55.00, 82.90),
        moment("b.jpg", base + 2 * HOUR),  # no position
    ]
    assert len(ev.cluster_events(moments).events) == 1


def test_place_radius_is_a_parameter_not_a_constant():
    base = wall()
    moments = [
        moment("a.jpg", base, 55.000, 82.900),
        moment("b.jpg", base + 2 * HOUR, 55.018, 82.900),  # ~2 km
    ]
    assert len(ev.cluster_events(moments).events) == 1
    tighter = replace(ev.DEFAULT_THRESHOLDS, place_radius_m=1000)
    assert len(ev.cluster_events(moments, thresholds=tighter).events) == 2


# --- rule 3: geography removing a boundary --------------------------------


def test_same_day_same_place_survives_a_long_gap():
    """The wedding with five hours between the registry office and dinner."""
    base = wall(hour=9)
    moments = [
        moment("a.jpg", base, 55.0, 82.9),
        moment("b.jpg", base + 10 * HOUR, 55.001, 82.9),  # same evening, same venue
    ]
    assert len(ev.cluster_events(moments).events) == 1
    strict = replace(ev.DEFAULT_THRESHOLDS, keep_same_day_same_place=False)
    assert len(ev.cluster_events(moments, thresholds=strict).events) == 2


def test_same_place_on_a_different_day_is_still_two_events():
    """Otherwise every evening at home would merge into one endless album —
    the failure this rule is deliberately narrow to avoid."""
    base = wall(hour=22)
    moments = [
        moment("a.jpg", base, 55.0, 82.9),
        moment("b.jpg", base + 10 * HOUR, 55.0, 82.9),  # 08:00 next morning
    ]
    assert len(ev.cluster_events(moments).events) == 2


def test_same_day_far_apart_still_splits():
    base = wall(hour=8)
    moments = [
        moment("a.jpg", base, 55.0, 82.9),
        moment("b.jpg", base + 10 * HOUR, 56.0, 84.0),
    ]
    assert len(ev.cluster_events(moments).events) == 2


# --- broken data ----------------------------------------------------------


def test_impossible_speed_is_a_warning_not_a_boundary():
    """A 500 km jump in four minutes is a broken coordinate or a broken
    clock. Inventing an event boundary out of it would be worse than
    missing one, so the pair is decided on time alone and recorded."""
    base = wall()
    moments = [
        moment("a.jpg", base, 55.0, 82.9),
        moment("b.jpg", base + 240, 60.0, 90.0),
    ]
    clustering = ev.cluster_events(moments)
    assert len(clustering.events) == 1
    assert len(clustering.warnings) == 1
    assert "Невозможная скорость" in clustering.warnings[0]


def test_impossible_speed_does_not_suppress_the_time_rule():
    base = wall()
    moments = [
        moment("a.jpg", base, 55.0, 82.9),
        moment("b.jpg", base + 40 * HOUR, 55.0, 82.9),
    ]
    # 40 hours apart at the same place, two days: still two events, no warning
    clustering = ev.cluster_events(moments)
    assert len(clustering.events) == 2
    assert clustering.warnings == []


def test_undated_photos_go_to_their_own_list_and_never_into_an_event():
    base = wall()
    moments = [moment("a.jpg", base), moment("nodate.jpg", None)]
    clustering = ev.cluster_events(moments)
    assert clustering.clustered_photos == 1
    assert [m.display_path for m in clustering.undated] == ["nodate.jpg"]
    assert all("nodate.jpg" not in e.paths for e in clustering.events)


def test_nothing_datable_at_all_is_not_an_error():
    clustering = ev.cluster_events([moment("a.jpg", None), moment("b.jpg", None)])
    assert clustering.events == []
    assert len(clustering.undated) == 2
    assert clustering.summary()["events"] == 0


# --- Р3: what stays out of albums -----------------------------------------


def test_screenshots_are_excluded_and_counted_not_dropped():
    base = wall()
    moments = [
        moment("photo.jpg", base),
        moment("Screenshot_20200615_120000.png", base + 60),
    ]
    clustering = ev.cluster_events(
        moments, excluded_paths=["Screenshot_20200615_120000.png"]
    )
    assert clustering.clustered_photos == 1
    assert clustering.excluded == ["Screenshot_20200615_120000.png"]


def test_an_excluded_photo_does_not_bridge_two_events():
    """Exclusion happens before clustering, not after. If a screenshot were
    dropped from the result but still used to compute gaps, thousands of
    them would quietly glue neighbouring events together — the exact
    failure Р3 excludes them to prevent."""
    base = wall()
    moments = [
        moment("a.jpg", base),
        moment("shot.png", base + 10 * HOUR),
        moment("b.jpg", base + 20 * HOUR),
    ]
    clustering = ev.cluster_events(moments, excluded_paths=["shot.png"])
    assert [e.size for e in clustering.events] == [1, 1]
    assert clustering.events[1].boundary.gap_seconds == pytest.approx(20 * HOUR)


# --- confidence -----------------------------------------------------------


def test_camera_timed_event_is_high_confidence():
    base = wall()
    clustering = ev.cluster_events([moment(f"a{i}.jpg", base + i * 60) for i in range(4)])
    assert clustering.events[0].confidence is ev.EventConfidence.HIGH


def test_mtime_dominated_event_is_low_confidence():
    """The phantom-event guard: 889 photos "taken" in 36 minutes on the day
    the folder was copied looked exactly like a very busy afternoon. It is
    still shown, but it is labelled."""
    base = wall()
    moments = [
        moment(f"a{i}.jpg", base + i * 60, source=ev.TimeSource.MTIME) for i in range(4)
    ]
    assert ev.cluster_events(moments).events[0].confidence is ev.EventConfidence.LOW


def test_filename_timed_event_is_medium_confidence():
    base = wall()
    moments = [
        moment(f"a{i}.jpg", base + i * 60, source=ev.TimeSource.FILENAME) for i in range(4)
    ]
    assert ev.cluster_events(moments).events[0].confidence is ev.EventConfidence.MEDIUM


# --- geometry and reporting ----------------------------------------------


def test_haversine_matches_a_known_distance():
    # One degree of latitude is 111.2 km, near enough for a 3 km threshold.
    assert ev.haversine_m(55.0, 82.9, 56.0, 82.9) == pytest.approx(111_195, rel=0.01)


def test_centroid_and_radius_ignore_photos_without_a_position():
    base = wall()
    clustering = ev.cluster_events(
        [
            moment("a.jpg", base, 55.0, 82.9),
            moment("b.jpg", base + 60),
            moment("c.jpg", base + 120, 55.002, 82.9),
        ]
    )
    event = clustering.events[0]
    assert event.geo_known == 2
    assert event.centroid == pytest.approx((55.001, 82.9))
    assert event.radius_m == pytest.approx(111, rel=0.05)


def test_event_serialises_with_its_reason_and_sources():
    base = wall()
    clustering = ev.cluster_events([moment("a.jpg", base), moment("b.jpg", base + 30 * HOUR)])
    payload = clustering.events[1].to_dict()
    assert payload["size"] == 1
    assert payload["boundary"]["rule"] == "time_gap"
    assert payload["time_sources"] == {"exif": 1}
    assert payload["start_date"] == payload["end_date"]
    json.dumps(payload)  # must survive the trip to a file and to the web layer


def test_gap_sensitivity_never_increases_with_the_threshold():
    """The table that makes the default defensible. A larger threshold can
    only ever merge, so a rise here would mean the clustering is not a
    function of the gaps alone."""
    base = wall()
    moments = [moment(f"a{i}.jpg", base + i * 5 * HOUR) for i in range(12)]
    table = ev.gap_sensitivity(moments, [h * HOUR for h in (2, 4, 6, 9, 12, 24)])
    counts = [table[h * HOUR] for h in (2, 4, 6, 9, 12, 24)]
    assert counts == sorted(counts, reverse=True)


# --- stage 2: trips ------------------------------------------------------


def test_trips_merge_neighbouring_days_in_the_same_place():
    base = wall()
    moments = [
        moment("d1a.jpg", base, 41.7, 44.8),
        moment("d1b.jpg", base + 3600, 41.7, 44.8),
        moment("d2a.jpg", base + 24 * HOUR, 41.71, 44.81),
        moment("d2b.jpg", base + 25 * HOUR, 41.71, 44.81),
    ]
    clustering = ev.cluster_events(moments)
    assert len(clustering.events) == 2
    trips = ev.merge_into_trips(clustering)
    assert len(trips) == 1
    assert trips[0].size == 4


def test_trips_never_merge_without_coordinates():
    """Without this, "consecutive and less than thirty hours apart" would
    swallow every ordinary week at home into one album."""
    base = wall()
    moments = [
        moment("d1.jpg", base),
        moment("d2.jpg", base + 24 * HOUR),
        moment("d3.jpg", base + 48 * HOUR),
    ]
    clustering = ev.cluster_events(moments)
    trips = ev.merge_into_trips(clustering)
    assert len(trips) == len(clustering.events) == 3


def test_trips_do_not_merge_across_a_long_pause():
    base = wall()
    moments = [
        moment("a.jpg", base, 41.7, 44.8),
        moment("b.jpg", base + 20 * 24 * HOUR, 41.7, 44.8),
    ]
    clustering = ev.cluster_events(moments)
    assert len(ev.merge_into_trips(clustering)) == 2


# --- where a timestamp comes from ----------------------------------------


@pytest.mark.parametrize(
    "name, expected_source",
    [
        ("20170416_145106.jpg", ev.TimeSource.FILENAME),          # Samsung
        ("IMG_20200101_120000.jpg", ev.TimeSource.FILENAME),      # Android
        ("VID_20190101_120000.mp4", ev.TimeSource.FILENAME),      # video
        ("2021-07-10 08-06-58.JPG", ev.TimeSource.FILENAME),      # Google Photos rename
        ("WhatsApp Image 2024-01-02 at 10.11.12.jpeg", ev.TimeSource.FILENAME),
        ("Screenshot_20240812_204426_Instagram.jpg", ev.TimeSource.FILENAME),
        ("IMG-20240101-WA0001.jpg", ev.TimeSource.FILENAME_DATE),  # date only
        ("photo_5@10-11-2024.jpg", ev.TimeSource.FILENAME_DATE),   # Telegram, DD-MM-YYYY
        ("20240101_224608(0).jpg", ev.TimeSource.FILENAME),        # Google's collision suffix
        ("IMG_20170426_132143114-PHOTO_FRAME.jpg", ev.TimeSource.FILENAME),  # with milliseconds
        ("IMG_20190620_112929782.jpg", ev.TimeSource.FILENAME),     # same, straight from Android
        ("IMG_1234.JPG", ev.TimeSource.NONE),                      # camera name, no date in it
        ("a1b2c3d4e5f6a7b8c9d0e1f2.jpg", ev.TimeSource.NONE),      # CDN hash: shaped like a date
        ("20179999_999999.jpg", ev.TimeSource.NONE),               # not a real calendar date
        ("19700101_000000.jpg", ev.TimeSource.NONE),               # outside the plausible range
    ],
)
def test_filenames_that_do_and_do_not_carry_a_date(name, expected_source):
    """Names taken from the real library, not invented — a rule nobody has
    files for cannot be checked. The negatives matter more than the
    positives: a 24-hex-digit CDN name has the shape of a timestamp, and
    reading one as a date would file the photo into a year it has nothing
    to do with."""
    when, source = ev.parse_filename_time(name)
    assert source is expected_source
    assert (when is None) == (expected_source is ev.TimeSource.NONE)


def test_a_date_only_filename_lands_at_noon():
    """Midnight would drag the photo into the previous evening's event and
    guarantee a twelve-hour error; noon bounds the error at twelve hours in
    either direction."""
    when, source = ev.parse_filename_time("IMG-20240101-WA0001.jpg")
    assert source is ev.TimeSource.FILENAME_DATE
    assert dt.datetime.utcfromtimestamp(when).hour == 12


def test_exif_beats_the_filename_and_the_filename_beats_mtime(tmp_path):
    """Precedence, end to end over a real file with real EXIF."""
    photo = tmp_path / "20200101_010101.jpg"
    exif = Image.Exif()
    exif[0x0110] = "TestModel"
    exif.get_ifd(0x8769)[0x9003] = "2019:06:15 14:51:06"
    Image.new("RGB", (32, 24)).save(photo, exif=exif)

    signals = origin_module.read_signals(photo)
    from_exif = ev.moment_from_signals(signals, mtime=wall(2026, 9, 13))
    assert from_exif.time_source is ev.TimeSource.EXIF
    assert from_exif.taken_at == wall(2019, 6, 15, 14, 51, 6)

    # Same filename, no EXIF at all -> the filename answers.
    plain = tmp_path / "20200101_010101b.jpg"
    Image.new("RGB", (32, 24)).save(plain)
    from_name = ev.moment_from_signals(
        origin_module.read_signals(plain), mtime=wall(2026, 9, 13)
    )
    assert from_name.time_source is ev.TimeSource.FILENAME
    assert from_name.taken_at == wall(2020, 1, 1, 1, 1, 1)


def test_mtime_is_not_used_unless_asked(tmp_path):
    """The measured default: on the real library mtime is off by a median
    of 2288 days, so a photo with nothing else is undated, not misdated."""
    plain = tmp_path / "no-date-anywhere.jpg"
    Image.new("RGB", (32, 24)).save(plain)
    signals = origin_module.read_signals(plain)

    default = ev.moment_from_signals(signals, mtime=wall(2026, 9, 13))
    assert default.taken_at is None
    assert default.time_source is ev.TimeSource.NONE

    asked = ev.moment_from_signals(
        signals,
        mtime=wall(2026, 9, 13),
        policy=ev.MomentPolicy(use_mtime=True, utc_offset_seconds=0),
    )
    assert asked.time_source is ev.TimeSource.MTIME
    assert asked.taken_at == wall(2026, 9, 13)


def test_gps_is_read_from_the_exif_sub_ifd(tmp_path):
    """Both halves of the fix task 16 needed: DateTimeOriginal and the GPS
    block live in sub-IFDs, and the old code looked for the timestamp in
    IFD0 where it never is."""
    photo = tmp_path / "located.jpg"
    exif = Image.Exif()
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (55.0, 1.0, 30.0)
    gps[3], gps[4] = "E", (82.0, 55.0, 0.0)
    exif.get_ifd(0x8769)[0x9003] = "2021:07:10 08:06:58"
    Image.new("RGB", (32, 24)).save(photo, exif=exif)

    signals = origin_module.read_signals(photo)
    assert signals.has_exif_datetime
    assert signals.gps_latitude == pytest.approx(55.025)
    assert signals.gps_longitude == pytest.approx(82.9167, rel=1e-4)
    result = ev.moment_from_signals(signals, mtime=0)
    assert result.geo_source is ev.GeoSource.EXIF


def test_southern_and_western_hemispheres_get_their_sign(tmp_path):
    photo = tmp_path / "rio.jpg"
    exif = Image.Exif()
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "S", (22.0, 54.0, 0.0)
    gps[3], gps[4] = "W", (43.0, 10.0, 0.0)
    Image.new("RGB", (32, 24)).save(photo, exif=exif)
    signals = origin_module.read_signals(photo)
    assert signals.gps_latitude < 0 and signals.gps_longitude < 0


def test_a_zeroed_gps_block_is_not_a_place_in_the_atlantic(tmp_path):
    """A stripped position often survives as an exact 0/0 pair. Reading it
    as a location would invent a 6000 km move out of missing data."""
    photo = tmp_path / "stripped.jpg"
    exif = Image.Exif()
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (0.0, 0.0, 0.0)
    gps[3], gps[4] = "E", (0.0, 0.0, 0.0)
    Image.new("RGB", (32, 24)).save(photo, exif=exif)
    signals = origin_module.read_signals(photo)
    assert signals.gps_latitude is None and signals.gps_longitude is None


# --- Google sidecars (finding A3) ----------------------------------------


def test_google_sidecar_supplies_the_time_and_the_place(tmp_path):
    """Finding A3: where Google stripped the EXIF, the neighbouring JSON
    still has both. `read_google_sidecar` already read the time; task 16
    needed the coordinates out of the same file rather than a second
    reader."""
    photo = tmp_path / "20240101_224608.jpg"
    Image.new("RGB", (32, 24)).save(photo)
    (tmp_path / "20240101_224608.jpg.json").write_text(
        json.dumps(
            {
                "photoTakenTime": {"timestamp": "1704142000"},
                "geoData": {"latitude": 41.7151, "longitude": 44.8271},
                "googlePhotosOrigin": {"mobileUpload": {}},
            }
        ),
        encoding="utf-8",
    )
    origin_module._dir_has_sidecars.cache_clear()

    signals = origin_module.read_signals(photo)
    assert signals.sidecar is not None
    assert signals.sidecar.latitude == pytest.approx(41.7151)

    result = ev.moment_from_signals(
        signals, mtime=0, policy=ev.MomentPolicy(utc_offset_seconds=7 * HOUR)
    )
    # The filename is present and parseable, so this also pins the
    # precedence: Google's own record of the capture time wins over a name
    # a renamer produced.
    assert result.time_source is ev.TimeSource.SIDECAR
    assert result.taken_at == pytest.approx(1704142000 + 7 * HOUR)
    assert result.geo_source is ev.GeoSource.SIDECAR


def test_sidecar_zero_coordinates_are_absent_not_a_location(tmp_path):
    photo = tmp_path / "shot.jpg"
    Image.new("RGB", (32, 24)).save(photo)
    (tmp_path / "shot.jpg.json").write_text(
        json.dumps({"geoData": {"latitude": 0.0, "longitude": 0.0}}), encoding="utf-8"
    )
    origin_module._dir_has_sidecars.cache_clear()
    sidecar = origin_module.read_google_sidecar(photo)
    assert sidecar is not None
    assert sidecar.latitude is None and sidecar.has_geo is False


# --- one clock: the mixing trap ------------------------------------------


def test_epoch_sources_are_put_on_the_same_clock_as_exif(tmp_path):
    """The trap worth a test of its own: EXIF is local wall time with no
    zone, a sidecar timestamp is a true UTC epoch. Mixed unconverted, half
    a library shifts by the UTC offset — seven hours here, which is
    larger than the 9-hour session threshold and would both invent and
    erase boundaries.

    Two photos taken one minute apart, one timed by EXIF and one by a
    sidecar, must land in the same event.
    """
    policy = ev.MomentPolicy(utc_offset_seconds=7 * HOUR)

    with_exif = tmp_path / "a.jpg"
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = "2024:01:02 07:00:00"   # local wall clock
    Image.new("RGB", (32, 24)).save(with_exif, exif=exif)

    with_sidecar = tmp_path / "b.jpg"
    Image.new("RGB", (32, 24)).save(with_sidecar)
    epoch = calendar.timegm(dt.datetime(2024, 1, 2, 0, 1, 0).timetuple())  # 07:01 local
    (tmp_path / "b.jpg.json").write_text(
        json.dumps({"photoTakenTime": {"timestamp": str(epoch)}}), encoding="utf-8"
    )
    origin_module._dir_has_sidecars.cache_clear()

    moments = [
        ev.moment_from_signals(origin_module.read_signals(p), mtime=0, policy=policy)
        for p in (with_exif, with_sidecar)
    ]
    assert abs(moments[0].taken_at - moments[1].taken_at) == pytest.approx(60)
    assert len(ev.cluster_events(moments).events) == 1


def test_utc_offset_is_declared_not_guessed():
    policy = ev.MomentPolicy(utc_offset_seconds=7 * HOUR)
    assert policy.to_wall_clock(0) == 7 * HOUR


# --- the index round trip ------------------------------------------------


def photo_record(path: str, size: int = 10, mtime: float = 1000.0, kind=MediaKind.PHOTO):
    return FileRecord(
        display_path=path, real_path=path, size=size, mtime=mtime, media_kind=kind
    )


def test_moment_survives_the_index(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_files([photo_record("a.jpg", mtime=555.0)], "s1")
        index.set_moment("a.jpg", wall(), "exif", 55.0, 82.9, "exif")
        index.commit()
        rows = index.moments("s1")
    assert rows == [("a.jpg", wall(), "exif", 55.0, 82.9, "exif", 555.0)]
    restored = ev.moments_from_rows(rows)
    assert restored[0].time_source is ev.TimeSource.EXIF
    assert restored[0].has_geo


def test_needs_moment_stops_asking_and_asks_again_when_the_file_changes(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_files([photo_record("a.jpg")], "s1")
        assert [r.display_path for r in index.needs_moment("s1")] == ["a.jpg"]
        index.set_moment("a.jpg", None, "none")
        assert index.needs_moment("s1") == []
        # A photo with no date at all is a legitimate answer, so the stamp
        # and not the value is what records that the work was done.
        index.upsert_files([photo_record("a.jpg", size=99)], "s1")
        assert [r.display_path for r in index.needs_moment("s1")] == ["a.jpg"]


def test_needs_header_covers_videos_but_never_asks_them_for_an_origin(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_files(
            [photo_record("clip.mp4", kind=MediaKind.VIDEO), photo_record("a.jpg")], "s1"
        )
        assert {r.display_path for r in index.needs_header("s1")} == {"clip.mp4", "a.jpg"}
        # Р3 classifies images; a video only ever needs a moment.
        assert [r.display_path for r in index.needs_origin("s1")] == ["a.jpg"]
        index.set_moment("clip.mp4", wall(), "filename")
        index.set_moment("a.jpg", wall(), "exif")
        index.set_origin("a.jpg", "camera", "high", [])
        assert index.needs_header("s1") == []


def test_archive_members_are_never_asked_for_a_moment(tmp_path):
    """Reading EXIF from inside an archive is finding A2 coming back
    through a side door — a sequential pass per member."""
    member = FileRecord(
        display_path="a.zip::in/x.jpg",
        real_path="a.zip",
        size=10,
        mtime=1.0,
        media_kind=MediaKind.PHOTO,
        is_archive_member=True,
        archive_path="a.zip",
        member_name="in/x.jpg",
    )
    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_files([member, photo_record("loose.jpg")], "s1")
        assert [r.display_path for r in index.needs_header("s1")] == ["loose.jpg"]
        assert [r.display_path for r in index.needs_moment("s1")] == ["loose.jpg"]


def test_excluded_from_albums_is_wider_than_screenshots(tmp_path):
    """Р3 excludes screenshots; Р5 gives document scans their own tree, so
    they are out of the year/event hierarchy too. One SQL definition, taken
    from `OriginClass`, rather than a second list that drifts."""
    with ScanIndex(tmp_path / "i.db") as index:
        for name, origin in (
            ("shot.png", "screenshot_phone"),
            ("desk.png", "screenshot_desktop"),
            ("scan.jpg", "document_scan"),
            ("photo.jpg", "camera"),
        ):
            index.upsert_files([photo_record(name)], "s1")
            index.set_origin(name, origin, "high", [])
        assert index.screenshot_paths("s1") == ["desk.png", "shot.png"]
        assert index.excluded_from_albums_paths("s1") == ["desk.png", "scan.jpg", "shot.png"]


def test_moment_coverage_counts_sources_and_positions(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_files([photo_record("a.jpg"), photo_record("b.jpg")], "s1")
        index.set_moment("a.jpg", wall(), "exif", 55.0, 82.9, "exif")
        index.set_moment("b.jpg", None, "none")
        coverage = index.moment_coverage("s1")
    assert coverage["exif"] == 1
    assert coverage["none"] == 1
    assert coverage["with_geo"] == 1


def test_latest_scan_id_answers_when_nothing_has_been_scanned(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        assert index.latest_scan_id() is None
        index.upsert_files([photo_record("a.jpg")], "s1")
        assert index.latest_scan_id() == "s1"


def test_mtime_fallback_is_applied_when_clustering_not_when_storing(tmp_path):
    """`--use-mtime` must cost a re-cluster, not a re-scan: the decision is
    about one library, and the row already holds everything needed to
    change your mind."""
    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_files([photo_record("x.jpg", mtime=wall(2026, 9, 13))], "s1")
        index.set_moment("x.jpg", None, "none")
        rows = index.moments("s1")

    default = ev.moments_from_rows(rows)
    assert default[0].taken_at is None
    with_mtime = ev.moments_from_rows(
        rows, policy=ev.MomentPolicy(use_mtime=True, utc_offset_seconds=0)
    )
    assert with_mtime[0].time_source is ev.TimeSource.MTIME
    assert with_mtime[0].taken_at == wall(2026, 9, 13)


# --- the scan phase ------------------------------------------------------


def _library(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    exif[0x0110] = "TestModel"
    exif.get_ifd(0x8769)[0x9003] = "2019:06:15 14:51:06"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (55.0, 1.0, 30.0)
    gps[3], gps[4] = "E", (82.0, 55.0, 0.0)
    Image.new("RGB", (48, 36), (10, 20, 30)).save(root / "IMG_0001.jpg", exif=exif)
    Image.new("RGB", (48, 36), (30, 20, 10)).save(root / "20200101_120000.jpg")
    (root / "VID_20190101_120000.mp4").write_bytes(b"not really a video, only a name")


def test_a_full_scan_fills_in_when_and_where(tmp_path):
    """End to end through the real phase: one header read per file, both
    answers written, videos included without opening anything."""
    root = tmp_path / "lib"
    _library(root)
    job = ScanJob(
        roots=[str(root)],
        db_path=tmp_path / "i.db",
        mode=ScanMode.FULL,
        moment_policy=ev.MomentPolicy(utc_offset_seconds=0),
    )
    job.run()
    assert job.report is not None

    with ScanIndex(tmp_path / "i.db") as index:
        rows = {r[0]: r for r in index.moments(job.scan_id)}
        coverage = index.moment_coverage(job.scan_id)

    exif_row = next(r for p, r in rows.items() if p.endswith("IMG_0001.jpg"))
    assert exif_row[1] == wall(2019, 6, 15, 14, 51, 6)
    assert exif_row[2] == "exif"
    assert exif_row[3] == pytest.approx(55.025)

    name_row = next(r for p, r in rows.items() if p.endswith("20200101_120000.jpg"))
    assert (name_row[1], name_row[2]) == (wall(2020, 1, 1, 12), "filename")

    video_row = next(r for p, r in rows.items() if p.endswith(".mp4"))
    assert (video_row[1], video_row[2]) == (wall(2019, 1, 1, 12), "filename")

    assert coverage["exif"] == 1 and coverage["filename"] == 2
    assert coverage["with_geo"] == 1


def test_a_quick_scan_stores_no_moments_at_all(tmp_path):
    """Р7: the header phase belongs to full processing. A quick run must
    not half-fill the table, or `events` would cluster a third of the
    library and look like it had clustered all of it."""
    root = tmp_path / "lib"
    _library(root)
    job = ScanJob(roots=[str(root)], db_path=tmp_path / "i.db", mode=ScanMode.QUICK)
    job.run()

    with ScanIndex(tmp_path / "i.db") as index:
        rows = index.moments(job.scan_id)
        assert rows and all(row[1] is None and row[2] == "none" for row in rows)
        assert index.needs_moment(job.scan_id)


def test_rerunning_a_full_scan_reads_no_header_twice(tmp_path):
    root = tmp_path / "lib"
    _library(root)
    first = ScanJob(roots=[str(root)], db_path=tmp_path / "i.db", mode=ScanMode.FULL)
    first.run()
    with ScanIndex(tmp_path / "i.db") as index:
        assert index.needs_header(first.scan_id) == []

    second = ScanJob(roots=[str(root)], db_path=tmp_path / "i.db", mode=ScanMode.FULL)
    second.run()
    with ScanIndex(tmp_path / "i.db") as index:
        assert index.needs_header(second.scan_id) == []
        assert len(index.moments(second.scan_id)) == 3


def test_clustering_a_real_scan_end_to_end(tmp_path):
    root = tmp_path / "lib"
    _library(root)
    job = ScanJob(
        roots=[str(root)],
        db_path=tmp_path / "i.db",
        mode=ScanMode.FULL,
        moment_policy=ev.MomentPolicy(utc_offset_seconds=0),
    )
    job.run()
    with ScanIndex(tmp_path / "i.db") as index:
        moments = ev.moments_from_rows(index.moments(job.scan_id))
        excluded = index.excluded_from_albums_paths(job.scan_id)

    clustering = ev.cluster_events(moments, excluded_paths=excluded)
    # 2019-01-01, 2019-06-15 and 2020-01-01: three events, in order.
    assert [e.date_range[0].isoformat() for e in clustering.events] == [
        "2019-01-01",
        "2019-06-15",
        "2020-01-01",
    ]
    assert clustering.undated == []
