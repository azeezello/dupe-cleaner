from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import py7zr

from dupecleaner import archives


def test_archive_kind_for_recognizes_common_suffixes(tmp_path: Path):
    assert archives.archive_kind_for(Path("a.zip")) == "zip"
    assert archives.archive_kind_for(Path("a.tar.gz")) == "tar"
    assert archives.archive_kind_for(Path("a.tgz")) == "tar"
    assert archives.archive_kind_for(Path("a.7z")) == "7z"
    assert archives.archive_kind_for(Path("a.rar")) == "rar"
    assert archives.archive_kind_for(Path("a.txt")) is None


def test_zip_list_and_open_member(tmp_path: Path):
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("hello.txt", b"hello zip world")

    members = list(archives.list_members(archive, "zip"))
    assert len(members) == 1
    assert members[0].name == "hello.txt"
    assert members[0].size == len(b"hello zip world")

    with archives.open_member(archive, "zip", "hello.txt") as stream:
        assert stream.read() == b"hello zip world"


def test_tar_list_and_open_member(tmp_path: Path):
    inner = tmp_path / "hello.txt"
    inner.write_bytes(b"hello tar world")
    archive = tmp_path / "test.tar"
    with tarfile.open(archive, "w") as tf:
        tf.add(inner, arcname="hello.txt")

    members = list(archives.list_members(archive, "tar"))
    assert len(members) == 1
    assert members[0].name == "hello.txt"

    with archives.open_member(archive, "tar", "hello.txt") as stream:
        assert stream.read() == b"hello tar world"


def test_7z_list_and_open_member(tmp_path: Path):
    inner = tmp_path / "hello.txt"
    inner.write_bytes(b"hello 7z world")
    archive = tmp_path / "test.7z"
    with py7zr.SevenZipFile(archive, "w") as zf:
        zf.write(inner, arcname="hello.txt")

    members = list(archives.list_members(archive, "7z"))
    assert len(members) == 1
    assert members[0].name == "hello.txt"

    with archives.open_member(archive, "7z", "hello.txt") as stream:
        assert stream.read() == b"hello 7z world"
