from __future__ import annotations

from pathlib import Path

from dupecleaner.dedupe import find_duplicate_groups
from dupecleaner.models import MediaKind
from dupecleaner.scanner import Scanner


def test_finds_plain_and_mixed_and_archive_only_groups(tmp_tree: Path):
    scanner = Scanner(include_archives=True)
    records = list(scanner.iter_records([str(tmp_tree)]))
    warnings: list[str] = []
    groups = find_duplicate_groups(records, warnings=warnings)

    assert warnings == []

    # Group 1: a1.txt, sub/a2.txt, and backup.zip::inner/a_copy.txt — a
    # "mixed" group spanning plain files and an archive member.
    mixed = next(g for g in groups if any("a1.txt" in r.display_path for r in g.records))
    assert len(mixed.records) == 3
    assert mixed.has_archive_members
    assert not mixed.only_archive_members
    assert mixed.wasted_bytes == mixed.size * 2

    # Group 2: archive_only.zip::one.bin / two.bin — both archive members,
    # no plain-file counterpart anywhere.
    archive_only = next(g for g in groups if any("one.bin" in r.display_path for r in g.records))
    assert archive_only.only_archive_members
    assert len(archive_only.records) == 2

    # Group 3: photo1.jpg / photo2.jpg — identical media files.
    media_group = next(g for g in groups if g.is_media)
    assert len(media_group.records) == 2
    assert all(r.media_kind is MediaKind.PHOTO for r in media_group.records)

    # unique.txt and inner/note.txt must not appear in any group.
    all_display_paths = {r.display_path for g in groups for r in g.records}
    assert not any("unique.txt" in p for p in all_display_paths)
    assert not any("note.txt" in p for p in all_display_paths)


def test_no_false_positive_for_same_size_different_content(tmp_path: Path):
    from dupecleaner.models import FileRecord

    f1 = tmp_path / "one.bin"
    f2 = tmp_path / "two.bin"
    f1.write_bytes(b"AAAA")
    f2.write_bytes(b"BBBB")

    records = [
        FileRecord(display_path=str(f1), real_path=str(f1), size=4, mtime=0),
        FileRecord(display_path=str(f2), real_path=str(f2), size=4, mtime=0),
    ]
    groups = find_duplicate_groups(records)
    assert groups == []


def test_empty_files_are_ignored(tmp_path: Path):
    from dupecleaner.models import FileRecord

    f1 = tmp_path / "one.bin"
    f2 = tmp_path / "two.bin"
    f1.write_bytes(b"")
    f2.write_bytes(b"")

    records = [
        FileRecord(display_path=str(f1), real_path=str(f1), size=0, mtime=0),
        FileRecord(display_path=str(f2), real_path=str(f2), size=0, mtime=0),
    ]
    groups = find_duplicate_groups(records)
    assert groups == []


def test_hash_archive_members_reads_archive_once_regardless_of_candidate_count(tmp_path: Path):
    """Finding A2, end to end: hashing several members of the same tar
    archive must cost one sequential pass, not one archive-open per member.
    """
    import tarfile
    import io

    from dupecleaner import archives
    from dupecleaner.dedupe import group_by_archive, hash_archive_members
    from dupecleaner.models import FileRecord
    from dupecleaner.storage import ScanIndex

    archive = tmp_path / "photos.tar"
    pair_a = b"duplicate pair A " * 10
    pair_b = b"duplicate pair B, different length " * 7
    unique = b"only one of this size"
    with tarfile.open(archive, "w") as tf:
        for name, content in [
            ("a1.jpg", pair_a),
            ("a2.jpg", pair_a),
            ("b1.jpg", pair_b),
            ("b2.jpg", pair_b),
            ("solo.jpg", unique),
        ]:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))

    records = [
        FileRecord(
            display_path=f"{archive}::{name}",
            real_path=str(archive),
            size=len(content),
            mtime=0,
            is_archive_member=True,
            archive_path=str(archive),
            member_name=name,
        )
        for name, content in [
            ("a1.jpg", pair_a),
            ("a2.jpg", pair_a),
            ("b1.jpg", pair_b),
            ("b2.jpg", pair_b),
        ]
    ]

    real_open = tarfile.open
    open_calls = []

    def _counting_open(*args, **kwargs):
        open_calls.append(1)
        return real_open(*args, **kwargs)

    import unittest.mock

    with unittest.mock.patch.object(tarfile, "open", side_effect=_counting_open):
        with ScanIndex(":memory:") as index:
            index.upsert_files(records, "s1")
            index.commit()

            plain, by_archive = group_by_archive(records)
            assert plain == []
            assert set(by_archive) == {str(archive)}

            errors = hash_archive_members(index, str(archive), by_archive[str(archive)])
            assert errors == []
            index.commit()

            groups = index.duplicate_groups("s1")

    assert len(open_calls) == 1  # one sequential pass for all four members
    assert len(groups) == 2
    sizes = sorted(len(g.records) for g in groups)
    assert sizes == [2, 2]


