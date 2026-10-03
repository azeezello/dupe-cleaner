r"""Planning the library Р5 describes — and nothing else (задача 21).

What this module is allowed to do
---------------------------------
Read, count and explain. It does not import `shutil`, `os.rename` or
`quarantine`, it never opens a file for writing, and the only filesystem
questions it asks are "does something already sit here" and "can this file
be renamed right now" — both answered through `PlanProbe`, both read-only.
Carrying the plan out is задача 22, which reuses задача 5's
append-before-execute journal (`quarantine._JournalWriter`, `_move_one`).
The split is deliberate and it is Р5's own requirement: *the whole plan is
visible before a single file moves.*

So `plan_library` returns a `LibraryPlan` — every move, every conflict,
every file it could not plan, and the reason in each case — and that
object is the handover to задача 22. It serialises (`to_dict` /
`plan_from_dict`) and carries a `fingerprint`, so execution can assert
that what it is about to do is what a person actually read, rather than a
freshly recomputed plan that drifted because a threshold moved. Events are
deliberately not stored (задача 16), which means re-clustering legitimately
returns a different answer; a plan that cannot prove its own identity would
turn that into a silent surprise on the one operation in this project that
touches thirty thousand real files.

The problem, in one real frame
------------------------------
`20170416_145106.jpg` lies in `Pictures\`, `Pictures\Wedding Day\`,
`Pictures\Diljon\` and `Wedding 16042017\` at the same time (pilot, 14
September). Google Photos albums overlap, an export has nothing to
materialise a link with except a copy, and that is how one photograph
becomes four files. Р5's answer is a rule with two halves, and this module
is the second half applied to a real library:

    mutually exclusive grouping is materialised as folders;
    overlapping grouping stays tags and is never materialised.

Events are mutually exclusive by construction, so folders for them are
safe. Faces and topics overlap, so they stay in the XMP sidecar — which
задача 22 writes, and which is why `PlannedMove.also_at` carries every
other place the same bytes were found: the sidecar has to be able to say
"this file was also at …", and after the move nobody can recover that
list.

One content, one destination
----------------------------
A duplicate group contributes exactly **one** file to the library. Which
one is not a new decision: Р8 already answers "which copy is the real
one", `keeper.choose_keeper` implements it, and this module calls it
rather than inventing a second ordering. The practical consequence is
that the library inherits the filename from the copy in the folder a
person named themselves — the same link Р8 exists to protect and Р4's
chain depends on.

The copies that lose are **not moved and not touched**: they stay exactly
where they are, listed as `RedundantCopy`. Deciding that a byte-identical
leftover should go away is ось A in Р0 — redundancy — and it already has
a path (Р8, задача 6, задача 12, `quarantine`). This module is ось C,
organisation, and Р0 says plainly that ось C has no authority to remove
anything. Planning the library and emptying the old folders are therefore
two separate decisions a person makes separately, and that is not an
oversight.

What a person has to resolve by hand, and what resolves itself
--------------------------------------------------------------
Four things can go wrong when thirty thousand files are given new paths,
and they are not equally serious:

* **a redundant copy** — same bytes, same destination. Not a conflict at
  all; Р8 picks the mover, the rest are left. Automatic.
* **two different files with one basename in one folder** — real, and
  cheap: the second becomes `<stem>__1<suffix>`, exactly as
  `quarantine._unique_destination` has always done. Flagged, counted,
  automatic — nothing is lost and no name becomes ambiguous.
* **two events wanting one folder** — this one is not cosmetic, and it
  splits in two. If neither event has a subject (both live on a date
  range, Р4's link 5), the folder name carries no information that would
  distinguish them, so same-day events merge into one day folder: a
  person choosing between `2012-11-18` and `2012-11-18 (2)` learns
  nothing. If the subjects are equal and non-empty, two genuinely
  different occasions are claiming one name, and only a person can say
  whether that is one album or two. **Needs a human.**
* **something already sits at the destination**, the path is longer than
  Windows will accept, or the file is open in another process. The first
  two need a person; the third is Р5's "пропуск занятых файлов с
  предупреждением" and is reported here so it is not a surprise at
  execution time.

Moving between volumes is a mode, not a conflict
------------------------------------------------
Inside one volume a move is a rename: instant, atomic, no extra space.
Across volumes it is a copy, a hash comparison and only then a removal of
the source — Р5 says the mode switches automatically, and `Transfer` is
where that switch is written down. The plan reports both the count and
the bytes per mode, because a cross-volume plan needs that many bytes free
at the destination before it starts, and finding that out halfway through
is the kind of thing journals exist to survive rather than to prevent.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, Protocol, Sequence

from .albums import AlbumNaming, AlbumSuggestion
from .events import EventCluster, EventClustering
from .keeper import choose_keeper
from .models import FileRecord

#: The three trees Р5 puts outside the year/event hierarchy, spelled once.
SCREENSHOTS_DIR = "_screenshots"
DOCUMENTS_DIR = "_documents"
UNSORTED_DIR = "_unsorted"
#: Where a screenshot with no capture date goes: inside `_screenshots`, but
#: not mixed in with a year it cannot be shown to belong to.
UNDATED_DIR = "_undated"

#: Month names for the folder a small day lands in. Russian, in the
#: nominative, because the folder name is read as a label («2021-07 Июль»)
#: and not as part of a sentence — the same choice `albums` makes for a
#: confirmed name.
MONTH_NAMES = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)

#: Characters Windows refuses in a path component, plus the control range.
#: An album name comes from a geocoder, from a folder a person named, or
#: from a name a person typed by hand at `albums --confirm`, so it can
#: contain anything at all.
_ILLEGAL_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: Windows device names, which are illegal as a whole component in any
#: directory, with or without an extension.
_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)
_WINDOWS_DRIVE_RE = re.compile(r"^([A-Za-z]):[/\\]")
_UNC_RE = re.compile(r"^[/\\]{2}([^/\\]+)[/\\]+([^/\\]+)")


class Bucket(str, Enum):
    """Which of Р5's trees a file belongs to."""

    EVENT = "event"
    #: A day too small to deserve a folder of its own, filed under its
    #: month. Not a failure of clustering: two frames shot on a Tuesday
    #: are a Tuesday, not an occasion, and a library where every such
    #: Tuesday is a folder is unreadable — measured on this library, 866
    #: of 2163 folders would hold one or two photographs.
    MONTH = "month"
    SCREENSHOTS = "screenshots"
    DOCUMENTS = "documents"
    UNSORTED = "unsorted"


