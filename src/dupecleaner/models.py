"""Data model shared by the scanner, dedupe engine, quarantine and web layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath


class MediaKind(str, Enum):
    NONE = "none"
    PHOTO = "photo"
    VIDEO = "video"


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
        )