def test_hash_archive_members_reports_missing_member_without_losing_others(tmp_path: Path):
    import tarfile
    import io

    from dupecleaner.dedupe import hash_archive_members
    from dupecleaner.models import FileRecord
    from dupecleaner.storage import ScanIndex

    archive = tmp_path / "single.tar"
    content = b"the only real member"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo(name="real.txt")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))

    real_record = FileRecord(
        display_path=f"{archive}::real.txt",
        real_path=str(archive),
        size=len(content),
        mtime=0,
        is_archive_member=True,
        archive_path=str(archive),
        member_name="real.txt",
    )
    ghost_record = FileRecord(
        display_path=f"{archive}::ghost.txt",
        real_path=str(archive),
        size=5,
        mtime=0,
        is_archive_member=True,
        archive_path=str(archive),
        member_name="ghost.txt",
    )

    with ScanIndex(":memory:") as index:
        index.upsert_files([real_record, ghost_record], "s1")
        index.commit()

        errors = hash_archive_members(index, str(archive), [real_record, ghost_record])

    assert len(errors) == 1
    failed_record, exc = errors[0]
    assert failed_record.member_name == "ghost.txt"
    assert isinstance(exc, FileNotFoundError)


def test_find_duplicate_groups_works_for_tar_archives_too(tmp_path: Path):
    """The existing tmp_tree fixture only exercises .zip; tar/gzip is where
    finding A2 actually lived, so cover it through the public one-shot API
    as well.
    """
    import tarfile
    import io

    from dupecleaner.models import FileRecord

    content = b"tar duplicate content " * 5
    archive = tmp_path / "backup.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for name in ("one.dat", "two.dat"):
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))

    records = [
        FileRecord(
            display_path=f"{archive}::{name}",
            real_path=str(archive),
            size=len(content),
            mtime=0,
            is_archive_member=True,
            archive_path=str(archive),
            member_name=name,
        )
        for name in ("one.dat", "two.dat")
    ]

    warnings: list[str] = []
    groups = find_duplicate_groups(records, warnings=warnings)

    assert warnings == []
    assert len(groups) == 1
    assert len(groups[0].records) == 2
    assert groups[0].only_archive_members


# --- thumbnail generation hook (task 8) --------------------------------------


def test_run_full_stage_caches_a_thumbnail_for_a_plain_photo(tmp_path: Path):
    from PIL import Image

    from dupecleaner import thumbnails
    from dupecleaner.dedupe import run_full_stage
    from dupecleaner.models import FileRecord, MediaKind
    from dupecleaner.storage import ScanIndex

    photo_path = tmp_path / "real.jpg"
    Image.new("RGB", (400, 300), (10, 120, 200)).save(photo_path, format="JPEG")

    record = FileRecord(
        display_path=str(photo_path),
        real_path=str(photo_path),
        size=photo_path.stat().st_size,
        mtime=photo_path.stat().st_mtime,
        media_kind=MediaKind.PHOTO,
    )

    db_path = tmp_path / "index.db"
    with ScanIndex(db_path) as index:
        index.upsert_files([record], "scan1")
        index.commit()

        run_full_stage(index, record)

        row = index._conn.execute(
            "SELECT full_hash FROM files WHERE display_path = ?", (record.display_path,)
        ).fetchone()
        content_hash = row["full_hash"]
        assert content_hash is not None

        meta = index.get_thumbnail_meta(content_hash)
        assert meta is not None
        cached_path = thumbnails.get_cached_path(index, content_hash)
        assert cached_path is not None and cached_path.is_file()


def test_hash_archive_members_never_generates_thumbnails(tmp_tree: Path, tmp_path: Path):
    """Archive members are excluded on purpose (see thumbnails.py and
    dedupe.hash_archive_members docstrings) — the review UI has never
    requested a preview for one, and decoding image bytes out of an
    already-consumed archive stream would reintroduce the per-member
    re-read pilot finding A2 exists to prevent.
    """
    import zipfile

    from PIL import Image

    from dupecleaner import thumbnails
    from dupecleaner.dedupe import group_by_archive, hash_archive_members
    from dupecleaner.models import FileRecord, MediaKind
    from dupecleaner.storage import ScanIndex

    photo_bytes_buf = __import__("io").BytesIO()
    Image.new("RGB", (100, 100), (1, 2, 3)).save(photo_bytes_buf, format="JPEG")
    photo_bytes = photo_bytes_buf.getvalue()

    archive_path = tmp_path / "photos.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("a.jpg", photo_bytes)

    record = FileRecord(
        display_path=f"{archive_path}::a.jpg",
        real_path=str(archive_path),
        size=len(photo_bytes),
        mtime=1000.0,
        media_kind=MediaKind.PHOTO,
        is_archive_member=True,
        archive_path=str(archive_path),
        member_name="a.jpg",
        source_size=archive_path.stat().st_size,
        source_mtime=archive_path.stat().st_mtime,
    )

    db_path = tmp_path / "index.db"
    with ScanIndex(db_path) as index:
        index.upsert_files([record], "scan1")
        index.commit()

        errors = hash_archive_members(index, str(archive_path), [record])
        assert errors == []

        row = index._conn.execute(
            "SELECT full_hash FROM files WHERE display_path = ?", (record.display_path,)
        ).fetchone()
        content_hash = row["full_hash"]
        assert content_hash is not None

        # The archive member hashed cleanly, but no thumbnail exists for it.
        assert index.get_thumbnail_meta(content_hash) is None

        cache_dir = thumbnails.cache_dir_for(db_path)
        assert not cache_dir.exists() or not any(cache_dir.rglob("*.jpg"))
