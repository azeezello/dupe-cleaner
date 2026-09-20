"""Where a photo came from: a camera, a screen, a messenger, a scanner, the web.

Why origin and not content (Р3)
--------------------------------
"Is this a screenshot?" answered by looking at the pixels is a guess. A
screenshot of a photograph looks like a photograph; a photograph of a
monitor looks like a screenshot. Answered by *provenance* it is close to
free and close to certain: a screenshot has no camera EXIF, carries the
exact pixel dimensions of some real screen, and is named by the tool that
took it. Р3 chose provenance for exactly that reason, and this module is
that decision written out.

The six classes are Р3's, unchanged. What they are *for* is the part worth
keeping in view: Р3 grants none of them the right to move a file. A
verdict here is a label and an album filter, nothing else — axis C in Р0,
which has no authority over the filesystem. `quarantine` neither imports
this module nor could learn anything from it if it did.

Why this is keyed by path, unlike previews and metrics
-------------------------------------------------------
Tasks 8 and 9 key their results by content hash, because a thumbnail and a
sharpness score are properties of *bytes*: every copy in a duplicate group
shares them, so one measurement answers for all of them.

Origin is not. Half the evidence here — the folder, the filename — is a
property of *where a copy sits*, and the pilot found the same bytes in
four different folders at once (`20170416_145106.jpg` in `Pictures\\`,
`Pictures\\Wedding Day\\`, `Pictures\\Diljon\\` and `Wedding 16042017\\`).
A copy of a screenshot that someone filed into `Краснодар` is still a
screenshot, but a copy sitting in `Screenshots 1` is one for a *stronger*
reason, and the two readings must not overwrite each other in a
hash-keyed table. So the verdict is stored per `display_path`, and two
copies of one file may legitimately carry different confidences.

What is deliberately not attempted
-----------------------------------
**Archive members.** Reading EXIF from inside an archive means another
sequential pass over it per member — finding A2, which task 3 spent a
whole session removing. Task 16 organises the library on disk, so this
costs nothing it needs today.

**A seventh class for edited derivatives.** `-COLLAGE`, `-EFFECTS`,
`-edited`, `Picsart_*` and friends are roughly 4% of `D:\\Photos`, and
they are not any of Р3's six: a gallery app's collage did not come from a
camera, a screen, a messenger or the web — it was made from photos
already in the library. They stay UNKNOWN, but the evidence records what
they are, so the number is visible rather than hidden inside the
fallback. Inventing the class is a decision for Aziz, not for this task.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from enum import Enum
from pathlib import Path, PurePath

from PIL import Image

try:  # pragma: no cover - mirrors thumbnails.py; registration is idempotent
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - declared dependency, may be missing
    pass


class OriginClass(str, Enum):
    """Р3's six classes, plus the honest seventh: we could not tell."""

    CAMERA = "camera"
    SCREENSHOT_DESKTOP = "screenshot_desktop"
    SCREENSHOT_PHONE = "screenshot_phone"
    MESSENGER = "messenger"
    DOCUMENT_SCAN = "document_scan"
    WEB_DOWNLOAD = "web_download"
    UNKNOWN = "unknown"

    @property
    def is_screenshot(self) -> bool:
        return self in (OriginClass.SCREENSHOT_DESKTOP, OriginClass.SCREENSHOT_PHONE)

    @property
    def excluded_from_albums(self) -> bool:
        """Whether task 16 should keep this out of event clustering.

        Р3 names screenshots and gives the reason: thousands of them
        dissolve any event structure they are mixed into. Document scans
        are here too, on Р5's authority rather than Р3's — Р5's library
        layout already gives `_documents/` its own tree outside the
        year/event hierarchy, which is the same statement made about
        folders. Both are labels, never grounds to move anything.
        """
        return self.is_screenshot or self is OriginClass.DOCUMENT_SCAN


