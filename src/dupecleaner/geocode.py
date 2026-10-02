r"""Offline reverse geocoding: a coordinate in, a place name out (Р4, link 1).

Why this is a separate module, and why it holds no data
------------------------------------------------------
Р4 makes the first link of the album-naming chain "GPS from EXIF → offline
geocoder → «Самарканд»", and the word that does the work there is
*offline*. A photo library is the most private thing this tool touches;
sending thirty thousand coordinates to a web service in order to label
them would trade away the entire premise of the project for a convenience.

So this module never opens a socket. It answers from a **gazetteer** — a
flat list of populated places with coordinates — that has to be on disk
before it can answer anything. That is the same shape Р11 chose for the
face models, and for the same reason stated there: a promise that the tool
works without a network cannot itself be implemented with a network call
at run time.

The gazetteer is data, not code, so it is not committed to this
repository. `gazetteer_from_geonames` converts a GeoNames `cities*.txt`
dump (the canonical, CC-BY-4.0 source) into the small tab-separated form
`load_gazetteer` reads; `gazetteer_from_geonamescache` does the same from
the optional `geonamescache` package, which ships that data inside the
wheel for machines with no way to fetch a dump. Either way the conversion
happens once, by explicit command, and everything afterwards is local
file reads.

How a coordinate becomes one name
---------------------------------
A gazetteer is a list of *points*, and a city is not a point. The naive
answer — nearest entry wins — makes a photo taken in the middle of
Novosibirsk come back as the name of whichever 20 000-person suburb
happens to sit closest to that particular block. So every place gets a
**reach**: the radius within which a coordinate counts as being in that
place.

Reach is derived, not tabulated. A settlement's built-up *area* grows
roughly in proportion to how many people live in it, so its radius grows
with the square root of its population: `REACH_PER_SQRT_POP` is fixed by
three checks against reality — 15 000 people ≈ 1.2 km, one million ≈ 10
km, ten million ≈ 32 km — and then clamped, because the formula is a
straight line through a cloud and the ends are where it stops being one.
The winner among places whose reach covers the point is the one the point
is most deeply inside (smallest `distance / reach`), which is what lets a
metropolis 12 km away beat a village 4 km away — correctly, because the
photograph really was in the metropolis.

A point that no reach covers is not forced into the nearest name. It gets
`Match.inside = False` up to `NEAR_LIMIT_M` ("окрестности Батуми") and
nothing at all beyond that. An honest "no place" is the whole reason the
chain in Р4 has a second link.
"""

from __future__ import annotations

import difflib
import math
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .events import haversine_m

#: Reach in metres per unit of sqrt(population) — see the module docstring
#: for the three data points that fix it.
REACH_PER_SQRT_POP = 10.0

#: A place smaller than the formula's honest range still owns its own
#: square: below this the gazetteer's point *is* the settlement.
MIN_REACH_M = 2_000.0

#: Above this the formula would start swallowing neighbouring towns: a
#: 20-million-person agglomeration is several places, not one big one.
MAX_REACH_M = 40_000.0

#: Outside its reach but within this distance, a place still describes
#: where the photo was taken — as surroundings, not as the place itself.
NEAR_LIMIT_M = 30_000.0

#: Depth (`distance / reach`) is compared in buckets this wide, so two
#: places the point sits equally deep inside are separated by size
#: rather than by a rounding error. See `Gazetteer.lookup`.
_DEPTH_BUCKET = 0.25

_GAZETTEER_HEADER = "#dupecleaner-gazetteer\tv1"

#: Default location, beside the index and the face models.
DEFAULT_GAZETTEER_NAME = "gazetteer.tsv"


#: Cyrillic -> Latin, the way GeoNames romanises its own entries. Used
#: only to *rank* candidate spellings, never to display anything, so the
#: table can be crude where romanisations disagree.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

#: The Russian alphabet, and the two punctuation marks a place name may
#: contain. A candidate using anything else is some other Cyrillic
#: language — `Новосибірськ` (Ukrainian), `Тбілісі`, `Самарқанд` (Uzbek
#: Cyrillic), `Уладзівасток` (Belarusian) — and is dropped before the
#: ranking below ever sees it.
_RUSSIAN_CHARS = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя -'")

