"""Persistent scan index (SQLite).

This is what makes a scan survivable. Every file discovered and every hash
computed is written to disk as the scan progresses, so:

- if the process crashes, is killed, or the machine reboots mid-scan,
  nothing that was already hashed has to be hashed again;
- re-running a scan over mostly-unchanged disks is nearly free, because
  hashing — the expensive part — is answered from cache;
- you can see, in numbers, how much came from cache instead of being
  re-read (`dupecleaner index --stats`, or the counter in the web UI).

A cached hash is only trusted while the underlying bytes provably haven't
changed: every hash is stamped with the size+mtime of the *real file on
disk* it came from (for a file inside an archive that's the archive
itself, since that's what would change if the member were replaced). Any
mismatch on the next scan clears the hash automatically — see the
`ON CONFLICT` clause in `upsert_files`.

Rows are tagged with the scan that last saw them, so results are always
computed from what this scan actually found; files deleted since an
earlier scan can never resurrect as phantom duplicates.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Iterator

from .models import DuplicateGroup, FileRecord, MediaKind

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    display_path        TEXT PRIMARY KEY,
    real_path           TEXT NOT NULL,
    size                INTEGER NOT NULL,
    mtime               REAL NOT NULL,
    media_kind          TEXT NOT NULL,
    is_archive_member   INTEGER NOT NULL,
    archive_path        TEXT,
    member_name         TEXT,
    source_size         INTEGER NOT NULL,
    source_mtime        REAL NOT NULL,
    quick_hash          TEXT,
    full_hash           TEXT,
    hashed_source_size  INTEGER,
    hashed_source_mtime REAL,
    last_scan_id        TEXT NOT NULL,
    seen_at             REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_scan_size  ON files(last_scan_id, size);
CREATE INDEX IF NOT EXISTS idx_files_full_hash  ON files(last_scan_id, full_hash);
CREATE INDEX IF NOT EXISTS idx_files_quick_hash ON files(last_scan_id, size, quick_hash);
"""

# Versioned, additive migrations layered on top of _SCHEMA. Each script must
# be safe to re-run (IF NOT EXISTS everywhere) so a crash mid-upgrade can
# simply be retried on the next open, and so opening a brand-new database
# (schema_version starts at 0) and upgrading an old one go through the exact
# same code path in __init__ below.
#
# v2 adds `content_previews`: one row per unique file *content* (keyed by
# full_hash, the same content hash duplicate grouping already computes —
# see thumbnails.py for why that key was chosen over a path or a weaker
# hash). It holds the thumbnail's size/dimensions now, and is exactly where
# task 9's quality metrics (resolution is already here; sharpness and
# recompression columns are pre-created below, NULL until task 9 fills
# them in) land without another migration or touching `files` at all.
_MIGRATIONS: dict[int, str] = {
    2: """
    CREATE TABLE IF NOT EXISTS content_previews (
        content_hash         TEXT PRIMARY KEY,
        thumbnail_bytes      INTEGER NOT NULL,
        width                INTEGER,
        height               INTEGER,
        -- Task 9 (quality metrics): resolution is width/height above;
        -- these two are left NULL until that task computes them, so it
        -- only has to start writing, not migrate anything.
        sharpness_score      REAL,
        recompression_score  REAL,
        created_at           REAL NOT NULL,
        accessed_at          REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_content_previews_accessed
        ON content_previews(accessed_at);
    """,
}

DEFAULT_DB_PATH = Path.home() / ".dupecleaner" / "index.db"

# A cached hash is valid only while the bytes it was taken from are
# unchanged. This expression is reused by every "what still needs work"
# query, so the definition of "stale" lives in exactly one place.
_HASH_IS_FRESH = (
    "(hashed_source_size = source_size AND hashed_source_mtime = source_mtime)"
)


