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
from typing import TYPE_CHECKING, Callable, Iterable, Iterator

from .models import DuplicateGroup, FileRecord, MediaKind

if TYPE_CHECKING:  # pragma: no cover - import kept out of runtime on purpose
    # Only needed for annotations. Importing it for real would pull Pillow
    # into every process that merely opens the index — the CLI's `index
    # --stats`, a quarantine run reading a saved report — for a type name.
    from .quality import QualityMetrics

# Highest migration this build knows about. Note the gaps: 5 belongs to
# task 12's `review_decisions` and 8 to task 19's face clusters, both
# written on branches running beside this one. Task 13 took **9** rather
# than the next free-looking number for the reason the note below spells
# out, and the set-based bookkeeping in `__init__` is what makes a gap a
# fact the index records instead of a hole it silently skips.
#
# Note the gap at 5: that
# number belongs to task 12's `review_decisions`, which landed on `main`
# in a session running in parallel with this one. Two branches cannot
# both own "the next number", so this one took 6 and left 5 alone rather
# than colliding in the middle of a merge — and the bookkeeping in
# `__init__` below was changed at the same time so that a gap is a fact
# the index can record instead of a hole it silently skips.
SCHEMA_VERSION = 9

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
# hash). It holds the thumbnail's size and dimensions, and pre-created the
# `sharpness_score`/`recompression_score` columns task 9 fills in. Its
# comment claimed resolution was "already here" in `width`/`height`; it was
# not — those are the *thumbnail's* dimensions — so v3 adds the source
# resolution rather than quietly redefining them.
# v3 adds the rest of task 9's quality metrics next to the two columns v2
# pre-created for them. Four columns, each earning its place:
#
# - `source_width`/`source_height` — the *source* photo's resolution. Not a
#   re-reading of `width`/`height`, which hold the thumbnail's size and
#   always did: re-interpreting those would have turned every preview
#   already in the index into a claim that the photo is 240 px wide.
# - `jpeg_quality` — the quality factor recovered from the file's own
#   quantization tables. Kept rather than folded into the score because it
#   is the one number a person can act on ("saved at ~62"), and UX-BRIEF's
#   first principle is to show the evidence rather than assert a verdict.
# - `recompression_basis` — which of the three derivations produced
#   `recompression_score`. Scores from different bases are not comparable
#   (quality.py says why), so whatever ranks copies in task 17 has to be
#   able to read this, not guess it from `jpeg_quality IS NULL`.
#
# Written as a Python step rather than a SQL script because SQLite has no
# `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, and the invariant above —
# every migration safe to re-run — is what makes a crash between the
# migration and the version bump recoverable.
def _migrate_v3_quality_metrics(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(content_previews)")
    }
    for column, declaration in (
        ("source_width", "INTEGER"),
        ("source_height", "INTEGER"),
        ("jpeg_quality", "INTEGER"),
        ("recompression_basis", "TEXT"),
    ):
        if column not in existing:
            conn.execute(
                f"ALTER TABLE content_previews ADD COLUMN {column} {declaration}"
            )


