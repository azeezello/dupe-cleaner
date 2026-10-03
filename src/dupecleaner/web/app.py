"""Local web UI: start a scan in the background, watch real progress, cancel
it safely, then review duplicate groups and send confirmed ones to quarantine.

Scans run in a worker thread and persist their work to the SQLite index, so
the HTTP layer here is thin: it starts jobs, reports their progress, and
serves results. Closing the browser, or restarting the server, never
destroys completed hashing work — it lives in the index, not in this
process.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.gzip import GZipMiddleware

from .. import album_review, thumbnails
from ..archive_classify import classify_archives
from ..dedupe import verify_group
from ..jobs import ScanRegistry
from ..models import ScanMode
from ..quarantine import (
    journal_summary,
    quarantine_archives,
    quarantine_review_drops,
    quarantine_reviewed_groups,
    restore_from_journal,
    run_quarantine,
)
from ..similar import PHASH_ALGO, entries_from_rows, find_similar_groups
from ..similar_review import DEFAULT_UI_THRESHOLD, build_review, thresholds_for
from ..storage import DEFAULT_DB_PATH, ScanIndex

BASE_DIR = Path(__file__).parent

app = FastAPI(title="dupe-cleaner")
# Task 11 / pilot finding P2.9: `/api/scan/{id}/result` is one JSON response
# with no pagination -- on the real 8814-group report this project was built
# against, that is 7.5 MB uncompressed. Gzip alone (repeated keys, long
# shared path prefixes) brings it to ~0.7 MB, a ~10x cut, for the cost of
# one middleware line. That is not "solved": a report an order of magnitude
# larger would still be a single multi-megabyte fetch. It is the explicit
# call task 11 asks for when it isn't paginating -- see
# claude/design-decisions.md for the measurement this is based on and why a
# single (now compressed) response stays tolerable at this scale: the
# server is loopback-only, the fetch happens once per completed scan (not
# per scroll -- the grid's own virtualization is what makes scrolling
# cheap), and minimum_size skips compressing the small responses where
# gzip's own overhead would not pay for itself.
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

registry = ScanRegistry()
DB_PATH: Path | str = DEFAULT_DB_PATH


class ScanRequest(BaseModel):
    paths: list[str]
    # Р7: quick is the default, here and in the CLI. The two modes differ
    # in coverage only — see models.ScanMode.
    mode: ScanMode = ScanMode.QUICK
    # Legacy/escape hatch: may only narrow what the mode allows.
    include_archives: bool | None = None


class QuarantineRequest(BaseModel):
    quarantine_dir: str
    group_hashes: Optional[list[str]] = None
    confirm_media: bool = False


class DecisionRequest(BaseModel):
    """One human decision on one group (задача 12). `keeper_path` overrides
    Р8's default keeper — must be one of the group's own record paths, or
    left unset to accept Р8's choice."""

    action: str  # "quarantine" | "keep"
    keeper_path: Optional[str] = None


class ApplyDecisionsRequest(BaseModel):
    quarantine_dir: str
    confirm_media: bool = False


class ArchiveActionRequest(BaseModel):
    quarantine_dir: str
    archive_path: str
    confirm_media: bool = False


class RestoreRequest(BaseModel):
    quarantine_dir: str
    op_ids: Optional[list[str]] = None


class AlbumStateRequest(BaseModel):
    """Одно решение по одному снимку (задача 24): «оставить» / «в печать»
    / «убрать». Другая ось, чем `DecisionRequest` выше: та отвечает, что
    станет с лишними копиями этих байт, эта — стоит ли снимок хранить и
    печатать. Один снимок может быть и хранителем группы дублей, и
    отобранным в печать."""

    content_hash: str
    path: Optional[str] = None
    state: str  # "keep" | "print" | "drop"


class ApplyDropsRequest(BaseModel):
    """Пачка «убрать» в карантин. `confirm` обязателен — это тот же
    второй явный шаг, которым обставлено любое перемещение в этом
    проекте; `folder` ограничивает применение одним альбомом."""

    quarantine_dir: str
    confirm: bool = False
    folder: Optional[str] = None


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/index-stats")
def index_stats():
    """How much is already in the persistent index — the number that shows a
    restarted scan won't redo the expensive work.
    """
    with ScanIndex(DB_PATH) as index:
        return index.stats()


