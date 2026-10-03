r"""Задача 22: carrying the library plan out, and undoing it.

This is the one module in the project that moves a person's photographs
somewhere they did not put them, thirty thousand times in a row, so the
order of operations here is the product, not an implementation detail.
Four rules, and each one exists because of a way this goes wrong:

1. **The journal line comes before the file moves.** Not after, not
   batched. Р5 says so and задача 5 already built it; this module imports
   `quarantine.journalled_move` rather than writing a second journal,
   because a second implementation of "write the intent first" is a second
   chance to get the order backwards, and the order *is* the guarantee. A
   process killed between the two leaves a `move_pending` with no outcome —
   which is exactly what tells `rollback_library` to go and look at both
   paths instead of assuming either.

2. **The plan executed is the plan a person read.** `LibraryPlan`
   fingerprints its own move list, the plan file carries that fingerprint,
   and `execute_plan` recomputes it and refuses on any difference. The
   point is not file corruption: it is that the plan is derived from event
   clustering that is deliberately not stored (задача 16), so moving
   `--session-gap-hours` by one hour, or `--subject-order` by one word,
   legitimately renames hundreds of albums. Without the check that would
   arrive as a silent surprise on the one operation that touches real
   files. With it, the answer is "rebuild the plan and read it".

3. **Nothing is ever overwritten, and a busy file is skipped, not
   forced.** Both are re-checked at the moment of the move rather than
   taken from the plan: the plan may have been built on a machine where
   the photographs were not even mounted (`library.PathProbe`, which says
   so through `probe_blind`), and a file can be opened in a photo editor
   in the seconds between reading a plan and running it. Р5 already
   decided what to do about an open file — "пропускается с
   предупреждением" — so it is a warning and a journal line, not a
   question.

4. **Across volumes the source dies last.** Inside one volume a move is a
   rename: atomic, instant, nothing to verify. Across volumes it is a
   copy, a hash of both ends, and only then the removal of the source — in
   that order, and if the hashes disagree the source stays and the
   operation is `move_failed` in the journal. `os.rename` is used rather
   than `shutil.move` precisely so that a volume boundary the plan did not
   predict fails loudly with `EXDEV` and falls back to the verified path,
   instead of `shutil.move` quietly copying and deleting without ever
   comparing the bytes.

What the sidecar is for
-----------------------
Р5's second half: overlapping grouping is never materialised as folders.
Faces, topics and — the part that cannot be recovered any other way — every
*other* path the same bytes were found at go into an XMP sidecar next to
the file (`PlannedMove.also_at`). After the move those paths exist nowhere
else: the folders are still there but the connection between them and this
one file in the library is gone. The same provenance goes into the index
(`storage.ScanIndex.record_library_move`), which is Р5's "и в базе, и в
XMP" — two copies because the index is queryable and the sidecar travels
with the file through a cloud sync, and neither property is worth losing.

Rolling back
------------
`rollback_library` walks the journal **backwards** and checks at every
step, because a library root can legitimately sit inside the tree being
moved out of, which makes the moves order-dependent. An interrupted run
rolls back exactly like a finished one — that is the whole reason the
journal is append-before-execute — and the three states a half-finished
operation can be in are handled separately rather than averaged: the file
is at the destination (move it back), the file is still at the source
(nothing happened, nothing to do), or it is at neither (both copies are
unaccounted for — report it for a human and touch nothing).

Nothing in this module is executed by default. `execute_plan` and
`rollback_library` do read-only checks and report what they would do until
`move_files=True` is passed, which the CLI only passes for
`--move-files-for-real`.
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping
from xml.sax.saxutils import escape

from . import hashing
from .library import (
    LibraryPlan,
    PlanProbe,
    PlannedMove,
    RealProbe,
    Transfer,
    plan_from_dict,
)
from .quarantine import (
    JournalWriter,
    MoveRefused,
    journalled_move,
    operations_from_journal,
    read_journal,
)

#: The journal for a library build. A different filename from quarantine's
#: `journal.jsonl` and in a different place — it belongs to the library it
#: describes, so it travels with it and a rollback months later needs
#: nothing but the library root. The *format* is identical, which is the
#: point: one reader, one writer, one append-before-execute.
JOURNAL_FILENAME = "library-journal.jsonl"
#: Where that journal lives relative to the library root.
JOURNAL_DIRNAME = "_dupecleaner"
#: Marks a journal operation as задача 22's rather than quarantine's. Both
#: kinds can end up in one file if someone points them at the same path, so
#: the rollback filters rather than assumes.
KIND = "library"
#: Extension of the partial file a cross-volume copy writes before it has
#: been verified. Never left behind on purpose; if one survives a crash it
#: is a partial copy and nothing reads it, because the verified copy is only
#: renamed into place after the hashes match.
PART_SUFFIX = ".dupecleaner-part"

_XMP_NS = "https://dupecleaner.local/ns/library/1.0/"


class PlanFingerprintMismatch(Exception):
    """The plan on disk is not the plan that was read.

    Carries both fingerprints and says the likely reason out loud, because
    the likely reason is not corruption — it is a threshold or a naming
    order that moved between building the plan and running it, which
    renames albums and therefore changes destinations.
    """

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"отпечаток плана не совпадает: в файле плана записан {expected}, "
            f"у перемещений в нём отпечаток {actual}. Ни один файл не тронут. "
            "Обычная причина — план пересобран с другим порогом события "
            "(--session-gap-hours) или с другим порядком цепочки названий "
            "(--subject-order): от этого меняются имена папок, а значит и "
            "цели перемещений. Постройте план заново, прочитайте его и "
            "выполняйте тот отпечаток, который увидели."
        )


# --- the filesystem, in one place so a test can poison one call -----------


class FileOps:
    """Every filesystem side effect this module can have.

    Not an abstraction for its own sake. The three things that must never
    quietly change — the journal is written before the move, a cross-volume
    copy is verified before the source is removed, and a destination is
    never overwritten — are only testable if a test can make a move die
    halfway, make a copy arrive corrupted and make a directory refuse. Every
    method here is the real implementation; a test subclasses and breaks one
    at a time.
    """

    def exists(self, path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

    def mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def rename(self, source: Path, destination: Path) -> None:
        """Same-volume move. `os.rename` rather than `shutil.move` on
        purpose: across a volume boundary this raises `EXDEV` instead of
        silently copying and deleting without comparing a single byte, and
        the caller turns that into the verified path."""
        os.rename(str(source), str(destination))

    def copy(self, source: Path, destination: Path) -> None:
        shutil.copyfile(str(source), str(destination))

    def replace(self, source: Path, destination: Path) -> None:
        os.replace(str(source), str(destination))

    def remove(self, path: Path) -> None:
        path.unlink()

    def rmdir(self, path: Path) -> None:
        path.rmdir()

    def hash_file(self, path: Path) -> str:
        """The same xxh3_128 the dedupe funnel uses, so "the copy matches
        the source" means the same thing here as everywhere else."""
        with open(path, "rb") as fh:
            return hashing.full_hash(fh)

    def read_head(self, path: Path) -> None:
        with open(path, "rb") as fh:
            fh.read(1)

    def write_text(self, path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def is_dir_empty(self, path: Path) -> bool:
        try:
            next(path.iterdir())
        except StopIteration:
            return True
        except OSError:
            return False
        return False


# --- outcomes ---------------------------------------------------------------


@dataclass
class ExecutionOutcome:
    """What one build run did, or would do."""

    dry_run: bool
    fingerprint: str
    planned: int
    journal_path: str = ""
    moved: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    sidecars: list[dict] = field(default_factory=list)
    created_dirs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    refusal: str | None = None

    @property
    def moved_bytes(self) -> int:
        return sum(m.get("size", 0) for m in self.moved)

    def summary(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "fingerprint": self.fingerprint,
            "planned": self.planned,
            "moved": len(self.moved),
            "moved_bytes": self.moved_bytes,
            "skipped": len(self.skipped),
            "failed": len(self.failed),
            "sidecars": len(self.sidecars),
            "created_dirs": len(self.created_dirs),
            "refusal": self.refusal,
        }

    def to_dict(self) -> dict:
        return {
            "summary": self.summary(),
            "journal": self.journal_path,
            "moved": self.moved,
            "skipped": self.skipped,
            "failed": self.failed,
            "sidecars": self.sidecars,
            "created_dirs": self.created_dirs,
            "warnings": self.warnings,
        }


@dataclass
class RollbackOutcome:
    dry_run: bool
    journal_path: str
    considered: int = 0
    restored: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    sidecars_removed: list[str] = field(default_factory=list)
    dirs_removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "considered": self.considered,
            "restored": len(self.restored),
            "skipped": len(self.skipped),
            "failed": len(self.failed),
            "sidecars_removed": len(self.sidecars_removed),
            "dirs_removed": len(self.dirs_removed),
        }

    def to_dict(self) -> dict:
        return {
            "summary": self.summary(),
            "journal": self.journal_path,
            "restored": self.restored,
            "skipped": self.skipped,
            "failed": self.failed,
            "sidecars_removed": self.sidecars_removed,
            "dirs_removed": self.dirs_removed,
            "warnings": self.warnings,
        }


