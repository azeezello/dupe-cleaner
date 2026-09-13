"""Streaming hash helpers used by the dedupe engine.

Two-stage strategy (see dedupe.py for how these compose):
1. `quick_hash` — cheap fingerprint of a file's head + tail + size. Two
   files can only be truly identical if this also matches, so it prunes
   same-size-but-different files before we pay for a full read.
2. `full_hash` — streaming hash of the entire content, used only within
   groups that already share size + quick_hash, to confirm true equality.

`xxhash` is used (not sha256) because we only need collision-resistance
against *accidental* duplicates on one person's own disks, not
cryptographic guarantees — xxh3 is several times faster, which matters
when hashing large video files.
"""

from __future__ import annotations

from typing import BinaryIO, Callable

import xxhash

from .config import HASH_CHUNK_SIZE, QUICK_HASH_SAMPLE_BYTES


def quick_hash(stream: BinaryIO, size: int) -> str:
    """Hash the first and last QUICK_HASH_SAMPLE_BYTES of `stream`, plus size.

    `stream` must be seekable. Leaves the stream position undefined on
    return (callers should seek(0) again before a subsequent full read).
    """
    h = xxhash.xxh3_128()
    h.update(size.to_bytes(8, "little"))

    stream.seek(0)
    h.update(stream.read(QUICK_HASH_SAMPLE_BYTES))

    if size > QUICK_HASH_SAMPLE_BYTES:
        tail_start = max(0, size - QUICK_HASH_SAMPLE_BYTES)
        stream.seek(tail_start)
        h.update(stream.read(QUICK_HASH_SAMPLE_BYTES))

    return h.hexdigest()


def full_hash(stream: BinaryIO, chunk_size: int = HASH_CHUNK_SIZE) -> str:
    """Streaming hash of the full content. Does not require a seekable
    stream (works for archive member streams that only support sequential
    reads).
    """
    h = xxhash.xxh3_128()
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def full_hash_from_opener(open_stream: Callable[[], BinaryIO]) -> str:
    """Convenience wrapper for callers that hand us a factory rather than an
    already-open stream (archive backends need a fresh handle per read).
    """
    with open_stream() as stream:
        return full_hash(stream)
