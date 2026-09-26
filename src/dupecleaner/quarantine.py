r"""Turns a list of confirmed DuplicateGroups into safe filesystem actions.

Nothing here ever calls os.remove(). The only destructive-looking action is
`shutil.move` into the quarantine folder, which is reversible (see
docs/SAFETY.md for the manual restore procedure, or `dupecleaner restore`)
— actual permanent deletion of the quarantine folder is a separate,
explicit step the user takes themselves once they're satisfied.

Two categories are *never* auto-moved, even when a group is "confirmed":
- Archive members: modifying an archive in place risks corrupting it, so
  these are only ever reported (with a recommendation of which copy to
  keep), never touched.
- Media files (photos/videos): require `confirm_media=True` to be passed
  explicitly for that call, on top of being in the confirmed group list —
  a deliberate extra step so a batch confirmation of "regular file"
  duplicates can never accidentally sweep up a family photo.

Journal and restore (Р5: "каждая операция пишется в журнал до её
выполнения")
-----------------------------------------------------------------
Every single-file move is recorded in an append-only journal
(`journal.jsonl` in the quarantine root) *before* the move happens, and the
outcome is appended right after. Each line is flushed and fsync'd
immediately, so the journal reflects reality even if the process is killed
between two file moves — the original bug this fixes was `manifest.json`
being written once, after the whole batch, which meant a crash on file
3000 of 6000 left 3000 files quarantined with no record of where they
came from and no way back.

`manifest.json` is still written once per `run_quarantine` call, as a
convenience summary of that run — but it is no longer the source of truth
for anything. `restore_from_journal` reads only `journal.jsonl`, and a
missing or stale `manifest.json` doesn't affect its correctness.

Quarantine layout mirrors the source path structure (matching what
README.md promises), e.g. `D:\Photos\Wedding\a.jpg` -> `<quarantine
root>/D/Photos/Wedding/a.jpg`. Earlier this flattened everything into
`<quarantine root>/<hash prefix>/<basename>`, which made a 6299-file
quarantine unnavigable and wasn't what the docs described. Restore never
has to guess a destination name back from this layout, though — it always
uses the exact `original`/`quarantined` paths recorded in the journal.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .archive_classify import MemberTwins, member_twins, verify_members
# Р8 lives in its own module because `archive_classify` needs the same
# ordering and importing it from here would be circular. Re-exported under
# this name because `quarantine.choose_keeper` is where it has always been.
from .keeper import choose_keeper
from .models import (
    ArchiveClass,
    ArchiveVerdict,
    DuplicateGroup,
    FileRecord,
    ScanMode,
    ScanReport,
)

JOURNAL_FILENAME = "journal.jsonl"
MANIFEST_FILENAME = "manifest.json"

_WINDOWS_DRIVE_RE = re.compile(r"^([A-Za-z]):/(.*)$")


@dataclass
class QuarantineResult:
    kept: dict[str, str] = field(default_factory=dict)  # group hash -> kept path
    moved: list[dict] = field(default_factory=list)      # [{original, quarantined, group_hash}]
    failed: list[dict] = field(default_factory=list)     # [{original, quarantined, group_hash, error}]
    pending_media_review: list[dict] = field(default_factory=list)
    archive_only_notes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": time.time(),
            "kept": self.kept,
            "moved": self.moved,
            "failed": self.failed,
            "pending_media_review": self.pending_media_review,
            "archive_only_notes": self.archive_only_notes,
        }


@dataclass
class ArchiveQuarantineResult:
    """Outcome of the Р1 archive pass: whole archives moved, and every
    archive that was looked at and deliberately left alone, with why.

    The "left alone" half is not padding. A run that moves two archives out
    of forty needs to say what it decided about the other thirty-eight,
    otherwise the only readable outcome is "something happened" — the same
    complaint pilot finding A1 made about a fast scan reporting zero
    duplicate groups.
    """

    moved: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)
    pending_media_review: list[dict] = field(default_factory=list)
    not_actionable: list[dict] = field(default_factory=list)

    @property
    def freed_bytes(self) -> int:
        return sum(item.get("size", 0) for item in self.moved)

    def to_dict(self) -> dict:
        return {
            "generated_at": time.time(),
            "moved": self.moved,
            "failed": self.failed,
            "refused": self.refused,
            "pending_media_review": self.pending_media_review,
            "not_actionable": self.not_actionable,
            "freed_bytes": self.freed_bytes,
        }


@dataclass
class RestoreResult:
    restored: list[dict] = field(default_factory=list)  # [{original, quarantined, group_hash}]
    skipped: list[dict] = field(default_factory=list)    # [{original, quarantined, group_hash, reason}]

    def to_dict(self) -> dict:
        return {
            "generated_at": time.time(),
            "restored": self.restored,
            "skipped": self.skipped,
        }


class _JournalWriter:
    """Append-only writer for `journal.jsonl`. Every `write()` is flushed
    and fsync'd before returning, so the line is durable on disk before the
    caller goes on to do the thing the line describes (or, for a
    completion line, before anything else can happen that might crash).
    This is what makes the journal — not `manifest.json` — the safe source
    of truth for `restore_from_journal`.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")

    def write(self, entry: dict) -> None:
        self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "_JournalWriter":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _read_journal(journal_path: Path) -> list[dict]:
    if not journal_path.exists():
        return []
    entries: list[dict] = []
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            # Each write() is flushed+fsync'd as a whole line before the
            # next one starts, so a torn line can only ever be the very
            # last one (process killed mid-write). Drop it rather than
            # fail the whole read — everything before it is still intact.
            continue
    return entries