# v4 adds task 15's origin verdict (Р3) to `files` rather than to
# `content_previews`, and that placement is the decision, not an
# implementation detail. Previews and quality metrics are properties of
# *bytes*, so one row per content hash answers for every copy. An origin
# verdict is not: half its evidence is the folder and the filename, and
# the pilot found identical bytes sitting in four different folders at
# once. A copy in `Screenshots 1` and a copy of the same screenshot filed
# into `Краснодар` are the same photo and different evidence, so they get
# a row each — which `files`, keyed by display_path, already is.
#
# Stamped with the source size/mtime for the same reason hashes are: the
# EXIF half of the evidence comes from the bytes, so replacing the file
# must invalidate the verdict. The path half cannot go stale, because the
# path is the key.
def _migrate_v4_origin(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(files)")}
    for column, declaration in (
        ("origin_class", "TEXT"),
        ("origin_confidence", "TEXT"),
        ("origin_evidence", "TEXT"),
        ("origin_stamp_size", "INTEGER"),
        ("origin_stamp_mtime", "REAL"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE files ADD COLUMN {column} {declaration}")


# v6 adds task 16's capture moment — when and where the photo was taken —
# next to task 15's origin verdict, in `files`, and for the same reason:
# the evidence is partly the path. A filename like `20170416_145106.jpg` is
# a capture time (the only one Google left behind on 1156 of these photos),
# and the same bytes sitting in two folders can carry two different
# filenames, so a hash-keyed row could not hold both.
#
# Stamped separately from the origin verdict even though one header read
# produces both. They are written together today, but an index built by
# task 15 has verdicts and no moments, and `needs_moment` has to be able to
# tell that apart from "not classified at all" — the same argument that
# gave hashes and origins their own stamps.
#
# Clusters themselves are deliberately NOT stored. Every threshold in
# `events.EventThresholds` is meant to be changed and re-run; a stored
# clustering would be a cached answer to a question whose parameters are
# the point, and the expensive half (the header read) is what this table
# already keeps.
#
# The number 6, not 5, and that is worth a word: task 12 is adding
# `review_decisions` as v5 on `main` at the same time as this branch is
# written. Both migrations are additive and independent, so after the merge
# the dict simply holds both; numbering this one 5 as well would have made
# one of them unreachable on any index that had already seen the other.
def _migrate_v6_moment(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(files)")}
    for column, declaration in (
        ("taken_at", "REAL"),
        ("time_source", "TEXT"),
        ("gps_lat", "REAL"),
        ("gps_lon", "REAL"),
        ("geo_source", "TEXT"),
        ("moment_stamp_size", "INTEGER"),
        ("moment_stamp_mtime", "REAL"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE files ADD COLUMN {column} {declaration}")


_MIGRATIONS: dict[int, str | Callable[[sqlite3.Connection], None]] = {
    2: """
    CREATE TABLE IF NOT EXISTS content_previews (
        content_hash         TEXT PRIMARY KEY,
        thumbnail_bytes      INTEGER NOT NULL,
        width                INTEGER,
        height               INTEGER,
        -- Task 9 (quality metrics). Left NULL by task 8 so task 9 only
        -- had to start writing. Note `width`/`height` above are the
        -- thumbnail's, not the photo's: the source resolution arrives in
        -- v3 as source_width/source_height.
        sharpness_score      REAL,
        recompression_score  REAL,
        created_at           REAL NOT NULL,
        accessed_at          REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_content_previews_accessed
        ON content_previews(accessed_at);
    """,
    3: _migrate_v3_quality_metrics,
    4: _migrate_v4_origin,
    # v5 (task 12, задача 12): "просмотренное накапливается и применяется
    # пачкой" needs somewhere for the pile to live that survives a server
    # restart — the same requirement Р6 already solved for hashes and
    # thumbnails/quality (task 8/9) solved for previews. `report` itself is
    # a plain in-memory object (see jobs.ScanRegistry) and does not survive
    # one; the index does, so decisions go here rather than into the report
    # or the browser, both of which one restart or one closed tab erases.
    #
    # Keyed by content_hash, not (scan_id, content_hash): a decision is a
    # fact about *content* ("this photo's extra copies go to quarantine"),
    # not about one particular scan run of it — exactly the reasoning
    # `content_previews` already uses for thumbnails and quality metrics.
    # That is what makes recovery from a lost `report` automatic: re-running
    # a scan over the same roots rebuilds groups from the same index data
    # (Р6), gets a new scan_id, but the *content hashes are unchanged*, so
    # every decision reattaches to its group without the reviewer having to
    # redo a single group. `applied_at` distinguishes "queued" from
    # "already moved" so a second apply pass or a page reload after a batch
    # quarantine run doesn't try to move the same files twice.
    5: """
    CREATE TABLE IF NOT EXISTS review_decisions (
        content_hash TEXT PRIMARY KEY,
        action       TEXT NOT NULL,   -- 'quarantine' | 'keep'
        keeper_path  TEXT,            -- display_path override of Р8's
                                       -- default keeper; NULL = accept it
        decided_at   REAL NOT NULL,
        applied_at   REAL             -- NULL until actually moved
    );
    """,
    6: _migrate_v6_moment,
    # (5 is task 12's `review_decisions` — see SCHEMA_VERSION above.)
    7: """
    -- Task 18: faces. Keyed by content_hash, next to `content_previews`
    -- and for the same reason: a face is a property of *pixels*, so four
    -- copies of one photograph in four folders share one answer and one
    -- decode. That is the opposite of the choice v4 made for origin
    -- verdicts, and deliberately so — half of an origin verdict's
    -- evidence is the folder it sits in, and none of a face's is.
    --
    -- Two tables rather than one, because "we looked and found nobody"
    -- is a real answer that has to survive. Without `content_face_scans`
    -- a photograph with no faces in it is indistinguishable from one
    -- nobody has run the detector over, and every rescan would decode
    -- the whole landscape half of the library again. It is the same
    -- shape of mistake as pilot finding A1 ("not checked" read as a
    -- verdict), one layer down.
    CREATE TABLE IF NOT EXISTS content_face_scans (
        content_hash        TEXT PRIMARY KEY,
        engine              TEXT NOT NULL,
        detect_long_side    INTEGER NOT NULL,
        faces_found         INTEGER NOT NULL,
        faces_skipped_small INTEGER NOT NULL DEFAULT 0,
        created_at          REAL NOT NULL
    );

    -- One row per embedded face. `embedding` is 512 bytes: 128 raw
    -- little-endian float32, L2-normalised, packed by
    -- `faces.encode_embedding`. `score` and `width` are stored so task 19
    -- can raise the detector's bar or drop small faces without re-reading
    -- 59 GB of photographs to do it.
    CREATE TABLE IF NOT EXISTS content_faces (
        content_hash TEXT NOT NULL,
        face_index   INTEGER NOT NULL,
        x            INTEGER NOT NULL,
        y            INTEGER NOT NULL,
        width        INTEGER NOT NULL,
        height       INTEGER NOT NULL,
        score        REAL NOT NULL,
        embedding    BLOB NOT NULL,
        PRIMARY KEY (content_hash, face_index)
    );
    """,
    # Task 13: perceptual hashes. Keyed by content_hash for the third time
    # in this file and for the third time with the same argument (Р9, Р11):
    # what a photograph *looks like* is a property of its pixels, so the
    # four copies of it filed in four folders share one answer and one
    # decode. Origin verdicts (v4) went into `files` instead precisely
    # because half of their evidence is the folder — none of a perceptual
    # hash's is.
    #
    # A table of its own rather than two more columns on
    # `content_previews`, which is the tempting shortcut since both are
    # written by the same decode. `content_previews` is a **cache**: it is
    # capped at 512 MB and evicted LRU (Р9), so its rows are expected to
    # disappear. A perceptual hash is not cache — losing it means decoding
    # the photograph again, which is the one expensive thing here — and
    # attaching it to a row designed to be thrown away would mean a
    # library that quietly forgets what it looks like whenever the
    # thumbnail cache fills up.
    #
    # `phash` is NULLable on purpose, and the row's *existence* is the
    # record that the photo was looked at. An image with no low-frequency
    # structure (a frame shot in the dark, a blank scan — 30 of them on
    # `D:\Photos`) gets no fingerprint, because a fingerprint of noise
    # clusters with other noise for no reason. Without the row, every
    # rescan would decode those again to rediscover the same nothing, and
    # "not fingerprintable" would be indistinguishable from "not looked
    # at" — pilot finding A1, one layer further down, exactly as
    # `content_face_scans` says.
    #
    # `algo` is stored rather than assumed. Hashes made by two different
    # definitions of "the hash" are not comparable, and a silent mix of
    # them would surface as near-duplicate groups that are not.
    9: """
    CREATE TABLE IF NOT EXISTS content_phashes (
        content_hash TEXT PRIMARY KEY,
        phash        TEXT,             -- NULL = looked at, no usable hash
        structure    REAL NOT NULL,    -- mean |coeff - median|, grey levels
        aspect       REAL NOT NULL,    -- source width / height
        algo         TEXT NOT NULL,
        created_at   REAL NOT NULL
    );
    """,
}

DEFAULT_DB_PATH = Path.home() / ".dupecleaner" / "index.db"

# A cached hash is valid only while the bytes it was taken from are
# unchanged. This expression is reused by every "what still needs work"
# query, so the definition of "stale" lives in exactly one place.
_HASH_IS_FRESH = (
    "(hashed_source_size = source_size AND hashed_source_mtime = source_mtime)"
)

# The same idea for the origin verdict (task 15), stamped separately
# because the two are computed in different phases and either can be
# present without the other: a quick scan hashes and classifies nothing,
# an index built before v4 has hashes and no verdicts.
_ORIGIN_IS_FRESH = (
    "(origin_stamp_size = source_size AND origin_stamp_mtime = source_mtime)"
)

# And the same for task 16's capture moment. `time_source` rather than
# `taken_at` is the presence test on purpose: a file with no date at all is
# a legitimate answer ("none"), and re-reading its header on every scan to
# rediscover that would be the one avoidable cost in the phase.
_MOMENT_IS_FRESH = (
    "(moment_stamp_size = source_size AND moment_stamp_mtime = source_mtime)"
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

        # Which migrations have run is tracked as a *set*, not as a high
        # water mark. A single number was enough while the schema grew in
        # one line; it stopped being enough the moment two sessions worked
        # on two branches at once, because whoever merged second would
        # find their migration number already taken and, worse, an index
        # stamped past it would skip the other's table without a word.
        # A set composes in either merge order.
        #
        # An index written before this bookkeeping existed carries only
        # `schema_version`, and for those the old meaning is exactly
        # right: everything up to that number did run. That is the
        # `<= current_version` line, and it is what makes this change
        # invisible to Aziz's existing index.
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        current_version = int(row["value"]) if row else 0
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'applied_migrations'"
        ).fetchone()
        if row is None:
            # Pre-bookkeeping index: the high water mark is all it has, and
            # for those it means exactly what it used to.
            applied = {v for v in _MIGRATIONS if v <= current_version}
        else:
            # Once the set exists it is the only authority. Falling back to
            # the number here would undo the whole point: an index stamped
            # 9 by another branch would swallow this branch's 6 in silence,
            # which is the bug the set was introduced to prevent.
            applied = {int(v) for v in row["value"].split(",") if v}

        for version in sorted(v for v in _MIGRATIONS if v not in applied):
            migration = _MIGRATIONS[version]
            if callable(migration):
                migration(self._conn)
            else:
                self._conn.executescript(migration)
            applied.add(version)

        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            # never downgrade the stamp: a newer build may have opened this
            # same file already, and saying "4" after it said "7" would
            # invite an older build to re-run migrations it does not own.
            (str(max(SCHEMA_VERSION, current_version)),),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('applied_migrations', ?)",
            (",".join(str(v) for v in sorted(applied)),),
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

    # --- origin verdicts (see origin.py) -----------------------------------

    def needs_origin(self, scan_id: str) -> list[FileRecord]:
        """Photos in this scan with no usable origin verdict.

        Plain files only. An archive member is excluded here rather than
        filtered out by the caller, because reading EXIF from inside an
        archive costs a sequential pass per member — finding A2, which
        task 3 removed and which must not come back through a side door.

        Unlike `needs_full_hash` this has no same-size prefilter in front
        of it: every photo needs a verdict, not only the duplicated ones.
        Task 16 builds events from the whole library, so a screenshot with
        no copies is exactly as important to exclude as one with three.
        """
        cursor = self._conn.execute(
            f"""
            SELECT * FROM files
             WHERE last_scan_id = ? AND is_archive_member = 0 AND media_kind = 'photo'
               AND (origin_class IS NULL OR NOT {_ORIGIN_IS_FRESH})
             ORDER BY display_path
            """,
            (scan_id,),
        )
        return [_row_to_record(row) for row in cursor]

    def set_origin(
        self,
        display_path: str,
        origin: str,
        confidence: str,
        evidence: Iterable[str] = (),
    ) -> None:
        """Store one file's verdict, stamped with the bytes it was read from.

        `evidence` is joined into one string rather than normalised into a
        table: it is read by people and shown verbatim, never queried, and
        a join table would be three times the rows of `files` to support a
        query nobody makes.
        """
        self._conn.execute(
            """
            UPDATE files
               SET origin_class       = ?,
                   origin_confidence  = ?,
                   origin_evidence    = ?,
                   origin_stamp_size  = source_size,
                   origin_stamp_mtime = source_mtime
             WHERE display_path = ?
            """,
            (origin, confidence, " · ".join(evidence), display_path),
        )

    def get_origin(self, display_path: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT origin_class, origin_confidence, origin_evidence FROM files"
            " WHERE display_path = ?",
            (display_path,),
        ).fetchone()

    def origin_breakdown(self, scan_id: str) -> dict[tuple[str, str], int]:
        """Counts per (origin, confidence) for one scan.

        Returned as raw counts rather than percentages so the caller can
        decide what the denominator is — "of all photos" and "of photos we
        actually classified" are different questions, and UX-BRIEF asks
        for the evidence rather than a rounded verdict.
        """
        cursor = self._conn.execute(
            """
            SELECT origin_class AS c, origin_confidence AS conf, COUNT(*) AS n
              FROM files
             WHERE last_scan_id = ? AND origin_class IS NOT NULL
             GROUP BY origin_class, origin_confidence
            """,
            (scan_id,),
        )
        return {(row["c"], row["conf"]): int(row["n"]) for row in cursor}

    def screenshot_paths(self, scan_id: str) -> list[str]:
        """Every photo in this scan classified as a screenshot.

        The one query task 16 needs from task 15: Р3 excludes screenshots
        from album generation, and an event clusterer wants the exclusion
        list, not a per-file lookup it has to run 30 000 times.
        """
        cursor = self._conn.execute(
            """
            SELECT display_path FROM files
             WHERE last_scan_id = ? AND origin_class IN (?, ?)
             ORDER BY display_path
            """,
            (scan_id, "screenshot_desktop", "screenshot_phone"),
        )
        return [row["display_path"] for row in cursor]

    # --- faces (see faces.py) ----------------------------------------------
    #
    # Keyed by content_hash, like the previews below and unlike the origin
    # verdicts above: a face is a property of the pixels, so one answer
    # serves every copy of the same photograph. See the v7 migration for
    # why "no faces here" needs a row of its own.

    def needs_faces(self, scan_id: str) -> list[FileRecord]:
        """Photos in this scan with no face pass behind them.

        Runs over the whole library rather than over the duplicate groups,
        the same call `needs_origin` makes and for the same reason: Р4
        makes a person a filter across all events, so a face index that
        covered only the duplicated photographs would answer the wrong
        question. A face in a photo with no copies is exactly as much a
        face.

        A photo with no usable `full_hash` is included, because the face
        phase is where it gets one: that phase reads the file in full
        anyway (the decoder does), so hashing the same bytes on the way
        past is close to free, and it is what lets the *next* scan skip
        the photo entirely (Р6). Plain files only — reading pixels out of
        an archive member would be finding A2 again, and Р1 treats
        archive contents as cold storage besides.
        """
        cursor = self._conn.execute(
            f"""
            SELECT f.* FROM files f
            LEFT JOIN content_face_scans s ON s.content_hash = f.full_hash
             WHERE f.last_scan_id = ?
               AND f.is_archive_member = 0
               AND f.media_kind = 'photo'
               AND (
                    f.full_hash IS NULL
                 OR NOT (f.hashed_source_size = f.source_size
                         AND f.hashed_source_mtime = f.source_mtime)
                 OR s.content_hash IS NULL
               )
             ORDER BY f.display_path
            """,
            (scan_id,),
        )
        return [_row_to_record(row) for row in cursor]

    def has_face_scan(self, content_hash: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM content_face_scans WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
            is not None
        )

    def set_faces(
        self,
        content_hash: str,
        faces: Iterable[tuple[int, int, int, int, int, float, bytes]],
        *,
        engine: str,
        detect_long_side: int,
        skipped_small: int = 0,
    ) -> None:
        """Record one photo's faces, replacing whatever was there.

        `faces` is a sequence of `(face_index, x, y, width, height, score,
        embedding)` — plain tuples rather than `faces.DetectedFace`, so
        this module stays importable without the optional face
        dependencies, exactly as it stays importable without Pillow (see
        the TYPE_CHECKING import at the top).

        Replace-rather-than-merge because the unit of truth is a whole
        pass over one photo: re-running with a different
        `detect_long_side` must not leave yesterday's faces mixed in with
        today's, half of them measured at another scale.
        """
        face_rows = [
            (content_hash, index, x, y, width, height, float(score), embedding)
            for index, x, y, width, height, score, embedding in faces
        ]
        self._conn.execute(
            "DELETE FROM content_faces WHERE content_hash = ?", (content_hash,)
        )
        if face_rows:
            self._conn.executemany(
                """
                INSERT INTO content_faces (
                    content_hash, face_index, x, y, width, height, score, embedding
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                face_rows,
            )
        self._conn.execute(
            """
            INSERT INTO content_face_scans (
                content_hash, engine, detect_long_side, faces_found,
                faces_skipped_small, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO UPDATE SET
                engine              = excluded.engine,
                detect_long_side    = excluded.detect_long_side,
                faces_found         = excluded.faces_found,
                faces_skipped_small = excluded.faces_skipped_small,
                created_at          = excluded.created_at
            """,
            (
                content_hash,
                engine,
                detect_long_side,
                len(face_rows),
                skipped_small,
                time.time(),
            ),
        )

    def get_faces(self, content_hash: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM content_faces WHERE content_hash = ?"
                " ORDER BY face_index",
                (content_hash,),
            )
        )

    def iter_scan_faces(self, scan_id: str) -> Iterator[sqlite3.Row]:
        """Every stored face belonging to a photo this scan saw, with the
        path it was found in.

        The one query task 19 needs from task 18, shaped the way task 15
        shaped `screenshot_paths`: clustering wants the whole set in one
        pass, not thirty thousand lookups. An iterator rather than a list
        because 66 000 rows of 512-byte vectors is 34 MB, and a clusterer
        that wants them all in memory should say so itself.

        DISTINCT on the join: several copies of one photograph share a
        content hash, and a person appearing four times because their
        photo was filed four times would be a cluster artefact invented by
        the storage layer.
        """
        return self._conn.execute(
            f"""
            SELECT DISTINCT cf.content_hash, cf.face_index, cf.x, cf.y,
                   cf.width, cf.height, cf.score, cf.embedding
              FROM content_faces cf
              JOIN files f ON f.full_hash = cf.content_hash
             WHERE f.last_scan_id = ? AND {_HASH_IS_FRESH}
             ORDER BY cf.content_hash, cf.face_index
            """,
            (scan_id,),
        )

    def face_stats(self, scan_id: str | None = None) -> dict:
        """Counts for the CLI and the report. Scoped to one scan when
        asked, over the whole index otherwise."""
        if scan_id is None:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS photos,
                       COALESCE(SUM(faces_found), 0) AS faces,
                       COALESCE(SUM(faces_skipped_small), 0) AS small,
                       COALESCE(SUM(CASE WHEN faces_found > 0 THEN 1 ELSE 0 END), 0)
                           AS photos_with_faces
                  FROM content_face_scans
                """
            ).fetchone()
        else:
            row = self._conn.execute(
                f"""
                SELECT COUNT(*) AS photos,
                       COALESCE(SUM(s.faces_found), 0) AS faces,
                       COALESCE(SUM(s.faces_skipped_small), 0) AS small,
                       COALESCE(SUM(CASE WHEN s.faces_found > 0 THEN 1 ELSE 0 END), 0)
                           AS photos_with_faces
                  FROM content_face_scans s
                 WHERE s.content_hash IN (
                     SELECT full_hash FROM files
                      WHERE last_scan_id = ? AND full_hash IS NOT NULL
                        AND {_HASH_IS_FRESH}
                 )
                """,
                (scan_id,),
            ).fetchone()
        return {
            "content_scanned": int(row["photos"]),
            "content_with_faces": int(row["photos_with_faces"]),
            "faces": int(row["faces"]),
            "faces_too_small": int(row["small"]),
        }

    def total_embedding_bytes(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(LENGTH(embedding)), 0) AS n FROM content_faces"
        ).fetchone()
        return int(row["n"])

    # --- perceptual hashes (see similar.py) --------------------------------
    # Keyed by content hash like previews and faces: what a photograph
    # looks like is a property of its pixels. See the v9 migration for why
    # this is its own table and not two columns on the preview cache.

    def has_phash(self, content_hash: str, algo: str | None = None) -> bool:
        """Whether this content has been fingerprinted already.

        A row with `phash IS NULL` still counts: it means the photo was
        decoded and found to have no usable structure, which is an answer.
        """
        if algo is None:
            row = self._conn.execute(
                "SELECT 1 FROM content_phashes WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT 1 FROM content_phashes WHERE content_hash = ? AND algo = ?",
                (content_hash, algo),
            ).fetchone()
        return row is not None

    def set_phash(
        self,
        content_hash: str,
        phash: str | None,
        structure: float,
        aspect: float,
        algo: str,
    ) -> None:
        """Record one photograph's perceptual fingerprint, or its absence.

        Plain values rather than a `similar.PerceptualHash`, so this module
        stays importable without Pillow — the same reason `set_faces` takes
        tuples instead of `faces.DetectedFace` (see the TYPE_CHECKING
        import at the top).
        """
        self._conn.execute(
            """
            INSERT INTO content_phashes (
                content_hash, phash, structure, aspect, algo, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO UPDATE SET
                phash      = excluded.phash,
                structure  = excluded.structure,
                aspect     = excluded.aspect,
                algo       = excluded.algo,
                created_at = excluded.created_at
            """,
            (content_hash, phash, structure, aspect, algo, time.time()),
        )

    def needs_phash(self, scan_id: str, algo: str) -> list[FileRecord]:
        """Photos in this scan with no perceptual fingerprint behind them.

        Over the whole library, not over the duplicate groups, and here
        that is not a preference but the definition of the job: two photos
        that are *similar* are by construction **not** byte-identical, so
        they are not in a duplicate group, so a near-duplicate pass
        restricted to duplicate groups would be searching the one place
        its answers cannot be. Same reach as `needs_faces` and
        `needs_origin`, different reason from both.

        A photo with no usable `full_hash` is included for the same reason
        `needs_faces` includes it: the phase reads the whole file anyway
        (the decoder does), so hashing those bytes on the way past is
        nearly free, and it is what lets the next scan skip the photo
        (Р6). Plain files only — decoding pixels out of an archive member
        is finding A2 again, and Р1 treats archive contents as cold
        storage.

        `algo` is a parameter rather than a constant so this module need
        not import `similar` (and through it Pillow) to know the name of
        the current definition: a row stamped with another one is work to
        redo, not work already done.
        """
        cursor = self._conn.execute(
            """
            SELECT f.* FROM files f
            LEFT JOIN content_phashes p
                   ON p.content_hash = f.full_hash AND p.algo = ?
             WHERE f.last_scan_id = ?
               AND f.is_archive_member = 0
               AND f.media_kind = 'photo'
               AND (
                    f.full_hash IS NULL
                 OR NOT (f.hashed_source_size = f.source_size
                         AND f.hashed_source_mtime = f.source_mtime)
                 OR p.content_hash IS NULL
               )
             ORDER BY f.display_path
            """,
            (algo, scan_id),
        )
        return [_row_to_record(row) for row in cursor]

    def phash_rows(self, scan_id: str, algo: str) -> list[tuple]:
        """Every fingerprinted photo this scan saw, as raw tuples:

            (content_hash, phash, aspect, size, [display_path, ...])

        Raw tuples rather than `similar.PhashEntry` for the layering reason
        `moments()` returns tuples rather than `events.PhotoMoment`: this
        module must not import the one that needs Pillow. The conversion
        lives in `similar.entries_from_rows`.

        One row per unique content with all of its paths attached, because
        that is what the grouping works on: four filed copies of one
        photograph are one fingerprint, and showing the reviewer the group
        means showing all four paths.

        Rows whose `phash` is NULL — decoded, no usable structure — are
        **not** returned; `phash_stats` counts them instead. They are not
        missing data, and they are not groupable either.
        """
        cursor = self._conn.execute(
            f"""
            SELECT f.full_hash AS ch, f.display_path, f.size,
                   p.phash, p.aspect
              FROM files f
              JOIN content_phashes p
                   ON p.content_hash = f.full_hash AND p.algo = ?
             WHERE f.last_scan_id = ?
               AND f.is_archive_member = 0
               AND f.media_kind = 'photo'
               AND f.full_hash IS NOT NULL AND {_HASH_IS_FRESH}
               AND p.phash IS NOT NULL
             ORDER BY f.full_hash, f.display_path
            """,
            (algo, scan_id),
        )
        by_hash: dict[str, list] = {}
        for row in cursor:
            entry = by_hash.get(row["ch"])
            if entry is None:
                by_hash[row["ch"]] = [row["ch"], row["phash"], row["aspect"],
                                      row["size"], [row["display_path"]]]
            else:
                entry[4].append(row["display_path"])
        return [tuple(v) for v in by_hash.values()]

    def phash_stats(self, scan_id: str, algo: str) -> dict:
        """Coverage, so a person can see how much of the library actually
        carries a fingerprint before reading anything into the groups."""
        row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS photos,
                   SUM(CASE WHEN p.content_hash IS NOT NULL THEN 1 ELSE 0 END) AS looked,
                   SUM(CASE WHEN p.phash IS NOT NULL THEN 1 ELSE 0 END) AS hashed
              FROM files f
              LEFT JOIN content_phashes p
                     ON p.content_hash = f.full_hash AND p.algo = ?
             WHERE f.last_scan_id = ? AND f.is_archive_member = 0
               AND f.media_kind = 'photo'
            """,
            (algo, scan_id),
        ).fetchone()
        photos = int(row["photos"] or 0)
        looked = int(row["looked"] or 0)
        hashed = int(row["hashed"] or 0)
        return {
            "photos": photos,
            "with_phash": hashed,
            "without_phash": looked - hashed,   # decoded, no structure
            "not_looked": photos - looked,
        }

    # --- capture moments (see events.py) -----------------------------------

    def needs_header(self, scan_id: str) -> list[FileRecord]:
        """Files whose header this scan still has to read.

        The union of "needs an origin verdict" and "needs a capture
        moment", because both come out of one `origin.read_signals` call
        and reading the same header twice to answer two questions would
        double the only expensive part of either feature.

        Wider than `needs_origin` in one way: videos are included. A clip
        shot at the party belongs in the party's event, and its moment
        comes from its filename or its mtime at no cost at all — nothing
        opens it (see `events.moment_without_header`). Р3 has no verdict to
        give a video, so it never gets one.
        """
        cursor = self._conn.execute(
            f"""
            SELECT * FROM files
             WHERE last_scan_id = ? AND is_archive_member = 0
               AND media_kind IN ('photo', 'video')
               AND (
                    (media_kind = 'photo'
                     AND (origin_class IS NULL OR NOT {_ORIGIN_IS_FRESH}))
                 OR (time_source IS NULL OR NOT {_MOMENT_IS_FRESH})
               )
             ORDER BY display_path
            """,
            (scan_id,),
        )
        return [_row_to_record(row) for row in cursor]

    def needs_moment(self, scan_id: str) -> list[FileRecord]:
        """Files with no usable capture moment. Photos and videos both."""
        cursor = self._conn.execute(
            f"""
            SELECT * FROM files
             WHERE last_scan_id = ? AND is_archive_member = 0
               AND media_kind IN ('photo', 'video')
               AND (time_source IS NULL OR NOT {_MOMENT_IS_FRESH})
             ORDER BY display_path
            """,
            (scan_id,),
        )
        return [_row_to_record(row) for row in cursor]

    def set_moment(
        self,
        display_path: str,
        taken_at: float | None,
        time_source: str,
        latitude: float | None = None,
        longitude: float | None = None,
        geo_source: str = "none",
    ) -> None:
        """Store when and where one file was taken, stamped with its bytes.

        `taken_at` may legitimately be NULL — a photo with no date anywhere
        is Р5's `_unsorted/`, not an error — which is why the stamp, and
        not the value, is what says the work was done.
        """
        self._conn.execute(
            """
            UPDATE files
               SET taken_at           = ?,
                   time_source        = ?,
                   gps_lat            = ?,
                   gps_lon            = ?,
                   geo_source         = ?,
                   moment_stamp_size  = source_size,
                   moment_stamp_mtime = source_mtime
             WHERE display_path = ?
            """,
            (taken_at, time_source, latitude, longitude, geo_source, display_path),
        )

    def moments(self, scan_id: str, *, exclude_origins: Iterable[str] = ()) -> list[tuple]:
        """Every media file in this scan as a raw tuple:

            (display_path, taken_at, time_source, gps_lat, gps_lon,
             geo_source, mtime)

        `mtime` rides along deliberately. Whether the filesystem's
        modification time may stand in for a missing capture date is a
        decision with a threshold-shaped answer — it is wrong by six years
        on this library and right on one that was never bulk-copied — so it
        belongs to the moment the clustering runs, not to the moment the
        scan wrote the row. Keeping it here means `--use-mtime` costs a
        re-cluster and not a re-scan.

        Returned as plain tuples rather than `events.PhotoMoment` so this
        module stays free of the event layer — `storage` is imported by the
        CLI's `index --stats` and by a quarantine run reading a saved
        report, neither of which should pull in the clusterer. `events`
        knows how to build itself from these; the direction of the
        dependency is the point.

        `exclude_origins` is applied in SQL rather than in Python because
        Р3's exclusion list is thousands of rows on a real library, and
        `screenshot_paths` already established that the index answers this
        kind of question in one query instead of thirty thousand.
        """
        excluded = list(exclude_origins)
        clause = ""
        params: list[object] = [scan_id]
        if excluded:
            placeholders = ",".join("?" * len(excluded))
            clause = f" AND (origin_class IS NULL OR origin_class NOT IN ({placeholders}))"
            params.extend(excluded)
        cursor = self._conn.execute(
            f"""
            SELECT display_path, taken_at, time_source, gps_lat, gps_lon,
                   geo_source, mtime
              FROM files
             WHERE last_scan_id = ? AND is_archive_member = 0
               AND media_kind IN ('photo', 'video'){clause}
             ORDER BY taken_at IS NULL, taken_at, display_path
            """,
            params,
        )
        return [
            (
                row["display_path"],
                row["taken_at"],
                row["time_source"] or "none",
                row["gps_lat"],
                row["gps_lon"],
                row["geo_source"] or "none",
                row["mtime"],
            )
            for row in cursor
        ]

    def latest_scan_id(self) -> str | None:
        """The scan whose rows are newest in the index.

        Needed because a report on disk does not carry its scan id, while
        every phase's results are keyed by one. Without this, `dupecleaner
        events` would have to ask a person to copy a UUID out of a log in
        order to do the obvious thing.
        """
        row = self._conn.execute(
            "SELECT last_scan_id FROM files ORDER BY seen_at DESC LIMIT 1"
        ).fetchone()
        return row["last_scan_id"] if row else None

    def moment_coverage(self, scan_id: str) -> dict[str, int]:
        """Counts per time source, plus how many have coordinates.

        The number that decides whether an event boundary is worth
        believing: a library timed mostly by `mtime` is a library whose
        events are a guess, and this is where that shows up before anyone
        looks at a cluster.
        """
        cursor = self._conn.execute(
            """
            SELECT COALESCE(time_source, 'unknown') AS src, COUNT(*) AS n
              FROM files
             WHERE last_scan_id = ? AND is_archive_member = 0
               AND media_kind IN ('photo', 'video')
             GROUP BY src
            """,
            (scan_id,),
        )
        out = {row["src"]: int(row["n"]) for row in cursor}
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n FROM files
             WHERE last_scan_id = ? AND is_archive_member = 0
               AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL
            """,
            (scan_id,),
        ).fetchone()
        out["with_geo"] = int(row["n"])
        return out

    def excluded_from_albums_paths(self, scan_id: str) -> list[str]:
        """Everything Р3 keeps out of albums: screenshots *and* scans.

        `screenshot_paths` (task 15) answers the narrower question and is
        kept as it is. This is `OriginClass.excluded_from_albums` expressed
        in SQL — the class list is imported from `origin` so there is one
        definition of the set rather than a second copy that drifts.
        """
        from .origin import OriginClass

        classes = [c.value for c in OriginClass if c.excluded_from_albums]
        placeholders = ",".join("?" * len(classes))
        cursor = self._conn.execute(
            f"""
            SELECT display_path FROM files
             WHERE last_scan_id = ? AND origin_class IN ({placeholders})
             ORDER BY display_path
            """,
            (scan_id, *classes),
        )
        return [row["display_path"] for row in cursor]

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
        metrics: "QualityMetrics | None" = None,
    ) -> None:
        """Record a cached thumbnail, and — when the same decode produced
        them — the quality metrics that belong to the same content.

        The conflict clause deliberately leaves the metric columns alone:
        they are written by `set_quality_metrics` below, called right after,
        so a thumbnail rewrite can never blank out metrics that are already
        there.
        """
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
        if metrics is not None:
            self.set_quality_metrics(content_hash, metrics)

    def set_quality_metrics(self, content_hash: str, metrics: "QualityMetrics") -> None:
        """Write the task-9 metrics for one content hash.

        Separate from `upsert_thumbnail` because the two are not always
        written together: an index built before task 9 has thumbnails
        without metrics, and backfilling those must not rewrite the JPEG or
        disturb its LRU position (see `thumbnails.maybe_generate`).

        Does nothing if the row does not exist — metrics describe a cached
        preview, and a metrics row with no preview would be invisible to
        eviction and never reclaimed.
        """
        self._conn.execute(
            """
            UPDATE content_previews
               SET source_width        = ?,
                   source_height       = ?,
                   sharpness_score     = ?,
                   recompression_score = ?,
                   recompression_basis = ?,
                   jpeg_quality        = ?
             WHERE content_hash = ?
            """,
            (
                metrics.source_width,
                metrics.source_height,
                metrics.sharpness,
                metrics.recompression,
                metrics.recompression_basis,
                metrics.jpeg_quality,
                content_hash,
            ),
        )

    def quality_for_hashes(self, content_hashes: Iterable[str]) -> dict[str, dict]:
        """Metrics for many content hashes at once, keyed by hash.

        Batched on purpose: the review screen asks about every group it is
        about to draw, and the real report has 8814 of them (task 4). One
        query per group would be 8814 round trips to answer a question the
        index can answer in one.

        Hashes with no metrics are simply absent from the result — a photo
        that would not decode, a group of non-photo files, or a preview
        cached before task 9 and not yet revisited. The caller shows the
        group without the numbers rather than inventing zeroes for it;
        UX-BRIEF's "честность в цифрах" applies to this exactly as much as
        to scan progress.
        """
        wanted = list(dict.fromkeys(content_hashes))
        if not wanted:
            return {}

        out: dict[str, dict] = {}
        # SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds; chunk rather
        # than assume the modern 32766.
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            cursor = self._conn.execute(
                f"""
                SELECT content_hash, width, height, source_width, source_height,
                       sharpness_score, recompression_score, recompression_basis,
                       jpeg_quality
                  FROM content_previews
                 WHERE content_hash IN ({placeholders})
                   AND recompression_basis IS NOT NULL
                """,
                chunk,
            )
            for row in cursor:
                out[row["content_hash"]] = _row_to_quality_dict(row)
        return out

    def count_quality_metrics(self) -> int:
        """How many cached previews carry metrics. The number that shows a
        re-run actually backfilled an index built before task 9."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM content_previews "
            "WHERE recompression_basis IS NOT NULL"
        ).fetchone()
        return int(row["n"])

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

    # --- review decisions (task 12) -----------------------------------------
    #
    # See the schema-v5 comment above for why these are keyed by content
    # hash and live in the index rather than in the in-memory report or the
    # browser. A decision is one human choice per group: quarantine the
    # non-keeper copies (optionally overriding which copy Р8 would have
    # kept), or keep everything and move on. Recording one is deliberately
    # cheap and separate from acting on it — задача 12's whole point is a
    # gap between "reviewed" and "moved" that today does not exist.

    def record_decision(
        self, content_hash: str, action: str, keeper_path: str | None = None
    ) -> None:
        """Queue (or replace) a human decision for one group.

        A fresh decision always resets `applied_at` to NULL, including one
        that overwrites an earlier decision on the same group — the human
        changed their mind, so whatever the previous decision's apply state
        was no longer describes this one. Not reachable in practice for an
        already-applied group: the non-keeper copies are gone from disk by
        then, so the group stops appearing in the next scan's report and
        nothing in the UI offers to re-decide it.
        """
        if action not in ("quarantine", "keep"):
            raise ValueError(f"record_decision: неизвестное действие {action!r}")
        self._conn.execute(
            """
            INSERT INTO review_decisions (content_hash, action, keeper_path, decided_at, applied_at)
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(content_hash) DO UPDATE SET
                action      = excluded.action,
                keeper_path = excluded.keeper_path,
                decided_at  = excluded.decided_at,
                applied_at  = NULL
            """,
            (content_hash, action, keeper_path, time.time()),
        )
        self._conn.commit()

    def clear_decision(self, content_hash: str) -> None:
        """Undo — back to "not reviewed". Used by the keyboard `U` action
        and by nothing else; there is no other way for a decision to stop
        existing short of the group itself disappearing.
        """
        self._conn.execute(
            "DELETE FROM review_decisions WHERE content_hash = ?", (content_hash,)
        )
        self._conn.commit()

    def decisions_for_hashes(self, content_hashes: Iterable[str]) -> dict[str, dict]:
        """Decisions for many groups at once, keyed by hash — same batching
        reasoning as `quality_for_hashes`: the review screen asks about
        every group in the report it just loaded, and on the real
        8814-group report that is one query instead of 8814.

        A hash with no row simply isn't a key in the result — "not yet
        reviewed" is the absence of a decision, not a decision of its own.
        """
        wanted = list(dict.fromkeys(content_hashes))
        if not wanted:
            return {}
        out: dict[str, dict] = {}
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            cursor = self._conn.execute(
                f"""
                SELECT content_hash, action, keeper_path, decided_at, applied_at
                  FROM review_decisions
                 WHERE content_hash IN ({placeholders})
                """,
                chunk,
            )
            for row in cursor:
                out[row["content_hash"]] = {
                    "action": row["action"],
                    "keeper_path": row["keeper_path"],
                    "decided_at": row["decided_at"],
                    "applied_at": row["applied_at"],
                }
        return out

    def mark_decisions_applied(
        self, content_hashes: Iterable[str], when: float | None = None
    ) -> None:
        """Stamp `applied_at` once `quarantine.quarantine_reviewed_groups`
        has actually moved a group's files, so a second apply pass (or the
        same report reloaded after a restart) treats it as done rather than
        queued — the file is gone from disk either way, but only this call
        is what tells the index so.
        """
        hashes = [(when or time.time(), h) for h in content_hashes]
        if not hashes:
            return
        self._conn.executemany(
            "UPDATE review_decisions SET applied_at = ? WHERE content_hash = ?",
            hashes,
        )
        self._conn.commit()

    def pending_decision_count(self) -> int:
        """How many groups are queued for quarantine but not yet applied —
        the number that answers "did an evening of review survive?" after a
        restart, independent of any one scan_id."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM review_decisions "
            "WHERE action = 'quarantine' AND applied_at IS NULL"
        ).fetchone()
        return int(row["n"])


def _row_to_quality_dict(row: sqlite3.Row) -> dict:
    """Shape one `content_previews` row for the web layer.

    `megapixels` is derived here rather than stored: it is the form a
    person reads ("12.2 МП"), while the pixel counts are the form a
    comparison needs, and deriving is cheaper than keeping two columns
    honest about each other.
    """
    source_width = row["source_width"]
    source_height = row["source_height"]
    megapixels = (
        round(source_width * source_height / 1_000_000, 2)
        if source_width and source_height
        else None
    )
    return {
        "source_width": source_width,
        "source_height": source_height,
        "megapixels": megapixels,
        "sharpness": (
            round(row["sharpness_score"], 2)
            if row["sharpness_score"] is not None
            else None
        ),
        "recompression": (
            round(row["recompression_score"], 3)
            if row["recompression_score"] is not None
            else None
        ),
        "recompression_basis": row["recompression_basis"],
        "jpeg_quality": row["jpeg_quality"],
        "thumbnail_width": row["width"],
        "thumbnail_height": row["height"],
    }


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
