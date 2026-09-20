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


@pytest.fixture
def archive_tree(tmp_path: Path) -> Path:
    """A tree holding one archive of every Р1 class, plus the traps.

    Built so each archive's class is decided by construction, not by luck:

    - `fully.zip`      — both members also lie loose on disk  -> fully redundant
    - `photos.zip`     — same, but the members are photos     -> fully redundant,
                          and must not move without --confirm-media
    - `partial.zip`    — one member loose, one only in here   -> partially redundant
    - `unique.zip`     — nothing in it exists outside         -> unique
    - `locked.7z`      — encrypted: it *lists* fine (a 7z directory is
                          readable without the password) but its bytes are
                          not. Its member is sized to collide with a loose
                          file so the funnel actually tries to read it —
                          otherwise nothing would ever discover it can't be
                          read. This is the class the pilot never saw.
    - `broken.zip`     — not a zip at all                     -> unread
    - `mirror_a/b.zip` — byte-identical to each other and to nothing on
                          disk. Each is the other's only "twin", so a rule
                          that counted archive members as twins would call
                          both fully redundant and quarantine both.
    """
    import py7zr

    root = tmp_path / "archives"
    (root / "loose").mkdir(parents=True)

    red1 = b"redundant one " * 40
    red2 = b"redundant two " * 40
    only_inside = b"this text exists nowhere else " * 20
    photo = b"\xff\xd8\xff" + b"fake jpeg payload " * 30

    (root / "loose" / "red1.txt").write_bytes(red1)
    (root / "loose" / "red2.txt").write_bytes(red2)
    (root / "loose" / "shot.jpg").write_bytes(photo)

    with zipfile.ZipFile(root / "fully.zip", "w") as zf:
        zf.writestr("a/red1.txt", red1)
        zf.writestr("a/red2.txt", red2)

    with zipfile.ZipFile(root / "photos.zip", "w") as zf:
        zf.writestr("dcim/shot.jpg", photo)

    with zipfile.ZipFile(root / "partial.zip", "w") as zf:
        zf.writestr("mixed/red1.txt", red1)
        zf.writestr("mixed/private.txt", only_inside)

    with zipfile.ZipFile(root / "unique.zip", "w") as zf:
        zf.writestr("solo/one.txt", b"nothing else looks like this one " * 10)
        zf.writestr("solo/two.txt", b"nor like this second one " * 12)

    # py7zr's writestr takes (data, arcname) — the reverse of zipfile's.
    with py7zr.SevenZipFile(root / "locked.7z", "w", password="not-the-password") as zf:
        zf.writestr(red1.decode(), "sealed/red1.txt")

    (root / "broken.zip").write_bytes(b"PK\x03\x04 this is not really a zip file")

    mirrored = b"two archives, one content " * 30
    for name in ("mirror_a.zip", "mirror_b.zip"):
        with zipfile.ZipFile(root / name, "w") as zf:
            zf.writestr("copy.txt", mirrored)

    return root