def _operations_from_journal(entries: list[dict]) -> dict[str, dict]:
    """Collapse a flat event stream into one record per op_id, keeping the
    original move_pending fields plus which events were seen for it.
    """
    ops: dict[str, dict] = {}
    for entry in entries:
        op_id = entry.get("op_id")
        if not op_id:
            continue
        op = ops.setdefault(op_id, {"events": []})
        event = entry.get("event")
        op["events"].append(event)
        if event == "move_pending":
            op.update({k: v for k, v in entry.items() if k not in ("event",)})
        elif event == "move_failed":
            op["move_error"] = entry.get("error")
        elif event == "restore_failed":
            op["restore_error"] = entry.get("reason")
    return ops


def _mirrored_relative_parts(source: Path) -> list[str]:
    """Turn an absolute source path into path segments to nest under the
    quarantine root, preserving directory structure end to end (README:
    "с сохранением структуры путей"). Works on the path's *text*, not
    `pathlib`'s host-OS parsing, so a Windows-style path (`D:\\Photos\\x`)
    mirrors correctly even when this code runs on Linux, and vice versa.

    A Windows drive letter becomes a top-level folder (`D:\\Photos\\x.jpg`
    -> `D/Photos/x.jpg`) since `:` isn't a legal path character on
    Windows. A POSIX absolute path keeps its segments as-is.
    """
    normalized = str(source).replace("\\", "/")
    m = _WINDOWS_DRIVE_RE.match(normalized)
    if m:
        drive, rest = m.group(1).upper(), m.group(2)
        parts = [drive] + [p for p in rest.split("/") if p]
    else:
        parts = [p for p in normalized.split("/") if p]
    return parts or ["_root"]


def _mirrored_destination(quarantine_root: Path, source: Path) -> Path:
    parts = _mirrored_relative_parts(source)
    return quarantine_root.joinpath(*parts)