class Transfer(str, Enum):
    """How a move has to be carried out — Р5's "режим переключается
    автоматически", decided per file rather than per run, because a
    library can perfectly well straddle two volumes."""

    RENAME = "rename"
    COPY_VERIFY = "copy_verify"


class Problem(str, Enum):
    """Everything that can stop one file from being planned, or stop a
    folder from being unambiguous. `needs_human` below is the only
    distinction that matters operationally."""

    ALBUM_COLLISION = "album_collision"
    DESTINATION_EXISTS = "destination_exists"
    PATH_TOO_LONG = "path_too_long"
    BUSY = "busy"
    NO_RECORD = "no_record"
    ARCHIVE_MEMBER = "archive_member"

    @property
    def needs_human(self) -> bool:
        """Whether a person has to decide something before задача 22 can
        run this plan to the end.

        `BUSY` is not on the list on purpose: Р5 already decided it
        ("пропуск занятых файлов с предупреждением"), so an open file is
        a warning and a skipped move, not a question. It is also the one
        problem here whose answer can change by itself between the plan
        and the execution — closing a photo editor is not a decision
        anybody records.
        """
        return self in (
            Problem.ALBUM_COLLISION,
            Problem.DESTINATION_EXISTS,
            Problem.PATH_TOO_LONG,
        )


# --- the filesystem, asked only the two questions a plan needs -------------


class PlanProbe(Protocol):
    """The whole filesystem surface this module is allowed to touch.

    Three read-only questions. A protocol rather than direct `os` calls
    for two reasons, and the second one is not about testing: the real
    library is reachable only from the machine it sits on, while a plan is
    worth building wherever the index is — so a run against an index whose
    files are not mounted needs a probe that answers from the paths alone
    and says so, instead of a planner that silently reports every
    destination as free.
    """

    def exists(self, path: str) -> bool: ...

    def is_busy(self, path: str) -> bool: ...

    def volume_of(self, path: str) -> str: ...


def volume_from_path(path: str) -> str:
    r"""Which volume a path is on, decided from its text.

    `D:\Photos\x.jpg` -> `D:`; `\\nas\photos\x.jpg` -> `\\nas\photos`
    (an SMB share is one volume regardless of what the server does
    underneath); a POSIX path -> `/`. Text rather than `os.stat().st_dev`
    because the planner routinely runs where the paths are not mounted,
    and because the answer the plan needs is "will this be a rename or a
    copy", which on Windows is exactly the drive letter question.

    The known imprecision: two NTFS folders on one physical disk but
    different volumes compare equal here if the drive letters match, which
    they cannot, and a junction pointing across volumes compares equal
    when it should not. Р5 forbids junctions in this library outright, so
    the remaining case is a mount point — rare on Windows, and задача 22
    discovers it anyway, because a cross-volume rename fails loudly with
    `EXDEV` rather than silently doing the wrong thing.
    """
    text = path.strip()
    unc = _UNC_RE.match(text)
    if unc:
        return f"\\\\{unc.group(1)}\\{unc.group(2)}"
    drive = _WINDOWS_DRIVE_RE.match(text)
    if drive:
        return f"{drive.group(1).upper()}:"
    return "/"


