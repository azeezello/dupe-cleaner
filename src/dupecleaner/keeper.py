r"""Р8: which copy of a byte-identical group stays where it is.

The problem this replaces
------------------------
`choose_keeper` used to pick the shortest path. On the pilot's real
`D:\Photos` that rule kept the flat dump `Photos\Pictures\` in 62% of
groups and sent hand-named folders to quarantine: `Краснодар` (914 files),
`Wedding Day` (656), `2010 -2020\2020` (679), `Грузия 2022-2023` (345),
`Pamir 2016` (70). A shorter path is not a better one — on a photo disk it
is reliably the *worse* one, because the dump is what sits closest to the
root and every deliberate folder the user made hangs below something.

That is not cosmetic. Р4 builds album names from a chain whose fourth link
is "the folder name the user invented themselves", and it is the only link
that cannot be recovered from the file: GPS, dates and faces all live in
the pixels or the EXIF, a folder name lives nowhere else. Quarantining the
named copy and keeping the anonymous one destroys the only source that has
no backup.

What this module decides, and what it does not
----------------------------------------------
Every copy here is byte-for-byte identical, so there is nothing to choose
between them *as files*. The only thing that differs is where each one
sits, so location is the only admissible evidence. This is deliberately
not the same question as задача 17 / Р2 ("which copy is the best one"),
where the copies differ in resolution, sharpness and recompression
artefacts and the pixels are the evidence.

The rule
--------
Each directory segment above the file is classified into one of three
kinds, and a path is judged by the most specific segment it has:

- **NAMED** — a word a person chose: `Краснодар`, `Wedding Day`,
  `Pamir 2016`, `Грузия 2022-2023`.
- **DATED** — nothing but a date or a period: `2020`, `2010 -2020`,
  `13 May 2018`. Real organisation, but produced by a rule rather than by
  someone remembering what the photos are of — and the year is recoverable
  from EXIF anyway, which the name is not.
- **GENERIC** — a container that says only "files live here": `Pictures`,
  `Photos`, `DCIM`, `Camera`, `Downloads`, `Новая папка`.

Copies are then ordered by, in this order:

1. a plain file before an archive member — an archive member can never be
   quarantined anyway (Р1: you cannot take one file out of an archive), so
   it may only be a keeper when there is nothing else;
2. the most specific segment kind on the path, NAMED > DATED > GENERIC;
3. fewer GENERIC segments — of two named copies, the one with less dump
   above it got there on purpose rather than by being swept along;
4. more NAMED segments — `Грузия 2022-2023\Батуми` is filed more precisely
   than `Грузия 2022-2023`;
5. then the old rule, unchanged: shorter path, then older mtime, then the
   path itself so the result never depends on dict or scan order.

Point 5 matters more than it looks: the new keys are a *prefix* added in
front of the old ones, so on any group the new rule cannot distinguish,
the answer is exactly what it was before. The change is an added
preference, not a replacement.

Considered and rejected
-----------------------
**Oldest mtime ("the original wins").** Measured on the pilot report: in
all 6000 groups the mtimes differ, and in 3878 of them (65%) the oldest
copy is the one in the dump. Windows copies preserve the modification
time, so "oldest" tracks which copy the camera wrote, not which folder the
user meant. It keeps its place as a late tiebreaker and nothing more.

**Deepest path (shortest-path inverted).** It prefers
`Pictures\Wedding Day` over `Wedding 16042017` and `2010 -2020\2020` over
`Краснодар` purely on segment count, and it rewards exactly the
accidental nesting (`Pictures\New folder\copy`) that a keeper rule should
be immune to. Depth is a proxy for filing; the segment names are the thing
itself.

**Folder size, or the files-to-subfolders ratio.** Attractive because it
needs no word list, but it is not local: `choose_keeper` sees one group,
and answering "how big is that folder" means carrying a census of the
whole scan through `quarantine` and `member_twins` — after which the same
group could get a different keeper depending on what else was scanned
alongside it. It also does not separate the real corpus: the dump holds
5117 grouped files, `Краснодар` holds 918, and no threshold sits cleanly
between an album and a dump.

**Asking the person per group.** That is задача 12, and it is the right
answer for the groups someone actually looks at. Р8 has to decide what
happens to the other eight thousand.

Known limits, stated rather than hidden
---------------------------------------
`GENERIC_SEGMENTS` is a list, so it is incomplete by construction. A
folder called `Отсортировать потом` reads as NAMED here, because "sort
this later" is a sentence, not a known container word — on the pilot disk
that affects 7 groups, where the alternative was the dump anyway. The
failure mode of a missing word is that one copy is not preferred as
strongly as it deserves; it is never a lost file, because nothing in this
module moves anything.
"""

from __future__ import annotations

import re
from enum import IntEnum

from .models import FileRecord


class SegmentKind(IntEnum):
    """How much a single folder name says about what is inside it.

    Ordered, and compared as numbers: a bigger value is a more specific
    name. `IntEnum` rather than `Enum` so `max()` over a path's segments
    is the whole of "how specific is this path".
    """

    GENERIC = 0
    DATED = 1
    NAMED = 2