def _unique_destination(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 1
    while True:
        candidate = directory / f"{stem}__{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _move_one(
    source: Path,
    quarantine_root: Path,
    group_hash: str | None,
    size: int,
    journal: _JournalWriter,
    extra: dict | None = None,
) -> tuple[str, str | None]:
    """Move one file into quarantine, journalling the intent first.

    Returns `(destination, error_or_None)` and appends nothing to any
    result — the caller decides where the outcome is recorded, which is
    what lets a duplicate file and a whole redundant archive (Р1) take the
    same journalled path and therefore be undone by the same `restore`.

    `extra` is merged into the `move_pending` line, so anything the caller
    wants on the record *before* the move happens — such as how many twins
    were re-verified for an archive — is durable even if the process dies
    during the move itself.
    """
    mirrored = _mirrored_destination(quarantine_root, source)
    destination = _unique_destination(mirrored.parent, mirrored.name)

    op_id = uuid.uuid4().hex
    entry = {
        "op_id": op_id,
        "event": "move_pending",
        "ts": time.time(),
        "group_hash": group_hash,
        "original": str(source),
        "quarantined": str(destination),
        "size": size,
    }
    if extra:
        entry.update(extra)
    journal.write(entry)

    try:
        shutil.move(str(source), str(destination))
    except OSError as exc:
        journal.write(
            {"op_id": op_id, "event": "move_failed", "ts": time.time(), "error": str(exc)}
        )
        return str(destination), str(exc)

    journal.write({"op_id": op_id, "event": "move_done", "ts": time.time()})
    return str(destination), None


def quarantine_group(
    group: DuplicateGroup,
    quarantine_root: Path,
    result: QuarantineResult,
    journal: _JournalWriter,
    confirm_media: bool = False,
    keeper_override: FileRecord | None = None,
) -> None:
    if group.only_archive_members:
        # Nothing is safe to move automatically. Report it and let the user
        # decide which archived copy to delete manually.
        result.archive_only_notes.append(
            {
                "group_hash": group.content_hash,
                "size": group.size,
                "members": [r.display_path for r in group.records],
                "recommendation": "Все копии находятся внутри архивов — "
                "автоматически ничего не перемещается. Оставьте одну копию "
                "вручную (например, самую раннюю по дате архива).",
            }
        )
        return

    if group.is_media and not confirm_media:
        result.pending_media_review.append(
            {
                "group_hash": group.content_hash,
                "size": group.size,
                "records": [r.display_path for r in group.records],
            }
        )
        return

    # Task 12: a human reviewing this specific group may override Р8's
    # default keeper (they can see the actual files, Р8 cannot). Falls
    # back to Р8 exactly as before when nothing overrides it, so every
    # existing caller of this function is unaffected.
    keeper = keeper_override if keeper_override is not None else choose_keeper(group.records)
    result.kept[group.content_hash] = keeper.display_path

    for record in group.records:
        if record is keeper:
            continue
        if record.is_archive_member:
            # Same reasoning as only_archive_members, but this group also
            # had at least one plain-file copy, which is why we got this far.
            result.archive_only_notes.append(
                {
                    "group_hash": group.content_hash,
                    "size": record.size,
                    "members": [record.display_path],
                    "recommendation": f"Дубликат внутри архива не тронут; "
                    f"актуальная копия сохранена: {keeper.display_path}.",
                }
            )
            continue

        source = Path(record.real_path)
        destination, error = _move_one(
            source, quarantine_root, group.content_hash, record.size, journal
        )
        entry = {
            "group_hash": group.content_hash,
            "original": str(source),
            "quarantined": destination,
        }
        if error is None:
            result.moved.append(entry)
        else:
            result.failed.append({**entry, "error": error})


def _append_manifest(quarantine_root: Path, result: QuarantineResult) -> None:
    """Append one run's summary to `manifest.json` (human-readable, not the
    source of truth — see module docstring). Shared by `run_quarantine` and
    `quarantine_reviewed_groups` so the two paths a file can take into
    quarantine (bulk confirm_media pass, or a batch of individually
    reviewed decisions) produce manifests that look the same.
    """
    manifest_path = quarantine_root / MANIFEST_FILENAME
    existing = []
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = [existing]
        except (json.JSONDecodeError, OSError):
            existing = []
    existing.append(result.to_dict())
    manifest_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")


def run_quarantine(
    groups: list[DuplicateGroup],
    quarantine_root: Path,
    confirm_media: bool = False,
    group_hashes: set[str] | None = None,
) -> QuarantineResult:
    """Process every group (optionally filtered to `group_hashes`, e.g. the
    ones a user explicitly ticked in the web review UI). Every individual
    file move is written to `journal.jsonl` before it happens and confirmed
    right after (see module docstring) — that journal, not `manifest.json`,
    is what `restore_from_journal` reads. `manifest.json` is still written
    once at the end as a human-readable summary of this run.
    """
    result = QuarantineResult()
    quarantine_root.mkdir(parents=True, exist_ok=True)

    journal_path = quarantine_root / JOURNAL_FILENAME
    with _JournalWriter(journal_path) as journal:
        for group in groups:
            if group_hashes is not None and group.content_hash not in group_hashes:
                continue
            quarantine_group(group, quarantine_root, result, journal, confirm_media=confirm_media)

    _append_manifest(quarantine_root, result)
    return result


