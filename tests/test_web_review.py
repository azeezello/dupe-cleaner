r"""Веб-слой разбора альбома (задача 24).

Как и `tests/test_web_decisions.py` для задачи 12: сами правила проверены
в `test_album_review.py` и `test_quarantine.py`, здесь — обвязка. Что
стоит прибить гвоздями именно на уровне HTTP:

- экран не привязан к скану вообще: альбомы, метрики и решения берутся из
  индекса по хэшу содержимого, поэтому ручки работают в сессии, где
  `scan` не вызывался ни разу;
- три состояния пишутся и приезжают обратно в очереди;
- «убрать» без подтверждения не двигает ни одного файла;
- «убрать» с подтверждением перемещает — и файл лежит в карантине
  побайтно тем же, каким был;
- пустая библиотека отвечает честным «не собрана», а не пустой сеткой.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dupecleaner.models import FileRecord, MediaKind
from dupecleaner.storage import ScanIndex
from dupecleaner.web import app as web_app

ALBUM_A = r"Library/2018/2018 Novosibirsk"


class _Metrics:
    """Та же минимальная подделка `quality.QualityMetrics`, что в
    `test_storage.py`: шесть полей, которые читает `set_phash`."""

    def __init__(self, w, h, sharp=8.0, recomp=0.1, basis="jpeg_quant_tables", jq=95):
        self.source_width = w
        self.source_height = h
        self.sharpness = sharp
        self.recompression = recomp
        self.recompression_basis = basis
        self.jpeg_quality = jq


@pytest.fixture
def library(tmp_path: Path):
    """Собранная библиотека: настоящие файлы на диске плюс те записи в
    индексе, которые после себя оставляет задача 22.

    Пять снимков в одном альбоме: три с измеренными метриками (один из них
    явно мелкий — он обязан уехать в отдельный фильтр), один без метрик
    вовсе, и один в другой папке, чтобы проверить, что альбом — это папка.
    """
    root = tmp_path / "Library" / "2018"
    album = root / "2018 Novosibirsk"
    album.mkdir(parents=True)
    other = root / "2018 Dushanbe"
    other.mkdir(parents=True)

    plan = [
        (album / "big.jpg", "hbig", _Metrics(4000, 3000)),
        (album / "mid.jpg", "hmid", _Metrics(2000, 1500, sharp=6.0)),
        (album / "tiny.jpg", "htiny", _Metrics(800, 600, sharp=5.0)),
        (album / "unknown.jpg", "hunk", None),
        (other / "elsewhere.jpg", "hels", _Metrics(4000, 3000)),
    ]

    digests: dict[str, str] = {}
    with ScanIndex(tmp_path / "index.db") as index:
        records = []
        for path, content_hash, _ in plan:
            path.write_bytes(b"\xff\xd8\xff" + content_hash.encode() * 500)
            digests[content_hash] = hashlib.sha256(path.read_bytes()).hexdigest()
            records.append(
                FileRecord(
                    display_path=str(path),
                    real_path=str(path),
                    size=path.stat().st_size,
                    mtime=path.stat().st_mtime,
                    media_kind=MediaKind.PHOTO,
                )
            )
        index.upsert_files(records, "scan-1")
        for path, content_hash, metrics in plan:
            index.set_full_hash(str(path), content_hash)
            if metrics is not None:
                index.set_phash(content_hash, "0f0f", 1.0, 1.33, "phash", metrics=metrics)
            index.record_library_move(
                op_id=f"op-{content_hash}",
                content_key=content_hash,
                source=str(tmp_path / "Photos" / path.name),
                destination=str(path),
                album="Новосибирск" if path.parent == album else "Душанбе",
            )
        index.commit()

    return {
        "db": tmp_path / "index.db",
        "album": str(album),
        "other": str(other),
        "paths": {h: p for p, h, _ in plan},
        "digests": digests,
        "quarantine": tmp_path / "Quarantine",
    }


@pytest.fixture
def client(library, monkeypatch):
    monkeypatch.setattr(web_app, "DB_PATH", library["db"])
    monkeypatch.setattr(web_app, "registry", web_app.ScanRegistry())
    with TestClient(web_app.app) as client:
        yield client


@pytest.fixture
def empty_client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(web_app, "DB_PATH", tmp_path / "blank.db")
    monkeypatch.setattr(web_app, "registry", web_app.ScanRegistry())
    with TestClient(web_app.app) as client:
        yield client


# --- альбомы ---------------------------------------------------------------


def test_an_unbuilt_library_says_so_instead_of_showing_nothing(empty_client):
    """Пустая сетка читалась бы как «альбомов нет» — ровно наоборот правде.
    Это та же находка, что у задачи 11 с вкладкой «Обычные файлы»."""
    data = empty_client.get("/api/library/albums").json()
    assert data["albums"] == []
    assert data["photos"] == 0
    # Пороги печати приезжают всё равно: это арифметика, а не данные.
    assert [f["key"] for f in data["formats"]] == ["a4", "a5", "10x15"]


def test_albums_are_folders_with_their_planned_names(client, library):
    data = client.get("/api/library/albums").json()
    folders = {a["folder"]: a for a in data["albums"]}
    assert folders[library["album"]]["photos"] == 4
    assert folders[library["album"]]["name"] == "Новосибирск"
    assert folders[library["other"]]["photos"] == 1
    assert data["photos"] == 5


def test_the_queue_needs_no_scan_in_this_session(client, library):
    """Ни одна ручка разбора не принимает `scan_id`. Отчёт скана не
    переживает перезапуск сервера, а индекс переживает (Р6/Р9/Р10) —
    поэтому разбор можно начать в любой момент."""
    assert web_app.registry.get("whatever") is None
    resp = client.get("/api/library/album", params={"folder": library["album"]})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["summary"]["photos"] == 4


def test_an_unknown_folder_is_a_404(client):
    resp = client.get("/api/library/album", params={"folder": "D:/nope"})
    assert resp.status_code == 404


def test_the_queue_is_sorted_and_the_small_photo_is_filtered_out(client, library):
    payload = client.get(
        "/api/library/album", params={"folder": library["album"]}
    ).json()

    queue = [c["content_hash"] for c in payload["queue"]]
    unfit = [c["content_hash"] for c in payload["unfit"]]

    # 0.48 МП — не хватает и на 10×15, значит отдельный фильтр.
    assert unfit == ["htiny"]
    # Измеренное впереди, неизмеренное в конце очереди — но в очереди.
    assert queue == ["hbig", "hmid", "hunk"]
    assert payload["queue"][-1]["print"]["measured"] is False
    assert "не измерялось" in payload["queue"][-1]["reason"]
    assert payload["unfit"][0]["print"]["unfit"] is True
    assert "10×15" in payload["unfit"][0]["print"]["text"]


def test_every_card_says_why_it_is_where_it_is(client, library):
    payload = client.get(
        "/api/library/album", params={"folder": library["album"]}
    ).json()
    for card in payload["queue"] + payload["unfit"]:
        assert card["reason"]
        assert card["tier_label"]


# --- три состояния ---------------------------------------------------------


@pytest.mark.parametrize("state", ["keep", "print", "drop"])
def test_a_state_is_recorded_and_comes_back_in_the_queue(client, library, state):
    resp = client.post(
        "/api/library/review/state",
        json={"content_hash": "hbig", "path": library["paths"]["hbig"].as_posix(), "state": state},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == state

    payload = client.get(
        "/api/library/album", params={"folder": library["album"]}
    ).json()
    card = next(c for c in payload["queue"] if c["content_hash"] == "hbig")
    assert card["state"] == state
    assert card["state_label"]
    assert card["applied_at"] is None
    assert payload["summary"]["states"][state] == 1


def test_an_invented_state_is_rejected(client):
    resp = client.post(
        "/api/library/review/state",
        json={"content_hash": "hbig", "state": "beautiful"},
    )
    assert resp.status_code == 400
    assert "keep" in resp.text


def test_a_photo_without_a_content_hash_cannot_be_decided(client):
    """Решение, которому некуда лечь по хэшу содержимого, не переживёт
    перескан — а в этом весь смысл Р10. Поэтому отказ явный, а не тихая
    запись по пустому ключу."""
    resp = client.post(
        "/api/library/review/state", json={"content_hash": "", "state": "print"}
    )
    assert resp.status_code == 400
    assert "Р10" in resp.text


def test_undo_clears_only_the_album_axis(client, library):
    client.post(
        "/api/library/review/state", json={"content_hash": "hbig", "state": "drop"}
    )
    with ScanIndex(library["db"]) as index:
        index.record_decision("hbig", "quarantine")

    resp = client.delete(
        "/api/library/review/state", params={"content_hash": "hbig"}
    )
    assert resp.status_code == 200

    with ScanIndex(library["db"]) as index:
        assert index.album_states_for_hashes(["hbig"]) == {}
        assert index.decisions_for_hashes(["hbig"])["hbig"]["action"] == "quarantine"


def test_decisions_survive_a_server_restart(client, library, monkeypatch):
    """Решения живут в индексе, а не в процессе и не в браузере: именно
    поэтому вечер разбора переживает перезапуск (Р10)."""
    client.post(
        "/api/library/review/state", json={"content_hash": "hmid", "state": "print"}
    )
    monkeypatch.setattr(web_app, "registry", web_app.ScanRegistry())
    with TestClient(web_app.app) as fresh:
        payload = fresh.get(
            "/api/library/album", params={"folder": library["album"]}
        ).json()
    card = next(c for c in payload["queue"] if c["content_hash"] == "hmid")
    assert card["state"] == "print"


# --- применение: только в карантин -----------------------------------------


def test_apply_without_confirmation_moves_nothing(client, library):
    client.post(
        "/api/library/review/state", json={"content_hash": "hmid", "state": "drop"}
    )
    resp = client.post(
        "/api/library/review/apply",
        json={"quarantine_dir": str(library["quarantine"]), "confirm": False},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["applied"] == 0
    assert data["queued"] == 1
    assert len(data["pending_media_review"]) == 1
    assert not library["quarantine"].exists()
    assert library["paths"]["hmid"].is_file()
    # Решение осталось в очереди, а не потерялось.
    with ScanIndex(library["db"]) as index:
        assert index.pending_drop_hashes() == ["hmid"]


def test_apply_moves_the_dropped_photo_into_quarantine_byte_for_byte(client, library):
    path = library["paths"]["hmid"]
    client.post(
        "/api/library/review/state", json={"content_hash": "hmid", "state": "drop"}
    )
    data = client.post(
        "/api/library/review/apply",
        json={"quarantine_dir": str(library["quarantine"]), "confirm": True},
    ).json()

    assert data["applied"] == 1
    assert not path.exists()
    moved = Path(data["moved"][0]["quarantined"])
    assert hashlib.sha256(moved.read_bytes()).hexdigest() == library["digests"]["hmid"]

    # Перемещено — значит отмечено перемещённым, и второй вызов не пытается
    # двинуть его снова.
    with ScanIndex(library["db"]) as index:
        assert index.pending_drop_hashes() == []
    again = client.post(
        "/api/library/review/apply",
        json={"quarantine_dir": str(library["quarantine"]), "confirm": True},
    ).json()
    assert again["applied"] == 0


def test_the_restore_screen_brings_a_dropped_photo_back(client, library):
    path = library["paths"]["hmid"]
    client.post(
        "/api/library/review/state", json={"content_hash": "hmid", "state": "drop"}
    )
    client.post(
        "/api/library/review/apply",
        json={"quarantine_dir": str(library["quarantine"]), "confirm": True},
    )
    assert not path.exists()

    # Экран журнала видит перемещение разбора так же, как любое другое:
    # он читает тот же `journal.jsonl`, и новой строчки в нём не понадобилось.
    journal = client.get(
        "/api/quarantine/journal", params={"quarantine_dir": str(library["quarantine"])}
    ).json()
    assert [row["status"] for row in journal["entries"]] == ["moved"]
    assert journal["entries"][0]["group_hash"] == "hmid"

    resp = client.post(
        "/api/quarantine/restore",
        json={"quarantine_dir": str(library["quarantine"])},
    )
    assert resp.status_code == 200, resp.text
    assert path.is_file()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == library["digests"]["hmid"]


def test_only_decided_photos_are_ever_visited(client, library):
    """Непомеченное не двигается, и это свойство итерации: применение
    ходит по решениям, а не по альбому."""
    client.post(
        "/api/library/review/state", json={"content_hash": "hmid", "state": "drop"}
    )
    client.post(
        "/api/library/review/state", json={"content_hash": "hbig", "state": "print"}
    )
    client.post(
        "/api/library/review/apply",
        json={"quarantine_dir": str(library["quarantine"]), "confirm": True},
    )
    assert not library["paths"]["hmid"].exists()
    assert library["paths"]["hbig"].is_file()
    assert library["paths"]["hunk"].is_file()
    assert library["paths"]["htiny"].is_file()


def test_apply_can_be_limited_to_one_album(client, library):
    for content_hash in ("hmid", "hels"):
        client.post(
            "/api/library/review/state",
            json={"content_hash": content_hash, "state": "drop"},
        )
    data = client.post(
        "/api/library/review/apply",
        json={
            "quarantine_dir": str(library["quarantine"]),
            "confirm": True,
            "folder": library["album"],
        },
    ).json()
    assert data["applied"] == 1
    assert not library["paths"]["hmid"].exists()
    assert library["paths"]["hels"].is_file()
    with ScanIndex(library["db"]) as index:
        assert index.pending_drop_hashes() == ["hels"]
