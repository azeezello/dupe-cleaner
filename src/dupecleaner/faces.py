r"""Offline face detection and embeddings.

Why this module exists, and the one line it may never cross
------------------------------------------------------------
Р4 makes an event an album and a *person* a filter over events, and the
fourth link of its naming chain ("с Каримом") needs to know who is in a
photo. Р2 gives faces a second, quieter job: a face in the frame is a
safety catch, the thing that stops a quality score from ever reading as
"probably junk". Both want the same primitive — for each photo, where the
faces are and a vector per face that can be compared with another photo's.

That is all this module does. It finds faces and turns each one into 128
numbers. It does not decide who anyone is, does not cluster, does not
name, and — this is the Р0 invariant, not a preference — nothing it
produces may ever reach `quarantine`. Faces are axis C (organisation) with
a foot in axis B (value); neither has authority to move a file. Clustering
and labelling are task 19.

Р4 also fixes the hard constraint: **no cloud APIs, ever**. Sending a
person's family photographs to a face-recognition service is the one
thing a local-first tool must not do quietly, and "offline" has to mean
offline at scan time, not "offline once the weights finish downloading".
So this module never touches the network during a scan. Models are
installed once, on purpose, by a separate command (`install_models`
below, `dupecleaner faces --install-models` in the CLI), verified by
SHA-256, and read from disk afterwards. With no models present the face
phase does not run at all and says so; it never silently reaches out.

The engine, and what it was chosen over
----------------------------------------
**YuNet** (detection) + **SFace** (embeddings), both from the OpenCV
model zoo, executed by OpenCV's own DNN module through
`cv2.FaceDetectorYN` / `cv2.FaceRecognizerSF`. Licences are permissive and
compatible with this project's MIT: YuNet is MIT (Shiqi Yu), SFace is
Apache-2.0. Weight: `opencv-python-headless` is 43.8 MB to download and
117.6 MB on disk (Windows/amd64 wheel, the platform this runs on), numpy
12.5 / 42.5 MB, the two model files 37.1 MB — about 93 MB of download and
197 MB on disk, and no compiler.

Rejected, with the reason rather than a shrug:

- **InsightFace (buffalo_l: SCRFD + ArcFace)** — the strongest accuracy of
  the three and the one this would use if licensing allowed. Its model zoo
  states, at the top of the page: "ALL models are available for
  non-commercial research purposes only." This repository is MIT and
  public. A dependency that is free to *use* while its weights may not be
  redistributed or used commercially is a licence trap for anyone who
  forks it, and Р4's promise is about what the tool does to a person's
  photos, not only about where the bytes travel. Its Python package also
  downloads the ~275 MB weight bundle from the network on first use, which
  is the behaviour this module is built to avoid.
- **face_recognition / dlib** — the obvious first answer, and the one the
  `[media]` extra in `pyproject.toml` has been carrying as a placeholder
  since the scaffold. dlib publishes **no wheels at all** (PyPI has a
  3.3 MB source tarball and nothing else), so installing it on Windows
  means CMake plus a Visual C++ toolchain. Asking that of someone who
  wants to tidy up their photographs is not a dependency, it is a
  weekend. Accuracy is also the oldest of the three.
- **DeepFace / facenet-pytorch** — pull in TensorFlow or PyTorch (hundreds
  of megabytes, and in DeepFace's case another download-on-first-use), for
  no accuracy that matters here.

The measurements behind the numbers below, and the one that decided the
minimum face size, are in `claude/task-18-faces-report.md`.

Working resolution, and why 32 pixels is the floor
----------------------------------------------------
Detection and embedding both run on a copy of the photo scaled to
`DETECT_LONG_SIDE` (1024 px). Measured on `D:\Photos`, going from 1024 to
1600 finds 7% more faces for twice the time; going down to 512 loses 22%
of them. 1024 is where that curve flattens.

Scaling a photo down before looking at it costs something, and the cost is
not uniform — it falls entirely on small faces. Measured against the same
faces cropped from a full-resolution decode, an embedding taken from the
1024 px copy agrees at cosine 0.95-0.98 for faces at least 32 px wide, and
at only 0.72 for faces below that. So a face narrower than
`MIN_FACE_WIDTH` is detected, counted, and deliberately **not** embedded:
its vector would be a number that looks like evidence and is not. The
count is still recorded (`faces_skipped_small`), so a photo of a crowd
does not read as a photo of nobody.

Everything above that floor is stored with its detector confidence and its
pixel width, and nothing is filtered on quality here. `DETECT_SCORE_MIN`
is deliberately loose (0.6, against the 0.9 OpenCV's own demo uses) for the
same reason `origin.py` stores its evidence: task 19 has to be able to
raise the bar without re-reading 59 GB of photographs. Choosing a
threshold is its job; making the choice re-playable is this one's.

Embeddings are L2-normalised on the way in, so comparing two of them is a
dot product and nothing downstream has to remember to normalise. SFace's
own reference threshold for "same person" is a cosine of 0.363; that
number belongs to task 19 and is quoted here only so the stored vectors'
scale is not a mystery.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from PIL import Image

logger = logging.getLogger(__name__)

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - pillow-heif is a declared dependency
    logger.warning(
        "pillow-heif не установлен — лица в HEIC/HEIF найдены не будут."
    )

# Registered here as well as in `thumbnails.py`, and that duplication is
# on purpose: the call is what teaches Pillow to open HEIC at all, and
# neither module may quietly depend on the other having been imported
# first. It cost a measurement to learn — the first library-wide sample
# run of this module reported seven "unreadable" files, all of them
# HEIC, purely because the sampling script imported `faces` and not
# `thumbnails`. Inside a scan it would have worked by luck. There are 783
# HEIC files in `D:\Photos` (task 8, pilot finding P2.8), which is 783
# photographs whose faces would have gone missing depending on an import
# order nothing states. `register_heif_opener()` is idempotent.

# --- the models --------------------------------------------------------

ENGINE_NAME = "yunet+sface"

# Long side the photo is scaled to before detection. See the module
# docstring for the measurement that picked it.
DETECT_LONG_SIDE = 1024
# Detector confidence floor. Loose on purpose — the score is stored.
DETECT_SCORE_MIN = 0.6
DETECT_NMS = 0.3
DETECT_TOP_K = 5000
# Below this width (in pixels, at DETECT_LONG_SIDE scale) a face is counted
# but not embedded: measured cosine agreement with a full-resolution crop
# collapses from ~0.95 to ~0.72 under it.
MIN_FACE_WIDTH = 32

EMBEDDING_DIM = 128
_EMBEDDING_STRUCT = struct.Struct(f"<{EMBEDDING_DIM}f")
EMBEDDING_BYTES = _EMBEDDING_STRUCT.size  # 512


@dataclass(frozen=True)
class ModelSpec:
    """One ONNX file this engine needs, pinned by content.

    `sha256` is what makes an unattended `--install-models` safe to run:
    the file either hashes to this or it is not installed. `url` is used by
    that one command and by nothing else in the codebase — a scan never
    reads it.
    """

    role: str
    filename: str
    sha256: str
    size: int
    license: str
    url: str


DETECTOR_MODEL = ModelSpec(
    role="detector",
    filename="face_detection_yunet_2023mar.onnx",
    sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    size=232589,
    license="MIT (Shiqi Yu, OpenCV Zoo)",
    url=(
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
    ),
)

RECOGNIZER_MODEL = ModelSpec(
    role="recognizer",
    filename="face_recognition_sface_2021dec.onnx",
    sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    size=38696353,
    license="Apache-2.0 (OpenCV Zoo)",
    url=(
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
    ),
)

MODELS: tuple[ModelSpec, ...] = (DETECTOR_MODEL, RECOGNIZER_MODEL)

MODELS_DIR_ENV = "DUPECLEANER_FACE_MODELS"


def models_dir() -> Path:
    """Where the ONNX files live.

    Next to the index by default (`~/.dupecleaner/models`), because they
    are per-installation rather than per-scan and have no business inside
    a git checkout — 37 MB of weights in a repository is how a tool ends
    up shipping a licence it did not read. `DUPECLEANER_FACE_MODELS`
    overrides it, which is the answer for a machine with no network at
    all: put the files somewhere by hand and point at them.
    """
    override = os.environ.get(MODELS_DIR_ENV)
    if override:
        return Path(override)
    from .storage import DEFAULT_DB_PATH

    return Path(DEFAULT_DB_PATH).parent / "models"


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def missing_models(directory: Path | None = None) -> list[ModelSpec]:
    """Which model files are absent or the wrong size.

    Size, not hash, on this path: it runs before every scan's face phase
    and hashing 37 MB to decide whether to skip a phase would be a silly
    tax. The hash is checked where it matters — at install time, on bytes
    that just arrived from somewhere.
    """
    directory = directory or models_dir()
    return [
        spec
        for spec in MODELS
        if not (directory / spec.filename).is_file()
        or (directory / spec.filename).stat().st_size != spec.size
    ]


def models_installed(directory: Path | None = None) -> bool:
    return not missing_models(directory)


def install_models(
    directory: Path | None = None,
    *,
    source_dir: Path | None = None,
    specs: Sequence[ModelSpec] = MODELS,
) -> list[str]:
    """Put the ONNX files in place, once, and verify them.

    The **only** function in this package that may open a network
    connection, and it is never called from a scan — that separation is
    the whole of Р4's "offline" promise as this module keeps it. With
    `source_dir` it copies instead of downloading, for a machine that has
    no network or an operator who would rather fetch the files by hand.

    A file whose SHA-256 does not match `ModelSpec.sha256` is deleted and
    the call raises. Silently accepting an unexpected face-recognition
    model would be a strange thing for this project to do.

    Returns human-readable lines describing what happened.
    """
    directory = Path(directory) if directory is not None else models_dir()
    directory.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    for spec in specs:
        dest = directory / spec.filename
        if dest.is_file() and dest.stat().st_size == spec.size:
            notes.append(f"{spec.filename}: уже на месте")
            continue

        tmp = dest.with_suffix(dest.suffix + ".part")
        if source_dir is not None:
            src = Path(source_dir) / spec.filename
            if not src.is_file():
                raise FileNotFoundError(f"{src} не найден")
            shutil.copyfile(src, tmp)
            origin = str(src)
        else:
            import urllib.request

            with urllib.request.urlopen(spec.url) as response, open(tmp, "wb") as out:
                shutil.copyfileobj(response, out)
            origin = spec.url

        actual = _sha256_of(tmp)
        if actual != spec.sha256:
            tmp.unlink(missing_ok=True)
            raise ValueError(
                f"{spec.filename}: контрольная сумма не совпала "
                f"(ожидалась {spec.sha256[:16]}…, получена {actual[:16]}…). "
                f"Источник: {origin}"
            )
        tmp.replace(dest)
        notes.append(f"{spec.filename}: установлен из {origin} ({spec.license})")

    return notes


# --- detection + embedding ---------------------------------------------


@dataclass(frozen=True)
class DetectedFace:
    """One face in one photo, in the coordinates of the scaled-down copy it
    was found in (`DETECT_LONG_SIDE` on the long side).

    The box is kept so a later screen can crop a face out of the original
    without re-running the detector, and `width` is the number task 19
    needs to decide how much to trust `embedding`. `embedding` is None for
    a face below `MIN_FACE_WIDTH` — see the module docstring; that is a
    recorded fact about the photo, not a failure.
    """

    index: int
    x: int
    y: int
    width: int
    height: int
    score: float
    embedding: bytes | None


@dataclass(frozen=True)
class PhotoFaces:
    """What one decode learned about one photo's faces."""

    faces: list[DetectedFace]
    skipped_small: int
    detect_long_side: int = DETECT_LONG_SIDE
    engine: str = ENGINE_NAME

    @property
    def embedded(self) -> int:
        return sum(1 for f in self.faces if f.embedding is not None)