class ScanIndex:
    """Thin wrapper over the SQLite index. Safe to use from one writer
    thread (the scan job) while readers poll progress — SQLite handles the
    locking, and WAL mode keeps readers from blocking the writer.
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        current_version = int(row["value"]) if row else 0
        for version in sorted(v for v in _MIGRATIONS if v > current_version):
            self._conn.executescript(_MIGRATIONS[version])
            current_version = version

        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> "ScanIndex":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- writing -----------------------------------------------------------

    def upsert_files(self, records: Iterable[FileRecord], scan_id: str) -> int:
        """Record discovered files. Existing hashes are preserved when the
        underlying bytes are unchanged, and dropped automatically when they
        are not — that automatic drop is what keeps a resumed scan correct
        rather than merely fast.
        """
        now = time.time()
        rows = [
            (
                r.display_path,
                r.real_path,
                r.size,
                r.mtime,
                r.media_kind.value,
                int(r.is_archive_member),
                r.archive_path,
                r.member_name,
                r.effective_source_size,
                r.effective_source_mtime,
                scan_id,
                now,
            )
            for r in records
        ]
        if not rows:
            return 0

        self._conn.executemany(
            """
            INSERT INTO files (
                display_path, real_path, size, mtime, media_kind,
                is_archive_member, archive_path, member_name,
                source_size, source_mtime, last_scan_id, seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(display_path) DO UPDATE SET
                real_path         = excluded.real_path,
                size              = excluded.size,
                mtime             = excluded.mtime,
                media_kind        = excluded.media_kind,
                is_archive_member = excluded.is_archive_member,
                archive_path      = excluded.archive_path,
                member_name       = excluded.member_name,
                source_size       = excluded.source_size,
                source_mtime      = excluded.source_mtime,
                last_scan_id      = excluded.last_scan_id,
                seen_at           = excluded.seen_at,
                quick_hash = CASE
                    WHEN files.hashed_source_size  = excluded.source_size
                     AND files.hashed_source_mtime = excluded.source_mtime
                    THEN files.quick_hash ELSE NULL END,
                full_hash = CASE
                    WHEN files.hashed_source_size  = excluded.source_size
                     AND files.hashed_source_mtime = excluded.source_mtime
                    THEN files.full_hash ELSE NULL END
            """,
            rows,
        )
        return len(rows)

    def set_quick_hash(self, display_path: str, quick_hash: str) -> None:
        self._conn.execute(
            """
            UPDATE files
               SET quick_hash = ?,
                   hashed_source_size  = source_size,
                   hashed_source_mtime = source_mtime
             WHERE display_path = ?
            """,
            (quick_hash, display_path),
        )

    def set_full_hash(self, display_path: str, full_hash: str) -> None:
        self._conn.execute(
            """
            UPDATE files
               SET full_hash = ?,
                   hashed_source_size  = source_size,
                   hashed_source_mtime = source_mtime
             WHERE display_path = ?
            """,
            (full_hash, display_path),
        )

    def commit(self) -> None:
        self._conn.commit()

    # --- reading -----------------------------------------------------------

    def needs_quick_hash(self, scan_id: str) -> list[FileRecord]:
        """Files that share a size with at least one other file in this scan
        and don't have a usable cached quick hash. Files with a unique size
        cannot possibly have a duplicate, so they are never read at all.
        """
        cursor = self._conn.execute(
            f"""
            SELECT * FROM files
             WHERE last_scan_id = ? AND size > 0
               AND size IN (
                   SELECT size FROM files
                    WHERE last_scan_id = ? AND size > 0
                    GROUP BY size HAVING COUNT(*) > 1
               )
               AND (quick_hash IS NULL OR NOT {_HASH_IS_FRESH})
             ORDER BY size
            """,
            (scan_id, scan_id),
        )
        return [_row_to_record(row) for row in cursor]

    def needs_full_hash(self, scan_id: str) -> list[FileRecord]:
        """Files that survived the quick-hash prefilter — same size *and*
        same head/tail sample as another file — and still lack a usable
        cached full hash. Only these get read end to end.
        """
        cursor = self._conn.execute(
            f"""
            SELECT * FROM files
             WHERE last_scan_id = ? AND size > 0 AND quick_hash IS NOT NULL
               AND (size, quick_hash) IN (
                   SELECT size, quick_hash FROM files
                    WHERE last_scan_id = ? AND size > 0 AND quick_hash IS NOT NULL
                    GROUP BY size, quick_hash HAVING COUNT(*) > 1
               )
               AND (full_hash IS NULL OR NOT {_HASH_IS_FRESH})
             ORDER BY size
            """,
            (scan_id, scan_id),
        )
        return [_row_to_record(row) for row in cursor]

    def count_cached_hashes(self, scan_id: str) -> int:
        """How many files in this scan were answered from cache instead of
        being re-read. This is the number that proves a resumed scan didn't
        redo the expensive work.
        """
        row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM files
             WHERE last_scan_id = ? AND full_hash IS NOT NULL AND {_HASH_IS_FRESH}
            """,
            (scan_id,),
        ).fetchone()
        return int(row["n"])

    def duplicate_groups(self, scan_id: str) -> list[DuplicateGroup]:
        cursor = self._conn.execute(
            f"""
            SELECT * FROM files
             WHERE last_scan_id = ? AND full_hash IS NOT NULL AND {_HASH_IS_FRESH}
               AND full_hash IN (
                   SELECT full_hash FROM files
                    WHERE last_scan_id = ? AND full_hash IS NOT NULL
                    GROUP BY full_hash HAVING COUNT(*) > 1
               )
             ORDER BY full_hash, display_path
            """,
            (scan_id, scan_id),
        )
        by_hash: dict[str, list[FileRecord]] = defaultdict(list)
        for row in cursor:
            by_hash[row["full_hash"]].append(_row_to_record(row))

        return [
            DuplicateGroup(content_hash=content_hash, records=records)
            for content_hash, records in by_hash.items()
            if len(records) > 1
        ]

    def scan_totals(self, scan_id: str) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS total"
            "  FROM files WHERE last_scan_id = ?",
            (scan_id,),
        ).fetchone()
        return int(row["n"]), int(row["total"])

    def stats(self) -> dict:
        row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS files,
                   COALESCE(SUM(size), 0) AS bytes,
                   SUM(CASE WHEN full_hash IS NOT NULL AND {_HASH_IS_FRESH}
                            THEN 1 ELSE 0 END) AS hashed
              FROM files
            """
        ).fetchone()
        return {
            "db_path": str(self.db_path),
            "files_indexed": int(row["files"]),
            "bytes_indexed": int(row["bytes"]),
            "files_with_valid_hash": int(row["hashed"] or 0),
        }

    # --- thumbnail cache (see thumbnails.py) -------------------------------
    #
    # Keyed by content_hash (full_hash), not by path: a duplicate group's
    # whole reason for existing is that its records share identical bytes,
    # so they share one cached preview. This also means quarantining or
    # restoring a file never touches this table at all — the bytes (and
    # therefore the key) haven't changed, only the file's location.

    def get_thumbnail_meta(self, content_hash: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM content_previews WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()

    def upsert_thumbnail(
        self,
        content_hash: str,
        thumbnail_bytes: int,
        width: int | None,
        height: int | None,
    ) -> None:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO content_previews (
                content_hash, thumbnail_bytes, width, height, created_at, accessed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO UPDATE SET
                thumbnail_bytes = excluded.thumbnail_bytes,
                width           = excluded.width,
                height          = excluded.height,
                accessed_at     = excluded.accessed_at
            """,
            (content_hash, thumbnail_bytes, width, height, now, now),
        )

    def touch_thumbnail(self, content_hash: str) -> None:
        """Bump the access time used for LRU eviction. Called every time a
        cached thumbnail is actually served, not just when it's generated —
        so a photo the user is currently reviewing survives eviction over
        one from a scan nobody has looked at since."""
        self._conn.execute(
            "UPDATE content_previews SET accessed_at = ? WHERE content_hash = ?",
            (time.time(), content_hash),
        )

    def total_thumbnail_bytes(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(thumbnail_bytes), 0) AS n FROM content_previews"
        ).fetchone()
        return int(row["n"])

    def lru_thumbnail_hashes(self, limit: int) -> list[str]:
        """The `limit` least-recently-accessed thumbnails, oldest first —
        what eviction removes first when the cache is over its size cap."""
        cursor = self._conn.execute(
            "SELECT content_hash FROM content_previews ORDER BY accessed_at ASC LIMIT ?",
            (limit,),
        )
        return [row["content_hash"] for row in cursor]

    def delete_thumbnails(self, content_hashes: Iterable[str]) -> None:
        hashes = [(h,) for h in content_hashes]
        if not hashes:
            return
        self._conn.executemany(
            "DELETE FROM content_previews WHERE content_hash = ?", hashes
        )

    def resolve_content_hash(self, path: str) -> str | None:
        """Look up the content hash for a file by either path form the web
        layer might be handed. Used only as a fallback when a caller (an
        older client, a direct link) doesn't already have the group's
        content_hash on hand — the normal path passes it explicitly.

        Matches `real_path` only for plain files: for an archive member,
        `real_path` is the *archive's* path (see models.FileRecord), shared
        by every member inside it, so matching on it there could resolve to
        an arbitrary member's hash rather than a specific one. Not reachable
        today — the web UI never requests a thumbnail for an archive member
        (see thumbnails.py) — but excluded here so this stays correct if
        that ever changes rather than relying on callers to know not to.
        """
        row = self._conn.execute(
            f"""
            SELECT full_hash FROM files
             WHERE (display_path = ? OR (real_path = ? AND is_archive_member = 0))
               AND full_hash IS NOT NULL AND {_HASH_IS_FRESH}
             LIMIT 1
            """,
            (path, path),
        ).fetchone()
        return row["full_hash"] if row else None

    def prune_missing(self) -> int:
        """Drop rows whose file no longer exists on disk. Pure housekeeping —
        results are already scoped per scan, so this only reclaims space.
        """
        cursor = self._conn.execute("SELECT display_path, real_path FROM files")
        gone = [
            (row["display_path"],)
            for row in cursor
            if not Path(row["real_path"]).exists()
        ]
        if gone:
            self._conn.executemany("DELETE FROM files WHERE display_path = ?", gone)
            self._conn.commit()
        return len(gone)


def _row_to_record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        display_path=row["display_path"],
        real_path=row["real_path"],
        size=row["size"],
        mtime=row["mtime"],
        media_kind=MediaKind(row["media_kind"]),
        is_archive_member=bool(row["is_archive_member"]),
        archive_path=row["archive_path"],
        member_name=row["member_name"],
        source_size=row["source_size"],
        source_mtime=row["source_mtime"],
    )
