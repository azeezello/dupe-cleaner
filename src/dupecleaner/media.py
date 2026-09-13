"""Phase 2 architecture: near-duplicate media detection, face recognition and
auto-album organization.

Nothing in this file runs yet — it is the interface contract phase 1 was
built against, so phase 2 slots in without reshaping the scanner/dedupe/
quarantine pipeline. Install the optional `media` extra
(`pip install -e ".[media]"`) once you start implementing these.

Chosen approach (see docs/ROADMAP.md for the full reasoning):

- Near-duplicate photos: perceptual hashing via `imagehash` (phash), which
  is resistant to re-encoding/resizing/minor edits but NOT to major crops
  or heavy color changes — by design, so it never over-matches two
  genuinely different photos. Runs fully offline.
- Near-duplicate videos: `videohash`-style approach — sample frames at
  fixed intervals, phash each, aggregate into one fingerprint. Offline.
- Face recognition: `face_recognition` (dlib) or `insightface` (more
  accurate, heavier) — both run fully offline/on-device. No cloud API is
  used anywhere in this project, matching the whole point of not handing
  your photos to another subscription service.
- Album naming: cluster photos by (time-gap between shots) + (GPS EXIF,
  reverse-geocoded via a local/offline database — no network calls) +
  (dominant face cluster in the group), then propose a name like
  "Bukhara — Aug 2026" or "Karim's birthday — the faces seen only in this
  cluster, cross-referenced against a user-labelled '家族' set". The
  *naming* step should stay a suggestion the user approves in the web UI,
  never an automatic file move for the same reason quarantine review is
  manual for media.

All of this must obey the same non-destructive posture as phase 1: a
proposed album is a set of *tags/moves into a new organized tree*, always
reversible, never an in-place delete.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NearDuplicateGroup:
    """Like DuplicateGroup, but content differs slightly (different
    resolution/compression/crop) — similarity score in [0, 1], not exact
    equality. Never auto-quarantined; always surfaced for manual compare.
    """

    representative_hash: str
    similarity: float
    paths: list[str]


class PerceptualHasher(ABC):
    @abstractmethod
    def hash_image(self, path: Path) -> str:
        """Return a perceptual hash string (e.g. 64-bit phash as hex)."""

    @abstractmethod
    def hash_video(self, path: Path) -> str:
        """Return an aggregate perceptual fingerprint for a video."""

    @abstractmethod
    def similarity(self, hash_a: str, hash_b: str) -> float:
        """0.0 (completely different) .. 1.0 (identical)."""


class FaceCluster:
    """A group of face detections believed to be the same person, across
    however many photos. `label` starts empty — the user names it once
    (e.g. "Карим"), and that name is remembered for future scans.
    """

    def __init__(self, cluster_id: str) -> None:
        self.cluster_id = cluster_id
        self.label: str | None = None
        self.photo_paths: list[str] = []


class FaceRecognizer(ABC):
    @abstractmethod
    def detect_and_encode(self, path: Path) -> list[list[float]]:
        """Return one face-encoding vector per detected face in the image."""

    @abstractmethod
    def cluster(self, encodings_by_path: dict[str, list[list[float]]]) -> list[FaceCluster]:
        """Group all detected faces across a whole scan into per-person
        clusters, without needing labels in advance.
        """


class AlbumOrganizer(ABC):
    @abstractmethod
    def propose_albums(
        self,
        photo_paths: list[str],
        face_clusters: list[FaceCluster] | None = None,
    ) -> dict[str, list[str]]:
        """Return {proposed_album_name: [photo_path, ...]}. Pure proposal —
        the caller (web UI) is responsible for asking the user to confirm
        before anything is moved/tagged on disk.
        """


# --- Reference (not-yet-wired) implementation notes -------------------------
#
# class ImageHashPerceptualHasher(PerceptualHasher):
#     def hash_image(self, path):
#         import imagehash
#         from PIL import Image
#         return str(imagehash.phash(Image.open(path)))
#     ...
#
# class FaceRecognitionEncoder(FaceRecognizer):
#     def detect_and_encode(self, path):
#         import face_recognition
#         image = face_recognition.load_image_file(path)
#         return [enc.tolist() for enc in face_recognition.face_encodings(image)]
#     ...
#
# These are intentionally left as comments rather than real imports so
# phase 1 has zero heavy ML dependencies.