@app.post("/api/scan")
def start_scan(req: ScanRequest):
    if not req.paths:
        raise HTTPException(400, "Укажите хотя бы одну папку для сканирования.")
    job = registry.create(
        req.paths,
        db_path=DB_PATH,
        include_archives=req.include_archives,
        mode=req.mode,
    )
    return {"scan_id": job.scan_id, "mode": job.mode.value, **job.progress.to_dict()}


@app.get("/api/scan/{scan_id}/progress")
def scan_progress(scan_id: str):
    job = registry.get(scan_id)
    if job is None:
        raise HTTPException(404, "Скан не найден.")
    return job.progress.to_dict()


@app.post("/api/scan/{scan_id}/cancel")
def cancel_scan(scan_id: str):
    job = registry.get(scan_id)
    if job is None:
        raise HTTPException(404, "Скан не найден.")
    job.cancel()
    return {"cancelled": True, "note": "Уже вычисленные хэши сохранены — "
            "повторный запуск продолжит с этого места."}


@app.get("/api/scan/{scan_id}/result")
def scan_result(scan_id: str):
    job = registry.get(scan_id)
    if job is None:
        raise HTTPException(404, "Скан не найден.")
    if job.report is None:
        raise HTTPException(
            409, f"Скан ещё не завершён (статус: {job.progress.status})."
        )
    # `to_dict` already carries "mode"; the counts below save the client
    # from re-deriving "what did this mode not look at" from the list.
    by_mode = [
        a for a in job.report.skipped_archives if a.reason == "excluded_by_mode"
    ]
    payload = {
        "scan_id": scan_id,
        "skipped_by_mode_count": len(by_mode),
        "skipped_by_mode_bytes": sum(a.size for a in by_mode),
        **job.report.to_dict(),
    }
    _attach_quality(payload["groups"])
    _attach_decisions(payload["groups"])
    # Screen 5 (docs/UX-MOCKUPS.html): one verdict card per archive the
    # scan met (task 4/Р1), computed here rather than carried on the report
    # object — `classify_archives` needs `member_twins`, which recomputes
    # Р8's ranking over every group, and doing that on every poll of a
    # running scan would be wasted work `report.to_dict()` never used to
    # do. Once, on the one endpoint that actually renders the archive tab.
    payload["archive_verdicts"] = [v.to_dict() for v in classify_archives(job.report)]
    return payload


def _attach_decisions(groups: list[dict]) -> None:
    """Hang each group's review decision (задача 12), if any, off its
    entry — same shape and same reasoning as `_attach_quality`: decisions
    live in the index, keyed by content hash, because that is what
    survives a lost `report` (see storage.py's schema-v5 comment), so the
    grid can show "already queued for quarantine" the moment a report
    loads, including a report rebuilt from scratch after a restart.

    A group with no decision gets `decision: null` — "not yet reviewed" is
    the default, not a zero-ish decision of its own.
    """
    if not groups:
        return
    with ScanIndex(DB_PATH) as index:
        decisions = index.decisions_for_hashes(g["content_hash"] for g in groups)
    for group in groups:
        group["decision"] = decisions.get(group["content_hash"])


def _attach_quality(groups: list[dict]) -> None:
    """Hang each group's quality metrics (task 9) off its entry, in place.

    Done here rather than in `ScanReport.to_dict` because the report is a
    pure in-memory object that travels to `report.json` and back, while the
    metrics live in the index — and the index is precisely where they need
    to live, because they outlive any one report (Р6) and are keyed by
    content, not by scan.

    One object per *group*, not per record: every copy in a group is
    byte-identical, so they share one measurement, exactly as they share one
    thumbnail. That keeps the cost of this against pilot finding P2.9 (the
    whole report is one 10.4 MB response) proportional to groups rather than
    files — on the real 8814-group report, a few hundred kilobytes.

    A group with no metrics simply has `quality: null`: quick-mode runs
    never measured anything, non-photo groups have nothing to measure, and
    a photo that would not decode honestly has no numbers. None of those is
    a zero.
    """
    if not groups:
        return
    with ScanIndex(DB_PATH) as index:
        quality = index.quality_for_hashes(g["content_hash"] for g in groups)
    for group in groups:
        group["quality"] = quality.get(group["content_hash"])


