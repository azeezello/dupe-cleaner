"""Load a `probe_events.py` dump into a real index, so the real code can cluster it.

Why this exists, rather than a full `dupecleaner scan` over the library:
hashing 63.7 GB through the cloud session's bridge to the disk is hours
(the pilot measured the bridge at a fraction of native speed), while the
event clustering needs no hashes at all — only the capture moment, which
the probe already read once per photo with the same `origin.read_signals`
call the scan phase uses.

So the split is deliberate and worth stating: the header reading and the
clustering run over the *whole* real library through this loader, and the
`ScanJob` phase that normally writes those rows is exercised end to end on
a smaller set of real folders (and in `tests/test_events.py`). Nothing here
re-implements a rule; it only puts rows where the scan would have put them.

    python pilot/load_probe_index.py <probe.jsonl> <index.db> [--scan-id ID]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dupecleaner.events import MomentPolicy  # noqa: E402
from dupecleaner.models import FileRecord, MediaKind  # noqa: E402
from dupecleaner.storage import ScanIndex  # noqa: E402

from probe_common import moment_for_row  # noqa: E402

BATCH = 500


def main(argv: list[str]) -> int:
    source = Path(argv[1])
    db_path = Path(argv[2])
    scan_id = argv[4] if len(argv) > 4 and argv[3] == "--scan-id" else "probe"
    # The library's zone, the one declared assumption (see events.py). The
    # scan would take it from the machine it runs on; this loader runs
    # somewhere else, so it says so out loud.
    policy = MomentPolicy(utc_offset_seconds=float(
        __import__("os").environ.get("PROBE_UTC_OFFSET_HOURS", "7")) * 3600)

    rows = [json.loads(line) for line in source.open(encoding="utf-8") if line.strip()]
    with ScanIndex(db_path) as index:
        batch: list[FileRecord] = []
        for row in rows:
            batch.append(
                FileRecord(
                    display_path=row["display_path"],
                    real_path=row["display_path"],
                    size=row["size"],
                    mtime=row["mtime"],
                    media_kind=MediaKind.PHOTO if row["kind"] == "image" else MediaKind.VIDEO,
                )
            )
            if len(batch) >= BATCH:
                index.upsert_files(batch, scan_id)
                index.commit()
                batch.clear()
        if batch:
            index.upsert_files(batch, scan_id)
        index.commit()

        for row in rows:
            if row.get("origin"):
                index.set_origin(
                    row["display_path"], row["origin"], row["origin_confidence"], ()
                )
            moment = moment_for_row(row, policy)
            index.set_moment(
                row["display_path"],
                moment.taken_at,
                moment.time_source.value,
                moment.latitude,
                moment.longitude,
                moment.geo_source.value,
            )
        index.commit()
        print(f"загружено {len(rows)} файлов в {db_path}, scan_id={scan_id}")
        print("покрытие:", index.moment_coverage(scan_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
