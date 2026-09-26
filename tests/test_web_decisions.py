"""Задача 12's web layer: per-group decisions, the batch apply, screen 5's
archive-quarantine button, and the journal/restore screen.

Each of these is a thin wrapper over already-tested pure functions
(`keeper.keeper_reason`, `storage.ScanIndex.record_decision`,
`quarantine.quarantine_reviewed_groups`, `quarantine.journal_summary`) --
what's worth pinning down at the HTTP layer is the wiring itself: status
codes, which fields round-trip, and the two safety checks that live only
in the endpoint (rejecting an archive member as a keeper override,
refusing a decision on an archive-only group).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dupecleaner.web import app as web_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(web_app, "DB_PATH", tmp_path / "index.db")
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
    return scan_id, result.json()


def _leaf(record: dict) -> str:
    path = record["display_path"]
    return Path(path.split("::")[-1]).name


def _find_group(report: dict, *leaf_names: str) -> dict:
    wanted = set(leaf_names)
    for g in report["groups"]:
        if {_leaf(r) for r in g["records"]} == wanted:
            return g
    raise AssertionError(f"no group with leaves {wanted}")


def test_deciding_a_plain_group_then_reading_it_back(client: TestClient, tmp_tree: Path):
    scan_id, report = _scan(client, tmp_tree, mode="full")
    group = _find_group(report, "a1.txt", "a2.txt", "a_copy.txt")

    resp = client.post(
        f"/api/scan/{scan_id}/group/{group['content_hash']}/decision",
        json={"action": "quarantine"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "quarantine"

    report = client.get(f"/api/scan/{scan_id}/result").json()
    group = _find_group(report, "a1.txt", "a2.txt", "a_copy.txt")
    assert group["decision"]["action"] == "quarantine"
    assert group["decision"]["applied_at"] is None


def test_an_invalid_action_is_rejected(client: TestClient, tmp_tree: Path):
    scan_id, report = _scan(client, tmp_tree, mode="full")
    group = _find_group(report, "a1.txt", "a2.txt", "a_copy.txt")
    resp = client.post(
        f"/api/scan/{scan_id}/group/{group['content_hash']}/decision",
        json={"action": "delete-everything"},
    )
    assert resp.status_code == 400


def test_a_keeper_path_outside_the_group_is_rejected(client: TestClient, tmp_tree: Path):
    scan_id, report = _scan(client, tmp_tree, mode="full")
    group = _find_group(report, "a1.txt", "a2.txt", "a_copy.txt")
    resp = client.post(
        f"/api/scan/{scan_id}/group/{group['content_hash']}/decision",
        json={"action": "quarantine", "keeper_path": r"D:\nowhere\near\this\group.jpg"},
    )
    assert resp.status_code == 400


def test_an_archive_member_cannot_be_chosen_as_keeper(client: TestClient, tmp_tree: Path):
    """Р8 already ranks an archive member last because it can never be
    the thing that moves out of the archive -- a human override must not
    be able to strand every plain copy behind it."""
    scan_id, report = _scan(client, tmp_tree, mode="full")
    group = _find_group(report, "a1.txt", "a2.txt", "a_copy.txt")
    archive_member = next(r for r in group["records"] if r["is_archive_member"])

    resp = client.post(
        f"/api/scan/{scan_id}/group/{group['content_hash']}/decision",
        json={"action": "quarantine", "keeper_path": archive_member["display_path"]},
    )
    assert resp.status_code == 400


def test_archive_only_groups_have_no_decision_of_their_own(client: TestClient, tmp_tree: Path):
    scan_id, report = _scan(client, tmp_tree, mode="full")
    archive_only = next(g for g in report["groups"] if g["only_archive_members"])
    resp = client.post(
        f"/api/scan/{scan_id}/group/{archive_only['content_hash']}/decision",
        json={"action": "quarantine"},
    )
    assert resp.status_code == 400


def test_clearing_a_decision_undoes_it(client: TestClient, tmp_tree: Path):
    scan_id, report = _scan(client, tmp_tree, mode="full")
    group = _find_group(report, "a1.txt", "a2.txt", "a_copy.txt")
    content_hash = group["content_hash"]

    client.post(f"/api/scan/{scan_id}/group/{content_hash}/decision", json={"action": "keep"})
    resp = client.delete(f"/api/scan/{scan_id}/group/{content_hash}/decision")
    assert resp.status_code == 200

    report = client.get(f"/api/scan/{scan_id}/result").json()
    group = _find_group(report, "a1.txt", "a2.txt", "a_copy.txt")
    assert group["decision"] is None


def test_apply_decisions_moves_only_the_queued_groups(client: TestClient, tmp_tree: Path, tmp_path: Path):
    """P1.1, at the HTTP layer: the gap between nothing and everything."""
    scan_id, report = _scan(client, tmp_tree, mode="full")
    plain_group = _find_group(report, "a1.txt", "a2.txt", "a_copy.txt")

    client.post(
        f"/api/scan/{scan_id}/group/{plain_group['content_hash']}/decision",
        json={"action": "quarantine"},
    )

    quarantine_dir = tmp_path / "qdir"
    resp = client.post(
        f"/api/scan/{scan_id}/apply-decisions",
        json={"quarantine_dir": str(quarantine_dir), "confirm_media": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] == 1
    assert len(body["moved"]) == 1
    # The media group was never decided on, so it must be untouched.
    assert (tmp_tree / "photo1.jpg").exists()
    assert (tmp_tree / "photo2.jpg").exists()

    report = client.get(f"/api/scan/{scan_id}/result").json()
    plain_group = _find_group(report, "a1.txt", "a2.txt", "a_copy.txt")
    assert plain_group["decision"]["applied_at"] is not None


def test_apply_decisions_defers_media_until_confirmed(client: TestClient, tmp_tree: Path, tmp_path: Path):
    scan_id, report = _scan(client, tmp_tree, mode="full")
    media_group = _find_group(report, "photo1.jpg", "photo2.jpg")
    client.post(
        f"/api/scan/{scan_id}/group/{media_group['content_hash']}/decision",
        json={"action": "quarantine"},
    )

    quarantine_dir = tmp_path / "qdir"
    first = client.post(
        f"/api/scan/{scan_id}/apply-decisions",
        json={"quarantine_dir": str(quarantine_dir), "confirm_media": False},
    ).json()
    assert first["applied"] == 0
    assert len(first["pending_media_review"]) == 1
    assert (tmp_tree / "photo1.jpg").exists()

    second = client.post(
        f"/api/scan/{scan_id}/apply-decisions",
        json={"quarantine_dir": str(quarantine_dir), "confirm_media": True},
    ).json()
    assert second["applied"] == 1


def test_journal_and_restore_endpoints_wrap_the_same_journal(
    client: TestClient, tmp_tree: Path, tmp_path: Path
):
    scan_id, report = _scan(client, tmp_tree, mode="full")
    plain_group = _find_group(report, "a1.txt", "a2.txt", "a_copy.txt")
    client.post(
        f"/api/scan/{scan_id}/group/{plain_group['content_hash']}/decision",
        json={"action": "quarantine"},
    )
    quarantine_dir = tmp_path / "qdir"
    client.post(
        f"/api/scan/{scan_id}/apply-decisions",
        json={"quarantine_dir": str(quarantine_dir), "confirm_media": True},
    )

    journal = client.get("/api/quarantine/journal", params={"quarantine_dir": str(quarantine_dir)}).json()
    assert len(journal["entries"]) == 1
    assert journal["entries"][0]["status"] == "moved"

    restore = client.post(
        "/api/quarantine/restore", json={"quarantine_dir": str(quarantine_dir)}
    )
    assert restore.status_code == 200

    journal_after = client.get(
        "/api/quarantine/journal", params={"quarantine_dir": str(quarantine_dir)}
    ).json()
    assert journal_after["entries"][0]["status"] == "restored"


def test_archive_quarantine_refuses_a_quick_mode_report(client: TestClient, archive_tree: Path, tmp_path: Path):
    """Задача 7: a quick-mode report never opened an archive, so every
    verdict in it is UNREAD -- screen 5 must show no actionable archive at
    all, and the server must refuse the request too, not just hide the
    button."""
    scan_id, report = _scan(client, archive_tree, mode="quick")
    resp = client.post(
        f"/api/scan/{scan_id}/archive-quarantine",
        json={
            "quarantine_dir": str(tmp_path / "qdir"),
            "archive_path": str(archive_tree / "fully.zip"),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["refused"]) == 1
    assert body["moved"] == []


def test_archive_quarantine_moves_a_fully_redundant_archive_in_full_mode(
    client: TestClient, archive_tree: Path, tmp_path: Path
):
    scan_id, report = _scan(client, archive_tree, mode="full")
    resp = client.post(
        f"/api/scan/{scan_id}/archive-quarantine",
        json={
            "quarantine_dir": str(tmp_path / "qdir"),
            "archive_path": str(archive_tree / "fully.zip"),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["moved"]) == 1
    assert not (archive_tree / "fully.zip").exists()
