"""Turn `probe_events.py`'s JSONL into the numbers that pick the thresholds.

Pure analysis: reads one file, writes stdout, touches no library. Every
table `claude/task-16-events-report.md` quotes is printed here, in the same
order, so the report can be checked rather than believed — and re-derived
after a rule changes without going back to the disk (see probe_common).

    python pilot/analyze_events.py <probe.jsonl>

Two things to know when reading the output:

* **Ground truth is a person's own folders.** Where two neighbouring photos
  sit in the same folder that a human named, on the same calendar day, that
  human has said "one occasion"; where they sit in different ones, they have
  said less than it looks — two folders can hold one wedding. So the
  precision column (are we cutting things the owner kept together) is
  meaningful and the recall column is not, and the script prints only the
  first as a rate.
* **Dump folders are excluded from that ground truth.** `Pictures`,
  `Photos`, `No date` and the bare-year folders are where everything was
  swept; they say nothing about occasions. Р8's `keeper.classify_segment`
  makes the same distinction for the same reason.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dupecleaner import events as ev  # noqa: E402

from probe_common import moment_for_row  # noqa: E402

HOUR = 3600.0
# Folders that were swept into rather than named: they are not evidence
# about where an occasion ended.
DUMP_FOLDERS = {"Pictures", "Photos", "No date", "DCIM", "Camera", "collages", "GIF"}


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def occasion_key(path: str, moment: ev.PhotoMoment):
    """The owner's own answer to "is this one occasion", or None."""
    folder = Path(path.replace("\\", "/")).parent.name
    if folder in DUMP_FOLDERS or folder.strip().isdigit():
        return None
    return (folder, moment.local_date())


