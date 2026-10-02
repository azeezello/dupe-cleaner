"""Task 18: the offline face engine, and the index that holds its output.

Split deliberately into two halves. Everything about *storage* — the v6
migration, the tables, `needs_faces`, and what the scan job does when the
engine is not there — runs everywhere, with no OpenCV and no 37 MB of
ONNX weights, because that is the half a stranger cloning this repository
can break. The half that needs the real engine is skipped when it is
absent and says so, rather than quietly passing on an empty code path.

The tests that matter most here are not the round trips. They are:

- `test_face_phase_invents_no_duplicate_groups` — the face phase writes
  `files.full_hash` for photographs the funnel never hashed, and that is
  only safe because two files of different sizes cannot be identical.
  This is the test that keeps it safe.
- `test_zero_faces_is_recorded_not_forgotten` — "we looked and found
  nobody" has to be a stored fact. Without it every rescan re-decodes
  every landscape in the library, and pilot finding A1 comes back one
  layer down.
- `test_migration_runs_even_when_a_later_version_is_already_stamped` —
  two sessions on two branches each added a migration; the index has to
  end up with both tables whichever order they arrive in.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
from pathlib import Path

import pytest
from PIL import Image

from dupecleaner import faces
from dupecleaner.jobs import ScanJob
from dupecleaner.storage import SCHEMA_VERSION
from dupecleaner.models import ScanMode
from dupecleaner.storage import ScanIndex


# --- helpers -----------------------------------------------------------


def _engine_available() -> bool:
    try:
        faces.FaceEngine()
    except Exception:
        return False
    return True


requires_engine = pytest.mark.skipif(
    not _engine_available(),
    reason="нет opencv-python-headless и/или моделей "
    "(pip install -e '.[faces]'; dupecleaner faces --install-models)",
)


def _vector(seed: float = 0.0) -> list[float]:
    """A unit vector of the right length, deterministic and not all-equal
    (an all-equal vector would hide an ordering bug in the packer)."""
    raw = [(i * 7 + seed) % 13 - 6 for i in range(faces.EMBEDDING_DIM)]
    norm = sum(v * v for v in raw) ** 0.5
    return [v / norm for v in raw]


def _face_row(index: int = 0, seed: float = 0.0):
    return (index, 10 + index, 20, 64, 64, 0.91, faces.encode_embedding(_vector(seed)))


def _photo(path: Path, colour: tuple[int, int, int], size=(600, 400)) -> None:
    """A real, decodable JPEG with no face in it."""
    Image.new("RGB", size, colour).save(path, "JPEG", quality=90)


# --- embeddings on the wire --------------------------------------------


def test_embedding_round_trip_is_exact_in_float32():
    values = _vector(3.0)
    blob = faces.encode_embedding(values)
    assert len(blob) == faces.EMBEDDING_BYTES == 512
    back = faces.decode_embedding(blob)
    # float32, so equality is to float32 precision — but order must be
    # preserved exactly, which is the thing a packing bug would break.
    assert back == tuple(struct.unpack(f"<{faces.EMBEDDING_DIM}f",
                                       struct.pack(f"<{faces.EMBEDDING_DIM}f", *values)))
    assert back[0] != back[1]


def test_embedding_rejects_the_wrong_length_both_ways():
    with pytest.raises(ValueError):
        faces.encode_embedding([0.1, 0.2])
    with pytest.raises(ValueError):
        faces.decode_embedding(b"\x00" * 511)


# --- model bookkeeping (no real weights needed) -------------------------


def test_missing_models_reports_both_when_the_directory_is_empty(tmp_path):
    missing = faces.missing_models(tmp_path)
    assert {spec.role for spec in missing} == {"detector", "recognizer"}
    assert not faces.models_installed(tmp_path)


def test_install_from_a_local_folder_verifies_the_hash(tmp_path):
    """The offline install path, and the reason it exists: a machine with
    no network still has to be able to get the models in place — and a
    file that is not the model must not be installed just because it has
    the right name."""
    source = tmp_path / "source"
    source.mkdir()
    payload = b"pretend onnx bytes " * 11
    (source / "model.onnx").write_bytes(payload)
    spec = faces.ModelSpec(
        role="detector",
        filename="model.onnx",
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        license="MIT",
        url="https://example.invalid/model.onnx",
    )
    dest = tmp_path / "models"
    notes = faces.install_models(dest, source_dir=source, specs=[spec])
    assert (dest / "model.onnx").read_bytes() == payload
    assert "установлен" in notes[0]


def test_install_refuses_and_leaves_nothing_behind_on_a_hash_mismatch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "model.onnx").write_bytes(b"not the model at all")
    spec = faces.ModelSpec(
        role="detector",
        filename="model.onnx",
        sha256="0" * 64,
        size=len(b"not the model at all"),
        license="MIT",
        url="https://example.invalid/model.onnx",
    )
    dest = tmp_path / "models"
    with pytest.raises(ValueError, match="контрольная сумма"):
        faces.install_models(dest, source_dir=source, specs=[spec])
    assert list(dest.iterdir()) == []


def test_models_dir_honours_the_environment_override(tmp_path, monkeypatch):
    monkeypatch.setenv(faces.MODELS_DIR_ENV, str(tmp_path / "elsewhere"))
    assert faces.models_dir() == tmp_path / "elsewhere"


# --- schema v7 ----------------------------------------------------------


def _legacy_v4_database(path: Path) -> None:
    """An index as task 15 left it: schema_version 4, no faces tables and
    no `applied_migrations` key at all."""
    with ScanIndex(path) as index:  # build the current schema, then rewind
        pass
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS content_faces")
    conn.execute("DROP TABLE IF EXISTS content_face_scans")
    conn.execute("DELETE FROM meta WHERE key = 'applied_migrations'")
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '4')"
    )
    conn.commit()
    conn.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    names = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    return names


def test_v6_migration_adds_the_face_tables_to_an_old_index(tmp_path):
    db = tmp_path / "index.db"
    _legacy_v4_database(db)
    assert "content_faces" not in _tables(db)

    with ScanIndex(db) as index:
        index.set_faces(
            "abc", [_face_row()], engine="test", detect_long_side=1024
        )
    assert {"content_faces", "content_face_scans"} <= _tables(db)


def test_reopening_is_idempotent(tmp_path):
    db = tmp_path / "index.db"
    _legacy_v4_database(db)
    with ScanIndex(db) as index:
        index.set_faces("abc", [_face_row()], engine="t", detect_long_side=1024)
    for _ in range(3):
        with ScanIndex(db) as index:
            assert len(index.get_faces("abc")) == 1


def test_migration_runs_even_when_a_later_version_is_already_stamped(tmp_path):
    """The parallel-branch case this migration was numbered around.

    Task 12 took schema version 5 on `main` while this branch was being
    written, so task 18 took 6. An index that has been through the other
    branch is stamped 5 (or higher) while never having seen migration 6 —
    under the old high-water-mark rule its face tables would never be
    created, and the failure would surface as a bare "no such table" days
    later. The applied-set makes the gap explicit.
    """
    db = tmp_path / "index.db"
    _legacy_v4_database(db)
    conn = sqlite3.connect(db)
    # Deliberately one past the current schema: the point of this test is a
    # stamp from the FUTURE, so the number has to follow SCHEMA_VERSION rather
    # than sit here as a literal that quietly stops being "later" the next time
    # someone adds a migration.
    ahead = str(SCHEMA_VERSION + 1)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (ahead,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value)"
        " VALUES ('applied_migrations', '2,3,4,5')"
    )
    conn.commit()
    conn.close()

    with ScanIndex(db) as index:
        index.set_faces("abc", [_face_row()], engine="t", detect_long_side=1024)
    assert {"content_faces", "content_face_scans"} <= _tables(db)

    conn = sqlite3.connect(db)
    stamped = dict(conn.execute("SELECT key, value FROM meta"))
    conn.close()
    # and the newer stamp is not walked backwards
    assert stamped["schema_version"] == ahead
    assert "7" in stamped["applied_migrations"].split(",")


# --- storing faces ------------------------------------------------------


def test_faces_round_trip_with_their_boxes_and_scores(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.set_faces(
            "hash1",
            [_face_row(0, 1.0), _face_row(1, 2.0)],
            engine=faces.ENGINE_NAME,
            detect_long_side=1024,
            skipped_small=3,
        )
        rows = index.get_faces("hash1")
        assert [r["face_index"] for r in rows] == [0, 1]
        assert rows[0]["width"] == 64 and rows[0]["score"] == pytest.approx(0.91)
        assert faces.decode_embedding(rows[0]["embedding"])[:3] == pytest.approx(
            _vector(1.0)[:3], rel=1e-6
        )
        stats = index.face_stats()
        assert stats == {
            "content_scanned": 1,
            "content_with_faces": 1,
            "faces": 2,
            "faces_too_small": 3,
        }
        assert index.total_embedding_bytes() == 2 * faces.EMBEDDING_BYTES


def test_rerunning_replaces_rather_than_accumulates(tmp_path):
    """A pass over one photo is the unit of truth: re-running at a
    different detection scale must not leave yesterday's faces mixed in
    with today's."""
    with ScanIndex(tmp_path / "i.db") as index:
        index.set_faces(
            "h", [_face_row(0), _face_row(1)], engine="t", detect_long_side=1024
        )
        index.set_faces("h", [_face_row(0)], engine="t", detect_long_side=1600)
        rows = index.get_faces("h")
        assert len(rows) == 1
        assert index.face_stats()["faces"] == 1