#: Folder names that mean "files live here" and nothing else. Matched
#: case-insensitively against the whole segment, never as a substring —
#: `Photos from Georgia` is not a generic folder just because it starts
#: with the word. Extending this list makes the rule sharper; forgetting
#: an entry only makes it weaker (see the module docstring).
GENERIC_SEGMENTS = frozenset(
    {
        # English
        "pictures", "picture", "pics", "pic", "photos", "photo", "images",
        "image", "img", "media", "camera", "camera roll", "camera uploads",
        "dcim", "google photos", "my pictures", "my photos", "saved pictures",
        "downloads", "download", "desktop", "temp", "tmp", "misc", "untitled",
        # Russian
        "фото", "фотки", "фотографии", "картинки", "изображения", "снимки",
        "медиа", "камера", "загрузки", "рабочий стол", "разное",
        "без названия",
    }
)

#: `New folder`, `New folder (2)`, `Новая папка (3)` — a container name
#: the operating system invented, with an optional copy number.
_UNNAMED_FOLDER_RE = re.compile(r"^(new folder|новая папка)\s*(\(\d+\))?$")

_MONTH_WORDS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec",
    "январь", "января", "февраль", "февраля", "март", "марта", "апрель",
    "апреля", "май", "мая", "июнь", "июня", "июль", "июля", "август",
    "августа", "сентябрь", "сентября", "октябрь", "октября", "ноябрь",
    "ноября", "декабрь", "декабря",
    "янв", "фев", "мар", "апр", "июн", "июл", "авг", "сен", "окт", "ноя",
    "дек",
)

_SEPARATORS_RE = re.compile(r"[\s\-_.,;/\\~–—]+")
_DIGITS_RE = re.compile(r"\d+")
_DRIVE_RE = re.compile(r"^[A-Za-z]:$")


def classify_segment(name: str) -> SegmentKind:
    """Classify one folder name.

    DATED requires a digit on purpose. Without it a folder called `Мая` or
    `May` — which is a person's name at least as often as a month — would
    be read as a date, and a date is the weaker class. Requiring the digit
    means a bare month word stays NAMED, which errs towards keeping a
    human-looking name rather than demoting it.
    """
    normalized = name.strip().lower()
    if not normalized:
        return SegmentKind.GENERIC
    if normalized in GENERIC_SEGMENTS or _UNNAMED_FOLDER_RE.match(normalized):
        return SegmentKind.GENERIC

    if any(char.isdigit() for char in normalized):
        remainder = normalized
        # Longest first, so "september" is consumed before "sep" can bite
        # a hole in the middle of it.
        for month in sorted(_MONTH_WORDS, key=len, reverse=True):
            remainder = remainder.replace(month, " ")
        remainder = _SEPARATORS_RE.sub("", _DIGITS_RE.sub("", remainder))
        if not remainder:
            return SegmentKind.DATED

    return SegmentKind.NAMED


def meaningful_segments(record: FileRecord) -> list[str]:
    """The folder names above this file that could carry intent.

    For a plain file that is its directory chain, minus the drive letter
    (`D:` says nothing about the photo). For an archive member it is the
    directory chain *inside* the archive: the archive's own filename and
    the folder it happens to sit in describe the container, not the
    member.
    """
    if record.is_archive_member:
        raw = (record.member_name or "").replace("\\", "/")
    else:
        raw = record.real_path.replace("\\", "/")

    parts = [p for p in raw.split("/") if p][:-1]  # drop the filename
    return [p for p in parts if not _DRIVE_RE.match(p)]


def keeper_key(record: FileRecord) -> tuple:
    """Sort key for Р8 — smaller is a better keeper.

    The last three components are the pre-Р8 rule verbatim (shortest path,
    then oldest mtime), plus the path itself as a final total order, so
    two copies the new keys cannot separate are resolved exactly as they
    were before this decision existed.
    """
    kinds = [classify_segment(s) for s in meaningful_segments(record)]
    specificity = max(kinds) if kinds else SegmentKind.GENERIC
    generic_count = sum(1 for k in kinds if k is SegmentKind.GENERIC)
    named_count = sum(1 for k in kinds if k is SegmentKind.NAMED)

    return (
        1 if record.is_archive_member else 0,
        -int(specificity),
        generic_count,
        -named_count,
        len(record.display_path),
        record.mtime,
        record.display_path,
    )


def rank_keepers(records: list[FileRecord]) -> list[FileRecord]:
    """The same records, best keeper first.

    Both consumers of Р8 need the whole order, not just the winner:
    `quarantine` moves everything after the first, and
    `archive_classify.member_twins` hands the list to `verify_member`,
    which walks it in order — so the copy it asks about first is the copy
    a file-level quarantine would have left in place.
    """
    return sorted(records, key=keeper_key)


def choose_keeper(records: list[FileRecord]) -> FileRecord:
    """The one copy that stays where it is. See the module docstring.

    A group always has at least two records, and a group of nothing but
    archive members is reported rather than acted on — but this is called
    from two places and will be called from more, so an empty list gets a
    named error instead of an IndexError from somewhere further in.
    """
    if not records:
        raise ValueError("choose_keeper: нечего выбирать, список копий пуст")
    return min(records, key=keeper_key)