def main(argv: list[str]) -> int:
    rows = [json.loads(l) for l in Path(argv[1]).open(encoding="utf-8") if l.strip()]
    offset = float(os.environ.get("PROBE_UTC_OFFSET_HOURS", "7")) * HOUR
    policy = ev.MomentPolicy(utc_offset_seconds=offset)
    moments = [moment_for_row(row, policy) for row in rows]
    excluded = {r["display_path"] for r in rows if r.get("excluded_from_albums")}
    base = ev.DEFAULT_THRESHOLDS

    section(f"1. Что известно о {len(rows)} медиафайлах")
    print("тип:", dict(Counter(r["kind"] for r in rows)))
    print("время:", dict(Counter(m.time_source.value for m in moments).most_common()))
    print("гео:", dict(Counter(m.geo_source.value for m in moments).most_common()))
    print("происхождение:", dict(Counter(r.get("origin", "видео") for r in rows).most_common()))
    print(f"исключено из альбомов (Р3): {len(excluded)}")
    print("сайдкаров Google:", sum(1 for r in rows if r.get("sidecar")))
    print(
        "EXIF OffsetTimeOriginal (зона прямо в файле):",
        sum(1 for r in rows if r.get("exif_offset_minutes") is not None),
    )

    section("2. Можно ли верить mtime")
    both = [(r["exif_taken_at"], r["mtime"]) for r in rows if r.get("exif_taken_at") and r.get("mtime")]
    deltas = sorted(abs(exif - policy.to_wall_clock(mtime)) for exif, mtime in both)
    print(f"файлов, где есть и EXIF, и mtime: {len(both)}")
    print(f"  совпадает в пределах минуты: {sum(1 for d in deltas if d <= 60)}")
    print(f"  медиана расхождения: {statistics.median(deltas) / 86400:.1f} сут")
    only_mtime = [m for m in moments if not m.has_time]
    print(f"файлов, у которых нет ничего кроме mtime: {len(only_mtime)}")
    with_mtime = [
        moment_for_row(r, replace(policy, use_mtime=True)) for r in rows
    ]
    phantom = ev.cluster_events(with_mtime, excluded_paths=excluded)
    worst = max(phantom.events, key=lambda e: e.time_source_counts["mtime"])
    print(
        f"  если их всё же датировать, они собираются в одно событие: "
        f"{worst.size} снимков за {worst.duration_seconds / 60:.0f} мин "
        f"({worst.date_range[0]}), уверенность {worst.confidence.value}, "
        f"из них по mtime {worst.time_source_counts['mtime']}"
    )

    section("3. Распределение разрывов между соседними снимками")
    gaps = ev.consecutive_gaps([m for m in moments if m.display_path not in excluded])
    print(f"пар: {len(gaps)}")
    buckets = [
        (0, 60, "< 1 мин"), (60, 300, "1–5 мин"), (300, 1800, "5–30 мин"),
        (1800, HOUR, "30–60 мин"), (HOUR, 2 * HOUR, "1–2 ч"), (2 * HOUR, 4 * HOUR, "2–4 ч"),
        (4 * HOUR, 6 * HOUR, "4–6 ч"), (6 * HOUR, 9 * HOUR, "6–9 ч"),
        (9 * HOUR, 12 * HOUR, "9–12 ч"), (12 * HOUR, 24 * HOUR, "12–24 ч"),
        (24 * HOUR, 72 * HOUR, "1–3 сут"), (72 * HOUR, float("inf"), "> 3 сут"),
    ]
    for low, high, label in buckets:
        n = sum(1 for g in gaps if low <= g < high)
        print(f"  {label:>10}: {n:6d}  {100.0 * n / max(1, len(gaps)):5.2f}%")
    print("\nплотность по часам — где у гистограммы дно, там и режем:")
    for hour in range(1, 25):
        n = sum(1 for g in gaps if (hour - 1) * HOUR <= g < hour * HOUR)
        bar = "#" * min(40, n // 3) if hour > 1 else "#" * 40 + " (обрезано)"
        print(f"  {hour - 1:2d}–{hour:2d} ч: {n:5d} {bar}")

    section("4. Чувствительность числа событий к порогу времени")
    kept = [m for m in moments if m.display_path not in excluded]
    hours = (2, 3, 4, 6, 8, 9, 10, 12, 16, 24)
    table = ev.gap_sensitivity(kept, [h * HOUR for h in hours], base=base)
    previous = None
    for hour in hours:
        n = table[hour * HOUR]
        delta = "" if previous is None else f"  ({100.0 * (n - previous) / previous:+.1f}%)"
        mark = " ← выбрано" if hour * HOUR == base.session_gap_seconds else ""
        print(f"  {hour:>2} ч: {n:5d} событий{delta}{mark}")
        previous = n

    section("5. Сверка с папками, которые человек назвал сам")
    ordered = sorted((m for m in moments if m.has_time and m.display_path not in excluded),
                     key=lambda m: (m.taken_at, m.display_path))
    pairs = []
    for previous_moment, moment in zip(ordered, ordered[1:]):
        first = occasion_key(previous_moment.display_path, previous_moment)
        second = occasion_key(moment.display_path, moment)
        if first is None or second is None:
            continue
        distance = ev.distance_between(previous_moment, moment)
        gap = moment.taken_at - previous_moment.taken_at
        if distance is not None and gap > 0 and (distance / 1000) / (gap / HOUR) > base.max_speed_kmh:
            distance = None
        pairs.append((gap, distance, first == second))
    same = sum(1 for _, _, s in pairs if s)
    print(f"пар, где обе стороны в названной человеком папке: {len(pairs)}")
    print(f"  человек считает одним событием: {same}")
    print(f"  человек считает разными:        {len(pairs) - same}")
    print(f"\n{'порог':>6} {'разрезали лишнего':>19} {'из них доля':>12} {'совпало с человеком':>21}")
    for hour in (2, 3, 4, 6, 8, 9, 10, 12, 16, 24):
        limit = hour * HOUR
        wrong = sum(1 for gap, _, s in pairs if s and gap >= limit)
        right = sum(1 for gap, _, s in pairs if not s and gap >= limit)
        mark = " ←" if limit == base.session_gap_seconds else ""
        print(f"{hour:>4} ч {wrong:>19} {100.0 * wrong / max(1, same):>11.2f}% "
              f"{100.0 * right / max(1, right + wrong):>20.1f}%{mark}")

    section("6. Расстояния: сколько человек проходит внутри одного события")
    inside = defaultdict(list)
    for moment in ordered:
        key = occasion_key(moment.display_path, moment)
        if key is not None and moment.has_geo:
            inside[key].append(moment)
    steps = []
    for group in inside.values():
        if len(group) < 10:
            continue
        group.sort(key=lambda m: m.taken_at)
        clean = []
        for a, b in zip(group, group[1:]):
            distance = ev.distance_between(a, b)
            gap = b.taken_at - a.taken_at
            if distance is None:
                continue
            if gap > 0 and (distance / 1000) / (gap / HOUR) > base.max_speed_kmh:
                continue
            clean.append(distance)
        if clean:
            steps.append(max(clean))
    steps.sort()
    print(f"событий с >=10 снимками с гео: {len(steps)}")
    for percentile in (50, 75, 90, 95):
        print(f"  {percentile}-й перцентиль макс. шага внутри события: "
              f"{steps[int(percentile / 100 * (len(steps) - 1))] / 1000:.2f} км")
    between = sorted(d for gap, d, s in pairs if not s and d is not None)
    if between:
        print(f"между разными событиями, пар {len(between)}:")
        for percentile in (50, 75, 90):
            print(f"  {percentile}-й перцентиль: "
                  f"{between[int(percentile / 100 * (len(between) - 1))] / 1000:.2f} км")

    section("7. Итог при выбранных порогах")
    for line in base.describe():
        print("  ·", line)
    clustering = ev.cluster_events(moments, excluded_paths=excluded)
    print("\nсводка:", clustering.summary())
    print("границы по правилам:",
          dict(Counter(e.boundary.rule for e in clustering.events if e.boundary)))
    no_geo = ev.cluster_events(
        [replace(m, latitude=None, longitude=None, geo_source=ev.GeoSource.NONE) for m in moments],
        excluded_paths=excluded,
    )
    print(f"событий, если убрать все координаты: {len(no_geo.events)} "
          f"(с координатами {len(clustering.events)})")
    strict = ev.cluster_events(
        moments, thresholds=replace(base, keep_same_day_same_place=False), excluded_paths=excluded
    )
    print(f"событий без правила «тот же день, то же место»: {len(strict.events)}")
    print(f"предупреждений о невозможной скорости: {len(clustering.warnings)}")

    section("8. Сколько событий приходится на каждую папку человека")
    per_folder: dict[str, Counter] = defaultdict(Counter)
    for index, event in enumerate(clustering.events):
        for path in event.paths:
            folder = str(Path(path.replace("\\", "/")).parent).replace("D:/Photos/Photos/", "")
            per_folder[folder][index] += 1
    print(f"{'папка':<36} {'снимков':>8} {'событий':>8} {'в крупнейшем':>13}")
    for folder, counter in sorted(per_folder.items(), key=lambda kv: -sum(kv[1].values()))[:16]:
        total = sum(counter.values())
        biggest_share = counter.most_common(1)[0][1]
        print(f"{folder[:36]:<36} {total:>8} {len(counter):>8} {100.0 * biggest_share / total:>12.0f}%")
    mixed = sum(
        1 for e in clustering.events
        if len({str(Path(p.replace("\\", "/")).parent) for p in e.paths}) > 1
    )
    print(f"событий, собравших снимки из нескольких папок: {mixed} из {len(clustering.events)}")
    duplicated = sum(
        1 for e in clustering.events
        if len({Path(p).name for p in e.paths}) < e.size
    )
    print(f"  из них содержат одноимённые файлы (копии одного кадра): {duplicated}")

    section("9. Что осталось за бортом")
    print(f"без даты вовсе: {len(clustering.undated)} (в _unsorted по Р5)")
    for moment in clustering.undated[:8]:
        print("   ", moment.display_path)
    print(f"исключено Р3: {len(clustering.excluded)}")

    section("10. Второй уровень: поездки")
    trips = ev.merge_into_trips(clustering)
    merged = [t for t in trips if len(t.events) > 1]
    print(f"поездок {len(trips)} вместо {len(clustering.events)} событий; "
          f"склеено из нескольких: {len(merged)}")
    for trip in sorted(merged, key=lambda t: -t.size)[:8]:
        first, last = trip.date_range
        print(f"  [{trip.size:5d}] {first} → {last}, событий {len(trip.events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