@app.post("/api/scan/{scan_id}/upgrade")
def upgrade_scan(scan_id: str):
    """«Досчитать полностью» — Р7's promise of reaching full coverage
    without scanning from scratch.

    This starts a *new* job in full mode over the same roots and, crucially,
    the same index. That is the whole trick, and it is not a new mechanism:
    Р6 already decided that resuming means "re-run and let the cache
    answer", because enumeration is the cheap phase and hashing is the
    expensive one. Every plain file whose size and mtime are unchanged
    keeps its cached hashes, so the second run re-walks the tree (seconds)
    and then reads only what the first run never looked at — archive
    contents — plus the previews quick mode skipped
    (`ScanJob._preview_phase`).

    The new scan gets its own id, and the quick report stays readable at
    its own. Nothing is discarded to make the upgrade: if the full run is
    cancelled halfway, the quick answer is still there.
    """
    job = registry.get(scan_id)
    if job is None:
        raise HTTPException(404, "Скан не найден.")
    if job.mode is ScanMode.FULL:
        raise HTTPException(409, "Этот скан уже выполнен в полном режиме.")
    if job.is_running:
        raise HTTPException(409, "Дождитесь окончания текущего скана.")

    full_job = registry.create(job.roots, db_path=DB_PATH, mode=ScanMode.FULL)
    return {
        "scan_id": full_job.scan_id,
        "mode": full_job.mode.value,
        "upgraded_from": scan_id,
        **full_job.progress.to_dict(),
    }


@app.post("/api/scan/{scan_id}/group/{content_hash}/verify")
def verify_scan_group(scan_id: str, content_hash: str):
    """«Сверить полностью» for one group: re-read every copy right now.

    See `dedupe.GroupVerification` for what this proves. Short version: the
    group was already byte-confirmed when the scan ran — this re-confirms
    it against the disk as it is at this moment, which is a different and
    useful question once a report is hours old.
    """
    job = registry.get(scan_id)
    if job is None or job.report is None:
        raise HTTPException(404, "Завершённый скан не найден.")

    group = next(
        (g for g in job.report.groups if g.content_hash == content_hash), None
    )
    if group is None:
        raise HTTPException(404, "Группа не найдена в этом скане.")

    return verify_group(group).to_dict()


@app.post("/api/scan/{scan_id}/quarantine")
def quarantine_scan(scan_id: str, req: QuarantineRequest):
    job = registry.get(scan_id)
    if job is None or job.report is None:
        raise HTTPException(404, "Завершённый скан не найден.")

    group_hashes = set(req.group_hashes) if req.group_hashes else None
    result = run_quarantine(
        job.report.groups,
        Path(req.quarantine_dir),
        confirm_media=req.confirm_media,
        group_hashes=group_hashes,
    )
    return result.to_dict()


def _find_group(job, content_hash: str):
    group = next((g for g in job.report.groups if g.content_hash == content_hash), None)
    if group is None:
        raise HTTPException(404, "Группа не найдена в этом скане.")
    return group


@app.post("/api/scan/{scan_id}/group/{content_hash}/decision")
def set_group_decision(scan_id: str, content_hash: str, req: DecisionRequest):
    """Задача 12, half one: record a per-group decision — this is the
    "решение... за секунду" step, and it is deliberately *not* the step
    that touches the filesystem. Recording is instant and reversible
    (`DELETE` below); moving files only happens in `apply_decisions`,
    batched, and only for groups a human actually queued — the gap pilot
    finding P1.1 asked for between "nothing" and "everything".
    """
    job = registry.get(scan_id)
    if job is None or job.report is None:
        raise HTTPException(404, "Завершённый скан не найден.")
    group = _find_group(job, content_hash)

    if group.only_archive_members:
        raise HTTPException(
            400,
            "У групп целиком внутри архивов нет отдельного решения — "
            "архив управляется целиком через /api/scan/{id}/archive-quarantine.",
        )
    if req.action not in ("quarantine", "keep"):
        raise HTTPException(400, "action должен быть 'quarantine' или 'keep'.")
    if req.keeper_path is not None:
        chosen = next((r for r in group.records if r.display_path == req.keeper_path), None)
        if chosen is None:
            raise HTTPException(400, "keeper_path не входит в состав этой группы.")
        if chosen.is_archive_member:
            # Р8 already ranks an archive member last precisely because it
            # can never be the thing that moves (Р1: an archive is the unit
            # of action). Letting a human override *to* one would strand
            # every plain copy in quarantine behind the one copy nobody can
            # act on.
            raise HTTPException(
                400, "Нельзя оставить копию внутри архива — её нельзя вынуть, не переписав архив."
            )

    with ScanIndex(DB_PATH) as index:
        index.record_decision(content_hash, req.action, req.keeper_path)
    return {"content_hash": content_hash, "action": req.action, "keeper_path": req.keeper_path}