def quarantine_reviewed_groups(
    groups: list[DuplicateGroup],
    quarantine_root: Path,
    decisions: dict[str, str | None],
    confirm_media: bool = False,
) -> QuarantineResult:
    """Задача 12's batch path: apply exactly the groups a human has
    reviewed and queued, and touch nothing else.

    Pilot finding P1.1 named the gap this closes: `run_quarantine` without
    `confirm_media` moves zero media groups, with it moves every one of
    them, and there was nothing in between. `decisions` is the accumulated
    pile — `{content_hash: keeper_override_display_path_or_None}` — built
    up one keyboard decision at a time and stored in the index (see
    `storage.ScanIndex.record_decision`) rather than kept only in the
    browser or the in-memory report, precisely so a crash or a server
    restart mid-review does not erase an evening of looking at 8814 groups.

    A group not in `decisions` is not visited at all — "непросмотренное не
    двигается" is enforced by iterating `decisions`, not `groups`. A media
    group inside `decisions` still needs `confirm_media=True` to actually
    move (same rule as everywhere else the guarantee is `only byte-
    confirmed content, explicitly confirmed, may be quarantined`): the
    fact that a human reviewed it is not by itself permission to move
    family photos, so it lands in `result.pending_media_review` exactly as
    `quarantine_group` already does, and the caller (see
    `web/app.py::apply_decisions`) knows to leave its decision queued
    rather than mark it applied.

    An archive-only group can never legitimately be a key here — the web
    layer refuses to record a decision for one (archives have their own
    card and their own action, task 4/Р1) — but a stale or hand-crafted
    request naming one is silently skipped rather than trusted, the same
    posture `quarantine_group` already takes toward archive members.
    """
    result = QuarantineResult()
    quarantine_root.mkdir(parents=True, exist_ok=True)
    by_hash = {g.content_hash: g for g in groups}

    journal_path = quarantine_root / JOURNAL_FILENAME
    with _JournalWriter(journal_path) as journal:
        for content_hash, keeper_path in decisions.items():
            group = by_hash.get(content_hash)
            if group is None or group.only_archive_members:
                continue
            keeper_override = None
            if keeper_path:
                keeper_override = next(
                    (r for r in group.records if r.display_path == keeper_path), None
                )
            quarantine_group(
                group,
                quarantine_root,
                result,
                journal,
                confirm_media=confirm_media,
                keeper_override=keeper_override,
            )

    _append_manifest(quarantine_root, result)
    return result


