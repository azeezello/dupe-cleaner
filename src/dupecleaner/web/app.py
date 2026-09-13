"""Minimal local web UI: run a scan, review duplicate groups (with special
handling for media and archive-only groups), send confirmed groups to
quarantine. Single-user, localhost-only, in-memory scan store — this is a
personal tool for your own machine, not a multi-tenant service.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..dedupe import find_duplicate_groups
from ..models import MediaKind, ScanReport
from ..quarantine import run_quarantine
from ..scanner import Scanner

BASE_DIR = Path(__file__).parent

app = FastAPI(title="dupe-cleaner")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# scan_id -> ScanReport. In-memory by design (see module docstring); restart
# the server and past scan results are gone, but nothing on disk depends on
# this — a fresh scan is cheap to re-run, and quarantine actions are what
# get persisted (to manifest.json), not scan results.
_SCANS: dict[str, ScanReport] = {}


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


@app.post("/api/scan")
def start_scan(req: ScanRequest):
    if not req.paths:
        raise HTTPException(400, "Укажите хотя бы одну папку для сканирования.")

    scanner = Scanner(include_archives=req.include_archives)
    records = list(scanner.iter_records(req.paths))
    groups = find_duplicate_groups(records, warnings=scanner.warnings)

    report = ScanReport(
        scanned_roots=req.paths,
        total_files_seen=scanner.total_files_seen,
        groups=groups,
        warnings=scanner.warnings,
    )
    scan_id = str(uuid.uuid4())
    _SCANS[scan_id] = report
    return {"scan_id": scan_id, **report.to_dict()}


@app.get("/api/scan/{scan_id}")
def get_scan(scan_id: str):
    report = _SCANS.get(scan_id)
    if report is None:
        raise HTTPException(404, "Скан не найден (сервер перезапускался?).")
    return {"scan_id": scan_id, **report.to_dict()}


@app.post("/api/scan/{scan_id}/quarantine")
def quarantine_scan(scan_id: str, req: QuarantineRequest):
    report = _SCANS.get(scan_id)
    if report is None:
        raise HTTPException(404, "Скан не найден (сервер перезапускался?).")

    group_hashes = set(req.group_hashes) if req.group_hashes else None
    result = run_quarantine(
        report.groups,
        Path(req.quarantine_dir),
        confirm_media=req.confirm_media,
        group_hashes=group_hashes,
    )
    return result.to_dict()


@app.get("/api/thumbnail")
def thumbnail(path: str):
    """Best-effort small preview for a plain-file image, used by the review
    UI so you can actually *see* which photo you're about to quarantine.
    Archive-member images are not thumbnailed in the MVP (would require
    extracting to a temp file) — shown as a generic icon client-side instead.
    """
    from PIL import Image

    file_path = Path(path)
    if not file_path.is_file():
        raise HTTPException(404, "Файл не найден.")
    try:
        with Image.open(file_path) as img:
            img.thumbnail((240, 240))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=80)
            buf.seek(0)
            return StreamingResponse(buf, media_type="image/jpeg")
    except Exception as exc:  # noqa: BLE001 - not every "image" opens cleanly
        raise HTTPException(415, f"Не удалось построить превью: {exc}") from exc