class Confidence(str, Enum):
    """How much the verdict is worth.

    HIGH means the file says so itself: EXIF written by the device, a
    filename its capture tool generated, a folder named for the tool.
    MEDIUM means a strong circumstantial match — an exact screen
    resolution, a camera-style filename with the EXIF stripped out. LOW
    means a weak hint or none at all.

    Kept as its own field rather than folded into the class because task
    16 will want to treat "screenshot, HIGH" and "screenshot, MEDIUM"
    differently when deciding what to exclude, and a merged number could
    not be asked that question afterwards.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class GoogleSidecar:
    """The bits of a Google Takeout `.json` sidecar that bear on origin.

    Finding A3: Google frequently strips EXIF out of the JPEG and writes
    it to a neighbouring JSON instead. `googlePhotosOrigin` in that file
    is not a reconstruction of the EXIF — it is Google's own record of how
    the photo entered the account, which is a *better* origin signal than
    anything in the pixels.
    """

    origin_key: str | None = None      # mobileUpload | webUpload | composition | ...
    has_geo: bool = False
    taken_at: float | None = None


@dataclass(frozen=True)
class OriginSignals:
    """Everything the classifier is allowed to look at.

    A plain data bag with no I/O in it, so `classify` is a pure function
    over facts and can be tested exhaustively on a table of real filenames
    without a single file on disk. `read_signals` below is the part that
    touches the filesystem.
    """

    path: str
    width: int | None = None
    height: int | None = None
    image_format: str | None = None
    exif_make: str | None = None
    exif_model: str | None = None
    exif_software: str | None = None
    has_exif_datetime: bool = False
    has_gps: bool = False
    sidecar: GoogleSidecar | None = None

    @property
    def name(self) -> str:
        return PurePath(self.path.replace("\\", "/")).name

    @property
    def parents(self) -> tuple[str, ...]:
        return tuple(PurePath(self.path.replace("\\", "/")).parts[:-1])

    @property
    def has_camera_exif(self) -> bool:
        return bool(self.exif_make or self.exif_model)

    @property
    def device(self) -> str:
        return " ".join(p for p in (self.exif_make, self.exif_model) if p).strip()


@dataclass(frozen=True)
class OriginVerdict:
    origin: OriginClass
    confidence: Confidence
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_screenshot(self) -> bool:
        return self.origin.is_screenshot

    @property
    def excluded_from_albums(self) -> bool:
        return self.origin.excluded_from_albums

    def to_dict(self) -> dict:
        return {
            "origin": self.origin.value,
            "confidence": self.confidence.value,
            "evidence": list(self.evidence),
            "is_screenshot": self.is_screenshot,
            "excluded_from_albums": self.excluded_from_albums,
        }


# --- vocabularies ----------------------------------------------------------
#
# Every list below is open-ended by construction, exactly like
# `keeper.GENERIC_SEGMENTS` (Р8). A missing word costs a weaker verdict —
# usually UNKNOWN — and never a moved or lost file, because nothing in
# this module may move anything.

_SCANNER_WORDS = (
    "canoscan", "scanjet", "perfection", "lide", "epson scan", "mustek",
    "plustek", "scansnap", "workforce ds", "brother mfc", "brother dcp",
    "adobe scan", "camscanner", "office lens", "microsoft lens",
    "genius scan", "tiny scanner", "scanbot", "photoscan", "notebloc",
)

_MESSENGER_FOLDERS = (
    "whatsapp", "whatsapp images", "telegram", "telegram images",
    "telegram desktop", "viber", "viber images", "signal", "messenger",
    "imo", "wechat", "weixin", "facebook messenger", "kato",
)

_SCREENSHOT_FOLDERS = (
    "screenshot", "screenshots", "screen shots", "снимки экрана",
    "скриншоты", "снимок экрана", "captures", "screen captures",
)

_DOCUMENT_FOLDERS = (
    "документы", "documents", "docs", "scans", "сканы", "справки",
    "паспорт", "docs scans",
)

_DOWNLOAD_FOLDERS = ("downloads", "загрузки", "download", "saved pictures")

# --- filename patterns -----------------------------------------------------

_RE_WHATSAPP = re.compile(r"^IMG-\d{8}-WA\d+", re.I)
_RE_TELEGRAM_DESKTOP = re.compile(r"^photo_\d+@\d{2}-\d{2}-\d{4}", re.I)
_RE_VIBER = re.compile(r"^viber[_ ]image", re.I)
_RE_MESSENGER_RECEIVED = re.compile(r"^received_\d{10,}", re.I)
_RE_FB = re.compile(r"^FB_IMG_\d+", re.I)
# WhatsApp Desktop and Web save with the app's name spelled out, e.g.
# `WhatsApp Image 2024-01-02 at 10.11.12.jpeg`. Sixteen of these sat in
# UNKNOWN until the real folder was looked at, which is the whole argument
# for measuring the vocabulary instead of imagining it.
_RE_WHATSAPP_SPELLED = re.compile(r"^whatsapp (image|video|audio|animated)", re.I)

# Android and Samsung: `Screenshot_20240812_204426_Instagram.jpg`,
# `Screenshot_20190122-181354.jpg`. The trailing app name is the giveaway
# that a phone took it, but the date-time shape alone is already Android.
_RE_SCREENSHOT_ANDROID = re.compile(r"^screenshot[_-]?\d{8}[-_]\d{6}", re.I)
# Windows, both locales: `Снимок экрана 2024-12-29 182756.png`,
# `Screenshot (17).png`.
_RE_SCREENSHOT_WINDOWS = re.compile(
    r"^(снимок экрана|screenshot)\s*[\(\d]", re.I
)
# macOS: `Screen Shot 2019-01-01 at 10.00.00.png`, and its localisations.
_RE_SCREENSHOT_MACOS = re.compile(r"^(screen shot|снимок экрана \d{4}-\d{2}-\d{2} в)", re.I)
# Anything else that announces itself as a capture.
_RE_SCREENSHOT_GENERIC = re.compile(
    r"^(screenshot|screen[ _-]?shot|скриншот|снимок[ _]экрана|zrzut ekranu|"
    r"bildschirmfoto|captura de pantalla|capture d)", re.I
)
# Samsung's frame grab from a playing video. A capture of a screen, so it
# lands in the screenshot family — see the session notes for why that is a
# judgement call and not a fact.
_RE_VIDEO_CAPTURE = re.compile(r"^videocapture[_-]?\d{8}", re.I)

_RE_DOCUMENT_NAME = re.compile(
    r"^(new doc|scan[_\- ]?\d|scanned|скан[_\- ]|документ[_\- ]|doc[_\- ]?\d{4})", re.I
)

# Matched against the *stem*, not the whole filename: a CDN's 64-hex-digit
# name ends in `.jpg`, so anchoring at the end of the full name never fired.
_RE_WEB_NAME = re.compile(
    r"^(download|unnamed|untitled|image[s]?[ _(]|изображение|img[ _]\(\d+\)"
    r"|[0-9a-f]{24,}$"
    # Facebook's CDN, e.g. 327182_10150530365669841_780244840_8471659_749067045_o
    r"|\d{5,}_\d{10,}(_\d+){2,}_[on]$)",
    re.I,
)

# Camera-generated filenames, in the shapes this archive actually contains
# (measured, not imagined): `20170416_145106.jpg` (Android),
# `IMG_20200101_120000.jpg`, `IMG_1234.JPG` / `_MG_1234.JPG` (Canon),
# `DSC_1234.JPG` / `_DSC1234.JPG` (Nikon/Sony), `P1010101.JPG`
# (Panasonic/Olympus), `IMAG0123.jpg` (HTC/Lumia), `SDC12345.JPG`
# (Samsung compacts), `100_1234.JPG` (generic DCF). These matter because
# Google Takeout strips EXIF: for those files the filename is the only
# camera evidence left (finding A3).
_RE_CAMERA_NAME = re.compile(
    r"^(\d{8}[_-]\d{6}"
    r"|img[_-]\d{8}[_-]\d{6}"
    r"|img[_-]\d{3,5}"
    r"|_mg_\d{3,5}"
    r"|dsc[_n]?\d{3,5}"
    r"|_dsc\d{3,5}"
    r"|p\d{7,8}"
    r"|imag\d{3,5}"
    r"|sdc\d{4,6}"
    r"|\d{3}[_-]\d{4}"
    r"|\d{3}o\d{4}a\d{3,5})",
    re.I,
)

# Gallery-app derivatives. Not a class (see the module docstring) — only
# recorded, so the size of the gap is a number rather than a feeling.
_RE_EDITED = re.compile(
    r"(-edited|-COLLAGE|-EFFECTS|-ANIMATION|-PHOTO_FRAME|_remastered"
    r"|^Picsart_|^SLazerprint|-min$|~\d+$)",
    re.I,
)

# --- screen resolutions ----------------------------------------------------
#
# Exact pixel dimensions of real screens. Exactness is the whole strength
# of the signal: a photograph that happens to be 1080 px wide is almost
# never also exactly 2400 px tall.

_PHONE_SCREENS: frozenset[tuple[int, int]] = frozenset(
    {
        (720, 1280), (750, 1334), (828, 1792), (1080, 1920), (1080, 2160),
        (1080, 2220), (1080, 2244), (1080, 2280), (1080, 2310), (1080, 2340),
        (1080, 2400), (1080, 2408), (1080, 2412), (1080, 2436), (1125, 2436),
        (1170, 2532), (1179, 2556), (1206, 2622), (1242, 2688), (1284, 2778),
        (1290, 2796), (1320, 2868), (1440, 2560), (1440, 2880), (1440, 2960),
        (1440, 3040), (1440, 3088), (1440, 3120), (1440, 3200), (640, 1136),
        (768, 1024), (1536, 2048), (1620, 2160), (1668, 2388), (2048, 2732),
    }
)

_DESKTOP_SCREENS: frozenset[tuple[int, int]] = frozenset(
    {
        (1280, 720), (1280, 800), (1280, 1024), (1360, 768), (1366, 768),
        (1440, 900), (1536, 864), (1600, 900), (1680, 1050), (1920, 1080),
        (1920, 1200), (2048, 1152), (2560, 1080), (2560, 1440), (2560, 1600),
        (2880, 1800), (3440, 1440), (3840, 2160), (1512, 982), (1728, 1117),
        (2736, 1824), (1470, 956), (1512, 945),
    }
)

# Aspect ratios a photograph is routinely born or resaved at, long side
# over short: 1:1, 5:4, 4:3, 3:2, 16:10, 16:9. This list is what stops the
# resolution rule from being wrong far more often than it is right — see
# `_is_photo_shaped`.
_PHOTO_RATIOS = (1.0, 1.25, 1.3333, 1.5, 1.6, 1.7778)
_PHOTO_RATIO_TOLERANCE = 0.02

# A4 and US Letter, long side over short side. A scan is the one kind of
# photo whose aspect ratio is a specification rather than a choice.
_PAPER_RATIOS = (1.4142, 1.2941)
_PAPER_TOLERANCE = 0.02


def _lower_parents(signals: OriginSignals) -> tuple[str, ...]:
    return tuple(p.lower().strip() for p in signals.parents)


def _folder_matches(signals: OriginSignals, words: tuple[str, ...]) -> str | None:
    for part in _lower_parents(signals):
        for word in words:
            if part == word or word in part:
                return part
    return None


def _text_matches(text: str | None, words: tuple[str, ...]) -> str | None:
    if not text:
        return None
    low = text.lower()
    for word in words:
        if word in low:
            return word
    return None


def _is_paper_shaped(signals: OriginSignals) -> bool:
    if not signals.width or not signals.height:
        return False
    long_side, short_side = max(signals.width, signals.height), min(
        signals.width, signals.height
    )
    if short_side <= 0:
        return False
    ratio = long_side / short_side
    return any(abs(ratio - target) <= _PAPER_TOLERANCE for target in _PAPER_RATIOS)


def _is_photo_shaped(signals: OriginSignals) -> bool:
    """Whether these dimensions are a shape photographs also come in.

    Measured on `D:\\Photos`, and it is the single most important
    correction this module got. Before it, an exact match against a screen
    resolution was treated as evidence on its own, and on real data it was
    wrong far more often than right: `download (3).jpg` at 1024x768 was
    called a phone screenshot, because 1024x768 is both an iPad screen and
    the most ordinary 4:3 image size in existence. 113 of the 158
    screenshots that run found were that mistake.

    The uncomfortable consequence, stated rather than hidden: every
    standard *desktop* resolution — 1920x1080, 2560x1440, 1366x768,
    1920x1200, 2880x1800 — is 16:9 or 16:10, and so is a shape photos come
    in. A desktop screenshot therefore cannot be recognised from its pixel
    dimensions at all, only from its name or its folder. Tall phone
    screens (2:1, 19.5:9, 20:9) and ultrawide monitors survive, because
    nothing photographs at 2.2:1.
    """
    if not signals.width or not signals.height:
        return True  # nothing to go on: withhold the weaker verdict
    long_side = max(signals.width, signals.height)
    short_side = min(signals.width, signals.height)
    if short_side <= 0:
        return True
    ratio = long_side / short_side
    return any(abs(ratio - r) <= _PHOTO_RATIO_TOLERANCE for r in _PHOTO_RATIOS)


def _looks_like_a_screen(signals: OriginSignals) -> bool:
    """Exactly the dimensions of a real screen, in a shape no photograph
    comes in. Both halves are required; see `_is_photo_shaped`."""
    size = (signals.width or 0, signals.height or 0)
    on_a_screen = (
        size in _DESKTOP_SCREENS
        or size in _PHONE_SCREENS
        or (size[1], size[0]) in _PHONE_SCREENS
    )
    return on_a_screen and not _is_photo_shaped(signals)


def _is_scroll_capture(signals: OriginSignals) -> bool:
    """A phone's scrolling screenshot: exactly a screen's width, and far
    taller than any screen or any photograph.

    Found on real data — `20200612_135531~2.jpg` at 1160x3289 — where it
    had slipped through as a camera photo because the filename is in the
    camera's own style and the height matches no screen, which is the
    whole point of a scroll capture. The risk this rule takes is a tall
    stitched panorama shot at exactly a screen's pixel width, so it is
    only ever LOW confidence.
    """
    width, height = signals.width or 0, signals.height or 0
    if width <= 0 or height <= 0 or height <= width:
        return False
    screen_widths = {w for w, _ in _PHONE_SCREENS}
    return width in screen_widths and height / width >= 2.5


def _screen_subtype(signals: OriginSignals) -> tuple[OriginClass, str]:
    """Desktop or phone, and the reason. Name first, pixels second.

    The name is checked before the resolution because the capture tools
    are unambiguous about themselves and screens are not: a phone
    screenshot taken in landscape is 1920x1080, which is also the single
    most common desktop resolution on earth. Where only the pixels are
    available, that collision is resolved in favour of the desktop and the
    evidence says so, rather than pretending the question was answered.
    """
    name = signals.name
    if _RE_SCREENSHOT_ANDROID.match(name) or _RE_VIDEO_CAPTURE.match(name):
        return OriginClass.SCREENSHOT_PHONE, "имя в стиле Android"
    if _RE_SCREENSHOT_WINDOWS.match(name) or _RE_SCREENSHOT_MACOS.match(name):
        return OriginClass.SCREENSHOT_DESKTOP, "имя в стиле настольной ОС"

    size = (signals.width or 0, signals.height or 0)
    flipped = (size[1], size[0])
    if size in _DESKTOP_SCREENS:
        return OriginClass.SCREENSHOT_DESKTOP, f"разрешение экрана {size[0]}x{size[1]}"
    if size in _PHONE_SCREENS or flipped in _PHONE_SCREENS:
        return OriginClass.SCREENSHOT_PHONE, f"разрешение экрана телефона {size[0]}x{size[1]}"
    if size[1] > size[0] and size[0] and size[1] / size[0] >= 1.6:
        return OriginClass.SCREENSHOT_PHONE, "вытянутый портретный кадр"
    return OriginClass.SCREENSHOT_DESKTOP, "ориентация и пропорции экрана"


def classify(signals: OriginSignals) -> OriginVerdict:
    """Decide one file's origin from facts already gathered.

    Pure: no I/O, no clock, no globals beyond the vocabularies above. The
    rules are tried in a fixed order and the first match wins, but every
    rule appends to `evidence` whether or not it decided the outcome, so a
    verdict can be argued with rather than merely trusted.

    The order is not arbitrary. Scanner EXIF is checked before camera EXIF
    because a scanner also writes Make/Model and would otherwise be read
    as a camera. Messenger and screenshot names are checked before camera
    EXIF because a messenger strips EXIF, so the two can rarely disagree —
    and where they do (a screenshot filed with a camera's EXIF intact, a
    thing that only happens when someone renamed a file), the name is the
    more deliberate statement.
    """
    evidence: list[str] = []

    if signals.has_camera_exif:
        evidence.append(f"EXIF Make/Model: {signals.device}")
    if signals.exif_software:
        evidence.append(f"EXIF Software: {signals.exif_software}")
    if signals.has_gps:
        evidence.append("EXIF GPS")
    if _RE_EDITED.search(signals.name):
        evidence.append("производное редактора (коллаж/фильтр/правка)")

    sidecar = signals.sidecar
    if sidecar and sidecar.origin_key:
        evidence.append(f"Google sidecar: {sidecar.origin_key}")

    # 1. Scanner — EXIF or scanning-app software string.
    scanner_word = _text_matches(signals.device, _SCANNER_WORDS) or _text_matches(
        signals.exif_software, _SCANNER_WORDS
    )
    if scanner_word:
        evidence.append(f"сканер по EXIF: {scanner_word}")
        return OriginVerdict(OriginClass.DOCUMENT_SCAN, Confidence.HIGH, tuple(evidence))

    # 2. Messenger — folder or the filename its client generates.
    folder = _folder_matches(signals, _MESSENGER_FOLDERS)
    name = signals.name
    name_hit = next(
        (
            label
            for pattern, label in (
                (_RE_WHATSAPP, "имя WhatsApp"),
                (_RE_WHATSAPP_SPELLED, "имя WhatsApp Desktop"),
                (_RE_TELEGRAM_DESKTOP, "имя Telegram"),
                (_RE_VIBER, "имя Viber"),
                (_RE_MESSENGER_RECEIVED, "имя Messenger"),
                (_RE_FB, "имя Facebook"),
            )
            if pattern.match(name)
        ),
        None,
    )
    if folder or name_hit:
        evidence.append(name_hit or f"папка мессенджера: {folder}")
        return OriginVerdict(OriginClass.MESSENGER, Confidence.HIGH, tuple(evidence))

    # 3. Screenshot — the capture tool names its own output, or the folder does.
    screenshot_folder = _folder_matches(signals, _SCREENSHOT_FOLDERS)
    named_capture = bool(
        _RE_SCREENSHOT_GENERIC.match(name) or _RE_VIDEO_CAPTURE.match(name)
    )
    if named_capture or screenshot_folder:
        subtype, why = _screen_subtype(signals)
        evidence.append(
            "имя снимка экрана" if named_capture else f"папка снимков: {screenshot_folder}"
        )
        evidence.append(why)
        # A folder alone is weaker than a name: `Screenshots 1` holds
        # whatever was dropped into it, and one of Aziz's does hold files
        # named only by date.
        confidence = Confidence.HIGH if named_capture else Confidence.MEDIUM
        return OriginVerdict(subtype, confidence, tuple(evidence))

    # 4. Camera — the device wrote its own name into the file.
    if signals.has_camera_exif:
        return OriginVerdict(OriginClass.CAMERA, Confidence.HIGH, tuple(evidence))

    # 5. Document scan — a scanning app's filename or a documents folder,
    #    strengthened (never replaced) by paper proportions.
    document_folder = _folder_matches(signals, _DOCUMENT_FOLDERS)
    document_name = bool(_RE_DOCUMENT_NAME.match(name))
    if document_name or document_folder:
        evidence.append(
            "имя сканирующего приложения" if document_name else f"папка документов: {document_folder}"
        )
        paper = _is_paper_shaped(signals)
        if paper:
            evidence.append("пропорции листа A4/Letter")
        return OriginVerdict(
            OriginClass.DOCUMENT_SCAN,
            Confidence.HIGH if (document_name and paper) else Confidence.MEDIUM,
            tuple(evidence),
        )

    # 6. Web — a downloads folder, a browser's or a CDN's filename, or a
    #    format that in a photo library only ever arrives from a web page.
    #
    #    Ahead of both pixel rules below, for the same reason the camera
    #    filenames used to be: a name someone's software chose beats a
    #    pixel count that merely coincides. On real data this is not a
    #    fine point — 23 files literally named `download (N).jpg` were
    #    being called phone screenshots because 1024x768 is an iPad.
    download_folder = _folder_matches(signals, _DOWNLOAD_FOLDERS)
    web_name = bool(_RE_WEB_NAME.match(PurePath(name).stem))
    if sidecar and sidecar.origin_key == "webUpload":
        evidence.append("Google: загружено через веб")
        return OriginVerdict(OriginClass.WEB_DOWNLOAD, Confidence.MEDIUM, tuple(evidence))
    if download_folder or web_name:
        evidence.append(
            f"папка загрузок: {download_folder}" if download_folder else "имя из браузера"
        )
        return OriginVerdict(OriginClass.WEB_DOWNLOAD, Confidence.MEDIUM, tuple(evidence))

    # 7. Screenshot by pixels — an exact screen size in a shape no
    #    photograph comes in, with no camera EXIF to contradict it.
    #
    #    This sits *above* the camera-filename rule, and the order was
    #    measured rather than reasoned. Putting the filename first (which
    #    is what the first working version did) buried real screenshots:
    #    `20190603_110020.jpg` at exactly 1080x2340 with no EXIF is an
    #    Android screenshot that Google Photos renamed on the way out, and
    #    a camera cannot produce 20:9 by accident. Filename beats a
    #    *coincidental* resolution — 1080x1920 is 16:9 and photos come in
    #    16:9, so `_is_photo_shaped` already withheld that one — but it
    #    does not beat a resolution that only a screen has.
    if _looks_like_a_screen(signals):
        subtype, why = _screen_subtype(signals)
        evidence.append(f"{why}, EXIF камеры нет, пропорции не фотографические")
        return OriginVerdict(subtype, Confidence.MEDIUM, tuple(evidence))

    # 8. Camera by filename — the shapes cameras generate, for files whose
    #    EXIF was stripped on the way through Google Photos (finding A3).
    if _RE_CAMERA_NAME.match(name):
        if _is_scroll_capture(signals):
            evidence.append(
                f"ширина экрана телефона при высоте {signals.height} — скролл-скриншот"
            )
            return OriginVerdict(
                OriginClass.SCREENSHOT_PHONE, Confidence.LOW, tuple(evidence)
            )
        evidence.append("имя в стиле камеры, EXIF вычищен")
        if sidecar and sidecar.origin_key == "mobileUpload":
            return OriginVerdict(OriginClass.CAMERA, Confidence.HIGH, tuple(evidence))
        return OriginVerdict(OriginClass.CAMERA, Confidence.MEDIUM, tuple(evidence))

    if signals.has_gps or (sidecar and sidecar.has_geo):
        evidence.append("геометка без EXIF камеры")
        return OriginVerdict(OriginClass.CAMERA, Confidence.MEDIUM, tuple(evidence))

    if (signals.image_format or "").upper() == "WEBP":
        evidence.append("формат WEBP — в фотоархиве приходит только из веба")
        return OriginVerdict(OriginClass.WEB_DOWNLOAD, Confidence.LOW, tuple(evidence))

    return OriginVerdict(OriginClass.UNKNOWN, Confidence.LOW, tuple(evidence))


# --- gathering the facts ---------------------------------------------------

_EXIF_MAKE = 0x010F
_EXIF_MODEL = 0x0110
_EXIF_SOFTWARE = 0x0131
_EXIF_DATETIME_ORIGINAL = 0x9003
_EXIF_GPS_IFD = 0x8825

_SIDECAR_ORIGIN_KEYS = (
    "mobileUpload", "webUpload", "driveDesktopUploader", "composition",
    "partnerSharing", "sharedAlbum",
)


@lru_cache(maxsize=512)
def _dir_has_sidecars(directory: str) -> bool:
    """Whether this directory holds any `.json` at all.

    One `scandir` per directory in place of three `stat` calls per photo.
    That is not micro-optimisation: the sidecars live in Takeout
    extractions and essentially nowhere else, so in an ordinary photo
    folder all three of those stats are guaranteed misses, and over a
    remote filesystem they tripled the cost of the whole classification
    pass when measured on 30 000 files.

    Cached for the life of the process, and the staleness that buys is
    worth naming: a `.json` written into a directory after it was first
    looked at will not be noticed until the process restarts. The cost of
    that is a *missing* piece of evidence — the file falls back to its
    filename, exactly as it would have before finding A3 — never a wrong
    verdict, because nothing here upgrades a verdict without reading the
    sidecar it claims to have read.
    """
    try:
        with os.scandir(directory) as entries:
            return any(e.name.lower().endswith(".json") for e in entries)
    except OSError:
        return False


def read_google_sidecar(path: Path) -> GoogleSidecar | None:
    """Read a Google Takeout metadata sidecar sitting next to `path`.

    Finding A3: Google often strips EXIF from the JPEG and writes the
    metadata to a neighbouring JSON. Takeout has used several naming
    schemes over the years, hence the candidate list; all of them are
    cheap `exists()` checks on a path we already hold.

    Returns None — never raises — when there is no sidecar or it will not
    parse. A malformed sidecar is missing evidence, not a broken scan.
    """
    if not _dir_has_sidecars(str(path.parent)):
        return None

    candidates = (
        path.with_name(path.name + ".json"),
        path.with_name(path.name + ".supplemental-metadata.json"),
        path.with_suffix(".json"),
    )
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        origin_key = None
        raw_origin = data.get("googlePhotosOrigin")
        if isinstance(raw_origin, dict):
            origin_key = next(
                (k for k in _SIDECAR_ORIGIN_KEYS if k in raw_origin), None
            )
        geo = data.get("geoData") or data.get("geoDataExif") or {}
        has_geo = bool(
            isinstance(geo, dict)
            and (geo.get("latitude") or geo.get("longitude"))
        )
        taken = data.get("photoTakenTime") or {}
        taken_at = None
        if isinstance(taken, dict) and taken.get("timestamp"):
            try:
                taken_at = float(taken["timestamp"])
            except (TypeError, ValueError):
                taken_at = None
        return GoogleSidecar(origin_key=origin_key, has_geo=has_geo, taken_at=taken_at)
    return None


def read_signals(path: Path | str, *, display_path: str | None = None) -> OriginSignals:
    """Open a file's header and collect what `classify` needs.

    Deliberately header-only: `Image.open` parses metadata and dimensions
    without decoding pixels, which measured at about 8 ms per file across
    `D:\\Photos` through the device bridge. That is the entire cost of this
    feature, and it is why origin can be computed for every photo rather
    than only for the ones that turned out to be duplicates — which task
    16 needs, since an event is built from the whole library.

    A file that will not open still yields signals: the path is evidence
    on its own, and a verdict from the name and folder alone is worth more
    than no verdict. Nothing here raises.
    """
    file_path = Path(path)
    shown = display_path or str(path)
    width = height = None
    image_format = make = model = software = None
    has_datetime = has_gps = False

    try:
        with Image.open(file_path) as img:
            width, height = img.size
            image_format = img.format
            exif = img.getexif()
            make = _clean(exif.get(_EXIF_MAKE))
            model = _clean(exif.get(_EXIF_MODEL))
            software = _clean(exif.get(_EXIF_SOFTWARE))
            has_datetime = bool(exif.get(_EXIF_DATETIME_ORIGINAL))
            try:
                gps = exif.get_ifd(_EXIF_GPS_IFD)
            except Exception:  # noqa: BLE001 - malformed EXIF blocks vary wildly
                gps = None
            has_gps = bool(gps)
    except Exception:  # noqa: BLE001 - Pillow raises many unrelated types
        pass

    return OriginSignals(
        path=shown,
        width=width,
        height=height,
        image_format=image_format,
        exif_make=make,
        exif_model=model,
        exif_software=software,
        has_exif_datetime=has_datetime,
        has_gps=has_gps,
        sidecar=read_google_sidecar(file_path),
    )


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    return text or None


def classify_file(path: Path | str, *, display_path: str | None = None) -> OriginVerdict:
    """`read_signals` then `classify` — the whole feature for one file."""
    return classify(read_signals(path, display_path=display_path))
