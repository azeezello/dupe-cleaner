"""Р1: classify a whole archive, and re-verify its twins before anything moves.

Why the archive and not the file
--------------------------------
You cannot take one file out of an archive without rewriting the archive,
and rewriting an archive is the single operation this project has promised
never to perform. Р1 draws the conclusion: the archive is the unit of
action. It gets exactly one verdict — fully redundant / partially redundant
/ unique / unread — and only the first of those grants permission to move
anything.

How a verdict is derived
------------------------
A duplicate report lists only files that have duplicates, which is not
enough on its own: a member with no twin simply isn't in it, and neither is
a member that failed to decrypt. So classification reads two things — the
report's groups, and the per-archive counts the scanner recorded
(`models.ArchiveStat`). A member counts as *redundant* when its duplicate
group also contains a plain file on disk.

Two rules in that sentence carry the whole safety argument:

**"a plain file on disk", never another archive member.** If archive A and
archive B are copies of each other, each one's members are "duplicated" —
by the other. Counting that as redundancy would declare both fully
redundant, quarantine both, and the day the user empties the quarantine
folder the content is gone from every place it ever existed. A twin only
counts when it is a loose file that stays where it is.

**"every member", with unreadable members disqualifying the archive.** An
encrypted or damaged member's bytes are unknown, and unknown content cannot
be declared redundant. Such an archive is UNREAD, not FULLY_REDUNDANT and
not PARTIALLY_REDUNDANT — it lands in the report's "not checked" list
(`ScanReport.skipped_archives`) so it reads as "we did not look inside",
which is the truth. Note this falls out of the mechanism as well as the
rule: a member whose bytes could not be read cannot be in any duplicate
group, so it can never be counted redundant.

The inverse question — "is UNIQUE honest when we never read most members?"
— also resolves cleanly. The funnel only reads a member whose *size*
matches something else, and size comes from the archive's directory, which
we did read. If no file on disk has that member's size, no file on disk can
be byte-identical to it. So "no member has a loose twin" is a sound
conclusion from sizes alone, without decompressing anything.

Verification before the move
----------------------------
Р1 attaches a hard precondition to quarantining a fully redundant archive:
re-check that every twin is present and readable first. The scenario it
guards against is the twins having lived on an external drive that is no
longer plugged in — the scan saw them, the quarantine would not, and the
archive would be the only remaining copy of content its own report called
redundant.

`verify_members` therefore re-reads each twin *in full* and re-hashes it,
rather than calling `os.path.exists`. Reading it is what proves "readable";
hashing the bytes we are already reading is nearly free (xxh3 runs at
~10 GB/s, far above any disk) and upgrades the check from "a file of the
right size is at that path" to "the same bytes are still there". A twin
that was silently truncated, half-restored from backup, or replaced by a
different photo with the same size fails here rather than after the archive
is gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .hashing import full_hash
from .models import (
    ArchiveClass,
    ArchiveStat,
    ArchiveVerdict,
    ScanReport,
    SkippedArchive,
)


@dataclass(frozen=True)
class MemberTwins:
    """One archive member together with the loose files on disk that hold
    the same bytes. `twin_paths` is ordered with the most "canonical" path
    first (shortest, then oldest), which is also the copy a file-level
    quarantine keeps in place — so the first candidate is normally the one
    that will still be there afterwards.
    """

    archive_path: str
    member_name: str
    size: int
    content_hash: str
    twin_paths: tuple[str, ...]


def member_twins(report: ScanReport) -> dict[str, list[MemberTwins]]:
    """Map every archive path to the members of it that have at least one
    byte-identical **plain file** on disk.

    Archive-to-archive duplication is deliberately not counted; see this
    module's docstring for the mutual-vouching failure it would cause.
    """
    by_archive: dict[str, list[MemberTwins]] = {}
    for group in report.groups:
        plain = [r for r in group.records if not r.is_archive_member]
        if not plain:
            continue
        plain.sort(key=lambda r: (len(r.display_path), r.mtime))
        twin_paths = tuple(r.real_path for r in plain)

        for record in group.records:
            if not record.is_archive_member or record.archive_path is None:
                continue
            by_archive.setdefault(record.archive_path, []).append(
                MemberTwins(
                    archive_path=record.archive_path,
                    member_name=record.member_name or "",
                    size=record.size,
                    content_hash=group.content_hash,
                    twin_paths=twin_paths,
                )
            )
    return by_archive


def classify_archive(stat: ArchiveStat, twins: list[MemberTwins]) -> ArchiveVerdict:
    """Give one archive its Р1 verdict. Precedence, strictest first:

    1. the archive could not be opened at all -> UNREAD;
    2. any member could not be read -> UNREAD (its content is unknown, and
       unknown content is never redundant);
    3. no members at all -> UNIQUE (there is nothing to have copies of, so
       "fully redundant" would be a vacuous truth granting permission to
       move a file for no reason);
    4. every member has a loose twin -> FULLY_REDUNDANT;
    5. some do -> PARTIALLY_REDUNDANT;
    6. none do -> UNIQUE.
    """
    by_member = {t.member_name: t for t in twins}
    members_redundant = len(by_member)
    redundant_bytes = sum(t.size for t in by_member.values())

    def verdict(kind: ArchiveClass, reason: str | None = None) -> ArchiveVerdict:
        return ArchiveVerdict(
            path=stat.path,
            size=stat.size,
            verdict=kind,
            members_total=stat.members_total,
            members_redundant=members_redundant,
            members_unreadable=stat.members_unreadable,
            redundant_bytes=redundant_bytes,
            reason=reason,
        )

    if not stat.opened:
        return verdict(ArchiveClass.UNREAD, stat.error or "архив не открылся")

    if stat.members_unreadable:
        return verdict(
            ArchiveClass.UNREAD,
            f"не прочитано участников: {stat.members_unreadable} из "
            f"{stat.members_total}"
            + (f" ({stat.error})" if stat.error else ""),
        )

    if stat.members_total == 0:
        return verdict(ArchiveClass.UNIQUE, "архив пуст")

    if members_redundant >= stat.members_total:
        return verdict(ArchiveClass.FULLY_REDUNDANT)

    if members_redundant:
        return verdict(ArchiveClass.PARTIALLY_REDUNDANT)

    return verdict(ArchiveClass.UNIQUE)


def classify_archives(report: ScanReport) -> list[ArchiveVerdict]:
    """Verdict for every archive the scan met, in the order they were met."""
    twins = member_twins(report)
    return [classify_archive(stat, twins.get(stat.path, [])) for stat in report.archives]


def unread_skipped_entries(
    verdicts: list[ArchiveVerdict], existing: list[SkippedArchive]
) -> list[SkippedArchive]:
    """The `SkippedArchive` rows that UNREAD verdicts add to a report.

    Task 3 already routes archives that fail to *open* into
    `ScanReport.skipped_archives`. An archive whose directory opened fine
    while its members refused to decrypt used to slip past that: every
    failure became one more line in free-text `warnings`, and the archive
    itself was never listed as unchecked. This closes that gap with the
    same list rather than a second one — "not checked" stays in exactly one
    place, which was the point of finding A1.

    Entries already present (by path) are not duplicated.
    """
    known = {s.path for s in existing}
    return [
        SkippedArchive(path=v.path, size=v.size, reason="unreadable_members")
        for v in verdicts
        if v.verdict is ArchiveClass.UNREAD and v.path not in known
    ]


@dataclass
class TwinVerification:
    """Outcome of re-checking one archive's twins right before a move."""

    archive_path: str
    ok: bool
    verified: list[dict]        # [{member, twin, size}]
    failures: list[dict]        # [{member, reason}]

    @property
    def failure_summary(self) -> str:
        if not self.failures:
            return ""
        first = self.failures[0]
        extra = f" (и ещё {len(self.failures) - 1})" if len(self.failures) > 1 else ""
        return f"{first['member']}: {first['reason']}{extra}"