def restore_from_journal(
    quarantine_root: Path,
    op_ids: set[str] | None = None,
) -> RestoreResult:
    """Reverse quarantine moves using `journal.jsonl` as the sole source of
    truth — never by guessing a destination name back from the quarantine
    layout, since `_unique_destination` can rename on collision
    (`<stem>__1<suffix>`).

    For each recorded move (optionally filtered to `op_ids`):
    - if the quarantined copy is gone and the original is back in place,
      the move never actually completed (crash before the rename) —
      nothing to do, not an error;
    - if the quarantined copy is gone and the original is *also* gone,
      both copies are lost — flagged for manual attention rather than
      silently skipped;
    - if a file already sits at the original path, it is never
      overwritten — the restore is skipped and reported so the user can
      look at it themselves;
    - otherwise the quarantined file is checked to be actually readable,
      then moved back, and a `restored` event is appended so re-running
      restore is idempotent.
    """
    journal_path = quarantine_root / JOURNAL_FILENAME
    ops = _operations_from_journal(_read_journal(journal_path))

    result = RestoreResult()
    with _JournalWriter(journal_path) as journal:
        for op_id, op in ops.items():
            if "move_pending" not in op.get("events", []):
                continue
            if op_ids is not None and op_id not in op_ids:
                continue
            if "restored" in op.get("events", []):
                continue  # already restored in a previous run; idempotent no-op

            original = Path(op["original"])
            quarantined = Path(op["quarantined"])
            group_hash = op.get("group_hash")
            entry = {
                "original": str(original),
                "quarantined": str(quarantined),
                "group_hash": group_hash,
            }

            if not quarantined.exists():
                if original.exists():
                    reason = "перемещение не завершилось (файл остался на исходном месте) — восстанавливать нечего"
                else:
                    reason = "файла нет ни в карантине, ни на исходном пути — требуется ручная проверка"
                result.skipped.append({**entry, "reason": reason})
                continue

            try:
                with open(quarantined, "rb") as fh:
                    fh.read(1)
            except OSError as exc:
                result.skipped.append(
                    {**entry, "reason": f"файл в карантине не читается: {exc}"}
                )
                continue

            if original.exists():
                result.skipped.append(
                    {**entry, "reason": "по исходному пути уже лежит другой файл — не затираю"}
                )
                continue

            original.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(quarantined), str(original))
            except OSError as exc:
                journal.write(
                    {
                        "op_id": op_id,
                        "event": "restore_failed",
                        "ts": time.time(),
                        "reason": str(exc),
                    }
                )
                result.skipped.append({**entry, "reason": f"перемещение не удалось: {exc}"})
                continue

            journal.write({"op_id": op_id, "event": "restored", "ts": time.time()})
            result.restored.append(entry)

    return result


def journal_summary(quarantine_root: Path) -> list[dict]:
    """One row per operation in `journal.jsonl`, newest first — the data
    behind the journal/restore screen (задача 12: "веб-обвязка поверх
    готового restore_from_journal", so this reads the same journal that
    function does and adds nothing new to the on-disk format).

    Status per op_id:
    - "moved"    — completed and still in quarantine, a candidate to
                   restore;
    - "restored" — already brought back, `dupecleaner restore`/this
                   screen already ran this one;
    - "failed"   — the move itself raised (see `_move_one`);
    - "pending"  — the intent line was written but neither a `move_done`
                   nor a `move_failed` followed — the process died mid-move,
                   the same crash scenario Р5 exists to make legible instead
                   of silent.
    """
    ops = _operations_from_journal(_read_journal(quarantine_root / JOURNAL_FILENAME))
    rows: list[dict] = []
    for op_id, op in ops.items():
        if "move_pending" not in op.get("events", []):
            continue
        events = op["events"]
        if "restored" in events:
            status = "restored"
        elif "move_failed" in events:
            status = "failed"
        elif "move_done" in events:
            status = "moved"
        else:
            status = "pending"
        rows.append(
            {
                "op_id": op_id,
                "status": status,
                "original": op.get("original"),
                "quarantined": op.get("quarantined"),
                "group_hash": op.get("group_hash"),
                "size": op.get("size"),
                "kind": op.get("kind", "file"),
                "ts": op.get("ts"),
                "error": op.get("move_error"),
            }
        )
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return rows


def _archive_has_media(report: ScanReport, archive_path: str) -> bool:
    return any(
        r.is_media
        for g in report.groups
        for r in g.records
        if r.is_archive_member and r.archive_path == archive_path
    )


