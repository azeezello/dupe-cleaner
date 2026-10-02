r"""Suggesting a name for an event (Р4, задача 20).

What this module does, and the line it does not cross
----------------------------------------------------
`events.py` (задача 16) cuts the library into events. Р4 says an event is
an album, and gives the chain a name comes out of:

1. GPS from EXIF → offline geocoder → «Самарканд»
2. the date range → «август 2026»
3. the dominant face cluster, if there is one → «Карим»
4. the folder name the person invented themselves → «Свадьба 2019»
5. nothing else available → the date range alone

Every name this module produces is a **suggestion, confirmed by hand**.
Nothing here renames, moves or writes a file; `albums` does not import
`quarantine`, and the only thing it ever stores is a name a person
explicitly confirmed (`ScanIndex.confirm_album_name`). Building the
library out of events is задача 21, and that one shows its whole plan
before touching a single file.

The fourth link exists only because Р8 stopped quarantining named folders
--------------------------------------------------------------------------
This is worth saying in the module that depends on it. Of the chain's four
sources, three live in the file: GPS and the date are in the EXIF, the face
is in the pixels. The folder name lives nowhere but the folder — it is the
one link with no backup, which is exactly the argument `keeper.py` makes
for Р8's ordering. Before Р8, a duplicate group kept the copy with the
shortest path and sent `Краснодар`, `Wedding Day` and `Pamir 2016` to
quarantine; 64% of groups changed their answer when that was fixed. Every
name below that comes out of a folder is a name that rule preserved.

So the classification of a segment into named / dated / generic is
**`keeper.classify_segment`, called, not reimplemented**. Two copies of
that word list would drift, and the half that drifted would be the half
deciding whether `2019` is a name.

Where the honest answer is a date and nothing else
--------------------------------------------------
Half the library has no coordinates at all (49% on `D:\Photos`, measured
in задача 16), so link 1 is silent on half the events by construction, and
the task-16 report names them: the events timed by filename, the
messenger photos with their EXIF stripped, the videos. Those events get a
date range, and the date range is the whole name. A place invented for
them — the nearest city to *some* photo, a country guessed from a folder —
would be worse than no place, in the specific way задача 16 already
established for timestamps: a photo filed under a place it was not taken
is a photo nobody will find again.

The shape of a suggestion
-------------------------
One event yields several `NameCandidate`s — one per link that fired — and
`AlbumSuggestion.primary` is the one `NamePolicy.subject_order` puts
first. The alternatives are kept rather than discarded because the
ordering is the one genuinely open question here (see
`claude/task-20-album-names-report.md`): Р4 puts the geocoder ahead of the
folder, and on this library that means an event whose photos all sit in
`Wedding 16042017` is offered as «Краснодар, 16 апреля 2017». Confirming a
name is therefore a choice between named candidates, not a yes/no on one
string — which costs nothing and means the open question cannot silently
resolve itself into the wrong default.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .events import EventCluster, EventClustering, PhotoMoment
from .geocode import Gazetteer, Match
from .keeper import SegmentKind, classify_segment
from .persons import person_display_name

#: Genitive, because a name carries a day: «14 мая 2018», not «14 май».
_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
#: Nominative, for a range that is a whole month or more: «август 2026».
_MONTHS_NOMINATIVE = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)

_DRIVE_SUFFIX = ":"


def format_date_range(first: dt.date, last: dt.date) -> str:
    """The date range as a person would write it.

    Four shapes, because repeating the month and the year on both sides of
    a dash is how a machine writes a date: «14 мая 2018», «14–17 мая
    2018», «28 апреля — 3 мая 2021», «29 декабря 2019 — 2 января 2020».
    """
    if first == last:
        return f"{first.day} {_MONTHS_GENITIVE[first.month - 1]} {first.year}"
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}–{last.day} {_MONTHS_GENITIVE[first.month - 1]} {first.year}"
    if first.year == last.year:
        return (
            f"{first.day} {_MONTHS_GENITIVE[first.month - 1]} — "
            f"{last.day} {_MONTHS_GENITIVE[last.month - 1]} {first.year}"
        )
    return (
        f"{first.day} {_MONTHS_GENITIVE[first.month - 1]} {first.year} — "
        f"{last.day} {_MONTHS_GENITIVE[last.month - 1]} {last.year}"
    )


def format_month(date: dt.date) -> str:
    """«август 2026» — Р4's own example of what link 2 produces."""
    return f"{_MONTHS_NOMINATIVE[date.month - 1]} {date.year}"


