"""Read-only probe: one header read per photo in a real library, dumped to JSONL.

Same shape as `pilot/probe_archive.py` — measure first, decide after. This
one exists because every threshold in `dupecleaner.events` is supposed to
come out of the data rather than out of taste, and the data is 30 000
files behind a slow mount: reading them once into a file makes the
threshold search free and repeatable, where re-walking the disk per
candidate value would make it unaffordable.

Writes nothing anywhere near the library. Opens image headers only —
`origin.read_signals` never decodes pixels — and never moves, renames or
deletes anything.

    python pilot/probe_events.py <root> <out.jsonl> [--display-prefix D:/Photos]

Resumable, and not as a nicety: run from a cloud session the library is
reached through a bridge with a hard time limit per command, and 30 000
header reads do not fit inside one. The output file is the cursor — a
`display_path` already in it is not read again — so the same command can
simply be issued until it reports that nothing is left. `PROBE_SECONDS`
caps one pass.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dupecleaner.config import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS  # noqa: E402
from dupecleaner import events as events_module  # noqa: E402
from dupecleaner import origin as origin_module  # noqa: E402

from probe_common import moment_for_row  # noqa: E402


def main(argv: list[str]) -> int:
    root = Path(argv[1])
    out_path = Path(argv[2])
    prefix = argv[4] if len(argv) > 4 and argv[3] == "--display-prefix" else str(root)
    # The library's own zone. Wall-clock normalisation needs one declared
    # offset (see events.py); the machine running this probe is in UTC,
    # Aziz's photos are not.
    offset = float(os.environ.get("PROBE_UTC_OFFSET_HOURS", "7")) * 3600
    policy = events_module.MomentPolicy(utc_offset_seconds=offset)

    started = time.time()
    budget = float(os.environ.get("PROBE_SECONDS", "150"))
    done: set[str] = set()
    if out_path.exists():
        with out_path.open(encoding="utf-8") as existing:
            for line in existing:
                try:
                    done.add(json.loads(line)["display_path"])
                except (ValueError, KeyError):
                    continue
        print(f"возобновление: уже прочитано {len(done)}", file=sys.stderr, flush=True)

    written = skipped = 0
    out_of_time = False
    with out_path.open("a", encoding="utf-8") as out:
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames.sort()
            if out_of_time:
                break
            for name in sorted(filenames):
                if time.time() - started > budget:
                    out_of_time = True
                    break
                suffix = Path(name).suffix.lower()
                is_image = suffix in IMAGE_EXTENSIONS
                is_video = suffix in VIDEO_EXTENSIONS
                if not (is_image or is_video):
                    skipped += 1
                    continue
                real = Path(dirpath) / name
                try:
                    stat = real.stat()
                except OSError:
                    skipped += 1
                    continue
                shown = prefix.rstrip("/") + "/" + str(real.relative_to(root)).replace("\\", "/")
                if shown in done:
                    continue

                row = {
                    "display_path": shown,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "kind": "image" if is_image else "video",
                }
                if is_image:
                    signals = origin_module.read_signals(real, display_path=shown)
                    verdict = origin_module.classify(signals)
                    row.update(
                        width=signals.width,
                        height=signals.height,
                        format=signals.image_format,
                        exif_make=signals.exif_make,
                        exif_model=signals.exif_model,
                        exif_software=signals.exif_software,
                        exif_taken_at=signals.exif_taken_at,
                        exif_offset_minutes=signals.exif_offset_minutes,
                        gps_lat=signals.gps_latitude,
                        gps_lon=signals.gps_longitude,
                        sidecar=bool(signals.sidecar),
                        sidecar_taken_at=signals.sidecar.taken_at if signals.sidecar else None,
                        sidecar_lat=signals.sidecar.latitude if signals.sidecar else None,
                        sidecar_lon=signals.sidecar.longitude if signals.sidecar else None,
                        origin=verdict.origin.value,
                        origin_confidence=verdict.confidence.value,
                        excluded_from_albums=verdict.excluded_from_albums,
                    )
                # Derived, and derived again by every consumer: see
                # probe_common. The columns below are a convenience, the raw
                # ones above are the evidence.
                moment = moment_for_row(row, policy)
                row.update(
                    taken_at=moment.taken_at,
                    time_source=moment.time_source.value,
                    moment_lat=moment.latitude,
                    moment_lon=moment.longitude,
                    geo_source=moment.geo_source.value,
                )
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
                if written % 2000 == 0:
                    out.flush()
                    print(
                        f"{written} файлов, {time.time() - started:.0f} с",
                        file=sys.stderr,
                        flush=True,
                    )

    elapsed = time.time() - started
    print(
        f"{'прервано по времени' if out_of_time else 'готово'}: "
        f"+{written} медиафайлов за {elapsed:.0f} с "
        f"({1000 * elapsed / max(1, written):.1f} мс/файл), не медиа {skipped}, "
        f"всего в файле {len(done) + written}",
        file=sys.stderr,
    )
    return 2 if out_of_time else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
