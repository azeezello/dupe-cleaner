r"""Вкладка похожих на уровне HTTP (задача 14).

Сетка, ручка порога и панель деталей — это JavaScript, который pytest не
видит. Что он видит и чем эта вкладка стоит или падает: одна ручка
`/api/scan/{id}/similar`, которая обязана (а) находить пережатую копию и не
находить чужой снимок на настоящих JPEG, (б) округлять нечётный порог вниз,
(в) честно отвечать «не искали», когда скан был быстрым, и (г) не иметь ни
одного способа что-нибудь переместить.

Снимки рисуются, а не фотографируются, но пересжимаются по-настоящему — тот
же приём и по той же причине, что в `tests/test_similar.py`: пережатие
должно быть настоящим, иначе проверяется ничто. Числа, выбравшие порог,
измерены на библиотеке Азиза и живут в
claude/task-13-threshold-measurements.md; в репозитории их нет и быть не
может.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from dupecleaner.similar_review import DEFAULT_UI_THRESHOLD
from dupecleaner.storage import ScanIndex
from dupecleaner.web import app as web_app


def photo_like(seed: int, size=(1200, 900)) -> Image.Image:
    """Картинка с фотографической структурой: плавная низкочастотная
    вариация — ровно та полоса, которую читает DCT-отпечаток.
    Детерминированная, чтобы падение воспроизводилось."""
    small = Image.new("RGB", (12, 9))
    small.putdata(
        [
            (
                (seed * 37 + i * 29) % 256,
                (seed * 61 + i * 17) % 256,
                (seed * 13 + i * 53) % 256,
            )
            for i in range(12 * 9)
        ]
    )
    return small.resize(size, Image.Resampling.BICUBIC)


@pytest.fixture
def photo_tree(tmp_path: Path) -> Path:
    """Оригинал, его уменьшенная и пережатая копия, и чужой снимок."""
    root = tmp_path / "photos"
    root.mkdir()
    original = photo_like(3)
    original.save(root / "original.jpg", quality=95)
    original.resize((600, 450), Image.Resampling.LANCZOS).save(
        root / "messenger_copy.jpg", quality=55
    )
    photo_like(29).save(root / "another_subject.jpg", quality=95)
    return root


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(web_app, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(web_app, "registry", web_app.ScanRegistry())
    with TestClient(web_app.app) as client:
        yield client


def _scan(client: TestClient, root: Path, mode: str = "full") -> str:
    response = client.post("/api/scan", json={"paths": [str(root)], "mode": mode})
    assert response.status_code == 200, response.text
    scan_id = response.json()["scan_id"]
    job = web_app.registry.get(scan_id)
    assert job is not None
    job.join(timeout=120)
    assert job.progress.status == "done", job.progress.error
    return scan_id


def _similar(client: TestClient, scan_id: str, **params) -> dict:
    response = client.get(f"/api/scan/{scan_id}/similar", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_recompressed_copy_lands_in_a_group_and_a_different_photo_does_not(
    client: TestClient, photo_tree: Path
):
    payload = _similar(client, _scan(client, photo_tree))

    assert payload["phash_available"] is True
    assert payload["threshold"]["max_distance"] == DEFAULT_UI_THRESHOLD
    assert len(payload["groups"]) == 1

    group = payload["groups"][0]
    names = sorted(Path(p).name for m in group["members"] for p in m["paths"])
    assert names == ["messenger_copy.jpg", "original.jpg"]
    assert "another_subject.jpg" not in json.dumps(payload, ensure_ascii=False)


def test_the_group_explains_itself_in_things_a_person_can_check(
    client: TestClient, photo_tree: Path
):
    group = _similar(client, _scan(client, photo_tree))["groups"][0]

    # Опорный — самый тяжёлый файл, то есть оригинал; и это подписано как
    # точка отсчёта, а не как «ту копию, что оставить».
    reference = next(m for m in group["members"] if m["is_reference"])
    assert Path(reference["paths"][0]).name == "original.jpg"

    copy = next(m for m in group["members"] if not m["is_reference"])
    assert copy["distance"] % 2 == 0, "расстояния чётны по построению"
    assert copy["distance"] <= DEFAULT_UI_THRESHOLD
    assert copy["size_ratio"] > 1.0
    assert copy["labels"], "копия без единого объяснения — это не объяснение"
    assert group["kind"] == "copies"
    assert group["spread"] is not None


def test_an_odd_threshold_is_answered_with_the_even_one_below_it(
    client: TestClient, photo_tree: Path
):
    scan_id = _scan(client, photo_tree)
    assert _similar(client, scan_id, max_distance=7)["threshold"]["max_distance"] == 6
    assert _similar(client, scan_id, max_distance=99)["threshold"]["max_distance"] == 16
    assert _similar(client, scan_id, max_distance=0)["threshold"]["max_distance"] == 0


def test_the_handle_actually_changes_the_answer(client: TestClient, photo_tree: Path):
    scan_id = _scan(client, photo_tree)
    zero = _similar(client, scan_id, max_distance=0)
    wide = _similar(client, scan_id, max_distance=16)
    # Порог — не украшение: при нулевом пороге в группу попадают только
    # отпечатки, совпавшие бит в бит, при широком — больше снимков.
    assert zero["summary"]["contents_in_groups"] <= wide["summary"]["contents_in_groups"]
    assert zero["threshold"]["live_groups"] == len(zero["groups"])


def test_quick_mode_says_not_looked_rather_than_not_found(
    client: TestClient, photo_tree: Path
):
    """Р7: отпечатки — часть полной обработки. Пустой список здесь читался бы
    как «похожих нет» — это находка A1 в другом месте."""
    payload = _similar(client, _scan(client, photo_tree, mode="quick"))
    assert payload["mode"] == "quick"
    assert payload["phash_available"] is False
    assert payload["groups"] == []
    assert payload["coverage"]["not_looked"] > 0


def test_coverage_is_reported_so_the_groups_can_be_read_in_proportion(
    client: TestClient, photo_tree: Path
):
    payload = _similar(client, _scan(client, photo_tree))
    coverage = payload["coverage"]
    assert coverage["photos"] == 3
    assert coverage["with_phash"] == 3
    assert coverage["not_looked"] == 0


def test_nothing_in_the_payload_offers_a_way_to_move_a_file(
    client: TestClient, photo_tree: Path
):
    payload = _similar(client, _scan(client, photo_tree))
    blob = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("keeper", "wasted", "decision", "quarantine"):
        assert forbidden not in blob, forbidden


def test_a_similar_group_cannot_be_decided_on_through_the_decision_endpoint(
    client: TestClient, photo_tree: Path
):
    """Решение по группе — ручка задачи 12, и она ищет группу в отчёте о
    точных дублях. У похожей группы там нет и не может быть записи: эти
    снимки не байт-в-байт равны, иначе они были бы на другой вкладке."""
    scan_id = _scan(client, photo_tree)
    payload = _similar(client, scan_id)
    content_hash = payload["groups"][0]["members"][0]["content_hash"]

    response = client.post(
        f"/api/scan/{scan_id}/group/{content_hash}/decision",
        json={"action": "quarantine"},
    )
    assert response.status_code == 404


def test_browsing_similar_photos_does_not_evict_the_duplicate_preview_cache(
    client: TestClient, photo_tree: Path, tmp_path: Path
):
    """`store=0` — не оптимизация, а защита бюджета Р9.

    Снимки, которые нашлись как похожие, в большинстве не попадали в группы
    дублей, поэтому готовой миниатюры у них нет, и `/api/thumbnail`
    декодирует на месте. Запись таких миниатюр в кэш на 512 МБ вытеснила бы
    из него ровно те, за которыми он нужен, — превью групп дублей.
    """
    scan_id = _scan(client, photo_tree)
    payload = _similar(client, scan_id)
    member = payload["groups"][0]["members"][0]
    path, content_hash = member["paths"][0], member["content_hash"]

    with ScanIndex(web_app.DB_PATH) as index:
        assert index.get_thumbnail_meta(content_hash) is None

    response = client.get(
        "/api/thumbnail", params={"path": path, "hash": content_hash, "store": "0"}
    )
    assert response.status_code == 200
    assert response.content[:2] == b"\xff\xd8"  # всё-таки JPEG

    with ScanIndex(web_app.DB_PATH) as index:
        assert index.get_thumbnail_meta(content_hash) is None, "кэш превью засорён"

    # А без флага — кэшируется, как и раньше: флаг сужает поведение, а не
    # меняет его по умолчанию.
    assert (
        client.get(
            "/api/thumbnail", params={"path": path, "hash": content_hash}
        ).status_code
        == 200
    )
    with ScanIndex(web_app.DB_PATH) as index:
        assert index.get_thumbnail_meta(content_hash) is not None


def test_similar_survives_a_lost_report_object(client: TestClient, photo_tree: Path):
    """Отпечатки живут в индексе, а не в объекте отчёта — в отличие от кнопок
    «досчитать» и «сверить», которые перезапуск сервера ломает (см. «Мелочь»
    в plan.md). Вкладка похожих обязана работать и без отчёта."""
    scan_id = _scan(client, photo_tree)
    job = web_app.registry.get(scan_id)
    assert job is not None
    job.report = None

    payload = _similar(client, scan_id)
    assert len(payload["groups"]) == 1


# --- задача 17: лучшая копия доезжает до ручки -----------------------------


def test_the_handle_ranks_the_copies_on_a_real_full_scan(
    client: TestClient, photo_tree: Path
):
    """Сквозная проверка того, что добавила задача 17: метрики, посчитанные
    фазой перцептивных хэшей на настоящих файлах, доезжают до ранжирования
    через индекс. До миграции 12 оба снимка этой группы были бы «разрешение
    не измерялось» — ни один из них не попадает в группу точных дублей,
    поэтому строки превью у них нет и быть не может (кэш Р9 рассчитан на
    группы дублей)."""
    group = _similar(client, _scan(client, photo_tree))["groups"][0]

    block = group["best_copy"]
    assert block["measured"] == 2 and block["unmeasured"] == 0
    assert block["is_choice"] is True

    best = next(m for m in group["members"] if m["content_hash"] == block["best"])
    assert Path(best["paths"][0]).name == "original.jpg"
    # 1200x900 против 600x450 — первая ступень лестницы, и фраза обязана
    # называть именно её.
    assert "выше разрешение" in block["reason"]
    assert block["confident"] is True
    assert best["resolution"] == "1200×900"
    # И числа, по которым это посчитано, лежат рядом с каждой копией.
    assert best["jpeg_quality"] is not None and best["sharpness"] is not None


def test_a_quick_scan_has_no_ranking_rather_than_an_empty_one(
    client: TestClient, photo_tree: Path
):
    """Р7: перцептивные хэши — часть полной обработки, а значит и метрики
    рядом с ними. Вкладка обязана сказать «не искали», а не показать
    группы без порядка."""
    payload = _similar(client, _scan(client, photo_tree, mode="quick"))
    assert payload["phash_available"] is False
    assert payload["groups"] == []


def test_ranking_adds_no_way_to_move_anything(client: TestClient, photo_tree: Path):
    """То же, чем задача 14 держит Р0/Р2, но после задачи 17: подсказка о
    лучшей копии не привезла с собой ни одного объекта, который
    `quarantine.py` умеет принять."""
    payload = _similar(client, _scan(client, photo_tree))
    blob = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("keeper", "wasted", "освободится", "applied_at", "decision"):
        assert forbidden not in blob, forbidden
    # И ни одной ручки действия над похожими не появилось.
    assert client.post(
        f"/api/scan/{payload['scan_id']}/similar", json={}
    ).status_code in (404, 405)
