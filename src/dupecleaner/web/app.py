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

from .. import thumbnails
from ..jobs import ScanRegistry
from ..quarantine import run_quarantine
from ..storage import DEFAULT_DB_PATH, ScanIndex

BASE_DIR = Path(__file__).parent

app = FastAPI(title="dupe-cleaner")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

registry = ScanRegistry()
DB_PATH: Path | str = DEFAULT_DB_PATH


class ScanRequest(BaseModel):
    paths: list[str]
    include_archives: bool = True


class QuarantineRequest(BaseModel):
    quarantine_dir: str
    group_hashes: Optional[list[str]] = None
    confirm_media: bool = False


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
    job = registry.create(req.paths, db_path=DB_PATH, include_archives=req.include_archives)
    return {"scan_id": job.scan_id, **job.progress.to_dict()}


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
    return {"scan_id": scan_id, **job.report.to_dict()}


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


@app.get("/api/thumbnail")
def thumbnail(path: str, content_hash: Optional[str] = Query(default=None, alias="hash")):
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
            data, width, height = thumbnails.generate(file_path)
        except Exception as exc:  # noqa: BLE001 - not every "image" opens cleanly
            raise HTTPException(415, f"Не удалось построить превью: {exc}") from exc

        if resolved_hash:
            thumbnails.store(index, resolved_hash, data, width, height)

        return StreamingResponse(io.BytesIO(data), media_type="image/jpeg")