@dataclass(frozen=True)
class NamePolicy:
    """Every threshold this module leans on, named and passed in — the same
    discipline `events.EventThresholds` and `persons.PersonThresholds`
    keep, and for the same reason: the library that measured a number is
    not the library that will run on it.
    """

    #: Share of an event's *geo-known* photos that must resolve to the same
    #: place before that place may name the event. A consensus over the
    #: photos themselves rather than a lookup of the centroid: a drive
    #: across a region has a centroid in a field halfway along it, and the
    #: centroid of two cities is neither of them.
    place_share: float = 0.6
    #: Below this many photos with coordinates, a place is still used but
    #: the suggestion is marked as resting on thin evidence.
    place_solid_photos: int = 3
    #: Share of the event's photos that must sit under the same
    #: hand-named folder before its name may be the album's.
    folder_share: float = 0.5
    #: Share of the event's photos *that contain any known face* in which
    #: one person must appear.
    person_share: float = 0.5
    #: Which link supplies the subject of the name. The default is Р4's
    #: own order. `("folder", "place", "person")` is the alternative the
    #: task-20 report argues for on this library; it is a tuple rather
    #: than an `if` so that changing the answer is a setting, not a patch.
    subject_order: tuple[str, ...] = ("place", "person", "folder")


DEFAULT_POLICY = NamePolicy()


@dataclass(frozen=True)
class NameCandidate:
    """One link of the chain, fired."""

    source: str          # "place" | "person" | "folder" | "dates"
    subject: str         # what goes before the date range ("" for "dates")
    text: str            # the whole suggested name
    evidence: str        # one line a person can argue with
    strength: float      # share of the event backing it, 0..1
    solid: bool = True   # False when the evidence is thin but real

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "subject": self.subject,
            "text": self.text,
            "evidence": self.evidence,
            "strength": round(self.strength, 3),
            "solid": self.solid,
        }


