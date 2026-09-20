"""Progress state for a running scan.

Deliberately reports *per-phase* progress with an exact denominator rather
than one invented global percentage. A scan has phases whose costs can't be
compared up front (enumerating 500k files is cheap; hashing 300 GB is not),
so a single blended "47%" would be a guess presented as a fact. Instead
each phase reports what it actually knows:

- `enumerating` — total unknown by definition (we're still discovering it),
  so it reports a running count and the folder currently being walked.
- `quick_hashing` / `full_hashing` — total known exactly, because the
  candidate set is known before the phase starts. Percentage, throughput
  and ETA here are real numbers, not decoration.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# If nothing has moved for this long, the scan is very likely stuck on the
# path in `current_path` (an unresponsive network share, a failing drive).
# Surfacing that is more useful than a spinner that spins forever.
STALL_SECONDS = 30.0

PHASE_LABELS = {
    "starting": "Подготовка",
    "enumerating": "Этап 1/3: обход файлов",
    "quick_hashing": "Этап 2/3: быстрая проверка кандидатов",
    "full_hashing": "Этап 3/3: полное хэширование",
    "grouping": "Формирование групп",
    "previewing": "Миниатюры для просмотра",
    "done": "Готово",
    "cancelled": "Остановлено",
    "failed": "Ошибка",
}


@dataclass
class ScanProgress:
    scan_id: str
    roots: list[str]
    status: str = "starting"

    files_seen: int = 0
    bytes_seen: int = 0

    phase_files_done: int = 0
    phase_files_total: int = 0
    phase_bytes_done: int = 0
    phase_bytes_total: int = 0

    files_from_cache: int = 0
    # Cumulative across the whole run and never reset by a phase change —
    # `phase_files_done` belongs to the current phase, so it can't answer
    # "how much did this run actually have to read?" once the run is over.
    # Counts hashing work only; see `advance(count_as_hashed=...)`.
    files_hashed: int = 0
    groups_found: int = 0

    current_path: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    phase_started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --- mutation (called from the scan thread) ----------------------------

    def enter_phase(self, status: str, files_total: int = 0, bytes_total: int = 0) -> None:
        with self._lock:
            self.status = status
            self.phase_files_total = files_total
            self.phase_bytes_total = bytes_total
            self.phase_files_done = 0
            self.phase_bytes_done = 0
            self.phase_started_at = time.time()
            self.updated_at = time.time()

    def advance(
        self,
        *,
        files: int = 0,
        size_bytes: int = 0,
        current_path: str | None = None,
        count_as_hashed: bool = True,
    ) -> None:
        """Move the current phase forward.

        `count_as_hashed=False` advances the phase without touching
        `files_hashed`, which answers a narrower question than "how many
        files did this phase process": it answers "how much did this run
        have to *re-read for hashing*", and that is the number proving the
        cache did its job on a resumed or upgraded scan. The preview phase
        reads files too, but hashing is not what it does, and counting its
        work there would make a fully cached re-run look like it re-hashed
        things.
        """
        with self._lock:
            self.phase_files_done += files
            if count_as_hashed:
                self.files_hashed += files
            self.phase_bytes_done += size_bytes
            if current_path is not None:
                self.current_path = current_path
            self.updated_at = time.time()

    def note_seen(self, size_bytes: int, current_path: str) -> None:
        with self._lock:
            self.files_seen += 1
            self.bytes_seen += size_bytes
            self.current_path = current_path
            self.updated_at = time.time()

    def finish(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self.status = status
            self.error = error
            self.finished_at = time.time()
            self.updated_at = time.time()

    # --- derived values ----------------------------------------------------

    @property
    def percent(self) -> float | None:
        """None when the phase genuinely has no known total (enumeration)."""
        if self.status in ("done", "cancelled"):
            return 100.0
        if self.phase_bytes_total > 0:
            return min(100.0, 100.0 * self.phase_bytes_done / self.phase_bytes_total)
        if self.phase_files_total > 0:
            return min(100.0, 100.0 * self.phase_files_done / self.phase_files_total)
        return None

    @property
    def bytes_per_second(self) -> float | None:
        elapsed = time.time() - self.phase_started_at
        if elapsed < 1.0 or self.phase_bytes_done <= 0:
            return None
        return self.phase_bytes_done / elapsed

    @property
    def eta_seconds(self) -> float | None:
        rate = self.bytes_per_second
        if not rate or self.phase_bytes_total <= 0:
            return None
        remaining = max(0, self.phase_bytes_total - self.phase_bytes_done)
        return remaining / rate

    @property
    def is_stalled(self) -> bool:
        if self.status in ("done", "cancelled", "failed"):
            return False
        return (time.time() - self.updated_at) > STALL_SECONDS

    @property
    def seconds_since_update(self) -> float:
        return time.time() - self.updated_at

    def to_dict(self) -> dict:
        return {
            "scan_id": self.scan_id,
            "roots": self.roots,
            "status": self.status,
            "phase_label": PHASE_LABELS.get(self.status, self.status),
            "percent": self.percent,
            "files_seen": self.files_seen,
            "bytes_seen": self.bytes_seen,
            "phase_files_done": self.phase_files_done,
            "phase_files_total": self.phase_files_total,
            "phase_bytes_done": self.phase_bytes_done,
            "phase_bytes_total": self.phase_bytes_total,
            "files_from_cache": self.files_from_cache,
            "files_hashed": self.files_hashed,
            "groups_found": self.groups_found,
            "current_path": self.current_path,
            "bytes_per_second": self.bytes_per_second,
            "eta_seconds": self.eta_seconds,
            "is_stalled": self.is_stalled,
            "seconds_since_update": self.seconds_since_update,
            "elapsed_seconds": (self.finished_at or time.time()) - self.started_at,
            "warnings": list(self.warnings),
            "error": self.error,
        }
