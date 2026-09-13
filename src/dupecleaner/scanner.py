"""Walks one or more root paths and yields a FileRecord for every regular
file *and* every file found inside a supported archive along the way — so
a duplicate can be detected whether it sits loose on disk or ended up
zipped inside a backup, "вперемешку" (mixed) as required.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Iterator

from . import archives
from .config import DEFAULT_EXCLUDE_DIR_NAMES, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from .models import FileRecord, MediaKind


def classify_media(name: str) -> MediaKind:
    ext = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return MediaKind.PHOTO
    if ext in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    return MediaKind.NONE


class Scanner:
    """Stateful only in that it accumulates human-readable warnings
    (unreadable files, unsupported/broken archives) encountered along the
    way, so a scan can report "N files scanned, M skipped: ..." instead of
    silently losing coverage.
    """

    def __init__(
        self,
        include_archives: bool = True,
        exclude_dir_names: frozenset[str] = frozenset(DEFAULT_EXCLUDE_DIR_NAMES),
    ) -> None:
        self.include_archives = include_archives
        self.exclude_dir_names = exclude_dir_names
        self.warnings: list[str] = []
        self.total_files_seen = 0

    def iter_records(self, roots: list[str | Path]) -> Iterator[FileRecord]:
        for root in roots:
            root_path = Path(root)
            if root_path.is_file():
                yield from self._records_for_path(root_path)
                continue
            if not root_path.exists():
                self.warnings.append(f"Папка не найдена, пропущена: {root_path}")
                continue
            for dirpath, dirnames, filenames in os.walk(root_path):
                dirnames[:] = [d for d in dirnames if d not in self.exclude_dir_names]
                for filename in filenames:
                    yield from self._records_for_path(Path(dirpath) / filename)

    def _records_for_path(self, path: Path) -> Iterator[FileRecord]:
        try:
            stat = path.stat()
        except OSError as exc:
            self.warnings.append(f"Не удалось прочитать {path}: {exc}")
            return

        kind = archives.archive_kind_for(path) if self.include_archives else None

        if kind is None:
            self.total_files_seen += 1
            yield FileRecord(
                display_path=str(path),
                real_path=str(path),
                size=stat.st_size,
                mtime=stat.st_mtime,
                media_kind=classify_media(path.name),
            )
            return

        if kind == "rar" and not archives.is_rar_supported():
            # Counted as "seen" (we did examine it) but not expanded into
            # members — the warning explains why to anyone reading the report.
            self.total_files_seen += 1
            self.warnings.append(
                f"Пропущен RAR-архив {path}: не найден unrar/unar в PATH."
            )
            return

        try:
            members = list(archives.list_members(path, kind))
        except Exception as exc:  # noqa: BLE001 - archive libs raise assorted errors
            self.total_files_seen += 1
            self.warnings.append(f"Не удалось открыть архив {path}: {exc}")
            return

        for member in members:
            self.total_files_seen += 1
            yield FileRecord(
                display_path=f"{path}::{member.name}",
                real_path=str(path),
                size=member.size,
                mtime=member.mtime,
                media_kind=classify_media(member.name),
                is_archive_member=True,
                archive_path=str(path),
                member_name=member.name,
                # The archive's own size/mtime: if the archive is rewritten,
                # every cached hash taken from inside it is invalidated.
                source_size=stat.st_size,
                source_mtime=stat.st_mtime,
            )