@dataclass
class AlbumSuggestion:
    """What to offer a person for one event."""

    #: `content_hash` of the event's earliest photo — the key a confirmed
    #: name is stored under. Content, never path: the same choice Р9 made
    #: for thumbnails, Р10 for review decisions and Р12 for person labels,
    #: so a confirmed name survives a rescan, a move into a better folder
    #: and a trip through quarantine. An event is deliberately not stored
    #: (задача 16: thresholds are meant to be changed), so it has no id to
    #: key by; the earliest photo is the one member re-clustering cannot
    #: move to a different event without also changing its date.
    anchor: str
    date_range: str
    candidates: list[NameCandidate] = field(default_factory=list)
    confirmed: str | None = None
    size: int = 0

    @property
    def primary(self) -> NameCandidate:
        return self.candidates[0]

    @property
    def name(self) -> str:
        """The confirmed name if there is one, otherwise the suggestion."""
        return self.confirmed or self.primary.text

    @property
    def alternatives(self) -> list[NameCandidate]:
        return self.candidates[1:]

    def to_dict(self) -> dict:
        return {
            "anchor": self.anchor,
            "date_range": self.date_range,
            "size": self.size,
            "name": self.name,
            "confirmed": self.confirmed,
            "suggested": self.primary.text,
            "source": self.primary.source,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def path_segments(display_path: str) -> list[str]:
    """The folder names above a file, drive letter dropped.

    The string version of `keeper.meaningful_segments`, which needs a whole
    `FileRecord`; an event carries paths, not records. The classification
    itself stays in `keeper.classify_segment` — this only decides what to
    hand it.
    """
    raw = display_path.replace("\\", "/")
    parts = [p for p in raw.split("/") if p][:-1]
    return [p for p in parts if not p.endswith(_DRIVE_SUFFIX)]


def deepest_named_segment(display_path: str) -> str | None:
    """The most specific hand-written folder name on this path.

    Deepest rather than most-specific-by-kind, because `Грузия
    2022-2023/Батуми` is filed inside a name, and the inner one is the one
    that describes these photographs. A path with no NAMED segment at all
    (`D:/Photos/Pictures/2019/x.jpg`) returns None, which is link 4 staying
    silent rather than inventing «Pictures».
    """
    for segment in reversed(path_segments(display_path)):
        if classify_segment(segment) is SegmentKind.NAMED:
            return segment
    return None


def _place_candidate(
    event: EventCluster, gazetteer: Gazetteer | None, policy: NamePolicy, when: str
) -> NameCandidate | None:
    if gazetteer is None:
        return None
    located = [m for m in event.moments if m.has_geo]
    if not located:
        return None

    labels: Counter[str] = Counter()
    example: dict[str, Match] = {}
    for moment in located:
        match = gazetteer.lookup(moment.latitude, moment.longitude)
        if match is None:
            continue
        labels[match.label] += 1
        example.setdefault(match.label, match)
    if not labels:
        return None

    label, count = labels.most_common(1)[0]
    share = count / len(located)
    if share < policy.place_share:
        return None

    match = example[label]
    solid = count >= policy.place_solid_photos
    population = f"{match.place.population:,}".replace(",", " ")
    detail = (
        f"{count} из {len(located)} снимков с координатами — {label} "
        f"({match.distance_m / 1000:.1f} км от центра, население {population})"
    )
    if not solid:
        detail += "; координаты есть у единиц — место правдоподобно, но не доказано"
    return NameCandidate(
        source="place",
        subject=label,
        text=f"{label}, {when}",
        evidence=detail,
        strength=share,
        solid=solid,
    )


def _folder_candidate(
    event: EventCluster, policy: NamePolicy, when: str
) -> NameCandidate | None:
    names: Counter[str] = Counter()
    for moment in event.moments:
        segment = deepest_named_segment(moment.display_path)
        if segment:
            names[segment] += 1
    if not names:
        return None
    name, count = names.most_common(1)[0]
    share = count / event.size
    if share < policy.folder_share:
        return None
    return NameCandidate(
        source="folder",
        subject=name,
        text=f"{name}, {when}",
        evidence=f"{count} из {event.size} снимков лежат в папке «{name}», названной вами",
        strength=share,
    )


def _person_candidate(
    event: EventCluster,
    hashes: Mapping[str, str],
    persons_by_hash: Mapping[str, Sequence[int]],
    labels: Mapping[int, str | None],
    policy: NamePolicy,
    when: str,
) -> tuple[NameCandidate | None, tuple[int, float] | None]:
    """Link 3, plus what it *would* have said if the cluster had a name.

    Only a **labelled** person may name an album. «Человек №742, 8 июля
    2019» is not a name anybody recognises, which is the one thing Р4 asks
    of this chain — so an unlabelled dominant cluster is returned
    separately, as the measurement of how much link 3 is worth once
    somebody spends five minutes in `dupecleaner persons --label`.

    The label goes in as written, in the nominative: «Карим», not «с
    Каримом». Declining an arbitrary label needs Russian morphology, and a
    name in the wrong case reads worse than a name in the plain one.
    """
    counted: Counter[int] = Counter()
    photos_with_faces = 0
    for content_hash in {hashes.get(m.display_path) for m in event.moments}:
        if not content_hash:
            continue
        people = persons_by_hash.get(content_hash)
        if not people:
            continue
        photos_with_faces += 1
        for person_id in set(people):
            counted[person_id] += 1
    if not counted or photos_with_faces == 0:
        return None, None

    person_id, count = counted.most_common(1)[0]
    share = count / photos_with_faces
    if share < policy.person_share:
        return None, None

    label = labels.get(person_id)
    if not label:
        return None, (person_id, share)

    return (
        NameCandidate(
            source="person",
            subject=label,
            text=f"{label}, {when}",
            evidence=(
                f"{person_display_name(label, person_id)} — на {count} из "
                f"{photos_with_faces} снимков события, где вообще есть лица"
            ),
            strength=share,
        ),
        (person_id, share),
    )


def suggest_for_event(
    event: EventCluster,
    *,
    gazetteer: Gazetteer | None = None,
    content_hashes: Mapping[str, str] | None = None,
    persons_by_hash: Mapping[str, Sequence[int]] | None = None,
    person_labels: Mapping[int, str | None] | None = None,
    confirmed: Mapping[str, str] | None = None,
    policy: NamePolicy = DEFAULT_POLICY,
) -> AlbumSuggestion:
    """Every link that fires for one event, best first.

    `content_hashes` maps `display_path` to `full_hash` and is what makes
    link 3 possible at all: faces are keyed by content (Р11), paths are
    what an event is made of.
    """
    hashes = content_hashes or {}
    first, last = event.date_range
    when = format_date_range(first, last)

    anchor_moment = min(event.moments, key=lambda m: (m.taken_at, m.display_path))
    anchor = hashes.get(anchor_moment.display_path) or anchor_moment.display_path

    dates_only = NameCandidate(
        source="dates",
        subject="",
        text=when,
        evidence=(
            "ни места, ни лиц, ни названной вами папки — честный диапазон дат "
            "лучше выдуманного места"
        ),
        strength=1.0,
    )

    by_source: dict[str, NameCandidate] = {}
    place = _place_candidate(event, gazetteer, policy, when)
    if place:
        by_source["place"] = place
    folder = _folder_candidate(event, policy, when)
    if folder:
        by_source["folder"] = folder
    person, _unlabelled = _person_candidate(
        event, hashes, persons_by_hash or {}, person_labels or {}, policy, when
    )
    if person:
        by_source["person"] = person

    ordered = [by_source[s] for s in policy.subject_order if s in by_source]
    ordered.append(dates_only)

    return AlbumSuggestion(
        anchor=anchor,
        date_range=when,
        candidates=ordered,
        confirmed=(confirmed or {}).get(anchor),
        size=event.size,
    )


@dataclass
class AlbumNaming:
    """Suggestions for a whole library, plus what the run learned."""

    suggestions: list[AlbumSuggestion] = field(default_factory=list)
    policy: NamePolicy = DEFAULT_POLICY
    #: Events where one person dominates but the cluster has no name yet —
    #: link 3's value, waiting on `dupecleaner persons --label`.
    unlabelled_dominant: list[tuple[str, int, float]] = field(default_factory=list)

    def summary(self) -> dict:
        by_source = Counter(s.primary.source for s in self.suggestions)
        return {
            "events": len(self.suggestions),
            "confirmed": sum(1 for s in self.suggestions if s.confirmed),
            "by_source": dict(by_source),
            "dates_only": by_source.get("dates", 0),
            "with_alternatives": sum(1 for s in self.suggestions if s.alternatives),
            "unlabelled_dominant": len(self.unlabelled_dominant),
        }


def suggest_names(
    clustering: EventClustering,
    *,
    gazetteer: Gazetteer | None = None,
    content_hashes: Mapping[str, str] | None = None,
    persons_by_hash: Mapping[str, Sequence[int]] | None = None,
    person_labels: Mapping[int, str | None] | None = None,
    confirmed: Mapping[str, str] | None = None,
    policy: NamePolicy = DEFAULT_POLICY,
) -> AlbumNaming:
    """Name every event in a clustering. Read-only from end to end."""
    naming = AlbumNaming(policy=policy)
    for event in clustering.events:
        suggestion = suggest_for_event(
            event,
            gazetteer=gazetteer,
            content_hashes=content_hashes,
            persons_by_hash=persons_by_hash,
            person_labels=person_labels,
            confirmed=confirmed,
            policy=policy,
        )
        naming.suggestions.append(suggestion)
        if not any(c.source == "person" for c in suggestion.candidates):
            _, unlabelled = _person_candidate(
                event,
                content_hashes or {},
                persons_by_hash or {},
                person_labels or {},
                policy,
                suggestion.date_range,
            )
            if unlabelled:
                naming.unlabelled_dominant.append(
                    (suggestion.anchor, unlabelled[0], unlabelled[1])
                )
    return naming


def moments_by_folder(moments: Iterable[PhotoMoment]) -> Counter:
    """How many photos sit under each hand-named folder — the raw material
    behind link 4, exposed so a report can show it without recomputing."""
    counted: Counter[str] = Counter()
    for moment in moments:
        segment = deepest_named_segment(moment.display_path)
        if segment:
            counted[segment] += 1
    return counted
