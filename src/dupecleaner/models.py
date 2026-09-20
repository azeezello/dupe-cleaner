"""Data model shared by the scanner, dedupe engine, quarantine and web layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath


class MediaKind(str, Enum):
    NONE = "none"
    PHOTO = "photo"
    VIDEO = "video"


class ScanMode(str, Enum):
    """How much of the disk a scan run actually looks at (Р7).

    The difference between the two modes is **coverage, never confidence**.
    Both run the same three-stage funnel through to a full, byte-for-byte
    hash, so a duplicate group means exactly the same thing in either one
    and the Р0 invariant — only byte-confirmed content may be quarantined —
    holds unchanged. Р7 considered and rejected the other reading of
    "fast" (group by name and size, act on that): it would have traded the
    guarantee rather than the amount of work, and on a real photo archive
    two different shots named `IMG_0001.jpg` of equal size are common, not
    hypothetical.

    What QUICK gives up, and nothing else:

    - **Archives are not opened at all.** They land in
      `ScanReport.skipped_archives` with reason ``excluded_by_mode``, so a
      run that never looked inside cannot be misread as one that looked
      and found nothing (finding A1). A consequence worth stating out
      loud: a quick run can only ever hand an archive the Р1 verdict
      UNREAD, so it can never grant permission to move one. That is
      enforced explicitly in `quarantine.quarantine_archives` rather than
      left to fall out of the mechanism.
    - **No previews and no quality metrics.** Both come out of one image
      decode per unique content hash — measured at roughly 4.5–9 minutes
      over the pilot's 6000 groups (задача 8; задача 9 added the metrics
      to that same decode for a few percent more) — for something only the
      review screen needs.

    Neither omission touches how a duplicate is established, which is the
    whole point of the split.
    """

    QUICK = "quick"
    FULL = "full"

    @property
    def include_archives(self) -> bool:
        return self is ScanMode.FULL

    @property
    def generate_previews(self) -> bool:
        return self is ScanMode.FULL


@dataclass(frozen=True)
class FileRecord:
    """One discovered file — either a real file on disk, or a member inside
    an archive. `real_path` is always a real filesystem path: for archive
    members it points at the *archive itself* (never at the member), because
    the member is not independently addressable on disk.
    """

    display_path: str          # human-readable path, e.g. "D:/a.zip::photos/img.jpg"
    real_path: str             # actual filesystem path (the archive, for members)
    size: int
    mtime: float
    media_kind: MediaKind = MediaKind.NONE
    is_archive_member: bool = False
    archive_path: str | None = None   # set only if is_archive_member
    member_name: str | None = None    # set only if is_archive_member

    # size+mtime of the real file on disk this record's bytes come from —
    # for an archive member that's the archive, not the member. The hash
    # cache is stamped with these, so a changed archive invalidates the
    # cached hashes of everything inside it. Defaults to the record's own
    # size/mtime, which is correct for plain files.
    source_size: int | None = None
    source_mtime: float | None = None

    @property
    def effective_source_size(self) -> int:
        return self.source_size if self.source_size is not None else self.size

    @property
    def effective_source_mtime(self) -> float:
        return self.source_mtime if self.source_mtime is not None else self.mtime

    @property
    def is_media(self) -> bool:
        return self.media_kind is not MediaKind.NONE

    @property
    def extension(self) -> str:
        name = self.member_name if self.is_archive_member else self.real_path
        return PurePosixPath(name.replace("\\", "/")).suffix.lower()


@dataclass(frozen=True)
class SkippedArchive:
    """An archive that was *not* looked inside during a scan run — either
    because the run used `--no-archives` (finding A1: a fast scan over a
    folder of archives used to report "0 duplicate groups" with nothing to
    show it never looked, which reads as "no duplicates" instead of "not
    checked"), or because the archive itself couldn't be opened.

    Kept separate from `warnings` (plain, unstructured strings) so a report
    can show "N archives not checked in this mode" as its own list rather
    than folding it into free-text warnings a person has to read fully to
    notice.
    """

    path: str
    size: int
    reason: str  # e.g. "excluded_by_mode" or "unreadable"


class ArchiveClass(str, Enum):
    """How a whole archive relates to what is already loose on disk (Р1).

    Р1 makes the *archive*, not the file inside it, the unit of action:
    pulling one member out of an archive would mean rewriting the archive,
    which is the one thing this project promised never to do. So an archive
    gets exactly one verdict, and that verdict decides what may happen to it.

    - FULLY_REDUNDANT   every member has a byte-identical twin sitting loose
                        on disk -> the archive itself may be quarantined as a
                        single file (after the twins are re-verified, see
                        `quarantine.quarantine_archives`).
    - PARTIALLY_REDUNDANT  some members do, some don't -> report only.
                        "Dissolving" such an archive (unpack the unique part
                        next to it, quarantine the rest) is explicitly out of
                        scope in Р1.
    - UNIQUE            no member has a loose twin -> leave it alone.
    - UNREAD            at least one member could not be read (password,
                        corruption, missing unrar), so the archive's content
                        is not fully known -> it goes in the report's
                        "not checked" list, never into an action.
    """

    FULLY_REDUNDANT = "fully_redundant"
    PARTIALLY_REDUNDANT = "partially_redundant"
    UNIQUE = "unique"
    UNREAD = "unread"


@dataclass
class ArchiveStat:
    """What one scan run learned about one archive, as facts rather than
    conclusions: how many members it holds and how many of those could not
    be read. `archive_classify` turns these into an `ArchiveVerdict`.

    This exists because a duplicate report alone cannot answer "is this
    archive fully redundant?". A report only ever lists files that *have*
    duplicates, so a member with no twin is invisible in it — and so is a
    member that failed to decrypt. Without `members_total` an archive whose
    every listed member is redundant is indistinguishable from one where
    three members out of a thousand are, and without `members_unreadable` an
    archive we could not actually read looks exactly like one we read and
    found nothing in. Both confusions are the same species as pilot finding
    A1: "not checked" presented as a verdict.
    """

    path: str
    size: int
    # False when the archive itself could not be opened or listed at all
    # (corrupt file, encrypted headers, no unrar). Distinguishes that from
    # a genuinely empty archive, which opens fine and has zero members.
    opened: bool = True
    members_total: int = 0
    # Members that were enumerated (so we know their names and sizes) but
    # whose bytes could not be read — the classic case being a
    # password-protected zip/7z, whose directory is readable while its
    # content is not. The pilot never saw one of these: the single archive
    # it found opened without trouble.
    members_unreadable: int = 0
    error: str | None = None  # first failure seen, verbatim

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "opened": self.opened,
            "members_total": self.members_total,
            "members_unreadable": self.members_unreadable,
            "error": self.error,
        }

    @staticmethod
    def from_dict(data: dict) -> "ArchiveStat":
        return ArchiveStat(
            path=data["path"],
            size=data["size"],
            opened=data.get("opened", True),
            members_total=data.get("members_total", 0),
            members_unreadable=data.get("members_unreadable", 0),
            error=data.get("error"),
        )


@dataclass(frozen=True)
class ArchiveVerdict:
    """One archive's Р1 class plus the counts it was derived from, so a
    person (or a UI) can see *why* without re-running anything.
    """

    path: str
    size: int
    verdict: ArchiveClass
    members_total: int
    members_redundant: int
    members_unreadable: int
    redundant_bytes: int
    reason: str | None = None

    @property
    def is_actionable(self) -> bool:
        """Only a fully redundant archive may ever be moved (Р1)."""
        return self.verdict is ArchiveClass.FULLY_REDUNDANT

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "verdict": self.verdict.value,
            "members_total": self.members_total,
            "members_redundant": self.members_redundant,
            "members_unreadable": self.members_unreadable,
            "redundant_bytes": self.redundant_bytes,
            "reason": self.reason,
        }


@dataclass
class DuplicateGroup:
    """A set of >=2 FileRecords that are byte-identical to each other."""

    content_hash: str
    records: list[FileRecord] = field(default_factory=list)

    @property
    def size(self) -> int:
        return self.records[0].size if self.records else 0

    @property
    def wasted_bytes(self) -> int:
        """Space that would be reclaimed by keeping exactly one copy."""
        return self.size * max(0, len(self.records) - 1)

    @property
    def is_media(self) -> bool:
        return any(r.is_media for r in self.records)

    @property
    def has_archive_members(self) -> bool:
        return any(r.is_archive_member for r in self.records)

    @property
    def only_archive_members(self) -> bool:
        return all(r.is_archive_member for r in self.records)


@dataclass
class ScanReport:
    scanned_roots: list[str]
    total_files_seen: int
    groups: list[DuplicateGroup]
    warnings: list[str] = field(default_factory=list)
    # Archives not looked inside during this run — see SkippedArchive.
    # Always present (possibly empty), so a reader never has to infer "not
    # checked" from the absence of a field the way finding A1 describes.
    skipped_archives: list[SkippedArchive] = field(default_factory=list)
    # One entry per archive the scan met, readable or not — the raw counts
    # `archive_classify.classify_archives` needs to give each archive a Р1
    # verdict. See ArchiveStat for why the groups alone aren't enough.
    archives: list[ArchiveStat] = field(default_factory=list)
    # Which mode produced this report. Carried on the report rather than
    # kept in the caller, because the report outlives the run: it is
    # written to report.json and read back by `quarantine`, possibly days
    # later, and the answer to "may this report authorise moving an
    # archive?" has to travel with it. A report from before this field
    # existed reads back as FULL, which is what those runs did.
    mode: ScanMode = ScanMode.FULL

    @property
    def total_wasted_bytes(self) -> int:
        return sum(g.wasted_bytes for g in self.groups)

    def to_dict(self) -> dict:
        return {
            "scanned_roots": self.scanned_roots,
            "total_files_seen": self.total_files_seen,
            "total_wasted_bytes": self.total_wasted_bytes,
            "warnings": self.warnings,
            "skipped_archives": [
                {"path": a.path, "size": a.size, "reason": a.reason}
                for a in self.skipped_archives
            ],
            "archives": [a.to_dict() for a in self.archives],
            "mode": self.mode.value,
            "groups": [
                {
                    "content_hash": g.content_hash,
                    "size": g.size,
                    "wasted_bytes": g.wasted_bytes,
                    "is_media": g.is_media,
                    "has_archive_members": g.has_archive_members,
                    "only_archive_members": g.only_archive_members,
                    "records": [
                        {
                            "display_path": r.display_path,
                            "real_path": r.real_path,
                            "size": r.size,
                            "mtime": r.mtime,
                            "media_kind": r.media_kind.value,
                            "is_archive_member": r.is_archive_member,
                            "archive_path": r.archive_path,
                            "member_name": r.member_name,
                        }
                        for r in g.records
                    ],
                }
                for g in self.groups
            ],
        }

    @staticmethod
    def from_dict(data: dict) -> "ScanReport":
        groups = []
        for g in data["groups"]:
            records = [
                FileRecord(
                    display_path=r["display_path"],
                    real_path=r["real_path"],
                    size=r["size"],
                    mtime=r["mtime"],
                    media_kind=MediaKind(r["media_kind"]),
                    is_archive_member=r["is_archive_member"],
                    archive_path=r.get("archive_path"),
                    member_name=r.get("member_name"),
                )
                for r in g["records"]
            ]
            groups.append(DuplicateGroup(content_hash=g["content_hash"], records=records))
        skipped_archives = [
            SkippedArchive(path=a["path"], size=a["size"], reason=a["reason"])
            for a in data.get("skipped_archives", [])
        ]
        return ScanReport(
            scanned_roots=data["scanned_roots"],
            total_files_seen=data["total_files_seen"],
            groups=groups,
            warnings=data.get("warnings", []),
            skipped_archives=skipped_archives,
            archives=[ArchiveStat.from_dict(a) for a in data.get("archives", [])],
            mode=ScanMode(data.get("mode", ScanMode.FULL.value)),
        )