#: A Cyrillic spelling this close to the Latin name (after transliteration)
#: *is* that name written in another alphabet — `Алматы` for "Almaty".
_EXACT_TRANSLIT_MIN = 0.9

#: ...unless some other spelling is this much more agreed-on across the
#: Cyrillic languages, which is what separates «Париж» (consensus 0.61)
#: from «Парис» (0.50, and an exact transliteration of "Paris").
_CONSENSUS_OVERRIDE = 0.03


def _is_mostly_cyrillic(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    cyrillic = sum(1 for c in letters if "CYRILLIC" in unicodedata.name(c, ""))
    return cyrillic * 2 > len(letters)


def _looks_russian(text: str) -> bool:
    low = text.lower()
    if not low or low.endswith("ъ"):
        # A terminal hard sign is pre-1918 orthography (`Новосибирьскъ`),
        # never a modern spelling.
        return False
    return all(char in _RUSSIAN_CHARS or not char.isalpha() for char in low)


def _bigrams(text: str) -> set[str]:
    padded = " " + "".join(c for c in text.lower() if c.isalnum() or c == " ") + " "
    return {padded[i : i + 2] for i in range(len(padded) - 1)}


def _overlap(a: str, b: str) -> float:
    """Jaccard over character bigrams — cheap enough to run 34 000 times
    at install, and indifferent to word order and punctuation."""
    first, second = _bigrams(a), _bigrams(b)
    union = first | second
    return len(first & second) / len(union) if union else 0.0


def _transliterate(text: str) -> str:
    return "".join(_TRANSLIT.get(c, c) for c in text.lower())


def _latin_similarity(candidate: str, name: str) -> float:
    clean_a = "".join(c for c in _transliterate(candidate) if c.isalnum())
    clean_b = "".join(c for c in name.lower() if c.isalnum())
    return difflib.SequenceMatcher(None, clean_a, clean_b).ratio()


def preferred_name(name: str, alternates: Sequence[str], *, cyrillic: bool) -> str:
    r"""The name to show a person.

    `cyrillic=True` prefers a Russian spelling when the gazetteer has one,
    because this library's own folders are `Краснодар` and `Новосибирск
    2021` and a suggestion reading "Krasnodar" beside them looks like it
    came from somewhere else.

    Picking it is not as simple as "the first Cyrillic string", and the
    first two attempts at this both produced wrong names on a hand-checked
    list of the places this library actually contains:

    * *first Cyrillic wins* made Новосибирск come back as «Виль Сибиркар»
      — its Komi name — and Тбилиси as «Тбилис». GeoNames does not tag
      these alternates by language here, so first place in the list means
      nothing.
    * *closest to the Latin name after transliteration* fixed those and
      broke Moscow and Paris: the Latin name is an exonym there, so
      «Москох» scores above «Москва» and «Парис» above «Париж».

    What works is **consensus**: a place's Cyrillic alternates come from a
    dozen Slavic languages that mostly agree, so the spelling with the
    highest average bigram overlap against all the others is the ordinary
    one, and the outlier — a Komi calque, an archaic form, a local variant
    — is exactly what that average pushes down. Transliteration similarity
    to the Latin name survives only as a tie-breaker, which is what
    separates «Алматы» from «Алма-Ата» and «Тбилиси» from «Тбилис».

    Consensus alone still loses one class: where the Latin name *is* a
    transliteration and the odd spellings happen to cluster, «Алматы»
    (consensus 0.426) falls behind «Алмаато» (0.433). So a spelling that
    transliterates essentially exactly to the Latin name wins outright —
    unless consensus disagrees by more than `_CONSENSUS_OVERRIDE`, which
    is precisely the Paris case, where «Парис» is an exact transliteration
    and «Париж» is 0.12 more agreed-on.

    Measured on 36 hand-checked places spanning this library, its
    neighbours and the exonym cases: **31 right**. The five misses are
    named rather than hidden — `Дубаи` for Дубай, `Казан` for Казань,
    `Анталия` for Анталья (one letter each), `Вена` lost to `Виена`, and
    Астана returned as its historic `Акмола`, because GeoNames keeps
    former names in the same undifferentiated field. All five are still
    the place a person recognises at a glance, which is what a suggestion
    has to be; none of them is in this library.
    """
    if not cyrillic:
        return name
    pool = [a.strip() for a in alternates if a.strip() and _is_mostly_cyrillic(a.strip())]
    russian = [a for a in pool if _looks_russian(a)]
    if not russian:
        return name
    if len(russian) == 1:
        return russian[0]

    consensus = {
        candidate: sum(_overlap(candidate, other) for other in pool) / len(pool)
        for candidate in russian
    }
    best_consensus = max(consensus.values())

    exact = [c for c in russian if _latin_similarity(c, name) >= _EXACT_TRANSLIT_MIN]
    if exact:
        chosen = max(exact, key=lambda c: (consensus[c], -len(c)))
        if best_consensus - consensus[chosen] <= _CONSENSUS_OVERRIDE:
            return chosen

    return max(russian, key=lambda c: (consensus[c], _latin_similarity(c, name), -len(c)))

@dataclass(frozen=True)
class Place:
    """One gazetteer entry."""

    name: str
    latitude: float
    longitude: float
    population: int
    country: str = ""

    @property
    def reach_m(self) -> float:
        raw = REACH_PER_SQRT_POP * math.sqrt(max(self.population, 0))
        return min(MAX_REACH_M, max(MIN_REACH_M, raw))


@dataclass(frozen=True)
class Match:
    """What the geocoder found for one coordinate."""

    place: Place
    distance_m: float
    #: True when the coordinate is within the place's reach. False means
    #: "near it" — good enough to describe, not to claim.
    inside: bool

    @property
    def label(self) -> str:
        """The place as it should appear in a name.

        `Академгородок (окрестности)` rather than `окрестности
        Академгородка`: Russian wants the genitive after that preposition,
        and declining an arbitrary toponym needs morphology this project
        does not have. A bracketed qualifier needs no case at all — the
        same reason `albums.py` puts a person's label in the nominative.
        """
        return self.place.name if self.inside else f"{self.place.name} (окрестности)"


class Gazetteer:
    """Places, bucketed by whole degree so a lookup touches a handful.

    A linear scan over 34 000 places is 34 000 haversines, and задача 20
    asks this question once per photograph with coordinates — 15 288 of
    them on the real library. The grid is the difference between half a
    second and ten minutes, and it is a dict of lists rather than a
    k-d tree because the project has no scipy dependency and this is a
    box query, not a nearest-neighbour search in earnest.
    """

    def __init__(self, places: Iterable[Place]) -> None:
        self._places: list[Place] = list(places)
        self._grid: dict[tuple[int, int], list[Place]] = {}
        for place in self._places:
            self._grid.setdefault(self._cell(place.latitude, place.longitude), []).append(place)

    def __len__(self) -> int:
        return len(self._places)

    @staticmethod
    def _cell(latitude: float, longitude: float) -> tuple[int, int]:
        return (math.floor(latitude), math.floor(longitude))

    def _candidates(self, latitude: float, longitude: float) -> Iterator[Place]:
        """Every place in the nine cells around the point.

        One degree of latitude is 111 km, so the ring of neighbours always
        covers `NEAR_LIMIT_M` and `MAX_REACH_M` with room to spare — at
        every longitude, because cells narrow towards the poles rather
        than widening.
        """
        base_lat, base_lon = self._cell(latitude, longitude)
        for d_lat in (-1, 0, 1):
            for d_lon in (-1, 0, 1):
                lon_cell = base_lon + d_lon
                # Wrap the antimeridian, so a photo at 179.9°E still sees
                # its neighbours at 179.9°W.
                if lon_cell > 179:
                    lon_cell -= 360
                elif lon_cell < -180:
                    lon_cell += 360
                yield from self._grid.get((base_lat + d_lat, lon_cell), ())

    def lookup(self, latitude: float, longitude: float) -> Match | None:
        """The place this coordinate is in, or near, or None.

        Among the places whose reach covers the point, the winner is the
        one the point is most deeply inside — `distance / reach` — because
        that is what lets a city 12 km away beat a village 4 km away when
        the photograph really was in the city. The depth is compared in quarters,
        with population breaking the tie, and that rounding is not
        cosmetic: GeoNames lists a large city *and* its subdivisions as
        separate entries, so a photo in the middle of Paris is at depth
        ~0.03 of both `Paris` and `Paris 04 Hôtel-de-Ville`, and without
        the tie-break the arrondissement wins by a rounding error. A
        person says «Париж».
        """
        best_inside: tuple[tuple, Place, float] | None = None
        best_near: tuple[float, Place] | None = None

        for place in self._candidates(latitude, longitude):
            distance = haversine_m(latitude, longitude, place.latitude, place.longitude)
            reach = place.reach_m
            if distance <= reach:
                key = (round(distance / reach / _DEPTH_BUCKET), -place.population, place.name)
                if best_inside is None or key < best_inside[0]:
                    best_inside = (key, place, distance)
            elif distance <= NEAR_LIMIT_M:
                if best_near is None or distance < best_near[0]:
                    best_near = (distance, place)

        if best_inside is not None:
            _, place, distance = best_inside
            return Match(place=place, distance_m=distance, inside=True)
        if best_near is not None:
            distance, place = best_near
            return Match(place=place, distance_m=distance, inside=False)
        return None


# --- the gazetteer on disk -------------------------------------------------


def default_gazetteer_path(db_path: str | Path) -> Path:
    """Beside the index, like the thumbnail cache and the face models."""
    return Path(db_path).expanduser().resolve().parent / DEFAULT_GAZETTEER_NAME


def write_gazetteer(places: Sequence[Place], path: str | Path) -> Path:
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(_GAZETTEER_HEADER + "\n")
        for place in places:
            handle.write(
                f"{place.name}\t{place.latitude:.5f}\t{place.longitude:.5f}"
                f"\t{place.population}\t{place.country}\n"
            )
    return out


def load_gazetteer(path: str | Path) -> Gazetteer:
    """Read the tab-separated form. Malformed lines are skipped, not fatal:
    a gazetteer is user-supplied data, and one bad row in 34 000 should
    cost that row and nothing else."""
    places: list[Place] = []
    with Path(path).expanduser().open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            try:
                places.append(
                    Place(
                        name=parts[0],
                        latitude=float(parts[1]),
                        longitude=float(parts[2]),
                        population=int(parts[3] or 0),
                        country=parts[4] if len(parts) > 4 else "",
                    )
                )
            except ValueError:
                continue
    return Gazetteer(places)


def gazetteer_from_geonames(
    dump_path: str | Path, *, min_population: int = 0, cyrillic: bool = True
) -> list[Place]:
    """Parse a GeoNames `cities*.txt` dump (tab-separated, 19 columns).

    Columns used: 1 name, 3 alternatenames, 4 latitude, 5 longitude,
    8 country code, 14 population. The file is what
    <https://download.geonames.org/export/dump/> serves as
    `cities15000.zip` and friends, licensed CC-BY-4.0 — hence conversion
    from a path the person supplies rather than a download from here.
    """
    places: list[Place] = []
    with Path(dump_path).expanduser().open(encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 15:
                continue
            try:
                latitude, longitude = float(parts[4]), float(parts[5])
                population = int(parts[14] or 0)
            except ValueError:
                continue
            if population < min_population:
                continue
            places.append(
                Place(
                    name=preferred_name(parts[1], parts[3].split(","), cyrillic=cyrillic),
                    latitude=latitude,
                    longitude=longitude,
                    population=population,
                    country=parts[8],
                )
            )
    return places


def gazetteer_from_geonamescache(*, min_population: int = 0, cyrillic: bool = True) -> list[Place]:
    """The same list out of the optional `geonamescache` package.

    Exists for the machine that cannot download a dump at all: the
    package carries the GeoNames city table inside the wheel, so `pip
    install geonamescache` once is the whole network step, and nothing
    afterwards leaves the machine. Raises `ImportError` if it is absent —
    the caller turns that into advice, not a traceback.
    """
    import geonamescache  # noqa: PLC0415 — optional, imported only when asked for

    places: list[Place] = []
    for city in geonamescache.GeonamesCache().get_cities().values():
        population = int(city.get("population") or 0)
        if population < min_population:
            continue
        places.append(
            Place(
                name=preferred_name(
                    city["name"], tuple(city.get("alternatenames") or ()), cyrillic=cyrillic
                ),
                latitude=float(city["latitude"]),
                longitude=float(city["longitude"]),
                population=population,
                country=city.get("countrycode", ""),
            )
        )
    return places