def encode_embedding(values: Iterable[float]) -> bytes:
    """Pack 128 floats into the 512 bytes stored in the index.

    Raw little-endian float32 rather than JSON or a child table: a vector
    is only ever read whole and compared whole, and at 30 000 photos and
    two faces each the alternatives cost eight million rows or a megabyte
    of decimal digits to say the same thing. `struct` rather than numpy so
    that reading the index never requires the optional face dependencies —
    `storage.py` must stay importable with nothing but the standard
    library and Pillow.
    """
    packed = tuple(float(v) for v in values)
    if len(packed) != EMBEDDING_DIM:
        raise ValueError(f"ожидалось {EMBEDDING_DIM} чисел, получено {len(packed)}")
    return _EMBEDDING_STRUCT.pack(*packed)


def decode_embedding(blob: bytes) -> tuple[float, ...]:
    """Unpack what `encode_embedding` stored. Vectors come back
    L2-normalised, so the cosine between two of them is their dot
    product."""
    if len(blob) != EMBEDDING_BYTES:
        raise ValueError(
            f"эмбеддинг должен быть {EMBEDDING_BYTES} байт, получено {len(blob)}"
        )
    return _EMBEDDING_STRUCT.unpack(blob)


class FaceEngineUnavailable(RuntimeError):
    """Raised when the engine cannot start: no opencv, or no models.

    A distinct exception rather than a bare RuntimeError because the scan
    treats it as "this phase does not run", which is a different thing
    from "this photo failed" — the first should be said once, the second
    is a per-file warning.
    """