@app.delete("/api/scan/{scan_id}/group/{content_hash}/decision")
def clear_group_decision(scan_id: str, content_hash: str):
    """Undo — the keyboard `U` action. No group lookup against the report
    is needed to delete a row that may or may not exist, and refusing to
    clear a decision just because the scan object is gone would defeat the
    entire point of decisions outliving it."""
    with ScanIndex(DB_PATH) as index:
        index.clear_decision(content_hash)
    return {"content_hash": content_hash, "cleared": True}


@app.post("/api/scan/{scan_id}/apply-decisions")
def apply_decisions(scan_id: str, req: ApplyDecisionsRequest):
    """Задача 12, half two: the batch path. Moves every group that is
    (a) queued for quarantine, (b) not already applied, and (c) still
    present in this scan's report — nothing else. Groups nobody decided on
    are never visited (see `quarantine.quarantine_reviewed_groups`).

    A media group without `confirm_media=True` stays queued rather than
    silently dropped: its decision's `applied_at` is left NULL, so calling
    this again with `confirm_media=True` (a second explicit step, matching
    the rule everywhere else in this project) picks it up.
    """
    job = registry.get(scan_id)
    if job is None or job.report is None:
        raise HTTPException(404, "Завершённый скан не найден.")

    hashes = [g.content_hash for g in job.report.groups]
    with ScanIndex(DB_PATH) as index:
        decisions = index.decisions_for_hashes(hashes)
    to_apply = {
        h: d["keeper_path"]
        for h, d in decisions.items()
        if d["action"] == "quarantine" and d["applied_at"] is None
    }

    if not to_apply:
        return {
            "applied": 0, "kept": {}, "moved": [], "failed": [],
            "pending_media_review": [], "archive_only_notes": [],
        }

    result = quarantine_reviewed_groups(
        job.report.groups, Path(req.quarantine_dir), to_apply, confirm_media=req.confirm_media,
    )

    failed_hashes = {f["group_hash"] for f in result.failed}
    media_pending_hashes = {m["group_hash"] for m in result.pending_media_review}
    applied_hashes = [
        h for h in to_apply if h not in failed_hashes and h not in media_pending_hashes
    ]
    with ScanIndex(DB_PATH) as index:
        index.mark_decisions_applied(applied_hashes)

    payload = result.to_dict()
    payload["applied"] = len(applied_hashes)
    return payload


@app.post("/api/scan/{scan_id}/archive-quarantine")
def quarantine_one_archive(scan_id: str, req: ArchiveActionRequest):
    """Screen 5's "в карантин целиком" button. `quarantine_archives`
    itself is what refuses the request outright when the report was taken
    in quick mode (Р7: every archive there is UNREAD, never actionable)
    and refuses any archive whose verdict is not FULLY_REDUNDANT — this
    endpoint adds nothing to that logic, it just aims it at one path.
    """
    job = registry.get(scan_id)
    if job is None or job.report is None:
        raise HTTPException(404, "Завершённый скан не найден.")

    verdicts = classify_archives(job.report)
    result = quarantine_archives(
        verdicts,
        job.report,
        Path(req.quarantine_dir),
        confirm_media=req.confirm_media,
        archive_paths={req.archive_path},
    )
    return result.to_dict()


