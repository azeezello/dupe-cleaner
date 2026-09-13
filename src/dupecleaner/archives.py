"""Read-only access to archive contents, for treating archived files exactly
like loose files during duplicate scanning.

Design choice: we only ever *read* archives here, never write/modify them.
Actually removing a duplicate that lives inside an archive would mean
rewriting the whole archive — risky and easy to corrupt — so that stays a
manual, user-driven action; see docs/SAFETY.md. This module's job is only
to enumerate members and hand back a readable stream for each one.

Supported out of the box: .zip, .tar/.tar.gz/.tgz/.tar.bz2/.tar.xz, .7z.
.rar additionally requires the external `unrar` or `unar` binary in PATH
(rarfile shells out to it) — if it's missing we skip .rar files and record
a warning rather than failing the whole scan.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

import py7zr

try:
    import rarfile
except ImportError:  # pragma: no cover - rarfile is a declared dependency
    rarfile = None  # type: ignore


ARCHIVE_SUFFIX_KIND = {
    ".zip": "zip",
    ".tar": "tar",
    ".tar.gz": "tar",
    ".tgz": "tar",
    ".tar.bz2": "tar",
    ".tbz2": "tar",
    ".tar.xz": "tar",
    ".7z": "7z",
    ".rar": "rar",
}


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    size: int
    mtime: float


def archive_kind_for(path: Path) -> str | None:
    name = path.name.lower()
    # Check the longest matching suffix first (e.g. ".tar.gz" before ".gz").
    for suffix in sorted(ARCHIVE_SUFFIX_KIND, key=len, reverse=True):
        if name.endswith(suffix):
            return ARCHIVE_SUFFIX_KIND[suffix]
    return None


def is_rar_supported() -> bool:
    if rarfile is None:
        return False
    try:
        return rarfile.tool_setup() is None or True
    except Exception:
        return False


def list_members(archive_path: Path, kind: str) -> Iterator[ArchiveMember]:
    """Enumerate members without extracting content."""
    if kind == "zip":
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                yield ArchiveMember(info.filename, info.file_size, _dos_to_epoch(info.date_time))

    elif kind == "tar":
        with tarfile.open(archive_path) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                yield ArchiveMember(member.name, member.size, float(member.mtime))

    elif kind == "7z":
        with py7zr.SevenZipFile(archive_path, mode="r") as zf:
            for info in zf.list():
                if info.is_directory:
                    continue
                mtime = info.creationtime.timestamp() if info.creationtime else 0.0
                yield ArchiveMember(info.filename, info.uncompressed, mtime)

    elif kind == "rar":
        if rarfile is None:
            return
        with rarfile.RarFile(archive_path) as rf:
            for info in rf.infolist():
                if info.is_dir():
                    continue
                yield ArchiveMember(info.filename, info.file_size, info.date_time and 0.0 or 0.0)

    else:  # pragma: no cover - guarded by archive_kind_for
        raise ValueError(f"Unsupported archive kind: {kind}")


def open_member(archive_path: Path, kind: str, member_name: str) -> BinaryIO:
    """Return a readable (sequential) binary stream for one member's content."""
    if kind == "zip":
        zf = zipfile.ZipFile(archive_path)
        return zf.open(member_name, "r")  # closing the returned stream is enough

    if kind == "tar":
        tf = tarfile.open(archive_path)
        extracted = tf.extractfile(member_name)
        if extracted is None:
            raise FileNotFoundError(member_name)
        return extracted

    if kind == "7z":
        # py7zr (1.x) has no in-memory read() for a single member — it only
        # extracts to a real directory. Extract just this one member to a
        # throwaway temp dir, load its bytes into memory, and clean up
        # immediately so no extracted file lingers on disk.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="dupecleaner-7z-") as tmpdir:
            with py7zr.SevenZipFile(archive_path, mode="r") as zf:
                zf.extract(path=tmpdir, targets=[member_name])
            extracted_path = Path(tmpdir) / member_name
            data = extracted_path.read_bytes()
        return io.BytesIO(data)

    if kind == "rar":
        if rarfile is None:
            raise RuntimeError("rarfile/unrar is not available")
        rf = rarfile.RarFile(archive_path)
        return rf.open(member_name)

    raise ValueError(f"Unsupported archive kind: {kind}")  # pragma: no cover


def _dos_to_epoch(date_time: tuple) -> float:
    import time

    try:
        return time.mktime((*date_time, 0, 0, -1))
    except (ValueError, OverflowError):
        return 0.0
