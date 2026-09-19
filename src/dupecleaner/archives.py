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
from typing import BinaryIO, Iterable, Iterator

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
    """Return a readable (sequential) binary stream for one member's content.

    For a one-off lookup of a single known member — a UI preview, a manual
    check — this is the right tool. For hashing many members of the same
    archive, use `open_members_sequential` instead: see its docstring for
    why looking members up one at a time here doesn't scale for tar.
    """
    if kind == "zip":
        zf = zipfile.ZipFile(archive_path)
        return zf.open(member_name, "r")  # closing the returned stream is enough

    if kind == "tar":
        # Deliberately NOT `tf.extractfile(member_name)`: passing a *name*
        # makes tarfile call `getmember()` -> `getmembers()`, which parses
        # every header in the archive before deciding the member isn't
        # already cached — for a gzip-compressed stream that means
        # decompressing the whole thing from byte zero, however early the
        # member actually sits (see pilot finding A2). Walking the archive
        # ourselves and stopping as soon as we reach the member we want,
        # then handing the already-found TarInfo object to `extractfile`
        # (which skips the by-name lookup entirely), costs only the bytes
        # up to that member.
        tf = tarfile.open(archive_path)
        for member in tf:
            if member.isfile() and member.name == member_name:
                extracted = tf.extractfile(member)
                if extracted is not None:
                    return extracted
                break
        tf.close()
        raise FileNotFoundError(member_name)

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


def open_members_sequential(
    archive_path: Path, kind: str, member_names: Iterable[str]
) -> Iterator[tuple[str, BinaryIO]]:
    """Yield `(member_name, stream)` for the requested members in **one**
    pass over the archive, instead of one open per member.

    This is the fix for pilot finding A2. `archives.open_member` for a
    tar-family archive used to do `tarfile.open(path)` followed by
    `extractfile(member_name)` — a fresh open plus a full by-name lookup —
    for every single member. `tarfile` keeps no member index up front, so
    that by-name lookup (`getmember()` -> `getmembers()`) parses every
    header from the start of the file to build one, and for a
    gzip-compressed stream (no random access) that means decompressing
    the archive from scratch. Doing that once per member turns hashing
    into "decompress the whole archive, once per candidate" — measured at
    71 seconds per member against 73 seconds for the entire 44.9 GB
    archive in one go (i.e. ~100x more work than necessary once there is
    more than one member to check).

    The fix is to walk the archive **once**, sequentially, and hand back a
    stream for exactly the members the caller asked for as they're
    reached — members not requested are still walked past (gzip can't
    skip forward without reading, so passing over them costs their bytes
    too), but nothing already-read is ever decompressed a second time.
    Pair this with `hashing.quick_and_full_hash`, which computes both the
    quick and full hash from one sequential read, and a scan that touches
    every requested member costs one pass over the archive — independent
    of how many members are being hashed.

    Only tar-family archives get the special single-pass walk: zip and
    rar entries are independently seekable via their central directory
    (opening one doesn't require reading the others), so they're handled
    with a plain per-member `open_member`-equivalent. 7z has no in-memory
    single-member read at all (see `open_member`), so all requested
    members are extracted together in one `py7zr` call instead of one
    temp-dir extraction per member.

    Each yielded stream must be fully read (or closed) before the next
    item is requested from this generator — for tar it shares the
    archive's one open file handle, so reading out of order isn't
    possible. This mirrors `hashing.full_hash`'s non-seekable-stream
    contract.
    """
    wanted = set(member_names)
    if not wanted:
        return

    if kind == "tar":
        with tarfile.open(archive_path) as tf:
            remaining = set(wanted)
            for member in tf:
                if not remaining:
                    break
                if not member.isfile() or member.name not in remaining:
                    continue
                stream = tf.extractfile(member)
                if stream is None:
                    continue
                remaining.discard(member.name)
                yield member.name, stream
        return

    if kind == "zip":
        with zipfile.ZipFile(archive_path) as zf:
            for name in wanted:
                try:
                    with zf.open(name, "r") as stream:
                        yield name, stream
                except KeyError:
                    continue
        return

    if kind == "7z":
        import tempfile

        with tempfile.TemporaryDirectory(prefix="dupecleaner-7z-") as tmpdir:
            with py7zr.SevenZipFile(archive_path, mode="r") as zf:
                zf.extract(path=tmpdir, targets=sorted(wanted))
            for name in wanted:
                extracted_path = Path(tmpdir) / name
                if extracted_path.exists():
                    yield name, io.BytesIO(extracted_path.read_bytes())
        return

    if kind == "rar":
        if rarfile is None:
            return
        with rarfile.RarFile(archive_path) as rf:
            for name in wanted:
                try:
                    with rf.open(name) as stream:
                        yield name, stream
                except KeyError:
                    continue
        return

    raise ValueError(f"Unsupported archive kind: {kind}")  # pragma: no cover


def _dos_to_epoch(date_time: tuple) -> float:
    import time

    try:
        return time.mktime((*date_time, 0, 0, -1))
    except (ValueError, OverflowError):
        return 0.0
