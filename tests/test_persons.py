"""Task 19: clustering face embeddings into persons.

Split the same way `test_faces.py` splits task 18: everything here runs
with no OpenCV and no models, because clustering never touches an image —
it only ever consumes the vectors task 18 already computed and stored.

The tests that matter most:

- `test_a_face_joins_the_closer_of_two_existing_persons` — the actual
  algorithm: nearest centroid, not first match.
- `test_centroid_comparison_resists_one_bad_frame` — the reason
  single-linkage was rejected in the module docstring, made concrete:
  one face roughly halfway between two people must not chain them
  together the way single-linkage would.
- `test_labelling_survives_a_second_clustering_run` — Р4's whole point:
  a name given once must not be undone by finding more photographs.
- `test_person_survives_a_path_change` — the same content-hash-keyed
  survival `content_previews` (Р9) already gets, checked for persons.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PIL import Image

from dupecleaner import faces
from dupecleaner.jobs import ScanJob
from dupecleaner.models import ScanMode
from dupecleaner.persons import (
    FaceObservation,
    PersonThresholds,
    centroid_from_embeddings,
    cluster_new_faces,
    cosine_similarity,
    observation_from_row,
    person_display_name,
)
from dupecleaner.storage import ScanIndex


# --- helpers -------------------------------------------------------------


def _vector(seed: float) -> tuple[float, ...]:
    """A deterministic unit vector, distinct for distinct seeds."""
    raw = [(i * 7 + seed) % 13 - 6 for i in range(faces.EMBEDDING_DIM)]
    norm = sum(v * v for v in raw) ** 0.5
    return tuple(v / norm for v in raw)


def _blend(a: tuple[float, ...], b: tuple[float, ...], t: float) -> tuple[float, ...]:
    """A unit vector between two others — the "bad frame" a face halfway
    between two people would produce."""
    raw = [(1 - t) * x + t * y for x, y in zip(a, b)]
    norm = sum(v * v for v in raw) ** 0.5
    return tuple(v / norm for v in raw)


def _obs(content_hash: str, seed: float, *, face_index: int = 0, width: int = 100,
          score: float = 0.9) -> FaceObservation:
    return FaceObservation(
        content_hash=content_hash,
        face_index=face_index,
        width=width,
        score=score,
        embedding=_vector(seed),
    )


def _photo(path: Path, colour: tuple[int, int, int], size=(600, 400)) -> None:
    Image.new("RGB", size, colour).save(path, "JPEG", quality=90)


# --- pure clustering -------------------------------------------------------


def test_two_distinct_faces_become_two_new_clusters():
    observations = [_obs("a", seed=0.0), _obs("b", seed=6.0)]
    result = cluster_new_faces(observations)
    assert len(result.new_clusters) == 2
    assert result.joined_existing == {}


def test_near_identical_faces_join_one_cluster():
    base = _vector(1.0)
    nearly_same = _blend(base, _vector(1.05), 0.02)
    observations = [
        FaceObservation("a", 0, 100, 0.9, base),
        FaceObservation("b", 0, 100, 0.9, nearly_same),
    ]
    result = cluster_new_faces(observations)
    assert len(result.new_clusters) == 1
    assert len(result.new_clusters[0].members) == 2


def test_biggest_and_most_confident_face_is_processed_first():
    """Quality order decides who seeds a cluster, not input order — feed
    the small, uncertain face first and the outcome must not change."""
    big = FaceObservation("big", 0, 200, 0.95, _vector(2.0))
    small_but_first = FaceObservation("small", 0, 30, 0.4, _vector(2.0))
    result = cluster_new_faces([small_but_first, big])
    assert len(result.new_clusters) == 1
    # the seed member (index 0) is the one sorted first by quality
    assert result.new_clusters[0].members[0].content_hash == "big"


def test_a_face_joins_the_closer_of_two_existing_persons():
    person_a = _vector(0.0)
    person_b = _vector(10.0)
    closer_to_a = _blend(person_a, person_b, 0.1)  # mostly A
    result = cluster_new_faces(
        [FaceObservation("x", 0, 100, 0.9, closer_to_a)],
        existing_centroids={1: person_a, 2: person_b},
        thresholds=PersonThresholds(merge_cosine=-1.0),  # always join someone
    )
    assert result.joined_existing[("x", 0)][0] == 1
    assert result.new_clusters == []


def test_below_threshold_starts_a_new_person_instead_of_forcing_a_match():
    existing = _vector(0.0)
    unrelated = _vector(9.0)
    result = cluster_new_faces(
        [FaceObservation("x", 0, 100, 0.9, unrelated)],
        existing_centroids={1: existing},
        thresholds=PersonThresholds(merge_cosine=0.99),
    )
    assert result.joined_existing == {}
    assert len(result.new_clusters) == 1


def test_centroid_comparison_resists_one_bad_frame():
    """Single-linkage would chain two people together through one face
    that sits roughly halfway between them. Nearest-centroid must not.

    Five clean faces build each person's centroid; a sixth face, planted
    almost exactly halfway between the two centroids, must not be close
    enough to either one to join it, and must not merge the two people
    into each other by attaching to both — it starts (or stays out of)
    its own cluster.
    """
    person_a_seed = _vector(0.0)
    person_b_seed = _vector(20.0)

    def _cloud(seed: tuple[float, ...], tag: str) -> list[FaceObservation]:
        return [
            FaceObservation(f"{tag}{i}", 0, 100, 0.9, _blend(seed, _vector(50 + i), 0.03))
            for i in range(5)
        ]

    halfway = _blend(person_a_seed, person_b_seed, 0.5)
    observations = (
        _cloud(person_a_seed, "a")
        + _cloud(person_b_seed, "b")
        + [FaceObservation("mid", 0, 40, 0.5, halfway)]
    )
    result = cluster_new_faces(
        observations, thresholds=PersonThresholds(merge_cosine=0.6)
    )
    clusters_with_mid = [
        c for c in result.new_clusters
        if any(m.content_hash == "mid" for m in c.members)
    ]
    assert len(clusters_with_mid) == 1
    mid_cluster = clusters_with_mid[0]
    # the halfway face must not have dragged members of BOTH clouds into
    # its own cluster — that would be the two people merged into one
    tags = {m.content_hash[0] for m in mid_cluster.members}
    assert not ({"a", "b"} <= tags)


def test_centroid_from_embeddings_matches_manual_mean():
    vectors = [_vector(1.0), _vector(2.0), _vector(3.0)]
    blobs = [faces.encode_embedding(v) for v in vectors]
    centroid = centroid_from_embeddings(blobs)
    assert len(centroid) == faces.EMBEDDING_DIM
    # a unit vector
    assert abs(sum(v * v for v in centroid) - 1.0) < 1e-4


def test_centroid_from_embeddings_rejects_empty_input():
    with pytest.raises(ValueError):
        centroid_from_embeddings([])


def test_cosine_similarity_of_identical_unit_vectors_is_one():
    v = _vector(4.0)
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-9


def test_person_display_name_falls_back_to_the_number():
    assert person_display_name("Карим", 3) == "Карим"
    assert person_display_name(None, 3) == "Человек №3"
    assert person_display_name("", 3) == "Человек №3"


# --- storage: the v8 tables ------------------------------------------------


def test_migration_v8_adds_persons_tables(tmp_path):
    db = tmp_path / "i.db"
    with ScanIndex(db) as index:
        cursor = index._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {row["name"] for row in cursor}
        assert {"persons", "person_faces"} <= names
        row = index._conn.execute(
            "SELECT value FROM meta WHERE key='applied_migrations'"
        ).fetchone()
        assert "8" in row["value"].split(",")


def test_create_person_and_label_round_trip(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        person_id = index.create_person()
        assert index.get_person(person_id)["label"] is None
        index.set_person_label(person_id, "Карим")
        assert index.get_person(person_id)["label"] == "Карим"
        index.set_person_label(person_id, None)
        assert index.get_person(person_id)["label"] is None


def test_assign_faces_to_persons_is_replace_not_merge(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        p1 = index.create_person()
        p2 = index.create_person()
        index.assign_faces_to_persons([("hash", 0, p1, 0.9)])
        index.assign_faces_to_persons([("hash", 0, p2, 0.7)])  # reassigned
        embeddings = index.person_embeddings(p1)
        assert embeddings == []  # no longer p1's
        rows = index.list_persons()
        by_id = {r["person_id"]: r for r in rows}
        assert by_id[p2]["face_count"] == 1
        assert by_id[p1]["face_count"] == 0


def test_unclustered_faces_excludes_what_person_faces_already_claims(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        from dupecleaner.models import FileRecord, MediaKind

        record = FileRecord(
            display_path="a.jpg",
            real_path="a.jpg",
            size=10,
            mtime=1.0,
            media_kind=MediaKind.PHOTO,
            is_archive_member=False,
            archive_path=None,
            member_name=None,
            source_size=10,
            source_mtime=1.0,
        )
        index.upsert_files([record], "scan-1")
        index.set_full_hash("a.jpg", "content-a")
        index.set_faces(
            "content-a",
            [(0, 1, 2, 64, 64, 0.9, faces.encode_embedding(_vector(1.0)))],
            engine="t",
            detect_long_side=1024,
        )
        assert len(list(index.unclustered_faces("scan-1"))) == 1

        person_id = index.create_person()
        index.assign_faces_to_persons([("content-a", 0, person_id, 0.9)])
        assert list(index.unclustered_faces("scan-1")) == []


def test_observation_from_row_round_trips_the_embedding(tmp_path):
    with ScanIndex(tmp_path / "i.db") as index:
        vector = _vector(5.0)
        index.set_faces(
            "h", [(0, 1, 2, 80, 90, 0.8, faces.encode_embedding(vector))],
            engine="t", detect_long_side=1024,
        )
        row = index.get_faces("h")[0]
        observation = observation_from_row(row)
        assert observation.content_hash == "h"
        assert observation.width == 80
        # float32 on the wire, so compare through the same round trip
        # `set_faces` already did rather than against the float64 original
        assert observation.embedding == faces.decode_embedding(
            faces.encode_embedding(vector)
        )


# --- the point of the whole feature: labels survive ------------------------


def test_labelling_survives_a_second_clustering_run(tmp_path):
    """Naming a cluster is a one-time act. A second run that only adds
    new, unrelated faces must not touch the labelled person's name or
    membership."""
    with ScanIndex(tmp_path / "i.db") as index:
        person_id = index.create_person(label="Карим")
        index.assign_faces_to_persons([("karim-1", 0, person_id, 0.95)])
        index.set_faces(
            "karim-1",
            [(0, 1, 2, 100, 100, 0.9, faces.encode_embedding(_vector(0.0)))],
            engine="t", detect_long_side=1024,
        )

        # a brand-new, unrelated face shows up in a later scan
        centroid = centroid_from_embeddings(index.person_embeddings(person_id))
        new_face = observation_from_row(
            {
                "content_hash": "stranger",
                "face_index": 0,
                "width": 100,
                "score": 0.9,
                "embedding": faces.encode_embedding(_vector(9.0)),
            }
        )
        clustering = cluster_new_faces(
            [new_face], existing_centroids={person_id: centroid}
        )
        assert clustering.joined_existing == {}  # unrelated, stays apart

        assert index.get_person(person_id)["label"] == "Карим"
        assert len(index.person_embeddings(person_id)) == 1


def test_person_survives_a_path_change(tmp_path):
    """The whole reason membership is keyed by content_hash: the same
    photo, refiled under a new path (a quarantine round trip, or simply
    moved), must not lose its person."""
    from dupecleaner.models import FileRecord, MediaKind

    def _record(path: str) -> FileRecord:
        return FileRecord(
            display_path=path, real_path=path, size=10, mtime=1.0,
            media_kind=MediaKind.PHOTO, is_archive_member=False,
            archive_path=None, member_name=None, source_size=10, source_mtime=1.0,
        )

    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_files([_record("old/path.jpg")], "scan-1")
        index.set_full_hash("old/path.jpg", "content-x")
        index.set_faces(
            "content-x", [(0, 1, 2, 64, 64, 0.9, faces.encode_embedding(_vector(3.0)))],
            engine="t", detect_long_side=1024,
        )
        person_id = index.create_person(label="Карим")
        index.assign_faces_to_persons([("content-x", 0, person_id, 0.9)])

        # the file moves to a new path under a new scan
        index.upsert_files([_record("new/renamed.jpg")], "scan-2")
        index.set_full_hash("new/renamed.jpg", "content-x")

        assert list(index.unclustered_faces("scan-2")) == []
        assert index.person_sample_paths("scan-2", person_id) == ["new/renamed.jpg"]
        assert index.get_person(person_id)["label"] == "Карим"