def test_zero_faces_is_recorded_not_forgotten(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        index.set_faces("empty", [], engine="t", detect_long_side=1024)
        assert index.get_faces("empty") == []
        assert index.has_face_scan("empty") is True
        assert index.has_face_scan("never-seen") is False
        assert index.face_stats()["content_with_faces"] == 0
        assert index.face_stats()["content_scanned"] == 1


# --- what the scan asks for --------------------------------------------


def test_needs_faces_covers_the_library_not_only_the_duplicates(tmp_path):
    """The one place this phase deliberately differs from the preview
    phase: Р4 makes a person a filter across all events, so a photo with
    no copies still needs its faces."""
    root = tmp_path / "tree"
    root.mkdir()
    _photo(root / "lonely.jpg", (10, 20, 30), size=(640, 480))
    _photo(root / "twin_a.jpg", (40, 50, 60), size=(320, 240))
    (root / "twin_b.jpg").write_bytes((root / "twin_a.jpg").read_bytes())
    (root / "notes.txt").write_bytes(b"not a photo")

    db = tmp_path / "i.db"
    job = ScanJob(roots=[str(root)], db_path=db, mode=ScanMode.FULL)
    with ScanIndex(db) as index:
        job._enumerate(index, __import__(
            "dupecleaner.scanner", fromlist=["Scanner"]
        ).Scanner(include_archives=True))
        wanted = {Path(r.display_path).name for r in index.needs_faces(job.scan_id)}
    assert wanted == {"lonely.jpg", "twin_a.jpg", "twin_b.jpg"}


def test_needs_faces_goes_quiet_once_the_content_has_been_looked_at(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    _photo(root / "one.jpg", (1, 2, 3))
    db = tmp_path / "i.db"
    job = ScanJob(roots=[str(root)], db_path=db, mode=ScanMode.FULL)
    from dupecleaner.scanner import Scanner

    with ScanIndex(db) as index:
        job._enumerate(index, Scanner(include_archives=True))
        [record] = index.needs_faces(job.scan_id)
        index.set_full_hash(record.display_path, "content-hash")
        index.set_faces("content-hash", [], engine="t", detect_long_side=1024)
        assert index.needs_faces(job.scan_id) == []


def test_iter_scan_faces_counts_one_person_once_per_content(tmp_path):
    """Four filed copies of one photograph are one face, not four. A
    clusterer that saw four would invent a person who is merely
    well-archived."""
    root = tmp_path / "tree"
    root.mkdir()
    _photo(root / "a.jpg", (9, 9, 9))
    for name in ("b.jpg", "c.jpg"):
        (root / name).write_bytes((root / "a.jpg").read_bytes())

    db = tmp_path / "i.db"
    job = ScanJob(roots=[str(root)], db_path=db, mode=ScanMode.FULL)
    from dupecleaner.scanner import Scanner

    with ScanIndex(db) as index:
        job._enumerate(index, Scanner(include_archives=True))
        for record in index.needs_faces(job.scan_id):
            index.set_full_hash(record.display_path, "shared")
        index.set_faces("shared", [_face_row()], engine="t", detect_long_side=1024)
        assert len(list(index.iter_scan_faces(job.scan_id))) == 1


# --- the mode switch ----------------------------------------------------


def test_quick_mode_never_detects_faces():
    assert ScanMode.QUICK.detect_faces is False
    assert ScanMode.FULL.detect_faces is True


def test_a_full_scan_without_models_still_finishes_and_says_why(tmp_path, monkeypatch):
    """No models is not an error. Someone who wanted a duplicate report
    should get one, with a line explaining that faces were not looked
    for — never a failed scan and never a silent omission."""
    monkeypatch.setenv(faces.MODELS_DIR_ENV, str(tmp_path / "no-models"))
    root = tmp_path / "tree"
    root.mkdir()
    _photo(root / "x.jpg", (3, 3, 3))
    (root / "y.jpg").write_bytes((root / "x.jpg").read_bytes())

    job = ScanJob(roots=[str(root)], db_path=tmp_path / "i.db", mode=ScanMode.FULL)
    report = job.run()
    assert job.progress.status == "done", job.progress.error
    assert len(report.groups) == 1
    assert any("Лица не распознавались" in w for w in job.progress.warnings)


# --- the engine itself --------------------------------------------------


@requires_engine
def test_engine_finds_nobody_in_a_blank_image():
    engine = faces.FaceEngine()
    result = engine.analyse_image(Image.new("RGB", (800, 600), (120, 130, 140)))
    assert result.faces == []
    assert result.skipped_small == 0
    assert result.engine == faces.ENGINE_NAME


@requires_engine
def test_face_phase_writes_a_row_for_every_photo_it_looked_at(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    _photo(root / "a.jpg", (200, 30, 30))
    _photo(root / "b.jpg", (30, 200, 30), size=(700, 500))

    db = tmp_path / "i.db"
    job = ScanJob(roots=[str(root)], db_path=db, mode=ScanMode.FULL)
    job.run()
    assert job.progress.status == "done", job.progress.error

    with ScanIndex(db) as index:
        stats = index.face_stats()
        assert stats["content_scanned"] == 2
        # and the phase filled in the content hashes nothing else needed
        assert index.needs_faces(job.scan_id) == []


@requires_engine
def test_face_phase_invents_no_duplicate_groups(tmp_path):
    """The face phase writes `files.full_hash` for photographs the funnel
    deliberately never hashed. That is only safe because byte-identical
    files necessarily share a size, and every same-size pair has already
    been through the funnel by then. If that argument is ever wrong, the
    second run here finds a group the first one did not.
    """
    root = tmp_path / "tree"
    root.mkdir()
    for i in range(5):  # all different sizes, none a duplicate
        _photo(root / f"solo_{i}.jpg", (i * 30, 60, 90), size=(300 + i * 40, 200))
    _photo(root / "pair_a.jpg", (7, 7, 7), size=(256, 256))
    (root / "pair_b.jpg").write_bytes((root / "pair_a.jpg").read_bytes())

    db = tmp_path / "i.db"
    first = ScanJob(roots=[str(root)], db_path=db, mode=ScanMode.FULL)
    report_one = first.run()
    second = ScanJob(roots=[str(root)], db_path=db, mode=ScanMode.FULL)
    report_two = second.run()

    def shape(report):
        return sorted(
            tuple(sorted(Path(r.display_path).name for r in g.records))
            for g in report.groups
        )

    assert shape(report_one) == [("pair_a.jpg", "pair_b.jpg")]
    assert shape(report_two) == shape(report_one)


@requires_engine
def test_second_scan_re_decodes_nothing(tmp_path):
    """Р6 applies to this phase as much as to hashing: the expensive work
    is remembered, so a rescan of an unchanged folder has nothing to do."""
    root = tmp_path / "tree"
    root.mkdir()
    _photo(root / "a.jpg", (11, 22, 33))
    db = tmp_path / "i.db"
    ScanJob(roots=[str(root)], db_path=db, mode=ScanMode.FULL).run()

    second = ScanJob(roots=[str(root)], db_path=db, mode=ScanMode.FULL)
    with ScanIndex(db) as index:
        # enumerate under the new scan id, then ask what is left to do
        from dupecleaner.scanner import Scanner

        second._enumerate(index, Scanner(include_archives=True))
        assert index.needs_faces(second.scan_id) == []


@requires_engine
@pytest.mark.skipif(
    not os.environ.get("DUPECLEANER_TEST_FACE_PHOTO"),
    reason="нет настоящей фотографии с лицом "
    "(DUPECLEANER_TEST_FACE_PHOTO=/путь/к/снимку)",
)
def test_engine_finds_a_face_in_a_real_photograph():
    """The only test here that proves the engine does its actual job.

    It needs a real photograph of a real person, which this repository
    must not contain — so it is opt-in through an environment variable
    and was run, during task 18, against photographs in `D:\\Photos`.
    Everything above it checks plumbing; this checks the thing.
    """
    engine = faces.FaceEngine()
    result = engine.analyse_path(Path(os.environ["DUPECLEANER_TEST_FACE_PHOTO"]))
    assert result.faces, "лицо не найдено"
    first = result.faces[0]
    assert first.width >= faces.MIN_FACE_WIDTH
    assert 0.0 <= first.score <= 1.0
    vector = faces.decode_embedding(first.embedding)
    assert len(vector) == faces.EMBEDDING_DIM
    norm = sum(v * v for v in vector) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-5)