@app.get("/api/quarantine/journal")
def get_journal(quarantine_dir: str):
    """The journal/restore screen's read side — a thin wrapper over
    `quarantine.journal_summary`, itself a thin reader of `journal.jsonl`
    (Р5: that file, not `manifest.json`, is the source of truth). Not
    scoped to a scan_id: the journal outlives any one scan or server
    restart, which is the entire reason it exists.
    """
    return {"entries": journal_summary(Path(quarantine_dir))}


@app.post("/api/quarantine/restore")
def restore_quarantine(req: RestoreRequest):
    """One-action restore from the journal/restore screen — a web wrapper
    over the already-complete `quarantine.restore_from_journal`, exactly as
    задача 12 asks for. `op_ids=None` restores everything restorable in one
    call; a specific set restores only the checked rows.
    """
    op_ids = set(req.op_ids) if req.op_ids else None
    result = restore_from_journal(Path(req.quarantine_dir), op_ids)
    return result.to_dict()


@app.get("/api/thumbnail")
def thumbnail(
    path: str,
    content_hash: Optional[str] = Query(default=None, alias="hash"),
    store: bool = Query(
        default=True,
        description="Сохранять ли результат декодирования в кэш превью. "
        "Вкладка похожих (задача 14) просит store=0: отпечатки есть у всей "
        "библиотеки, а кэш на 512 МБ (Р9) рассчитан на группы дублей — "
        "просмотр похожих вытеснил бы из него именно то, за чем он нужен.",
    ),
):
    """Serve a cached preview so you can actually *see* which photo you're
    about to quarantine — instant once a scan has run, because
    `dedupe.run_full_stage` already generated and cached it (see
    thumbnails.py). `hash` is the group's content hash and should always be
    sent by current clients (it's the cache key); `path` remains required
    as a fallback identifier and for the on-the-fly path below.
    """
    with ScanIndex(DB_PATH) as index:
        resolved_hash = content_hash or index.resolve_content_hash(path)
        if resolved_hash:
            cached = thumbnails.get_cached_path(index, resolved_hash)
            if cached is not None:
                return FileResponse(cached, media_type="image/jpeg")

        # Cache miss: nothing generated yet for this content (file below
        # the dedupe threshold, scan didn't reach the full-hash phase for
        # it, or the cache was cleared by hand). Decode once on the spot so
        # the UI never shows a blank tile, and cache the result if the
        # content hash is known so the next request is instant.
        file_path = Path(path)
        if not file_path.is_file():
            raise HTTPException(404, "Файл не найден.")
        try:
            preview = thumbnails.generate(file_path)
        except Exception as exc:  # noqa: BLE001 - not every "image" opens cleanly
            raise HTTPException(415, f"Не удалось построить превью: {exc}") from exc

        if resolved_hash and store:
            # Stores the quality metrics from this same decode too, so a
            # photo first seen through this fallback is as measured as one
            # the scan reached (thumbnails.store). Skipped when the caller
            # asked not to pollute the cache -- see `store` above.
            thumbnails.store(index, resolved_hash, preview)

        return StreamingResponse(io.BytesIO(preview.data), media_type="image/jpeg")


