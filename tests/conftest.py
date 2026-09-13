from __future__ import annotations

import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_tree(tmp_path: Path) -> Path:
    """A small directory tree with:
    - two byte-identical regular files (dupe A)
    - one unique regular file
    - a zip archive containing a member identical to dupe A (mixed dedupe)
    - a zip archive containing two internal duplicates of each other, with
      no plain-file counterpart (archive-only group)
    - two "media" files with identical bytes (to exercise the media path)
    """
    root = tmp_path / "data"
    root.mkdir()

    content_a = b"hello duplicate world " * 100
    (root / "a1.txt").write_bytes(content_a)
    (root / "sub").mkdir()
    (root / "sub" / "a2.txt").write_bytes(content_a)

    (root / "unique.txt").write_bytes(b"nothing else looks like this")

    with zipfile.ZipFile(root / "backup.zip", "w") as zf:
        zf.writestr("inner/a_copy.txt", content_a)
        zf.writestr("inner/note.txt", b"a note that is unique")

    archive_only_content = b"only lives inside archives " * 50
    with zipfile.ZipFile(root / "archive_only.zip", "w") as zf:
        zf.writestr("one.bin", archive_only_content)
        zf.writestr("two.bin", archive_only_content)

    photo_bytes = b"\xff\xd8\xff" + b"fake jpeg bytes" * 20  # not a real jpeg, fine for hashing tests
    (root / "photo1.jpg").write_bytes(photo_bytes)
    (root / "photo2.jpg").write_bytes(photo_bytes)

    return root
