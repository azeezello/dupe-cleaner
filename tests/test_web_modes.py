"""The two buttons Р7 asks for, at the HTTP layer.

«Досчитать полностью» and «сверить полностью» are the parts of task 7 that
exist only as endpoints plus a few lines of JavaScript, which makes them the
parts most likely to rot unnoticed: nothing else imports them, so a rename
elsewhere breaks a button and no test complains.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dupecleaner.web import app as web_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(web_app, "DB_PATH", tmp_path / "index.db")
    # A fresh registry per test: it is module state, and a scan id leaking
    # between tests would make failures depend on ordering.
    monkeypatch.setattr(web_app, "registry", web_app.ScanRegistry())
    with TestClient(web_app.app) as client:
        yield client


def _finish(scan_id: str) -> None:
    job = web_app.registry.get(scan_id)
    assert job is not None
    job.join(timeout=60)
    assert job.progress.status == "done", job.progress.error


def _scan(client: TestClient, root: Path, **body) -> dict:
    response = client.post("/api/scan", json={"paths": [str(root)], **body})
    assert response.status_code == 200, response.text
    scan_id = response.json()["scan_id"]
    _finish(scan_id)
    result = client.get(f"/api/scan/{scan_id}/result")
    assert result.status_code == 200, result.text
    return result.json()


def test_scan_screen_defaults_to_quick_and_says_what_it_skipped(
    client: TestClient, tmp_tree: Path
):
    report = _scan(client, tmp_tree)

    assert report["mode"] == "quick"
    # The banner's numbers come straight from here rather than being
    # re-derived in JavaScript from the list.
    assert report["skipped_by_mode_count"] == 2
    assert report["skipped_by_mode_bytes"] > 0


def test_full_mode_can_be_asked_for_explicitly(client: TestClient, tmp_tree: Path):
    report = _scan(client, tmp_tree, mode="full")
    assert report["mode"] == "full"
    assert report["skipped_by_mode_count"] == 0


def test_upgrade_starts_a_full_run_that_reuses_the_index(
    client: TestClient, tmp_tree: Path
):
    quick = _scan(client, tmp_tree)
    assert quick["mode"] == "quick"

    response = client.post(f"/api/scan/{quick['scan_id']}/upgrade")
    assert response.status_code == 200, response.text
    upgraded_id = response.json()["scan_id"]
    assert upgraded_id != quick["scan_id"]
    assert response.json()["mode"] == "full"

    _finish(upgraded_id)
    full = client.get(f"/api/scan/{upgraded_id}/result").json()

    assert full["mode"] == "full"
    assert full["skipped_by_mode_count"] == 0
    assert len(full["groups"]) > len(quick["groups"])
    # Only what the quick run never opened had to be read.
    assert web_app.registry.get(upgraded_id).progress.files_from_cache > 0

    # The quick answer is not thrown away to produce the full one — a
    # cancelled upgrade must leave something behind.
    assert client.get(f"/api/scan/{quick['scan_id']}/result").json()["mode"] == "quick"


def test_upgrading_a_full_scan_is_refused(client: TestClient, tmp_tree: Path):
    full = _scan(client, tmp_tree, mode="full")
    response = client.post(f"/api/scan/{full['scan_id']}/upgrade")
    assert response.status_code == 409


def test_verify_one_group_confirms_it(client: TestClient, tmp_tree: Path):
    report = _scan(client, tmp_tree)
    group = next(g for g in report["groups"] if not g["has_archive_members"])

    response = client.post(
        f"/api/scan/{report['scan_id']}/group/{group['content_hash']}/verify"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["confirmed"] == len(group["records"])


def test_verify_reports_a_copy_that_changed_since_the_scan(
    client: TestClient, tmp_tree: Path
):
    report = _scan(client, tmp_tree)
    group = next(g for g in report["groups"] if not g["has_archive_members"])
    Path(group["records"][0]["real_path"]).write_bytes(b"changed under our feet")

    body = client.post(
        f"/api/scan/{report['scan_id']}/group/{group['content_hash']}/verify"
    ).json()

    assert body["ok"] is False
    assert any(not check["ok"] for check in body["checked"])


def test_verify_unknown_group_is_404(client: TestClient, tmp_tree: Path):
    report = _scan(client, tmp_tree)
    response = client.post(f"/api/scan/{report['scan_id']}/group/deadbeef/verify")
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Task 9: the metrics reach the interface without it reopening any file
# --------------------------------------------------------------------------


def test_result_carries_quality_metrics_for_photo_groups(
    client: TestClient, tmp_path: Path
):
    """«доступны интерфейсу без дочитывания файла» — the acceptance line of
    task 9. One object per group, since every copy in a group is
    byte-identical and therefore shares one measurement.
    """
    from PIL import Image

    root = tmp_path / "photos"
    root.mkdir()
    Image.new("RGB", (900, 600), (180, 40, 90)).save(root / "shot.jpg", quality=92)
    (root / "copy.jpg").write_bytes((root / "shot.jpg").read_bytes())

    result = _scan(client, root, mode="full")
    group = next(g for g in result["groups"] if len(g["records"]) == 2)

    quality = group["quality"]
    assert quality is not None
    assert (quality["source_width"], quality["source_height"]) == (900, 600)
    assert quality["sharpness"] is not None
    assert quality["recompression_basis"] == "jpeg_quant_tables"
    assert quality["jpeg_quality"] == pytest.approx(92, abs=3)


def test_quick_mode_result_says_null_rather_than_zero(client: TestClient, tmp_path: Path):
    """A quick run measured nothing. Reporting a sharpness of 0 for it
    would be a number the scan never established — UX-BRIEF's «честность в
    цифрах» applied to metrics instead of progress.
    """
    from PIL import Image

    root = tmp_path / "photos"
    root.mkdir()
    Image.new("RGB", (640, 480), (20, 160, 200)).save(root / "shot.jpg", quality=88)
    (root / "copy.jpg").write_bytes((root / "shot.jpg").read_bytes())

    result = _scan(client, root, mode="quick")
    group = next(g for g in result["groups"] if len(g["records"]) == 2)
    assert group["quality"] is None
