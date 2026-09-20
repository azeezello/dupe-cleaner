"""Tests for origin classification (Р3, task 15).

Most of these are a table of real filenames and folders taken out of
`D:\\Photos` and the Google takeout, because that is where the rules came
from: every pattern in `origin.py` was written against a name that
actually exists in Aziz's archive, not against an imagined one. A rule
nobody's files match is a rule nobody can check.
"""

from __future__ import annotations

import json

import pytest
from PIL import Image

from dupecleaner.origin import (
    Confidence,
    OriginClass,
    OriginSignals,
    classify,
    classify_file,
    read_google_sidecar,
    read_signals,
)


def sig(path: str, **kwargs) -> OriginSignals:
    return OriginSignals(path=path, **kwargs)


# --- the six classes, on names that exist ---------------------------------


@pytest.mark.parametrize(
    "path, width, height, expected",
    [
        # Android screenshots, with and without the app-name suffix.
        ("D:/Photos/Pictures/Screenshot_20240812_204426_Facebook.jpg", 1080, 2400,
         OriginClass.SCREENSHOT_PHONE),
        ("D:/Photos/Pictures/Screenshot_20190122-181354.jpg", 1080, 1920,
         OriginClass.SCREENSHOT_PHONE),
        # Windows, Russian locale — the name says desktop even though the
        # pixels (1080x1920 rotated) would have said phone.
        ("D:/Photos/Pictures/Screenshots 1/Снимок экрана 2024-12-29 182756.png", 1920, 1080,
         OriginClass.SCREENSHOT_DESKTOP),
        # macOS.
        ("D:/Photos/Pictures/Screen Shot 2019-01-01 at 10.00.00.png", 2880, 1800,
         OriginClass.SCREENSHOT_DESKTOP),
        # WhatsApp's own naming.
        ("D:/Photos/Pictures/IMG-20240101-WA0001.jpg", 1600, 1200, OriginClass.MESSENGER),
        # Telegram Desktop.
        ("D:/Photos/Pictures/photo_12@01-02-2023_10-11-12.jpg", 1280, 960,
         OriginClass.MESSENGER),
        # A scanning app's filename plus paper proportions.
        ("D:/Photos/New Doc 2018-12-20 18.21.33_10.jpg", 2480, 3508,
         OriginClass.DOCUMENT_SCAN),
        ("D:/Photos/Scan_20170705_094138.jpg", 2480, 3508, OriginClass.DOCUMENT_SCAN),
        # A documents folder with an otherwise meaningless filename.
        ("D:/Photos/Документы/1(1).jpg", 2480, 3508, OriginClass.DOCUMENT_SCAN),
        # Browser-generated names.
        ("D:/Photos/Pictures/download (3).jpg", 800, 600, OriginClass.WEB_DOWNLOAD),
        ("D:/Photos/Pictures/unnamed.jpg", 640, 480, OriginClass.WEB_DOWNLOAD),
        # Camera filename shapes present in this archive.
        ("D:/Photos/2010 -2020/2019/20170416_145106.jpg", 4032, 3024, OriginClass.CAMERA),
        ("D:/Photos/Краснодар/IMG_1234.JPG", 4032, 3024, OriginClass.CAMERA),
        ("D:/Photos/Краснодар/_MG_5678.JPG", 5184, 3456, OriginClass.CAMERA),
        ("D:/Photos/Старые фотки/DSC_0042.JPG", 3008, 2000, OriginClass.CAMERA),
        ("D:/Photos/Старые фотки/IMAG0123.jpg", 2592, 1552, OriginClass.CAMERA),
    ],
)
def test_classifies_real_names(path, width, height, expected):
    assert classify(sig(path, width=width, height=height)).origin is expected


def test_camera_exif_beats_everything_ordinary():
    verdict = classify(
        sig("D:/Photos/Pictures/whatever.jpg", width=4000, height=3000,
            exif_make="samsung", exif_model="SM-G991B")
    )
    assert verdict.origin is OriginClass.CAMERA
    assert verdict.confidence is Confidence.HIGH
    assert any("samsung" in e for e in verdict.evidence)


