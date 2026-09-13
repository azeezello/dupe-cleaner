"""Central configuration: file-extension classification and tunables.

Kept as plain constants (no external config format) for the MVP so the
whole decision surface is readable in one place. Move to a proper settings
system (pydantic-settings) once phase 2 introduces per-user options like
face-recognition sensitivity.
"""

from __future__ import annotations

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ".heic", ".heif", ".raw", ".cr2", ".nef", ".arw", ".dng",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v",
    ".3gp", ".mts", ".m2ts",
}

MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

ARCHIVE_EXTENSIONS = {
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

# Files/dirs that are never worth scanning and commonly huge or noisy.
DEFAULT_EXCLUDE_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", "$RECYCLE.BIN", "System Volume Information",
}

# Bytes read from the start and end of a file for the cheap "quick hash"
# pre-filter, before committing to a full read. Two files can only be
# byte-identical if these also match, so this cheaply prunes the vast
# majority of same-size-but-different files (e.g. two videos of similar
# length) before we pay for a full read.
QUICK_HASH_SAMPLE_BYTES = 64 * 1024

# Streaming chunk size for full hashing.
HASH_CHUNK_SIZE = 1024 * 1024

# Below this size, skip the quick-hash step entirely and go straight to a
# full hash — the "cheap" sample would end up reading the whole file anyway.
QUICK_HASH_MIN_FILE_SIZE = QUICK_HASH_SAMPLE_BYTES * 3