@app.get("/api/scan/{scan_id}/similar")
def scan_similar(
    scan_id: str,
    max_distance: int = Query(
        default=DEFAULT_UI_THRESHOLD,
        description="Порог Хэмминга. Нечётное округляется вниз: расстояния "
        "чётные по построению, см. similar_review.UI_THRESHOLDS.",
    ),
):
    r"""Похожие снимки — четвёртый тип группы на экране (задача 14, Р2).

    **Только чтение, и это свойство формы, а не дисциплины.** Ручка возвращает
    словари, собранные `similar_review.build_review`: ни `ScanReport`, ни
    `DuplicateGroup`, ни `keeper` — то есть ни одного объекта, который
    `quarantine.py` умеет принять. Отдельного запрета «не отправлять похожие
    в карантин» здесь нет, потому что нечего запрещать: пути не существует
    (см. докстринг `similar.py` и `tests/test_similar_never_quarantines.py`).

    Группировка считается **на каждый запрос**, а не берётся из отчёта, и это
    то же решение, что у `dupecleaner similar` и у `events`: порог — ровно
    то, что человек будет двигать, а сохранённая кластеризация была бы
    закэшированным ответом на вопрос, параметр которого и есть суть. Дорогая
    половина (один декод на снимок) уже лежит в индексе, кластеризация 6000
    отпечатков — десятые доли секунды.

    Не привязано к `job.report`: отпечатки живут в индексе по хэшу
    содержимого и переживают перезапуск сервера, в отличие от объекта отчёта
    (см. «Мелочь» в plan.md). Нужен только `scan_id` — он есть у джобы, и по
    нему `phash_rows` отбирает снимки этого скана.
    """
    job = registry.get(scan_id)
    if job is None:
        raise HTTPException(404, "Скан не найден.")

    thresholds = thresholds_for(max_distance)
    with ScanIndex(DB_PATH) as index:
        stats = index.phash_stats(scan_id, PHASH_ALGO)
        rows = index.phash_rows(scan_id, PHASH_ALGO)
        entries = entries_from_rows(rows)
        quality = index.quality_for_hashes(e.content_hash for e in entries)
        # Момент съёмки (задача 16) — чтобы «похожи» могло означать «соседние
        # кадры серии», а не только «близкие отпечатки». Одним запросом по
        # всему скану, а не по группам: группы ещё не собраны, а строить их
        # дважды ради выборки путей дороже, чем прочитать таблицу один раз.
        moments = {
            row[0]: row[1] for row in index.moments(scan_id) if row[1] is not None
        }

    clustering = find_similar_groups(
        entries, thresholds, entries_without_hash=stats["without_phash"]
    )
    payload = build_review(
        clustering, quality=quality, taken_at=moments, coverage=stats
    )
    payload["scan_id"] = scan_id
    payload["mode"] = job.mode.value
    # Р7: перцептивные хэши — часть полной обработки. В быстром режиме их
    # нет вовсе, и экран обязан сказать это прямо, а не показать пустой
    # список, который читается как «похожих не нашлось».
    payload["phash_available"] = bool(entries)
    return payload


# --- разбор альбома по одному снимку (задача 24) ----------------------------
#
# Ни одна из этих ручек не привязана к `scan_id`, и это то же решение, что
# у `/api/scan/{id}/similar`, доведённое до конца. Разбор идёт по
# **собранной библиотеке** (задача 22), а не по отчёту скана: состав
# альбомов лежит в `library_moves`, метрики — в `content_phashes`
# (миграция 12), лица — в `content_faces`, решения — в `review_decisions`.
# Всё это по хэшу содержимого и всё это переживает перезапуск сервера, в
# отличие от объекта отчёта, который не переживает (см. «Мелочь» в
# plan.md). Поэтому разбор можно начать в любой момент, не сканируя
# заново.


def _library_rows() -> list[dict]:
    with ScanIndex(DB_PATH) as index:
        return index.library_contents()


@app.get("/api/library/albums")
def library_albums():
    """Папки собранной библиотеки со счётчиками решений.

    Пустой список — не ошибка, а честный ответ «библиотека ещё не
    собрана»: до задачи 22 в `library_moves` нет ни строки, и экран
    обязан сказать это прямо, а не показать пустую сетку, которая
    читается как «альбомов нет». Это та же находка, что у задачи 11 с
    вкладкой «Обычные файлы».
    """
    rows = _library_rows()
    albums = album_review.group_albums(rows)
    with ScanIndex(DB_PATH) as index:
        states = index.album_states_for_hashes(r["content_key"] for r in rows)
        totals = index.album_state_counts()

    by_folder: dict[str, dict] = {}
    for album in albums:
        by_folder[album.folder] = {**album.to_dict(), "states": {"keep": 0, "print": 0, "drop": 0, "none": 0}}
    for row in rows:
        folder = album_review.folder_of(row["destination"])
        entry = by_folder.get(folder)
        if entry is None:
            continue
        state = (states.get(row["content_key"]) or {}).get("album_state") or "none"
        entry["states"][state] = entry["states"].get(state, 0) + 1

    return {
        "albums": [by_folder[a.folder] for a in albums],
        "photos": len(rows),
        "totals": totals,
        "formats": [f.to_dict() for f in album_review.PRINT_FORMATS],
        "states": list(album_review.ALL_STATES),
        "state_labels": album_review.STATE_LABEL,
    }