class PathProbe:
    """A probe that knows only what the paths say.

    Reports every destination as free and nothing as busy, and says so
    through `blind = True` so a report can print the caveat instead of
    implying the checks passed. This is the honest probe for a plan built
    from an index on a machine that is not the one holding the photographs.
    """

    blind = True

    def exists(self, path: str) -> bool:
        return False

    def is_busy(self, path: str) -> bool:
        return False

    def volume_of(self, path: str) -> str:
        return volume_from_path(path)


class RealProbe:
    """The probe for a run on the machine that holds the library.

    `is_busy` asks the only question that matters for a move: can this
    file be renamed *now*. On Windows that is a `CreateFileW` with
    `DELETE` access — a rename needs delete rights on the source, and a
    file someone has open with `FILE_SHARE_NONE` refuses exactly that
    while still letting a plain read succeed. Opening a handle with
    `DELETE` access deletes nothing; `FILE_FLAG_DELETE_ON_CLOSE`, which
    would, is not passed.

    On POSIX there are no mandatory locks, so the answer is almost always
    "not busy" and that is a property of the operating system rather than
    a gap in this code. Either way the guarantee does not live here: Р5
    requires задача 22 to skip a busy file *at the moment of the move*,
    because a file can be opened in the seconds between a plan and its
    execution. What this probe buys is a warning in advance, not a
    promise.
    """

    blind = False

    #: ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION.
    _BUSY_ERRNOS = (32, 33)

    def exists(self, path: str) -> bool:
        from pathlib import Path

        try:
            return Path(path).exists()
        except OSError:
            return False

    def is_busy(self, path: str) -> bool:
        import os

        if os.name == "nt":
            return self._is_busy_windows(path)
        try:
            with open(path, "rb"):
                return False
        except PermissionError:
            return True
        except OSError:
            # Missing or unreadable for another reason: not this check's
            # question. The move itself will report it.
            return False

    def _is_busy_windows(self, path: str) -> bool:  # pragma: no cover - needs Windows
        import ctypes

        GENERIC_READ = 0x80000000
        DELETE = 0x00010000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.CreateFileW(
            ctypes.c_wchar_p(path),
            GENERIC_READ | DELETE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            return kernel32.GetLastError() in self._BUSY_ERRNOS
        kernel32.CloseHandle(handle)
        return False

    def volume_of(self, path: str) -> str:
        return volume_from_path(path)


# --- the layout -------------------------------------------------------------


def sanitize_component(name: str, *, max_chars: int = 120) -> str:
    r"""One path component a filesystem will actually accept.

    Illegal characters become `-` rather than disappearing, so «Самарканд:
    весна» stays readable instead of collapsing into «Самаркандвесна».
    Trailing dots and spaces go: Windows accepts them in an API call and
    then cannot open the result. A reserved device name gets an underscore.
    Truncation is by characters and keeps the head, because the head of an
    album name is the part that identifies it.
    """
    cleaned = _ILLEGAL_COMPONENT.sub("-", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        return "_"
    if cleaned.lower() in _RESERVED_NAMES or cleaned.split(".")[0].lower() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip(" .")
    return cleaned or "_"


def join_path(*parts: str) -> str:
    r"""Join with the separator the root already uses.

    The plan is built on one machine and may be read on another, so the
    separator is a property of the paths being planned, not of
    `os.sep`: a Windows root keeps backslashes even when the planner runs
    on Linux, which is the same decision `quarantine._mirrored_relative_parts`
    made and for the same reason.
    """
    root = parts[0]
    sep = "\\" if ("\\" in root or _WINDOWS_DRIVE_RE.match(root)) else "/"
    head = root.rstrip("/\\")
    tail = [p.strip("/\\") for p in parts[1:] if p.strip("/\\")]
    return sep.join([head, *tail]) if tail else head


def basename_of(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").split("/")[-1]


@dataclass(frozen=True)
class LibraryLayout:
    r"""Р5's tree, with every number it depends on named.

    The date goes in front of the album name so the folders sort
    chronologically in Explorer and in a cloud web view without anything
    having to sort them; the year is its own level so one directory does
    not end up holding two thousand subfolders — on this library it holds
    at most a few hundred per year instead.

    The folder carries the event's **start** date only. An event that
    crosses midnight would otherwise produce a name that changes when a
    threshold moves, and the full range is in the plan, in the album name
    itself and (after задача 22) in the sidecar.
    """

    root: str
    #: `<root>/<year>/<date> <album>` when true, `<root>/<date> <album>`
    #: when false. The year level is what keeps the directory listings
    #: readable; it is a flag because a small library does not need it.
    year_level: bool = True
    #: Windows refuses to open a path longer than this without the `\\?\`
    #: prefix, and a path that cannot be opened is worse than a long name.
    max_path_chars: int = 260
    max_component_chars: int = 120
    #: Two events on one day, neither of which has a subject, share a
    #: folder: see the module docstring.
    merge_same_day_unnamed: bool = True
    #: Below this many photographs an event gets no folder of its own and
    #: is filed under its month instead. 0 keeps every event's folder,
    #: which is the behaviour this field was added to make optional rather
    #: than mandatory. A name a person confirmed by hand always keeps its
    #: own folder regardless of size: they said what it is, and the size
    #: of an occasion is not the measure of whether it mattered.
    min_album_photos: int = 0

    def year_folder(self, date: dt.date) -> str:
        return join_path(self.root, str(date.year)) if self.year_level else self.root

    def event_folder_name(self, start: dt.date, subject: str) -> str:
        stamp = start.isoformat()
        if not subject.strip():
            return stamp
        return sanitize_component(f"{stamp} {subject}", max_chars=self.max_component_chars)

    def event_folder(self, start: dt.date, subject: str) -> str:
        return join_path(self.year_folder(start), self.event_folder_name(start, subject))

    def month_folder(self, date: dt.date) -> str:
        """`<root>/<year>/<year>-<month> <Месяц>` — where a small day goes.

        Same shape as an event folder, date first, so the month sorts in
        among the events of that year rather than above or below them all.
        """
        stamp = f"{date.year:04d}-{date.month:02d}"
        return join_path(
            self.year_folder(date),
            sanitize_component(
                f"{stamp} {MONTH_NAMES[date.month - 1]}",
                max_chars=self.max_component_chars,
            ),
        )

    def screenshots_folder(self, date: dt.date | None) -> str:
        bucket = str(date.year) if date else UNDATED_DIR
        return join_path(self.root, SCREENSHOTS_DIR, bucket)

    def documents_folder(self) -> str:
        return join_path(self.root, DOCUMENTS_DIR)

    def unsorted_folder(self) -> str:
        return join_path(self.root, UNSORTED_DIR)

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "year_level": self.year_level,
            "max_path_chars": self.max_path_chars,
            "max_component_chars": self.max_component_chars,
            "merge_same_day_unnamed": self.merge_same_day_unnamed,
            "min_album_photos": self.min_album_photos,
        }


# --- what a plan is made of -------------------------------------------------


@dataclass(frozen=True)
class PlannedMove:
    """One file, one destination, and why."""

    source: str
    destination: str
    bucket: Bucket
    transfer: Transfer
    size: int
    content_key: str
    album: str = ""
    #: Every other path the same bytes were found at. Not moved (see
    #: `RedundantCopy`); carried here because задача 22 writes it into the
    #: sidecar and the index, and after the move it is unrecoverable.
    also_at: tuple[str, ...] = ()
    #: Set when the basename had to change because a different file
    #: already claimed it in this folder.
    renamed_from: str | None = None
    reason: str = ""

    @property
    def is_rename_in_place(self) -> bool:
        return self.source == self.destination

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "destination": self.destination,
            "bucket": self.bucket.value,
            "transfer": self.transfer.value,
            "size": self.size,
            "content_key": self.content_key,
            "album": self.album,
            "also_at": list(self.also_at),
            "renamed_from": self.renamed_from,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RedundantCopy:
    """A byte-identical copy that does not move, and where its content went.

    Deliberately not a skipped move and not a conflict: Р0 gives ось C no
    authority over a redundant file, so the plan records it, points at the
    copy that does move, and leaves the decision to the quarantine path.
    """

    source: str
    content_key: str
    size: int
    kept: str          # the source path that is moving instead
    destination: str   # where that content ends up
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "content_key": self.content_key,
            "size": self.size,
            "kept": self.kept,
            "destination": self.destination,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PlanProblem:
    kind: Problem
    source: str
    destination: str
    detail: str

    @property
    def needs_human(self) -> bool:
        return self.kind.needs_human

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "source": self.source,
            "destination": self.destination,
            "detail": self.detail,
            "needs_human": self.needs_human,
        }


@dataclass(frozen=True)
class PlannedAlbum:
    """One folder the library will have, and the event behind it."""

    folder: str
    name: str
    anchor: str
    start: dt.date
    end: dt.date
    photos: int
    source: str            # which link of Р4's chain named it
    confirmed: bool
    merged_events: int = 1

    def to_dict(self) -> dict:
        return {
            "folder": self.folder,
            "name": self.name,
            "anchor": self.anchor,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "photos": self.photos,
            "source": self.source,
            "confirmed": self.confirmed,
            "merged_events": self.merged_events,
        }


@dataclass
class LibraryPlan:
    """Everything задача 22 needs, and everything a person has to read first."""

    layout: LibraryLayout
    moves: list[PlannedMove] = field(default_factory=list)
    redundant: list[RedundantCopy] = field(default_factory=list)
    problems: list[PlanProblem] = field(default_factory=list)
    albums: list[PlannedAlbum] = field(default_factory=list)
    #: Files already sitting at their canonical path — a re-plan after a
    #: partial run, which is the normal case once задача 22 has been
    #: interrupted once.
    already_in_place: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    probe_blind: bool = False

    @property
    def needs_human(self) -> list[PlanProblem]:
        return [p for p in self.problems if p.needs_human]

    @property
    def busy(self) -> list[PlanProblem]:
        return [p for p in self.problems if p.kind is Problem.BUSY]

    @property
    def total_bytes(self) -> int:
        return sum(m.size for m in self.moves)

    def bytes_by_transfer(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for move in self.moves:
            out[move.transfer.value] = out.get(move.transfer.value, 0) + move.size
        return out

    def fingerprint(self) -> str:
        """Identity of the move list, so задача 22 can refuse to execute a
        plan nobody read. Order-independent: the same plan built twice
        fingerprints the same, while one extra or one changed destination
        does not."""
        digest = hashlib.sha256()
        for line in sorted(f"{m.source}\x00{m.destination}" for m in self.moves):
            digest.update(line.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()[:16]

    def summary(self) -> dict:
        by_bucket = Counter(m.bucket.value for m in self.moves)
        by_problem = Counter(p.kind.value for p in self.problems)
        return {
            "moves": len(self.moves),
            "by_bucket": dict(by_bucket),
            "by_transfer": dict(Counter(m.transfer.value for m in self.moves)),
            "bytes": self.total_bytes,
            "bytes_by_transfer": self.bytes_by_transfer(),
            "albums": len(self.albums),
            "redundant_copies": len(self.redundant),
            "redundant_bytes": sum(r.size for r in self.redundant),
            "renamed": sum(1 for m in self.moves if m.renamed_from),
            "problems": dict(by_problem),
            "needs_human": len(self.needs_human),
            "busy": len(self.busy),
            "already_in_place": len(self.already_in_place),
            "merged_albums": sum(1 for a in self.albums if a.merged_events > 1),
            "fingerprint": self.fingerprint(),
            "probe_blind": self.probe_blind,
        }

    def to_dict(self) -> dict:
        return {
            "layout": self.layout.to_dict(),
            "summary": self.summary(),
            "albums": [a.to_dict() for a in self.albums],
            "moves": [m.to_dict() for m in self.moves],
            "redundant": [r.to_dict() for r in self.redundant],
            "problems": [p.to_dict() for p in self.problems],
            "already_in_place": list(self.already_in_place),
            "warnings": list(self.warnings),
        }


# --- planning ---------------------------------------------------------------


def album_subject(suggestion: AlbumSuggestion) -> str:
    """What goes after the date in the folder name.

    The suggestion's `text` already contains the date range («Душанбе, 16
    апреля 2017»), so putting it after a `2017-04-16` stamp would print
    the date twice. The `subject` is the part that is not the date.

    A confirmed name is used verbatim, date and all: a person typed it, and
    second-guessing the words they chose is exactly what Р4's fourth link
    exists to stop.
    """
    if suggestion.confirmed:
        return suggestion.confirmed
    return suggestion.primary.subject


def _records_for(
    paths: Iterable[str], records: Mapping[str, FileRecord]
) -> list[FileRecord]:
    return [records[p] for p in paths if p in records]


def _content_key(path: str, content_keys: Mapping[str, str]) -> str:
    r"""Which files are the same file.

    `full_hash` when the scan has one, the path itself otherwise — and the
    fallback is sound rather than lazy. The funnel only hashes files that
    share a size with another file (Р7), so a file with no hash has no
    size twin anywhere in the scan, and a byte-identical copy would
    necessarily have had the same size. It is the same argument задача 4
    uses to call an archive unique without reading it.
    """
    return content_keys.get(path) or path


def _bucket_for(origin_class: str | None) -> Bucket:
    if origin_class is None:
        return Bucket.UNSORTED
    if origin_class in ("screenshot_desktop", "screenshot_phone"):
        return Bucket.SCREENSHOTS
    if origin_class == "document_scan":
        return Bucket.DOCUMENTS
    return Bucket.UNSORTED


class _FolderNames:
    """Per-folder basename bookkeeping: the thing that turns "two files
    called IMG_1234.jpg" into two destinations instead of one lost file."""

    def __init__(self, probe: PlanProbe):
        self._probe = probe
        self._taken: dict[str, set[str]] = {}

    def claim(self, folder: str, filename: str) -> tuple[str, str | None]:
        """Reserve a basename in `folder`. Returns `(filename, renamed_from)`."""
        taken = self._taken.setdefault(folder, set())
        lowered = filename.lower()
        if lowered not in taken and not self._probe.exists(join_path(folder, filename)):
            taken.add(lowered)
            return filename, None
        stem, _, suffix = filename.rpartition(".")
        if not stem:
            stem, suffix = filename, ""
        dot_suffix = f".{suffix}" if suffix else ""
        n = 1
        while True:
            candidate = f"{stem}__{n}{dot_suffix}"
            if candidate.lower() not in taken and not self._probe.exists(
                join_path(folder, candidate)
            ):
                taken.add(candidate.lower())
                return candidate, filename
            n += 1


def _plan_albums(
    clustering: EventClustering,
    naming: AlbumNaming,
    layout: LibraryLayout,
) -> tuple[dict[int, str], set[int], list[PlannedAlbum], list[PlanProblem]]:
    """Decide one folder per event, and report the folders two events want.

    Returns `(event index -> folder, indices filed under a month, albums,
    problems)`. The folder map is what the move pass reads; the collision
    rules are in the module docstring.

    Events below `layout.min_album_photos` are taken out first and filed
    under their month, so they never reach the collision rules — two small
    days of one month are not two events claiming one folder, they are the
    month doing its job.
    """
    pairs: list[tuple[int, EventCluster, AlbumSuggestion]] = list(
        zip(range(len(clustering.events)), clustering.events, naming.suggestions)
    )
    folder_of: dict[int, str] = {}
    in_month: set[int] = set()
    albums: list[PlannedAlbum] = []

    floor = max(0, layout.min_album_photos)
    if floor:
        small = [
            (index, event, suggestion)
            for index, event, suggestion in pairs
            # A confirmed name outranks the floor: see `min_album_photos`.
            if event.size < floor and not suggestion.confirmed
        ]
        by_month: dict[tuple[int, int], list[tuple[int, EventCluster, AlbumSuggestion]]] = {}
        for item in small:
            start, _ = item[1].date_range
            by_month.setdefault((start.year, start.month), []).append(item)
        for (year, month), members in sorted(by_month.items()):
            folder = layout.month_folder(dt.date(year, month, 1))
            for index, _, _ in members:
                folder_of[index] = folder
                in_month.add(index)
            first = min(members, key=lambda m: m[1].start)
            albums.append(
                PlannedAlbum(
                    folder=folder,
                    name=f"{year:04d}-{month:02d} {MONTH_NAMES[month - 1]}",
                    anchor=first[2].anchor,
                    start=min(m[1].date_range[0] for m in members),
                    end=max(m[1].date_range[1] for m in members),
                    photos=sum(m[1].size for m in members),
                    source="month",
                    confirmed=False,
                    merged_events=len(members),
                )
            )
        pairs = [item for item in pairs if item[0] not in in_month]
    by_folder: dict[str, list[tuple[int, EventCluster, AlbumSuggestion]]] = {}
    for index, event, suggestion in pairs:
        start, _ = event.date_range
        folder = layout.event_folder(start, album_subject(suggestion))
        by_folder.setdefault(folder, []).append((index, event, suggestion))

    problems: list[PlanProblem] = []
    for folder, claimants in sorted(by_folder.items()):
        subjects = {album_subject(s).strip() for _, _, s in claimants}
        unnamed = subjects == {""}
        if len(claimants) > 1 and unnamed and layout.merge_same_day_unnamed:
            for index, _, _ in claimants:
                folder_of[index] = folder
            first = min(claimants, key=lambda c: c[1].start)
            start = min(c[1].date_range[0] for c in claimants)
            end = max(c[1].date_range[1] for c in claimants)
            albums.append(
                PlannedAlbum(
                    folder=folder,
                    name=first[2].name,
                    anchor=first[2].anchor,
                    start=start,
                    end=end,
                    photos=sum(c[1].size for c in claimants),
                    source=first[2].primary.source,
                    confirmed=bool(first[2].confirmed),
                    merged_events=len(claimants),
                )
            )
            continue

        for position, (index, event, suggestion) in enumerate(
            sorted(claimants, key=lambda c: (c[1].start, c[1].moments[0].display_path))
        ):
            start, end = event.date_range
            target = folder
            if position:
                # Two different occasions claiming one named folder. The
                # suffix keeps the plan executable and nothing ambiguous,
                # but the question of whether this is one album or two is
                # a person's.
                target = join_path(
                    layout.year_folder(start),
                    sanitize_component(
                        f"{basename_of(folder)} ({position + 1})",
                        max_chars=layout.max_component_chars,
                    ),
                )
                problems.append(
                    PlanProblem(
                        kind=Problem.ALBUM_COLLISION,
                        source=suggestion.anchor,
                        destination=folder,
                        detail=(
                            f"два события просят одну папку «{basename_of(folder)}»: "
                            f"{start.isoformat()}…{end.isoformat()} ({event.size} снимков) "
                            f"и ещё {len(claimants) - 1}. Предложено "
                            f"«{basename_of(target)}» — переименуйте или объедините вручную"
                        ),
                    )
                )
            folder_of[index] = target
            albums.append(
                PlannedAlbum(
                    folder=target,
                    name=suggestion.name,
                    anchor=suggestion.anchor,
                    start=start,
                    end=end,
                    photos=event.size,
                    source=suggestion.primary.source,
                    confirmed=bool(suggestion.confirmed),
                )
            )
    return folder_of, in_month, albums, problems


def plan_library(
    clustering: EventClustering,
    naming: AlbumNaming,
    *,
    records: Mapping[str, FileRecord],
    layout: LibraryLayout,
    content_keys: Mapping[str, str] | None = None,
    origins: Mapping[str, str] | None = None,
    capture_dates: Mapping[str, dt.date] | None = None,
    probe: PlanProbe | None = None,
) -> LibraryPlan:
    """Build the whole plan. Reads; never writes.

    `records` is `display_path -> FileRecord` for everything the scan saw —
    the planner needs the size (how many bytes a cross-volume copy costs)
    and Р8's ordering, both of which live on the record rather than on a
    `PhotoMoment`. `content_keys` is `display_path -> full_hash`;
    `origins` is `display_path -> origin_class` and is only consulted for
    the files Р3 excluded from events, to tell a screenshot from a scan.

    `capture_dates` exists for exactly those excluded files. Р3 drops them
    before clustering, so they are in no event and their date is nowhere in
    the clustering — yet Р5 files a screenshot under `_screenshots/<year>/`,
    which needs one. Without it every screenshot lands in
    `_screenshots/_undated/`, which is a true statement about this
    function's inputs and a false one about the photograph.
    """
    keys = content_keys or {}
    origin_map = origins or {}
    probe = probe or PathProbe()
    plan = LibraryPlan(layout=layout, probe_blind=bool(getattr(probe, "blind", False)))

    folder_of, in_month, albums, album_problems = _plan_albums(clustering, naming, layout)
    plan.albums = albums
    plan.problems.extend(album_problems)

    # --- every file this plan is responsible for, with where it belongs ---
    #
    # One pass to say *which tree* each path goes to, before deciding which
    # copy of each content actually moves: the keeper of a duplicate group
    # can sit in a different event from its twins, and picking the mover
    # per event would let the same bytes be planned twice.
    home: dict[str, tuple[Bucket, str, dt.date | None, str]] = {}
    for index, event in enumerate(clustering.events):
        folder = folder_of[index]
        album = next((a.name for a in albums if a.folder == folder), "")
        start, _ = event.date_range
        bucket = Bucket.MONTH if index in in_month else Bucket.EVENT
        for moment in event.moments:
            home[moment.display_path] = (bucket, folder, start, album)

    for moment in clustering.undated:
        home[moment.display_path] = (Bucket.UNSORTED, layout.unsorted_folder(), None, "")

    moment_dates: dict[str, dt.date | None] = {
        m.display_path: m.local_date()
        for event in clustering.events
        for m in event.moments
    }
    moment_dates.update(capture_dates or {})
    for path in clustering.excluded:
        bucket = _bucket_for(origin_map.get(path))
        if bucket is Bucket.SCREENSHOTS:
            folder = layout.screenshots_folder(moment_dates.get(path))
        elif bucket is Bucket.DOCUMENTS:
            folder = layout.documents_folder()
        else:
            folder = layout.unsorted_folder()
        home[path] = (bucket, folder, None, "")

    # --- one content, one mover (Р8) ---------------------------------------
    by_content: dict[str, list[str]] = {}
    for path in home:
        by_content.setdefault(_content_key(path, keys), []).append(path)

    movers: dict[str, str] = {}
    for key, paths in by_content.items():
        group = _records_for(paths, records)
        if not group:
            for path in sorted(paths):
                plan.problems.append(
                    PlanProblem(
                        kind=Problem.NO_RECORD,
                        source=path,
                        destination="",
                        detail=(
                            "файла нет в индексе этого скана — планировать перенос "
                            "по пути без размера и времени нельзя"
                        ),
                    )
                )
            continue
        if any(r.is_archive_member for r in group):
            for record in group:
                if record.is_archive_member:
                    plan.problems.append(
                        PlanProblem(
                            kind=Problem.ARCHIVE_MEMBER,
                            source=record.display_path,
                            destination="",
                            detail=(
                                "участник архива: вынуть один файл нельзя, архив "
                                "пришлось бы пересобрать (Р1) — в библиотеку не переезжает"
                            ),
                        )
                    )
            group = [r for r in group if not r.is_archive_member]
            if not group:
                continue
        movers[key] = choose_keeper(group).display_path

    # --- destinations ------------------------------------------------------
    names = _FolderNames(probe)
    ordered = sorted(
        movers.items(), key=lambda kv: (home[kv[1]][1], basename_of(kv[1]), kv[1])
    )
    for key, source in ordered:
        bucket, folder, _, album = home[source]
        record = records[source]
        filename, renamed_from = names.claim(folder, basename_of(source))
        destination = join_path(folder, filename)

        if len(destination) > layout.max_path_chars:
            plan.problems.append(
                PlanProblem(
                    kind=Problem.PATH_TOO_LONG,
                    source=source,
                    destination=destination,
                    detail=(
                        f"{len(destination)} символов — Windows не откроет путь длиннее "
                        f"{layout.max_path_chars} без префикса \\\\?\\; сократите "
                        "название альбома или корень библиотеки"
                    ),
                )
            )
            continue

        if source == destination:
            plan.already_in_place.append(source)
            continue

        if probe.exists(destination):
            # `_FolderNames.claim` already routes around an occupied name,
            # so reaching here means the destination appeared between the
            # two checks or the probe is inconsistent. Reported rather
            # than overwritten: nothing in this project overwrites a file.
            plan.problems.append(
                PlanProblem(
                    kind=Problem.DESTINATION_EXISTS,
                    source=source,
                    destination=destination,
                    detail="по целевому пути уже лежит файл — не затираю",
                )
            )
            continue

        if probe.is_busy(source):
            plan.problems.append(
                PlanProblem(
                    kind=Problem.BUSY,
                    source=source,
                    destination=destination,
                    detail=(
                        "файл открыт другим процессом — по Р5 пропускается с "
                        "предупреждением, остальной план это не отменяет"
                    ),
                )
            )
            continue

        transfer = (
            Transfer.RENAME
            if probe.volume_of(source) == probe.volume_of(destination)
            else Transfer.COPY_VERIFY
        )
        others = tuple(sorted(p for p in by_content[key] if p != source))
        reason = _move_reason(bucket, album, renamed_from, others, transfer)
        plan.moves.append(
            PlannedMove(
                source=source,
                destination=destination,
                bucket=bucket,
                transfer=transfer,
                size=record.size,
                content_key=key,
                album=album,
                also_at=others,
                renamed_from=renamed_from,
                reason=reason,
            )
        )
        for other in others:
            plan.redundant.append(
                RedundantCopy(
                    source=other,
                    content_key=key,
                    size=records[other].size if other in records else 0,
                    kept=source,
                    destination=destination,
                    reason=(
                        "байт-в-байт та же копия; в библиотеку переезжает одна "
                        "(Р8), эта остаётся на месте — убрать её это решение "
                        "об избыточности, а не об организации (Р0)"
                    ),
                )
            )

    if clustering.warnings:
        plan.warnings.extend(clustering.warnings)
    if plan.probe_blind:
        plan.warnings.append(
            "план построен без доступа к файлам: занятость и занятые целевые пути "
            "не проверялись, режим переноса определён по буквам диска"
        )
    return plan


def _move_reason(
    bucket: Bucket,
    album: str,
    renamed_from: str | None,
    others: Sequence[str],
    transfer: Transfer,
) -> str:
    if bucket is Bucket.EVENT:
        head = f"событие «{album}»" if album else "событие"
    elif bucket is Bucket.MONTH:
        head = (
            f"день слишком мал для своей папки — в месяц «{album}»"
            if album
            else "день слишком мал для своей папки — в папку месяца"
        )
    elif bucket is Bucket.SCREENSHOTS:
        head = "скриншот — вне событий по Р3, своя ветка по Р5"
    elif bucket is Bucket.DOCUMENTS:
        head = "скан документа — вне событий по Р3, своя ветка по Р5"
    else:
        head = "времени съёмки нет — в _unsorted/ по Р5, а не в придуманный месяц"
    parts = [head]
    if others:
        parts.append(f"канонический путь для {len(others) + 1} копий")
    if renamed_from:
        parts.append(f"имя занято другим файлом, переименовано из «{renamed_from}»")
    if transfer is Transfer.COPY_VERIFY:
        parts.append("другой том: копирование со сверкой хэша, потом удаление источника")
    return "; ".join(parts)


def plan_from_dict(payload: Mapping) -> LibraryPlan:
    """Rebuild a plan saved by `to_dict` — задача 22's entry point.

    The round trip exists so execution runs the plan a person read, not a
    recomputed one: `fingerprint()` on the rebuilt plan equals the
    fingerprint printed when it was shown, and a plan whose thresholds or
    library have moved underneath it will not match.
    """
    layout = LibraryLayout(**payload["layout"])
    plan = LibraryPlan(layout=layout)
    for raw in payload.get("moves", []):
        plan.moves.append(
            PlannedMove(
                source=raw["source"],
                destination=raw["destination"],
                bucket=Bucket(raw["bucket"]),
                transfer=Transfer(raw["transfer"]),
                size=raw["size"],
                content_key=raw["content_key"],
                album=raw.get("album", ""),
                also_at=tuple(raw.get("also_at", ())),
                renamed_from=raw.get("renamed_from"),
                reason=raw.get("reason", ""),
            )
        )
    for raw in payload.get("redundant", []):
        plan.redundant.append(RedundantCopy(**raw))
    for raw in payload.get("problems", []):
        plan.problems.append(
            PlanProblem(
                kind=Problem(raw["kind"]),
                source=raw["source"],
                destination=raw["destination"],
                detail=raw.get("detail", ""),
            )
        )
    for raw in payload.get("albums", []):
        plan.albums.append(
            PlannedAlbum(
                folder=raw["folder"],
                name=raw["name"],
                anchor=raw["anchor"],
                start=dt.date.fromisoformat(raw["start"]),
                end=dt.date.fromisoformat(raw["end"]),
                photos=raw["photos"],
                source=raw["source"],
                confirmed=raw["confirmed"],
                merged_events=raw.get("merged_events", 1),
            )
        )
    plan.already_in_place = list(payload.get("already_in_place", []))
    plan.warnings = list(payload.get("warnings", []))
    plan.probe_blind = bool(payload.get("summary", {}).get("probe_blind", False))
    return plan
