"""Shared by the three task-16 pilot scripts: turn one probed row back into
the library's own objects.

The JSONL the probe writes holds **raw evidence** — what EXIF said, what
the sidecar said, the filename, the mtime — and the derived capture moment
only as a convenience column. Everything downstream re-derives that moment
through `dupecleaner.events`, which is what makes a change to a rule cost
four seconds instead of twelve minutes of re-reading 30 000 headers over
the bridge. (It bought exactly that once already: teaching the filename
parser about Android's millisecond suffix re-dated 1 500 photos without
touching the disk.)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dupecleaner import events as ev  # noqa: E402
from dupecleaner.origin import GoogleSidecar, OriginSignals  # noqa: E402


def signals_from_row(row: dict) -> OriginSignals:
    """Rebuild the header read, without re-reading the header."""
    sidecar = None
    if row.get("sidecar"):
        sidecar = GoogleSidecar(
            origin_key=None,
            has_geo=row.get("sidecar_lat") is not None,
            taken_at=row.get("sidecar_taken_at"),
            latitude=row.get("sidecar_lat"),
            longitude=row.get("sidecar_lon"),
        )
    return OriginSignals(
        path=row["display_path"],
        width=row.get("width"),
        height=row.get("height"),
        image_format=row.get("format"),
        exif_make=row.get("exif_make"),
        exif_model=row.get("exif_model"),
        exif_software=row.get("exif_software"),
        has_exif_datetime=row.get("exif_taken_at") is not None,
        has_gps=row.get("gps_lat") is not None,
        exif_taken_at=row.get("exif_taken_at"),
        exif_offset_minutes=row.get("exif_offset_minutes"),
        gps_latitude=row.get("gps_lat"),
        gps_longitude=row.get("gps_lon"),
        sidecar=sidecar,
    )


def moment_for_row(row: dict, policy: ev.MomentPolicy) -> ev.PhotoMoment:
    """The library's own answer for this file — photo or video."""
    if row.get("kind") == "video":
        return ev.moment_without_header(
            row["display_path"], mtime=row.get("mtime"), policy=policy
        )
    return ev.moment_from_signals(
        signals_from_row(row), mtime=row.get("mtime"), policy=policy
    )