@app.get("/api/library/album")
def library_album(folder: str = Query(..., description="Папка альбома как её вернул /api/library/albums")):
    """Очередь одного альбома: сетка, порядок и фильтр «не годится для печати».

    `folder` приходит параметром запроса, а не частью пути: путь альбома
    содержит разделители (`2018\\2018 Novosibirsk`), и собирать его обратно
    из сегментов URL значило бы угадывать, какой разделитель был в
    исходном пути — ровно то, от чего `library.join_path` защищается.
    """
    rows = _library_rows()
    photos = album_review.photos_in_album(rows, folder)
    if not photos:
        raise HTTPException(404, "Такой папки нет среди альбомов библиотеки.")

    hashes = [p["content_hash"] for p in photos if p["content_hash"]]
    with ScanIndex(DB_PATH) as index:
        quality = index.quality_for_hashes(hashes)
        faces = index.face_counts_for_hashes(hashes)
        states = index.album_states_for_hashes(hashes)

    name = next(
        (a.name for a in album_review.group_albums(rows) if a.folder == folder),
        album_review.basename_of(folder),
    )
    queue = album_review.build_album_queue(
        photos, album=folder, name=name, quality=quality, faces=faces, states=states
    )
    payload = queue.to_dict()
    payload["state_labels"] = album_review.STATE_LABEL
    return payload


@app.post("/api/library/review/state")
def set_album_state(req: AlbumStateRequest):
    """Записать решение по снимку. Запись мгновенная и обратимая
    (`DELETE` ниже); файл от неё не двигается — двигает только
    `/api/library/review/apply`, пачкой и только то, что помечено
    «убрать». Тот же разрыв между «решил» и «произошло», который задача
    12 завела для групп."""
    if req.state not in album_review.ALL_STATES:
        raise HTTPException(
            400,
            "state должен быть одним из: " + ", ".join(album_review.ALL_STATES),
        )
    if not req.content_hash:
        raise HTTPException(
            400,
            "у снимка нет хэша содержимого — решение некуда записать так, чтобы "
            "оно пережило перескан (Р10). Досчитайте индекс полной обработкой.",
        )
    with ScanIndex(DB_PATH) as index:
        index.record_album_state(req.content_hash, req.state)
    return {
        "content_hash": req.content_hash,
        "state": req.state,
        "state_label": album_review.STATE_LABEL[req.state],
    }


@app.delete("/api/library/review/state")
def clear_album_state(content_hash: str = Query(...)):
    """Отмена — клавиша `U` на экране разбора. Снимает только альбомную
    ось: решение по группе дублей на том же хэше остаётся как было."""
    with ScanIndex(DB_PATH) as index:
        index.clear_album_state(content_hash)
    return {"content_hash": content_hash, "cleared": True}


@app.post("/api/library/review/apply")
def apply_album_drops(req: ApplyDropsRequest):
    """Применить «убрать»: пачкой, в карантин, и больше никуда.

    Двигаются только снимки, которые (а) помечены «убрать», (б) ещё не
    перемещены, (в) лежат в библиотеке по записи задачи 22 — и, если
    `folder` задан, только из этой папки. Непомеченное не посещается
    вовсе: итерация идёт по решениям, а не по альбому.

    `confirm=False` ничего не двигает и даже не создаёт папку карантина —
    решения остаются в очереди, и повторный вызов с `confirm=True`
    подхватит их без повторного просмотра.
    """
    rows = _library_rows()
    with ScanIndex(DB_PATH) as index:
        pending = set(index.pending_drop_hashes())

    photos = [
        {
            "path": row["destination"],
            "content_hash": row["content_key"],
            "size": row["size"],
        }
        for row in rows
        if row["content_key"] in pending
        and (req.folder is None or album_review.folder_of(row["destination"]) == req.folder)
    ]

    result = quarantine_review_drops(
        photos, Path(req.quarantine_dir), confirm=req.confirm
    )
    moved_hashes = [m["group_hash"] for m in result.moved if m.get("group_hash")]
    if moved_hashes:
        with ScanIndex(DB_PATH) as index:
            index.mark_album_states_applied(moved_hashes)

    payload = result.to_dict()
    payload["applied"] = len(moved_hashes)
    payload["queued"] = len(photos)
    payload["confirmed"] = req.confirm
    return payload