def test_scanner_exif_is_not_read_as_a_camera():
    """A scanner writes Make/Model exactly like a camera does, so the
    camera rule would swallow it if it ran first."""
    verdict = classify(
        sig("D:/Photos/Документы/img001.jpg", width=2480, height=3508,
            exif_make="EPSON", exif_model="Perfection V600")
    )
    assert verdict.origin is OriginClass.DOCUMENT_SCAN
    assert verdict.confidence is Confidence.HIGH


def test_scanning_app_software_tag_counts_as_a_scanner():
    verdict = classify(
        sig("D:/Photos/Pictures/page.jpg", width=2480, height=3508,
            exif_software="CamScanner")
    )
    assert verdict.origin is OriginClass.DOCUMENT_SCAN


def test_messenger_folder_alone_is_enough():
    verdict = classify(sig("D:/Photos/WhatsApp Images/anything.jpg", width=1, height=1))
    assert verdict.origin is OriginClass.MESSENGER
    assert verdict.confidence is Confidence.HIGH


# --- the ordering decisions, each pinned by its own test -------------------


def test_camera_filename_beats_a_coincidental_screen_resolution():
    """The rule this ordering exists for.

    A messenger-resized phone photo lands at exactly 1080x1920, which is
    also a real screen. Without the camera-filename rule running first,
    every such photo would be labelled a screenshot and then excluded from
    its own album by task 16.
    """
    verdict = classify(sig("D:/Photos/Pictures/20170416_145106.jpg", width=1080, height=1920))
    assert verdict.origin is OriginClass.CAMERA
    assert verdict.confidence is Confidence.MEDIUM


def test_a_tall_phone_screen_is_still_recognised_by_pixels_alone():
    """1080x2400 is 20:9. Nothing photographs at 20:9, so the resolution
    is evidence even with nothing else to go on."""
    verdict = classify(sig("D:/Photos/Pictures/a1b2.png", width=1080, height=2400))
    assert verdict.origin is OriginClass.SCREENSHOT_PHONE
    assert verdict.confidence is Confidence.MEDIUM


def test_a_photo_shaped_screen_size_is_not_evidence_on_its_own():
    """The correction that cost this module 113 of its 158 screenshots.

    1024x768 is an iPad screen *and* the most ordinary 4:3 image size
    there is; 1920x1080 is every monitor and also 16:9. On `D:\\Photos`
    the resolution rule fired on 23 files literally named
    `download (N).jpg`. A shape photographs come in is not evidence.
    """
    for width, height in ((1024, 768), (768, 1024), (1920, 1080), (1280, 720)):
        verdict = classify(sig("D:/Photos/Pictures/a1b2.png", width=width, height=height))
        assert not verdict.origin.is_screenshot, (width, height)


def test_a_browser_filename_beats_a_screen_sized_photo():
    verdict = classify(sig("D:/Photos/Pictures/download (3).jpg", width=1024, height=768))
    assert verdict.origin is OriginClass.WEB_DOWNLOAD


def test_a_cdn_filename_is_read_as_a_download():
    """Two real shapes from this archive: Facebook's CDN name, and a bare
    64-hex-digit content address."""
    for name in (
        "327182_10150530365669841_780244840_8471659_749067045_o.jpg",
        "14080d3eb92d6740b4e6e37f26945a54a973e0cb0725a337c65b219f7714e7f3.jpg",
    ):
        verdict = classify(sig(f"D:/Photos/Pictures/{name}", width=1024, height=768))
        assert verdict.origin is OriginClass.WEB_DOWNLOAD, name


def test_a_desktop_screenshot_is_only_findable_by_name_and_that_is_admitted():
    """Every standard desktop resolution is 16:9 or 16:10, which photos
    also are. The name is the only signal left, and the test says so
    rather than leaving the gap to be rediscovered."""
    by_pixels = classify(sig("D:/Photos/Pictures/whatever.png", width=2560, height=1440))
    assert by_pixels.origin is OriginClass.UNKNOWN
    by_name = classify(
        sig("D:/Photos/Pictures/Снимок экрана 2024-12-29 182756.png",
            width=2560, height=1440)
    )
    assert by_name.origin is OriginClass.SCREENSHOT_DESKTOP