def verify_member(twin: MemberTwins) -> tuple[str | None, str]:
    """Re-read this member's twins on disk until one proves it still holds
    the same bytes. Returns `(vouching_path_or_None, reason)`.

    Every candidate is tried, not just the first: a file-level quarantine
    run may have moved some of the copies, and the guarantee Р1 asks for is
    "a copy is present and readable", not "this particular copy is".
    """
    reasons: list[str] = []
    for path in twin.twin_paths:
        candidate = Path(path)
        try:
            stat = candidate.stat()
        except OSError as exc:
            reasons.append(f"{path}: {exc}")
            continue
        if stat.st_size != twin.size:
            reasons.append(
                f"{path}: размер изменился ({stat.st_size} вместо {twin.size})"
            )
            continue
        try:
            with open(candidate, "rb") as fh:
                digest = full_hash(fh)
        except OSError as exc:
            reasons.append(f"{path}: не читается ({exc})")
            continue
        if digest != twin.content_hash:
            reasons.append(f"{path}: содержимое изменилось")
            continue
        return path, ""
    return None, "; ".join(reasons) if reasons else "двойников на диске не осталось"


def verify_members(archive_path: str, twins: list[MemberTwins]) -> TwinVerification:
    """Run `verify_member` for every member of one archive.

    Stops at nothing — every member is checked even after the first
    failure, so the report can say how bad it is rather than just that it
    failed.
    """
    verified: list[dict] = []
    failures: list[dict] = []
    for twin in twins:
        vouching, reason = verify_member(twin)
        if vouching is None:
            failures.append({"member": twin.member_name, "reason": reason})
        else:
            verified.append(
                {"member": twin.member_name, "twin": vouching, "size": twin.size}
            )
    return TwinVerification(
        archive_path=archive_path,
        ok=not failures and bool(verified),
        verified=verified,
        failures=failures,
    )