class FaceEngine:
    """YuNet + SFace, loaded once and reused for a whole scan.

    Both nets are constructed eagerly in `__init__` (about 100 ms, measured
    once per scan, against ~120 ms per photo afterwards) so a missing or
    corrupt model fails at the start of the phase rather than on the first
    photograph. The instance is **not** thread-safe: `cv2.FaceDetectorYN`
    carries its input size as mutable state, so two threads sharing one
    would silently detect at each other's resolution. One engine per
    thread, when the phase eventually grows a thread pool.
    """

    def __init__(self, directory: Path | None = None) -> None:
        directory = Path(directory) if directory is not None else models_dir()
        missing = missing_models(directory)
        if missing:
            raise FaceEngineUnavailable(
                "нет файлов модели: "
                + ", ".join(spec.filename for spec in missing)
                + f" (папка {directory}). Установить: dupecleaner faces --install-models"
            )
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise FaceEngineUnavailable(
                "не установлены зависимости распознавания лиц: "
                'pip install "dupe-cleaner[faces]"'
            ) from exc

        self._cv2 = cv2
        self._np = numpy
        self.directory = directory
        # OpenCV 5's new graph engine logs a warning about unsupported
        # targets every time a net is constructed. It is noise, it is not
        # ours, and it would otherwise print once per scan into a CLI that
        # reserves its output for things a person can act on.
        try:
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        except AttributeError:  # pragma: no cover - older OpenCV
            pass
        self._detector = cv2.FaceDetectorYN.create(
            str(directory / DETECTOR_MODEL.filename),
            "",
            (320, 320),
            DETECT_SCORE_MIN,
            DETECT_NMS,
            DETECT_TOP_K,
        )
        self._recognizer = cv2.FaceRecognizerSF.create(
            str(directory / RECOGNIZER_MODEL.filename), ""
        )

    # -- the two halves, separately testable ----------------------------

    def _to_bgr(self, image: "Image.Image"):
        """PIL RGB -> the contiguous BGR array OpenCV wants.

        `.copy()` is load-bearing: the reversed slice is a negative-stride
        view, and OpenCV rejects those with an unhelpful error deep inside
        the DNN call rather than at the boundary.
        """
        rgb = image.convert("RGB")
        return self._np.asarray(rgb)[:, :, ::-1].copy()

    def analyse_image(self, image: "Image.Image") -> PhotoFaces:
        """Detect and embed every face in an already-decoded, already-scaled
        image. Split out from `analyse_path` so tests can hand it a
        synthetic image and so the scan can reuse a decode it already
        paid for.
        """
        array = self._to_bgr(image)
        height, width = array.shape[:2]
        self._detector.setInputSize((width, height))
        _, raw = self._detector.detect(array)
        if raw is None:
            return PhotoFaces(faces=[], skipped_small=0)

        faces: list[DetectedFace] = []
        skipped_small = 0
        # Biggest first: a group photo's stored order then means something
        # (the subject before the bystanders), and task 19 can stop early.
        ordered = sorted(raw, key=lambda row: -(float(row[2]) * float(row[3])))
        for row in ordered:
            x, y, w, h = (int(round(float(v))) for v in row[0:4])
            score = float(row[14])
            if w < MIN_FACE_WIDTH:
                skipped_small += 1
                continue
            try:
                aligned = self._recognizer.alignCrop(array, row)
                vector = self._recognizer.feature(aligned).flatten()
            except Exception as exc:  # noqa: BLE001 - one face must not fail a photo
                logger.debug("Не удалось построить эмбеддинг лица: %s", exc)
                continue
            norm = float(self._np.linalg.norm(vector))
            if norm <= 0:  # pragma: no cover - defensive
                continue
            faces.append(
                DetectedFace(
                    index=len(faces),
                    x=x,
                    y=y,
                    width=w,
                    height=h,
                    score=score,
                    embedding=encode_embedding(vector / norm),
                )
            )
        return PhotoFaces(faces=faces, skipped_small=skipped_small)

    def analyse_path(self, source: Path, *, data: bytes | None = None) -> PhotoFaces:
        """Decode `source` (or `data`, when the caller already has the bytes
        in hand) at `DETECT_LONG_SIDE` and analyse it.

        `draft()` before `convert()` for the same reason `thumbnails.py`
        uses it: for JPEG it decodes straight from the DCT coefficients at
        a reduced scale instead of doing a full-resolution IDCT and then
        throwing most of it away. It is the single biggest saving in this
        phase — measured at 66 ms per photo against 133 ms when the target
        is 1600 px, on the same photographs.

        BILINEAR, not LANCZOS, for the residual downscale: the detector
        looks at edges, not at how pleasant the picture is, and LANCZOS
        ringing is not a thing worth paying for here.
        """
        import io

        from PIL import Image

        handle = Image.open(io.BytesIO(data)) if data is not None else Image.open(source)
        with handle as img:
            img.draft("RGB", (DETECT_LONG_SIDE, DETECT_LONG_SIDE))
            img = img.convert("RGB")
            img.thumbnail((DETECT_LONG_SIDE, DETECT_LONG_SIDE), Image.BILINEAR)
            return self.analyse_image(img)