def quarantine_archives(
    verdicts: list[ArchiveVerdict],
    report: ScanReport,
    quarantine_root: Path,
    confirm_media: bool = False,
    archive_paths: set[str] | None = None,
) -> ArchiveQuarantineResult:
    """Move fully redundant archives into quarantine — as whole files, and
    only after re-verifying their twins (Р1).

    The order of operations is the entire point, so it is spelled out:

    1. Anything that isn't FULLY_REDUNDANT is recorded and skipped. Partial
       redundancy is reported, never acted on — "dissolving" a partly
       redundant archive is out of scope by decision, and an UNREAD one is
       not a verdict at all, it is an admission.
    2. Every member's twin on disk is re-read and re-hashed *now*
       (`archive_classify.verify_members`). The scan may have been hours or
       days ago; the drive holding the twins may have been unplugged since.
       One failure refuses the whole archive — the unit of action is the
       archive, so it is also the unit of veto.
    3. Only then is the intent written to `journal.jsonl`, and only then
       does the file move. Same journal, same writer, same format as a
       duplicate file move (task 5), which is why `dupecleaner restore`
       brings a quarantined archive back with no archive-specific code.

    Archives whose members include photos or video additionally need
    `confirm_media=True`, matching the rule for loose media files: the
    content is the user's photos either way, and the fact that it arrived
    wrapped in a .tgz is not a reason to apply a weaker safeguard.

    A report produced in quick mode (Р7) is refused outright, before any of
    the above. A quick run never opened an archive, so every archive in its
    report is UNREAD and step 1 would already skip it — but that is the
    mechanism agreeing with the rule by coincidence, and the rule is the
    part worth defending. Stating it here means the guarantee survives
    someone later teaching classification to be cleverer about archives it
    has not read, and it produces an answer a person can act on ("run it
    in full mode") instead of four identical "not fully redundant" lines.
    """
    result = ArchiveQuarantineResult()

    if report.mode is not ScanMode.FULL:
        for verdict in verdicts:
            if archive_paths is not None and verdict.path not in archive_paths:
                continue
            result.refused.append(
                {
                    "archive": verdict.path,
                    "size": verdict.size,
                    "reason": "отчёт получен в быстром режиме: внутрь архива не "
                    "заглядывали, значит его содержимое не подтверждено "
                    "байт-в-байт. Нужен полный режим "
                    "(dupecleaner scan --mode full) — тогда появится и право "
                    "на перемещение.",
                }
            )
        return result

    quarantine_root.mkdir(parents=True, exist_ok=True)
    twins_by_archive: dict[str, list[MemberTwins]] = member_twins(report)

    journal_path = quarantine_root / JOURNAL_FILENAME
    with _JournalWriter(journal_path) as journal:
        for verdict in verdicts:
            if archive_paths is not None and verdict.path not in archive_paths:
                continue

            if verdict.verdict is not ArchiveClass.FULLY_REDUNDANT:
                result.not_actionable.append(
                    {
                        "archive": verdict.path,
                        "size": verdict.size,
                        "verdict": verdict.verdict.value,
                        "members_total": verdict.members_total,
                        "members_redundant": verdict.members_redundant,
                        "members_unreadable": verdict.members_unreadable,
                        "reason": verdict.reason,
                    }
                )
                continue

            twins = twins_by_archive.get(verdict.path, [])
            if len(twins) != verdict.members_total:
                # The verdict and the group data disagree about what is in
                # this archive. That should be impossible from one scan, so
                # it means the report was edited or is stale — refuse rather
                # than trust half of it.
                result.refused.append(
                    {
                        "archive": verdict.path,
                        "size": verdict.size,
                        "reason": f"состав архива в отчёте не сходится: "
                        f"двойников {len(twins)}, участников {verdict.members_total}",
                    }
                )
                continue

            if _archive_has_media(report, verdict.path) and not confirm_media:
                result.pending_media_review.append(
                    {
                        "archive": verdict.path,
                        "size": verdict.size,
                        "members_total": verdict.members_total,
                    }
                )
                continue

            verification = verify_members(verdict.path, twins)
            if not verification.ok:
                result.refused.append(
                    {
                        "archive": verdict.path,
                        "size": verdict.size,
                        "reason": "двойники не подтвердились: "
                        + verification.failure_summary,
                        "failures": verification.failures,
                    }
                )
                continue

            source = Path(verdict.path)
            destination, error = _move_one(
                source,
                quarantine_root,
                group_hash=None,
                size=verdict.size,
                journal=journal,
                extra={
                    "kind": "archive",
                    "members_total": verdict.members_total,
                    "twins_verified": len(verification.verified),
                },
            )
            entry = {
                "archive": verdict.path,
                "original": verdict.path,
                "quarantined": destination,
                "size": verdict.size,
                "members_total": verdict.members_total,
                "twins_verified": len(verification.verified),
            }
            if error is None:
                result.moved.append(entry)
            else:
                result.failed.append({**entry, "error": error})

    return result
