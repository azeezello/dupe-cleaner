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


def _make_tar(tmp_path: Path, members: dict[str, bytes]) -> Path:
    archive = tmp_path / "test_multi.tar"
    with tarfile.open(archive, "w") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io_bytes(content))
    return archive


def io_bytes(content: bytes):
    import io

    return io.BytesIO(content)


def test_tar_open_member_does_not_call_getmembers(tmp_path: Path, monkeypatch):
    """Regression test for finding A2: `open_member` for tar must not force
    a full-archive parse. `getmembers()` is exactly the call that made the
    original bug quadratic (extractfile(name) -> getmember() ->
    getmembers()), so if it's ever invoked again, this test fails loudly
    rather than the regression only showing up as "the real scan takes
    hours" much later.
    """
    archive = _make_tar(
        tmp_path,
        {"a.txt": b"first member", "b.txt": b"second member", "c.txt": b"third member"},
    )

    def _boom(self, *a, **k):
        raise AssertionError("open_member must not call TarFile.getmembers()")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", _boom)

    with archives.open_member(archive, "tar", "b.txt") as stream:
        assert stream.read() == b"second member"


def test_tar_open_members_sequential_reads_archive_exactly_once(tmp_path: Path, monkeypatch):
    """The whole point of finding A2's fix: hashing N members of one tar
    archive must open the archive once, not once per member.
    """
    members = {f"m{i}.bin": f"content of member {i}".encode() for i in range(5)}
    archive = _make_tar(tmp_path, members)

    real_open = tarfile.open
    open_calls = []

    def _counting_open(*args, **kwargs):
        open_calls.append((args, kwargs))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(tarfile, "open", _counting_open)

    wanted = {"m0.bin", "m2.bin", "m4.bin"}
    found = {}
    for name, stream in archives.open_members_sequential(archive, "tar", wanted):
        with stream:
            found[name] = stream.read()

    assert len(open_calls) == 1
    assert found == {name: members[name] for name in wanted}


def test_tar_open_members_sequential_skips_unrequested_members(tmp_path: Path):
    # Each stream must be read within its own loop iteration (documented
    # contract of open_members_sequential): reading it later, after the
    # generator has moved on, would touch an already-closed tar handle.
    members = {"keep.txt": b"keep me", "skip.txt": b"skip me"}
    archive = _make_tar(tmp_path, members)

    found = {}
    for name, stream in archives.open_members_sequential(archive, "tar", {"keep.txt"}):
        with stream:
            found[name] = stream.read()

    assert found == {"keep.txt": b"keep me"}


def test_tar_open_members_sequential_missing_member_is_simply_absent(tmp_path: Path):
    archive = _make_tar(tmp_path, {"only.txt": b"here"})

    results = list(archives.open_members_sequential(archive, "tar", {"only.txt", "ghost.txt"}))
    names = [name for name, _ in results]
    assert names == ["only.txt"]
    for _, stream in results:
        stream.close()


def test_zip_open_members_sequential(tmp_path: Path):
    archive = tmp_path / "multi.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("one.txt", b"one")
        zf.writestr("two.txt", b"two")

    found = {}
    for name, stream in archives.open_members_sequential(archive, "zip", {"one.txt", "two.txt"}):
        with stream:
            found[name] = stream.read()
    assert found == {"one.txt": b"one", "two.txt": b"two"}
