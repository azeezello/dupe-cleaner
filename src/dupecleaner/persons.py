r"""Clustering face embeddings into persons (task 19).

Why this module exists, and the line it stays on this side of
-----------------------------------------------------------------
`faces.py` (task 18) answers "where are the faces, and a vector per
face". Р4 needs the next question answered: which vectors are the same
person, so a person can become a filter over events — the fourth link of
the album-naming chain ("с Каримом") — without a human tagging every
photograph one at a time.

Р4 is explicit about what a person is allowed to be: **a filter over
events, never an album**. This module therefore only ever produces a
grouping of faces; it has no access to `quarantine`, cannot move a file,
and does not know what an album is. Building the library out of events
and person-filters is a later task's job.

Labelling once, and having it survive a rescan
-------------------------------------------------
"Clustering" and "naming a cluster" are kept apart on purpose, the same
way `events.py` keeps thresholds apart from the folders task 21 will
someday write. A human names a *cluster* once; the cluster's membership
is free to keep growing as new photographs arrive without asking that
name again. What makes this possible, and the reason it is worth stating
loudly, is the key: a person's membership is recorded by
`(content_hash, face_index)` — the exact key `content_faces` already
uses — never by path. That is what a label survives: a rescan (Р6), a
file moved into a better-named folder, and a trip through quarantine and
back (the same promise Р9 makes for thumbnails and Р10 for review
decisions). Keying by path instead would zero every name out on the
first file move this tool exists to make.

An unlabelled cluster is not a failure, it is the default: it shows up
as "Человек №N" and works as a filter immediately — Р4 does not require a
name, only a grouping. `persons.label IS NULL` *is* "Человек №{person_id}"
as far as every caller of this module is concerned; the module itself
never manufactures that string, so renumbering never has to happen when
a person is later named.

Clustering: incremental nearest-centroid, not single-linkage
----------------------------------------------------------------
A new face joins the *closest* existing person if the cosine similarity
between its embedding and that person's centroid is at least
`PersonThresholds.merge_cosine`; otherwise it starts a cluster of its
own. This is "leader" (sequential, nearest-centroid) clustering, and the
choice not to use single-linkage — "join if similar enough to *any one*
member" — is deliberate: single-linkage chains. One blurry, half-turned,
badly-lit face sitting between two different people is all it takes to
merge them, and a library full of group photos and motion blur produces
exactly that face constantly. Comparing against a centroid — the mean of
everyone already accepted — makes one bad frame far less able to drag
two people together, at the cost of being a little slower to accept a
face that is genuinely a poor likeness of everyone already in its own
cluster. For a task where "one wrong merge silently hides someone in
someone else's filter" is worse than "one photo needs a human's second
click", that is the right side to lean on.

Faces are offered to the clusterer biggest-and-most-confident first
(`width * score`, descending) — the same "biggest first" rule `faces.py`
already applies inside one photograph, extended across the whole
library. The clearest evidence should seed a cluster and set its
centroid; a small, uncertain face should be the one asking "do I belong
here", never the one a cluster's identity is built from. Processing
order would otherwise depend on directory listing order, which answers
nothing about the person in the photo.

The centroid itself is the L2-renormalised mean of a cluster's member
embeddings, recomputed from the stored vectors every time rather than
cached anywhere — the same choice `events.py` makes for not storing
clusters at all: a stored centroid drifts out of sync the moment
membership changes underneath it, silently, and nothing would notice.
Storage (`ScanIndex.person_embeddings`) is what makes recomputing cheap:
128 floats per member, not a re-detection.

The threshold, and why it is an argument
-------------------------------------------
`merge_cosine` is measured on `D:\Photos`, not guessed — see
`docs/task-19-persons-report.md` for the histogram of same-photo and
same-folder pairwise similarities that picked the default, in exactly
the spirit `events.EventThresholds` documents its own numbers. It is a
constructor argument for the same reason: the library that measured it
is not the library that will run it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .faces import decode_embedding


@dataclass(frozen=True)
class FaceObservation:
    """One embedded face this run knows about, keyed exactly the way
    `content_faces` keys it. `embedding` is the 128-float tuple
    `faces.decode_embedding` returns — already L2-normalised, so a dot
    product between two of them is their cosine similarity.
    """

    content_hash: str
    face_index: int
    width: int
    score: float
    embedding: tuple[float, ...]

    @property
    def quality(self) -> float:
        """"Biggest and most confident first" as one sortable number."""
        return float(self.width) * float(self.score)


def observation_from_row(row) -> FaceObservation:
    """Build a `FaceObservation` from a `content_faces`-shaped row (as
    `ScanIndex.unclustered_faces` / `iter_scan_faces` return it). A
    module-level function rather than a classmethod so storage.py never
    has to import this module to hand back rows in the right shape.
    """
    return FaceObservation(
        content_hash=row["content_hash"],
        face_index=row["face_index"],
        width=int(row["width"]),
        score=float(row["score"]),
        embedding=decode_embedding(row["embedding"]),
    )


@dataclass(frozen=True)
class PersonThresholds:
    """The one number this module depends on, in the same spirit as
    `events.EventThresholds`: named, documented, and passed in rather
    than baked into the algorithm.
    """

    # SFace's own reference point for "same person" is a cosine of 0.363
    # (faces.py's docstring quotes it for exactly this reason), and it is
    # NOT what this project uses: measured directly on `D:\Photos`
    # (docs/task-19-persons-report.md), 0.363 — and even 0.42 — merges
    # visibly different family members into one cluster within a few
    # hundred faces. The ground truth is faces detected together in the
    # same, non-composite photograph — two boxes in one frame are, short
    # of a mirror, two different people. On `D:\Photos` that distribution
    # has 95% of its mass below 0.42 and 99% below 0.64, which is exactly
    # where the observed over-merging starts and stops. 0.6 sits just
    # under that 99th percentile: it trades a real cost (a majority of
    # resulting clusters end up as one-off "Человек №N" that a second
    # photo never joins — measured at 72% on the same library) for a
    # cluster a human can trust without checking every member, which the
    # report argues is the right side of that trade for a filter nobody
    # is going to audit face by face.
    merge_cosine: float = 0.6


def _renormalised_mean(vectors: Sequence[Sequence[float]]) -> tuple[float, ...]:
    """The L2-renormalised mean of one or more unit vectors — a
    centroid that is itself a unit vector, so comparing a new embedding
    against it is still a plain dot product."""
    dim = len(vectors[0])
    summed = [0.0] * dim
    for vector in vectors:
        for i, value in enumerate(vector):
            summed[i] += value
    norm = math.sqrt(sum(value * value for value in summed))
    if norm <= 0:
        return tuple(summed)
    return tuple(value / norm for value in summed)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# Public aliases for callers outside this module (the CLI, the web layer)
# that need the same two primitives without duplicating them.
cosine_similarity = _cosine


def centroid_from_embeddings(embeddings: Iterable[bytes]) -> tuple[float, ...]:
    """The renormalised-mean centroid of stored embedding blobs — what
    `ScanIndex.person_embeddings(person_id)` returns, turned into the
    vector `cluster_new_faces`'s `existing_centroids` expects.
    """
    vectors = [decode_embedding(blob) for blob in embeddings]
    if not vectors:
        raise ValueError("centroid_from_embeddings() требует хотя бы один эмбеддинг")
    return _renormalised_mean(vectors)


class _RunningCentroid:
    """A centroid kept as a running sum, so comparing a candidate face
    against it costs O(EMBEDDING_DIM) regardless of how many faces the
    cluster already has — the incremental-clustering equivalent of why
    `content_previews` keys by hash instead of re-decoding.
    """

    __slots__ = ("_sum", "_unit")

    def __init__(self, seed: Sequence[float]) -> None:
        self._sum = list(seed)
        self._unit: tuple[float, ...] | None = None

    def add(self, vector: Sequence[float]) -> None:
        for i, value in enumerate(vector):
            self._sum[i] += value
        self._unit = None

    @property
    def unit(self) -> tuple[float, ...]:
        if self._unit is None:
            norm = math.sqrt(sum(v * v for v in self._sum))
            self._unit = (
                tuple(v / norm for v in self._sum) if norm > 0 else tuple(self._sum)
            )
        return self._unit


@dataclass
class NewPersonCluster:
    """One brand-new cluster this run discovered, not yet a row in
    `persons` — creating that row is the caller's job (it owns the
    database and the next free id), this module only groups.
    """

    members: list[FaceObservation] = field(default_factory=list)
    centroid: tuple[float, ...] = ()


@dataclass
class PersonClustering:
    """The whole answer for one run of `cluster_new_faces`."""

    # (content_hash, face_index) -> (person_id, similarity), for faces
    # that joined a person the caller already knew about.
    joined_existing: dict[tuple[str, int], tuple[int, float]] = field(
        default_factory=dict
    )
    # Brand-new clusters, each of which becomes one freshly-created,
    # unlabelled person. Index in this list has no meaning outside one
    # call — it is not a person id.
    new_clusters: list[NewPersonCluster] = field(default_factory=list)

    @property
    def new_faces(self) -> int:
        return sum(len(c.members) for c in self.new_clusters)


def cluster_new_faces(
    observations: Iterable[FaceObservation],
    *,
    existing_centroids: dict[int, tuple[float, ...]] | None = None,
    thresholds: PersonThresholds = PersonThresholds(),
) -> PersonClustering:
    """Assign every face in `observations` to a person.

    `existing_centroids` maps an already-known `person_id` to its current
    centroid — computed by the caller from `ScanIndex.person_embeddings`,
    because this module keeps no database of its own (the same split
    `events.py` keeps from `storage.py`). Pass an empty mapping for a
    library's first run: every face then starts, or joins, one of the
    brand-new clusters this call discovers.

    Every previously-known person is a candidate for every new face
    (there is no assumption that a person's photos are close together in
    the input, which — the whole point of "the same person across
    years" — they are not). Within one call, a fresh cluster is also a
    candidate for a later, lower-quality face, so ten photos of the same
    unlabelled stranger in one run become one new person, not ten.
    """
    ordered = sorted(observations, key=lambda f: -f.quality)

    running: dict[int, _RunningCentroid] = {
        person_id: _RunningCentroid(list(vector))
        for person_id, vector in (existing_centroids or {}).items()
    }
    new_running: list[_RunningCentroid] = []
    new_clusters: list[NewPersonCluster] = []
    joined: dict[tuple[str, int], tuple[int, float]] = {}

    for face in ordered:
        best_similarity = thresholds.merge_cosine
        best_kind: str | None = None
        best_id: int | str = -1

        for person_id, centroid in running.items():
            similarity = _cosine(face.embedding, centroid.unit)
            if similarity >= best_similarity:
                best_similarity, best_kind, best_id = similarity, "existing", person_id

        for index, centroid in enumerate(new_running):
            similarity = _cosine(face.embedding, centroid.unit)
            if similarity >= best_similarity:
                best_similarity, best_kind, best_id = similarity, "new", index

        if best_kind == "existing":
            person_id = int(best_id)
            running[person_id].add(face.embedding)
            joined[(face.content_hash, face.face_index)] = (person_id, best_similarity)
        elif best_kind == "new":
            index = int(best_id)
            new_running[index].add(face.embedding)
            new_clusters[index].members.append(face)
        else:
            new_running.append(_RunningCentroid(list(face.embedding)))
            new_clusters.append(NewPersonCluster(members=[face]))

    for cluster, centroid in zip(new_clusters, new_running):
        cluster.centroid = centroid.unit

    return PersonClustering(joined_existing=joined, new_clusters=new_clusters)


def person_display_name(label: str | None, person_id: int) -> str:
    """"Карим" if named, "Человек №7" otherwise — the one place this
    string is built, so the CLI, the web UI and the report agree on it.
    """
    return label if label else f"Человек №{person_id}"
