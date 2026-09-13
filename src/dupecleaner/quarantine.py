"""Turns a list of confirmed DuplicateGroups into safe filesystem actions.

Nothing here ever calls os.remove(). The only destructive-looking action is
`shutil.move` into the quarantine folder, which is reversible (see
docs/SAFETY.md for the manual restore procedure) — actual permanent
deletion of the quarantine folder is a separate, explicit step the user
takes themselves once they're satisfied.

Two categories are *never* auto-moved, even when a group is "confirmed":
- Archive members: modifying an archive in place risks corrupting it, so
  these are only ever reported (with a recommendation of which copy to
  keep), never touched.
- Media files (photos/videos): require `confirm_media=True` to be passed
  explicitly for that call, on top of being in the confirmed group list —
  a deliberate extra step so a batch confirmation of "regular file"
  duplicates can never accidentally sweep up a family photo.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .models import DuplicateGroup, FileRecord


@dataclass
class QuarantineResult:
    kept: dict[str, str] = field(default_factory=dict)  # group hash -> kept path
    moved: list[dict] = field(default_factory=list)      # [{original, quarantined, group_hash}]
    pending_media_review: list[dict] = field(default_factory=list)
    archive_only_notes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": time.time(),
            "kept": self.kept,
            "moved": self.moved,
            "pending_media_review": self.pending_media_review,
            "archive_only_notes": self.archive_only_notes,
        }


def choose_keeper(records: list[FileRecord]) -> FileRecord:
    """Pick which copy survives in place. Preference order: a plain file
    over an archive member (plain files are trivial to keep working with),
    then the shortest/most "canonical-looking" path, then the oldest
    modification time (more likely to be the original rather than a copy).
    """
    non_archive = [r for r in records if not r.is_archive_member]
    candidates = non_archive or records
    return min(candidates, key=lambda r: (len(r.display_path), r.mtime))


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


def quarantine_group(
    group: DuplicateGroup,
    quarantine_root: Path,
    result: QuarantineResult,
    confirm_media: bool = False,
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

    keeper = choose_keeper(group.records)
    result.kept[group.content_hash] = keeper.display_path

    group_dir = quarantine_root / group.content_hash[:16]
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
        destination = _unique_destination(group_dir, source.name)
        shutil.move(str(source), str(destination))
        result.moved.append(
            {
                "group_hash": group.content_hash,
                "original": str(source),
                "quarantined": str(destination),
            }
        )


def run_quarantine(
    groups: list[DuplicateGroup],
    quarantine_root: Path,
    confirm_media: bool = False,
    group_hashes: set[str] | None = None,
) -> QuarantineResult:
    """Process every group (optionally filtered to `group_hashes`, e.g. the
    ones a user explicitly ticked in the web review UI) and write a
    manifest.json into the quarantine root describing exactly what happened,
    so it can be audited or reversed later.
    """
    result = QuarantineResult()
    quarantine_root.mkdir(parents=True, exist_ok=True)

    for group in groups:
        if group_hashes is not None and group.content_hash not in group_hashes:
            continue
        quarantine_group(group, quarantine_root, result, confirm_media=confirm_media)

    manifest_path = quarantine_root / "manifest.json"
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

    return result
