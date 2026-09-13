from __future__ import annotations

import io

from dupecleaner.hashing import full_hash, quick_hash


def test_full_hash_matches_for_identical_content():
    a = io.BytesIO(b"same content here")
    b = io.BytesIO(b"same content here")
    assert full_hash(a) == full_hash(b)


def test_full_hash_differs_for_different_content():
    a = io.BytesIO(b"content one")
    b = io.BytesIO(b"content two")
    assert full_hash(a) != full_hash(b)


def test_quick_hash_matches_for_identical_content():
    data = b"x" * 200_000
    a = io.BytesIO(data)
    b = io.BytesIO(data)
    assert quick_hash(a, len(data)) == quick_hash(b, len(data))


def test_quick_hash_differs_when_middle_differs_but_head_tail_same():
    # quick_hash intentionally only samples head+tail, so this documents
    # that it is a *prefilter*, not a full equality check — dedupe.py must
    # always follow up with full_hash before declaring files identical.
    size = 200_000
    data_a = bytearray(b"x" * size)
    data_b = bytearray(b"x" * size)
    data_b[size // 2] = ord("Y")
    a = io.BytesIO(bytes(data_a))
    b = io.BytesIO(bytes(data_b))
    assert quick_hash(a, size) == quick_hash(b, size)
    assert full_hash(io.BytesIO(bytes(data_a))) != full_hash(io.BytesIO(bytes(data_b)))