# --- the plan file ----------------------------------------------------------


def journal_path_for(root: str | Path) -> Path:
    return Path(str(root)) / JOURNAL_DIRNAME / JOURNAL_FILENAME


def recorded_fingerprint(payload: Mapping) -> str:
    """The fingerprint the plan file says it has — the one a person saw
    printed. Deliberately read from `summary`, not recomputed: recomputing
    both sides of a comparison compares nothing."""
    return str(payload.get("summary", {}).get("fingerprint", ""))


def load_plan(path: str | Path) -> tuple[LibraryPlan, str]:
    """Read a plan written by `dupecleaner library --json`.

    Returns the rebuilt plan and the fingerprint recorded in the file, so
    the caller can hand both to `execute_plan` and have it compare them.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return plan_from_dict(payload), recorded_fingerprint(payload)


# --- the XMP sidecar --------------------------------------------------------


def sidecar_text(move: PlannedMove, *, moved_at: float | None = None) -> str:
    """The sidecar for one moved file: where it came from, and everywhere
    else these bytes were.

    A plain XMP packet rather than anything clever, because Р5's reason for
    choosing sidecars is that they outlive this tool: "разметка не заперта
    внутри нашего приложения — страховка на случай, если инструмент
    надоест". Anything that reads XMP can read the original path out of
    this file with no code of ours involved.
    """
    stamp = dt.datetime.fromtimestamp(
        moved_at if moved_at is not None else time.time()
    ).isoformat(timespec="seconds")
    also = "".join(
        f"\n     <rdf:li>{escape(path)}</rdf:li>" for path in move.also_at
    )
    also_block = (
        f"\n    <dupecleaner:alsoAt>\n     <rdf:Bag>{also}\n     </rdf:Bag>"
        f"\n    </dupecleaner:alsoAt>"
        if move.also_at
        else ""
    )
    album_block = (
        f"\n    <dupecleaner:album>{escape(move.album)}</dupecleaner:album>"
        if move.album
        else ""
    )
    return (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="dupecleaner">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        f'    xmlns:dupecleaner="{_XMP_NS}">\n'
        f"    <dupecleaner:originalPath>{escape(move.source)}</dupecleaner:originalPath>"
        f"{also_block}"
        f"{album_block}\n"
        f"    <dupecleaner:contentKey>{escape(move.content_key)}</dupecleaner:contentKey>\n"
        f"    <dupecleaner:bucket>{escape(move.bucket.value)}</dupecleaner:bucket>\n"
        f"    <dupecleaner:movedAt>{stamp}</dupecleaner:movedAt>\n"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        '<?xpacket end="w"?>\n'
    )


def _sidecar_candidates(destination: Path) -> list[Path]:
    r"""Where this file's sidecar goes.

    Р5's layout shows `IMG_2201.jpg` next to `IMG_2201.xmp`, so that is
    first. But `a.jpg` and `a.png` in one album would both want `a.xmp`,
    and one sidecar describing two files is a sidecar describing neither —
    so the fallback keeps the full name (`a.png.xmp`). Picking the second
    form is recorded, not silent.
    """
    stem_form = destination.with_suffix(".xmp")
    full_form = destination.with_name(destination.name + ".xmp")
    return [stem_form] if stem_form == full_form else [stem_form, full_form]


# --- building ---------------------------------------------------------------


def _volume(probe: PlanProbe, path: str) -> str:
    return probe.volume_of(path)


def _transfer_for(probe: PlanProbe, move: PlannedMove) -> Transfer:
    """Decide the mode now, from the filesystem, rather than trusting what
    the plan guessed. Р5 says the mode switches automatically; a plan built
    with a blind probe guessed it from drive letters, and a guess is not
    what should decide whether a source file is deleted without its bytes
    being compared."""
    same = _volume(probe, move.source) == _volume(probe, move.destination)
    return Transfer.RENAME if same else Transfer.COPY_VERIFY


class _Mover:
    """One move's mechanics, as a callable `journalled_move` can invoke
    between writing the intent and writing the outcome."""

    def __init__(self, ops: FileOps, transfer: Transfer):
        self._ops = ops
        self.transfer = transfer
        self.fell_back = False
        self.source_hash: str | None = None

    def __call__(self, source: Path, destination: Path) -> None:
        if self.transfer is Transfer.RENAME:
            try:
                self._ops.rename(source, destination)
                return
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                # The plan said one volume, the kernel says two. Р5's rule
                # is not a preference, so fall through to the verified
                # path rather than letting anything copy unverified.
                self.fell_back = True
                self.transfer = Transfer.COPY_VERIFY
        self._copy_verify(source, destination)

    def _copy_verify(self, source: Path, destination: Path) -> None:
        """Copy, compare, and only then remove the source — in that order.

        The copy lands under a `.dupecleaner-part` name and is renamed into
        place only once the hashes match, so nothing ever sees a file at the
        destination that has not been verified, and a crash mid-copy leaves
        a partial nobody mistakes for the real thing.
        """
        partial = destination.with_name(destination.name + PART_SUFFIX)
        if self._ops.exists(partial):
            self._ops.remove(partial)
        self.source_hash = self._ops.hash_file(source)
        self._ops.copy(source, partial)
        copy_hash = self._ops.hash_file(partial)
        if copy_hash != self.source_hash:
            try:
                self._ops.remove(partial)
            except OSError:
                pass
            raise MoveRefused(
                "копия не сошлась с источником: "
                f"источник {self.source_hash}, копия {copy_hash} — источник "
                "оставлен на месте, в библиотеку ничего не положено"
            )
        self._ops.replace(partial, destination)
        try:
            self._ops.remove(source)
        except OSError as exc:
            raise MoveRefused(
                f"копия проверена и на месте, но источник не удалён: {exc}. "
                "Сейчас файл существует в двух местах — разберитесь вручную, "
                "откат такую операцию не трогает"
            ) from exc


def execute_plan(
    plan: LibraryPlan,
    *,
    expected_fingerprint: str | None,
    journal_path: str | Path | None = None,
    move_files: bool = False,
    probe: PlanProbe | None = None,
    ops: FileOps | None = None,
    index=None,
    write_sidecars: bool = True,
    accept_unresolved: bool = False,
    on_move: Callable[[dict], None] | None = None,
) -> ExecutionOutcome:
    """Build the library the plan describes. Moves nothing unless
    `move_files=True`.

    `expected_fingerprint` is the fingerprint recorded in the plan file —
    what a person read. It is compared against `plan.fingerprint()`, which
    is computed from the moves actually present, and any difference raises
    `PlanFingerprintMismatch` before a single directory is created. Passing
    `None` with `move_files=True` is itself refused: a plan whose identity
    nobody can confirm is exactly the plan this check exists to stop.
    """
    ops = ops or FileOps()
    probe = probe or RealProbe()
    actual = plan.fingerprint()

    if expected_fingerprint is None:
        if move_files:
            raise PlanFingerprintMismatch("(в плане не записан)", actual)
    elif expected_fingerprint != actual:
        raise PlanFingerprintMismatch(expected_fingerprint, actual)

    outcome = ExecutionOutcome(
        dry_run=not move_files, fingerprint=actual, planned=len(plan.moves)
    )
    if plan.probe_blind:
        outcome.warnings.append(
            "план строился без доступа к файлам: занятость и занятые целевые "
            "пути проверяются сейчас, в момент исполнения"
        )

    unresolved = plan.needs_human
    if unresolved and not accept_unresolved:
        kinds = sorted({p.kind.value for p in unresolved})
        outcome.refusal = (
            f"в плане {len(unresolved)} вопросов, на которые ещё никто не "
            f"ответил ({', '.join(kinds)}). Это не предупреждения: от ответа "
            "зависят имена папок, а значит и сам план. Решите их и соберите "
            "план заново — или, если ответ «оставить как предложено», "
            "запустите с accept_unresolved."
        )
        return outcome

    journal = Path(journal_path) if journal_path else journal_path_for(plan.layout.root)
    outcome.journal_path = str(journal)

    if not move_files:
        for move in plan.moves:
            verdict = _dry_check(move, probe, ops)
            if verdict is not None:
                bucket, entry = verdict
                getattr(outcome, bucket).append(entry)
        outcome.warnings.append(
            "сухой прогон: ни один файл не тронут и журнал не создан. Чтобы "
            "действительно перенести файлы, нужен явный флаг "
            "--move-files-for-real."
        )
        return outcome

    ops.mkdir(journal.parent)
    claimed_sidecars: set[str] = set()
    with JournalWriter(journal) as writer:
        for move in plan.moves:
            _execute_one(
                move,
                writer=writer,
                probe=probe,
                ops=ops,
                index=index,
                outcome=outcome,
                write_sidecars=write_sidecars,
                claimed_sidecars=claimed_sidecars,
            )
            if on_move is not None:
                on_move(outcome.summary())
    return outcome


def _dry_check(
    move: PlannedMove, probe: PlanProbe, ops: FileOps
) -> tuple[str, dict] | None:
    """The same three questions the real run asks, asked read-only. A dry
    run that only reprinted the plan would be a worse dry run than the plan
    itself: what is worth knowing now is what has changed since."""
    source, destination = Path(move.source), Path(move.destination)
    base = {"source": move.source, "destination": move.destination, "size": move.size}
    if not ops.exists(source):
        if ops.exists(destination):
            return "skipped", {**base, "reason": "уже на месте — перенос состоялся раньше"}
        return "failed", {**base, "reason": "источника нет на диске"}
    if ops.exists(destination):
        return "failed", {
            **base,
            "reason": "по целевому пути уже лежит файл — он не будет затёрт",
        }
    if probe.is_busy(move.source):
        return "skipped", {
            **base,
            "reason": "файл открыт другим процессом — будет пропущен (Р5)",
        }
    return None


def _execute_one(
    move: PlannedMove,
    *,
    writer: JournalWriter,
    probe: PlanProbe,
    ops: FileOps,
    index,
    outcome: ExecutionOutcome,
    write_sidecars: bool,
    claimed_sidecars: set[str],
) -> None:
    source, destination = Path(move.source), Path(move.destination)
    base = {"source": move.source, "destination": move.destination, "size": move.size}

    if not ops.exists(source):
        if ops.exists(destination):
            outcome.skipped.append(
                {**base, "reason": "уже на месте — перенос состоялся раньше"}
            )
        else:
            outcome.failed.append({**base, "reason": "источника нет на диске"})
        return

    if ops.exists(destination):
        # Something arrived here between the plan and now. Nothing in this
        # project overwrites a file, and a library built by overwriting is
        # exactly the failure Р0 exists to make impossible.
        reason = "по целевому пути уже лежит файл — не затираю"
        writer.write(
            {
                "event": "move_refused",
                "kind": KIND,
                "ts": time.time(),
                "original": move.source,
                "quarantined": move.destination,
                "reason": reason,
            }
        )
        outcome.failed.append({**base, "reason": reason})
        return

    # Busy is asked *now*, not read from the plan: the plan may have been
    # built blind, and an editor can be opened in the meantime.
    if probe.is_busy(move.source):
        reason = "файл открыт другим процессом — пропущен с предупреждением (Р5)"
        writer.write(
            {
                "event": "move_skipped",
                "kind": KIND,
                "ts": time.time(),
                "original": move.source,
                "quarantined": move.destination,
                "reason": "busy",
                "detail": reason,
            }
        )
        outcome.skipped.append({**base, "reason": reason})
        return

    for created in _missing_ancestors(destination.parent, ops):
        ops.mkdir(created)
        writer.write(
            {"event": "dir_created", "kind": KIND, "ts": time.time(), "path": str(created)}
        )
        outcome.created_dirs.append(str(created))
    ops.mkdir(destination.parent)

    transfer = _transfer_for(probe, move)
    mover = _Mover(ops, transfer)
    result = journalled_move(
        source,
        destination,
        writer,
        size=move.size,
        extra={
            "kind": KIND,
            "transfer": transfer.value,
            "planned_transfer": move.transfer.value,
            "bucket": move.bucket.value,
            "album": move.album,
            "content_key": move.content_key,
            "also_at": list(move.also_at),
        },
        mover=mover,
    )

    if not result.ok:
        outcome.failed.append({**base, "reason": result.error, "op_id": result.op_id})
        return

    if mover.fell_back:
        # The intent line said "rename" because that is what was intended;
        # amend the record rather than rewrite history. The rollback does
        # not depend on this line — it hits the same EXDEV and falls back
        # the same way — so this is for the person reading the journal.
        writer.write(
            {
                "op_id": result.op_id,
                "event": "transfer_changed",
                "ts": time.time(),
                "transfer": mover.transfer.value,
            }
        )
        outcome.warnings.append(
            f"{move.source}: план обещал переименование, но путь оказался на "
            "другом томе — перенесено копированием со сверкой хэша"
        )

    entry = {
        **base,
        "op_id": result.op_id,
        "transfer": mover.transfer.value,
        "album": move.album,
        "content_key": move.content_key,
        "also_at": list(move.also_at),
    }

    sidecar = None
    if write_sidecars:
        sidecar = _write_sidecar(
            move,
            destination,
            op_id=result.op_id,
            writer=writer,
            ops=ops,
            outcome=outcome,
            claimed=claimed_sidecars,
        )
        if sidecar:
            entry["sidecar"] = sidecar

    if index is not None:
        try:
            index.record_library_move(
                op_id=result.op_id,
                content_key=move.content_key,
                source=move.source,
                destination=move.destination,
                also_at=move.also_at,
                album=move.album,
                transfer=mover.transfer.value,
                sidecar=sidecar,
            )
        except Exception as exc:  # pragma: no cover - index is the mirror
            # The journal already has the move, and the journal is the
            # source of truth. An index that cannot be written is worth a
            # warning, not an aborted build.
            outcome.warnings.append(f"{move.source}: в индекс не записалось: {exc}")

    outcome.moved.append(entry)


def _missing_ancestors(folder: Path, ops: FileOps) -> list[Path]:
    """Which directories this move will bring into existence, outermost
    first. Recorded so a rollback can take them away again and leave the
    tree as it was — an empty `2017/` left behind is not damage, but it is
    not a rollback either."""
    missing: list[Path] = []
    current = folder
    while not ops.exists(current):
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return list(reversed(missing))


def _write_sidecar(
    move: PlannedMove,
    destination: Path,
    *,
    op_id: str,
    writer: JournalWriter,
    ops: FileOps,
    outcome: ExecutionOutcome,
    claimed: set[str],
) -> str | None:
    text = sidecar_text(move)
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    for candidate in _sidecar_candidates(destination):
        if str(candidate) in claimed or ops.exists(candidate):
            continue
        try:
            ops.write_text(candidate, text)
        except OSError as exc:
            outcome.warnings.append(f"{move.destination}: сайдкар не записан: {exc}")
            return None
        claimed.add(str(candidate))
        writer.write(
            {
                "event": "sidecar_written",
                "kind": KIND,
                "ts": time.time(),
                "move_op": op_id,
                "path": str(candidate),
                "digest": digest,
            }
        )
        outcome.sidecars.append({"path": str(candidate), "for": move.destination})
        return str(candidate)
    outcome.warnings.append(
        f"{move.destination}: рядом уже лежит .xmp, своего не пишу — "
        "исходный путь сохранён в индексе"
    )
    return None


# --- rolling back -----------------------------------------------------------


def rollback_library(
    journal_path: str | Path,
    *,
    move_files: bool = False,
    ops: FileOps | None = None,
    index=None,
    op_ids: Iterable[str] | None = None,
) -> RollbackOutcome:
    """Undo a library build from its journal, newest operation first.

    Backwards because a library root may sit inside the tree it is built
    from, which makes the forward order meaningful; and with a check at
    every step rather than a blanket "move it back", because an interrupted
    run leaves operations in three genuinely different states and guessing
    between them is how a rollback loses a file.

    Works identically on a finished run and an interrupted one: the journal
    is written before each move, so an operation with no outcome line is
    not an unknown — it is a known "look at both paths".
    """
    ops = ops or FileOps()
    journal = Path(journal_path)
    outcome = RollbackOutcome(dry_run=not move_files, journal_path=str(journal))
    if not ops.exists(journal):
        outcome.warnings.append(f"журнала нет: {journal} — откатывать нечего")
        return outcome

    entries = read_journal(journal)
    ops_map = operations_from_journal(entries)
    library_ops = [
        (op_id, op)
        for op_id, op in ops_map.items()
        if op.get("kind") == KIND and "move_pending" in op.get("events", [])
    ]
    wanted = set(op_ids) if op_ids is not None else None
    if wanted is not None:
        library_ops = [(i, o) for i, o in library_ops if i in wanted]
    outcome.considered = len(library_ops)

    sidecars: dict[str, list[dict]] = {}
    removed_sidecars: set[str] = set()
    created_dirs: list[str] = []
    for entry in entries:
        event = entry.get("event")
        if event == "sidecar_written":
            sidecars.setdefault(entry.get("move_op", ""), []).append(entry)
        elif event == "sidecar_removed":
            removed_sidecars.add(entry.get("path", ""))
        elif event == "dir_created":
            created_dirs.append(entry.get("path", ""))

    writer = JournalWriter(journal) if move_files else None
    try:
        for op_id, op in reversed(library_ops):
            if "rolled_back" in op.get("events", []):
                continue
            _rollback_one(
                op_id,
                op,
                sidecars=[
                    s for s in sidecars.get(op_id, []) if s.get("path") not in removed_sidecars
                ],
                writer=writer,
                ops=ops,
                index=index,
                outcome=outcome,
            )
        _remove_created_dirs(created_dirs, ops=ops, writer=writer, outcome=outcome)
    finally:
        if writer is not None:
            writer.close()
    return outcome


def _rollback_one(
    op_id: str,
    op: Mapping,
    *,
    sidecars: list[dict],
    writer: JournalWriter | None,
    ops: FileOps,
    index,
    outcome: RollbackOutcome,
) -> None:
    source = Path(op["original"])
    destination = Path(op["quarantined"])
    entry = {
        "op_id": op_id,
        "source": str(source),
        "destination": str(destination),
        "transfer": op.get("transfer", Transfer.RENAME.value),
    }
    finished = "move_done" in op.get("events", [])

    if not ops.exists(destination):
        if ops.exists(source):
            # The intent line was written and the move never happened (or
            # already came back). Not an error — this is the half-finished
            # state the journal exists to make legible.
            outcome.skipped.append(
                {
                    **entry,
                    "reason": "файл на исходном месте — перенос не состоялся, "
                    "откатывать нечего",
                }
            )
        else:
            outcome.failed.append(
                {
                    **entry,
                    "reason": "файла нет ни в библиотеке, ни на исходном пути — "
                    "требуется ручная проверка",
                }
            )
        return

    if ops.exists(source):
        outcome.skipped.append(
            {
                **entry,
                "reason": "файл есть и в библиотеке, и на исходном пути — "
                "ничего не трогаю, разберитесь вручную",
            }
        )
        return

    try:
        ops.read_head(destination)
    except OSError as exc:
        outcome.failed.append({**entry, "reason": f"файл в библиотеке не читается: {exc}"})
        return

    if not finished:
        outcome.warnings.append(
            f"{destination}: операция оборвалась без отметки о завершении, но "
            "файл лежит в библиотеке — откатываю так же, как завершённую"
        )

    if outcome.dry_run:
        outcome.restored.append({**entry, "reason": "будет возвращён"})
        for sidecar in sidecars:
            outcome.sidecars_removed.append(sidecar.get("path", ""))
        return

    for sidecar in sidecars:
        _remove_sidecar(sidecar, ops=ops, writer=writer, outcome=outcome)

    ops.mkdir(source.parent)
    transfer = (
        Transfer.COPY_VERIFY
        if op.get("transfer") == Transfer.COPY_VERIFY.value
        else Transfer.RENAME
    )
    mover = _Mover(ops, transfer)
    try:
        mover(destination, source)
    except (OSError, MoveRefused) as exc:
        if writer is not None:
            writer.write(
                {
                    "op_id": op_id,
                    "event": "rollback_failed",
                    "ts": time.time(),
                    "reason": str(exc),
                }
            )
        outcome.failed.append({**entry, "reason": f"возврат не удался: {exc}"})
        return

    if writer is not None:
        writer.write({"op_id": op_id, "event": "rolled_back", "ts": time.time()})
    if index is not None:
        try:
            index.mark_library_move_rolled_back(op_id)
        except Exception as exc:  # pragma: no cover - index is the mirror
            outcome.warnings.append(f"{source}: отметка откатa в индекс не легла: {exc}")
    outcome.restored.append(entry)


def _remove_sidecar(
    sidecar: Mapping, *, ops: FileOps, writer: JournalWriter | None, outcome: RollbackOutcome
) -> None:
    """Take away a sidecar this tool wrote — and only one it wrote.

    The bytes are compared against the digest recorded when it was written,
    so a sidecar a person has since edited (or one that was already there)
    is left alone and reported. This is the only `unlink` in the rollback
    path and it is narrow on purpose: deleting a file is not something this
    project does, except for a file it created itself and can prove is
    unchanged.
    """
    path = Path(sidecar.get("path", ""))
    if not ops.exists(path):
        return
    try:
        digest = hashlib.sha256(ops.read_bytes(path)).hexdigest()
    except OSError as exc:
        outcome.warnings.append(f"{path}: сайдкар не прочитан, оставлен на месте: {exc}")
        return
    if digest != sidecar.get("digest"):
        outcome.warnings.append(
            f"{path}: сайдкар изменился после записи — оставлен на месте"
        )
        return
    try:
        ops.remove(path)
    except OSError as exc:
        outcome.warnings.append(f"{path}: сайдкар не удалён: {exc}")
        return
    if writer is not None:
        writer.write(
            {
                "event": "sidecar_removed",
                "kind": KIND,
                "ts": time.time(),
                "move_op": sidecar.get("move_op"),
                "path": str(path),
            }
        )
    outcome.sidecars_removed.append(str(path))


def _remove_created_dirs(
    created_dirs: list[str],
    *,
    ops: FileOps,
    writer: JournalWriter | None,
    outcome: RollbackOutcome,
) -> None:
    """Remove the directories the build created, innermost first, and only
    while they are empty. A directory that still holds something is left
    where it is: that something is not ours."""
    for raw in sorted(set(created_dirs), key=len, reverse=True):
        path = Path(raw)
        if not ops.exists(path) or not ops.is_dir_empty(path):
            continue
        if outcome.dry_run:
            outcome.dirs_removed.append(raw)
            continue
        try:
            ops.rmdir(path)
        except OSError:
            continue
        if writer is not None:
            writer.write(
                {"event": "dir_removed", "kind": KIND, "ts": time.time(), "path": raw}
            )
        outcome.dirs_removed.append(raw)