def test_a_screenshot_name_outranks_camera_exif():
    """Only happens when someone renamed a file, and then the deliberate
    act is the name, not the leftover EXIF."""
    verdict = classify(
        sig("D:/Photos/Pictures/Screenshot_20240812_204426_Instagram.jpg",
            width=1080, height=2400, exif_make="samsung", exif_model="SM-G991B")
    )
    assert verdict.origin is OriginClass.SCREENSHOT_PHONE


def test_folder_only_screenshot_is_less_confident_than_a_named_one():
    """`Screenshots 1` really does hold files named only by date."""
    named = classify(sig("D:/Photos/Pictures/Screenshots 1/Снимок экрана 2024-12-29 182756.png",
                         width=1920, height=1080))
    folder_only = classify(sig("D:/Photos/Pictures/Screenshots 1/2024-05-06.png",
                               width=1920, height=1080))
    assert named.confidence is Confidence.HIGH
    assert folder_only.confidence is Confidence.MEDIUM
    assert folder_only.origin.is_screenshot


# --- what the verdict is allowed to mean -----------------------------------


def test_screenshots_are_the_ones_excluded_from_albums():
    for cls in (OriginClass.SCREENSHOT_PHONE, OriginClass.SCREENSHOT_DESKTOP):
        assert cls.is_screenshot and cls.excluded_from_albums
    # Р5 gives `_documents/` its own tree, so scans are excluded too.
    assert OriginClass.DOCUMENT_SCAN.excluded_from_albums
    assert not OriginClass.DOCUMENT_SCAN.is_screenshot
    for cls in (OriginClass.CAMERA, OriginClass.MESSENGER,
                OriginClass.WEB_DOWNLOAD, OriginClass.UNKNOWN):
        assert not cls.excluded_from_albums


def test_evidence_is_recorded_even_when_it_did_not_decide():
    """A verdict has to be arguable, not merely trusted."""
    verdict = classify(
        sig("D:/Photos/Pictures/20200101_120000-COLLAGE.jpg", width=2000, height=2000,
            exif_software="Picsart")
    )
    assert any("Picsart" in e for e in verdict.evidence)
    assert any("производное редактора" in e for e in verdict.evidence)


def test_gallery_derivatives_stay_unknown_rather_than_guessing():
    """Р3 has no class for a collage, and inventing one is not this
    task's decision — but the evidence has to say what it saw."""
    verdict = classify(sig("D:/Photos/Pictures/Picsart_24-01-02_10-11-12-345.jpg",
                           width=2000, height=2000))
    assert verdict.origin is OriginClass.UNKNOWN
    assert any("производное редактора" in e for e in verdict.evidence)


def test_unknown_is_returned_rather_than_a_confident_guess():
    verdict = classify(sig("D:/Photos/Pictures/Мда.Ну и рожа1.jpg", width=1234, height=987))
    assert verdict.origin is OriginClass.UNKNOWN
    assert verdict.confidence is Confidence.LOW


# --- finding A3: Google sidecars -------------------------------------------


def _write_sidecar(path, payload):
    path.with_name(path.name + ".json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_google_sidecar_upgrades_a_stripped_camera_file(tmp_path):
    photo = tmp_path / "20240101_224608.jpg"
    Image.new("RGB", (40, 30)).save(photo)
    _write_sidecar(photo, {
        "googlePhotosOrigin": {"mobileUpload": {"deviceType": "ANDROID_PHONE"}},
        "geoData": {"latitude": 41.3, "longitude": 69.2},
        "photoTakenTime": {"timestamp": "1704142000"},
    })

    verdict = classify_file(photo)
    assert verdict.origin is OriginClass.CAMERA
    # Without the sidecar this would be MEDIUM: the EXIF is gone.
    assert verdict.confidence is Confidence.HIGH
    assert any("mobileUpload" in e for e in verdict.evidence)


def test_google_sidecar_web_upload_reads_as_a_web_download(tmp_path):
    photo = tmp_path / "somepicture.jpg"
    Image.new("RGB", (40, 30)).save(photo)
    _write_sidecar(photo, {"googlePhotosOrigin": {"webUpload": {}}})
    assert classify_file(photo).origin is OriginClass.WEB_DOWNLOAD


def test_a_broken_sidecar_is_missing_evidence_not_an_error(tmp_path):
    photo = tmp_path / "20240101_224608.jpg"
    Image.new("RGB", (40, 30)).save(photo)
    photo.with_name(photo.name + ".json").write_text("{not json", encoding="utf-8")
    assert read_google_sidecar(photo) is None
    assert classify_file(photo).origin is OriginClass.CAMERA


# --- reading the file ------------------------------------------------------


def test_read_signals_gets_dimensions_without_decoding(tmp_path):
    photo = tmp_path / "IMG_0001.JPG"
    Image.new("RGB", (640, 480)).save(photo)
    signals = read_signals(photo)
    assert (signals.width, signals.height) == (640, 480)
    assert signals.image_format == "JPEG"


def test_an_unreadable_file_still_gets_a_verdict_from_its_path(tmp_path):
    """A scan must not lose a whole category to one corrupt JPEG, and the
    path is most of the evidence anyway."""
    broken = tmp_path / "Screenshot_20240812_204426_Instagram.jpg"
    broken.write_bytes(b"not an image at all")
    verdict = classify_file(broken)
    assert verdict.origin is OriginClass.SCREENSHOT_PHONE


def test_display_path_is_what_the_rules_see(tmp_path):
    """The file on disk may be a temp copy; the verdict must describe
    where the user's copy lives."""
    photo = tmp_path / "x.jpg"
    Image.new("RGB", (40, 30)).save(photo)
    verdict = classify_file(photo, display_path="D:/Photos/WhatsApp Images/x.jpg")
    assert verdict.origin is OriginClass.MESSENGER


# --- the two pixel-vs-name collisions, measured on real files -------------


def test_an_exact_phone_screen_beats_a_camera_style_filename():
    """`20190603_110020.jpg` at 1080x2340, no EXIF. Real file, real
    mistake: the first working version called it a camera photo because
    the name is in the camera's own style. A camera does not produce 20:9.
    """
    verdict = classify(sig("D:/Photos/Pictures/20190603_110020.jpg",
                           width=1080, height=2340))
    assert verdict.origin is OriginClass.SCREENSHOT_PHONE


def test_a_coincidental_16_9_does_not_beat_a_camera_filename():
    """The other side of the same rule, and why it is `_is_photo_shaped`
    that arbitrates rather than the order alone: 1080x1920 is 16:9, which
    both a screen and a photograph come in, so the name still wins."""
    verdict = classify(sig("D:/Photos/Pictures/20170416_145106.jpg",
                           width=1080, height=1920))
    assert verdict.origin is OriginClass.CAMERA


def test_a_panorama_is_not_mistaken_for_a_screen():
    """5472x2865 is 1.91:1 — not a photo ratio, but not any screen's
    size either, so nothing here should fire."""
    verdict = classify(sig("D:/Photos/2023/IMG_20200102_162506_109.jpg",
                           width=5472, height=2865))
    assert verdict.origin is OriginClass.CAMERA


def test_a_scroll_capture_is_caught_by_width_and_height():
    """`20200612_135531~2.jpg` at 1160x3289: a screen's width, taller
    than any screen. LOW confidence, because a stitched vertical panorama
    could in principle land here too."""
    verdict = classify(sig("D:/Photos/Pictures/20200612_135531~2.jpg",
                           width=1080, height=3289))
    assert verdict.origin is OriginClass.SCREENSHOT_PHONE
    assert verdict.confidence is Confidence.LOW


def test_whatsapp_desktop_spells_its_name_out():
    """Sixteen files in the real folder were sitting in UNKNOWN with
    `WhatsApp` literally in the filename."""
    verdict = classify(
        sig("D:/Photos/Pictures/WhatsApp Image 2024-01-02 at 10.11.12.jpeg",
            width=1600, height=1200)
    )
    assert verdict.origin is OriginClass.MESSENGER
    assert verdict.confidence is Confidence.HIGH
