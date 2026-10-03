r"""Задача 24: разбор альбома по одному снимку.

Что здесь проверяется и почему именно это:

- три состояния ставятся и читаются обратно — и живут **по хэшу
  содержимого**, то есть переживают смену пути, перескан и поездку в
  карантин (это и есть обещание Р10, и оно единственное, из-за которого
  решения не хранятся в отчёте или в браузере);
- «убрать» **перемещает**, а не удаляет: на настоящих файлах, с
  побайтной сверкой того, что оказалось в карантине, и с возвратом через
  уже готовый `restore_from_journal`;
- порог печати — арифметика: он считается из миллиметров и dpi, называет
  формат и не превращает «не измерялось» в «не годится»;
- порядок очереди устойчив между запусками;
- модуль разбора не импортирует ничего, что умеет удалять, — и это
  проверяется по исходнику, а не на доверии.
"""

from __future__ import annotations

import ast
import hashlib
import random
from pathlib import Path

import pytest

from dupecleaner import album_review as ar
from dupecleaner.storage import ScanIndex


# --- печать: деление, а не мнение ------------------------------------------


def test_print_thresholds_are_computed_from_millimetres_and_dpi():
    """«Нужно 8.7 МП на A4» должно быть одним и тем же делением в тесте и
    на экране, иначе порог станет магическим числом, которое однажды
    разойдётся с подписью под ним."""
    assert ar.A4.pixels == (round(210 / 25.4 * 300), round(297 / 25.4 * 300))
    assert ar.A4.megapixels == 8.7
    assert ar.A5.megapixels == 4.34
    assert ar.PHOTO_10X15.megapixels == 2.09
    # От большего к меньшему — на этом порядке держится и «самый большой
    # формат, на который хватает», и «не хватает даже на самый мелкий».
    assert [f.megapixels for f in ar.PRINT_FORMATS] == sorted(
        (f.megapixels for f in ar.PRINT_FORMATS), reverse=True
    )
    assert ar.SMALLEST_FORMAT is ar.PHOTO_10X15


@pytest.mark.parametrize(
    "megapixels, largest, unfit",
    [
        (12.0, "a4", False),
        (8.7, "a4", False),      # ровно порог — хватает
        (8.69, "a5", False),     # на волос меньше — уже не A4
        (4.34, "a5", False),
        (2.09, "10x15", False),
        (2.08, None, True),
        (0.3, None, True),
    ],
)
def test_the_named_format_is_the_largest_one_the_resolution_reaches(
    megapixels, largest, unfit
):
    fit = ar.print_fitness({"megapixels": megapixels})
    assert fit.measured
    assert (fit.largest.key if fit.largest else None) == largest
    assert fit.unfit is unfit
    if largest:
        assert fit.largest.label in fit.text
        assert "300 dpi" in fit.text
    else:
        # Фильтр обязан называть формат и требуемое число, иначе это
        # «плохой снимок», а не «на таком формате напечатается плохо».
        assert ar.SMALLEST_FORMAT.label in fit.text
        assert "2.09 МП" in fit.text


def test_unmeasured_resolution_says_so_and_is_never_called_unfit():
    """«Не смотрели» не становится «посмотрели и мелко». Иначе фильтр
    выбрасывал бы снимки за то, что до них не дошла полная обработка."""
    for quality in (None, {}, {"sharpness": 4.0}):
        fit = ar.print_fitness(quality)
        assert fit.measured is False
        assert fit.unfit is False
        assert fit.megapixels is None
        assert "не измерялось" in fit.text


def test_megapixels_come_from_the_pixel_counts_when_not_precomputed():
    assert ar.megapixels_of({"source_width": 4000, "source_height": 3000}) == 12.0
    assert ar.megapixels_of({"megapixels": 7.5}) == 7.5
    assert ar.megapixels_of({"source_width": 4000}) is None


def test_file_size_is_never_substituted_for_resolution():
    """Названная граница: вес файла — другое число про другое. Тяжёлый
    файл без измеренного разрешения остаётся «не измерялось»."""
    card = ar.build_album_queue(
        [{"path": "D:/L/a.jpg", "content_hash": "h", "size": 40_000_000}],
        album="D:/L",
    ).queue[0]
    assert card.megapixels is None
    assert card.print_fit.measured is False
    assert "не измерялось" in card.reason


def test_the_recompression_threshold_is_pinned_to_a_jpeg_quality():
    """`RECOMPRESSION_CLEAN` объявлен как «качество ≈71». Шкала живёт в
    `quality.py`, порог — в `album_review`, и разъехаться они не должны:
    это ровно тот сорт расхождения, который находка P1.5 запретила
    повторять."""
    from dupecleaner.quality import _QUALITY_CLEAN, _QUALITY_SQUEEZED

    expected = (_QUALITY_CLEAN - 71) / (_QUALITY_CLEAN - _QUALITY_SQUEEZED)
    assert round(expected, 2) == ar.RECOMPRESSION_CLEAN


# --- альбом как папка -------------------------------------------------------


def test_folder_keeps_the_separator_the_path_came_with():
    r"""План строится на Windows и может читаться на Linux: разделитель —
    свойство пути, а не машины (`library.join_path`). Иначе альбом,
    прочитанный из индекса, не совпал бы сам с собой при втором запросе."""
    assert ar.folder_of(r"D:\Library\2018\2018 Novosibirsk\IMG_1.jpg") == (
        r"D:\Library\2018\2018 Novosibirsk"
    )
    assert ar.folder_of("/srv/lib/2018/a.jpg") == "/srv/lib/2018"
    assert ar.basename_of(r"D:\Library\2018\a.jpg") == "a.jpg"


def test_albums_are_folders_and_take_their_name_from_the_plan():
    rows = [
        {"destination": r"D:\L\2018\2018 Novosibirsk\a.jpg", "album": "Новосибирск", "content_key": "h1"},
        {"destination": r"D:\L\2018\2018 Novosibirsk\b.jpg", "album": "Новосибирск", "content_key": "h2"},
        {"destination": r"D:\L\_unsorted\c.jpg", "album": "", "content_key": "h3"},
    ]
    albums = ar.group_albums(rows)
    assert [(a.folder, a.name, a.photos) for a in albums] == [
        (r"D:\L\2018\2018 Novosibirsk", "Новосибирск", 2),
        (r"D:\L\_unsorted", "_unsorted", 1),
    ]


def test_one_canonical_path_wins_when_the_journal_remembers_it_twice():
    """Р5: один файл — один канонический путь. Журнал может помнить
    прогон, откат и повторный прогон в то же место."""
    rows = [
        {"destination": r"D:\L\2018\a.jpg", "album": "A", "content_key": "old", "size": 1},
        {"destination": r"D:\L\2018\a.jpg", "album": "A", "content_key": "new", "size": 2},
    ]
    photos = ar.photos_in_album(rows, r"D:\L\2018")
    assert [p["content_hash"] for p in photos] == ["new"]


# --- очередь: порядок, причина, фильтр -------------------------------------


def _quality(mp: float, sharpness: float, recompression: float, quality: int = 95) -> dict:
    side = int((mp * 1_000_000 / 0.75) ** 0.5)
    return {
        "source_width": side,
        "source_height": int(side * 0.75),
        "sharpness": sharpness,
        "recompression": recompression,
        "recompression_basis": "jpeg_quant_tables",
        "jpeg_quality": quality,
    }


def _album(n: int = 6) -> tuple[list[dict], dict]:
    photos, quality = [], {}
    for i in range(n):
        h = f"h{i}"
        photos.append({"path": rf"D:\L\2018\p{i}.jpg", "content_hash": h, "size": 1000 + i})
        quality[h] = _quality(12.0, 5.0 + i, 0.1)
    return photos, quality


def test_the_three_signs_and_a_prominent_face_come_first():
    r"""Ступень важнее числа внутри ступени: снимок с крупным лицом
    обходит более резкие, но ступень он получает только если прошёл все
    три технических признака. Мягкий кадр с лицом в первую ступень не
    попадает — «есть все три признака **и** лица», а не «или»."""
    sharpness = {"h0": 7.0, "h1": 5.0, "h2": 5.5, "h3": 7.0, "h4": 8.0, "h5": 9.0}
    photos = [
        {"path": rf"D:\L\2018\p{i}.jpg", "content_hash": f"h{i}", "size": 1000 + i}
        for i in range(6)
    ]
    quality = {h: _quality(12.0, value, 0.1) for h, value in sharpness.items()}
    faces = {
        # Крупное лицо — 200 px при масштабе 1024, то есть 19.5% длинной стороны.
        "h0": {"scanned": True, "detect_long_side": 1024, "faces": 1, "face_long_sides": [200]},
        # Лицо есть, но мелкое: 20 px это 2% кадра.
        "h5": {"scanned": True, "detect_long_side": 1024, "faces": 1, "face_long_sides": [20]},
    }
    queue = ar.build_album_queue(
        photos, album=r"D:\L\2018", quality=quality, faces=faces
    ).queue

    assert queue[0].content_hash == "h0"
    assert queue[0].tier == ar.TIER_READY
    assert "крупное лицо" in queue[0].reason
    # h5 резче всех, но его лицо мелкое — вторая ступень, не первая.
    by_hash = {c.content_hash: c for c in queue}
    assert by_hash["h5"].tier == ar.TIER_MEASURED
    assert by_hash["h5"].prominent_faces == 0
    assert by_hash["h5"].faces == 1
    # Мягче медианы (7.0) более чем на четверть — вниз, независимо от лиц.
    assert by_hash["h1"].tier == ar.TIER_PARTIAL
    assert by_hash["h2"].tier == ar.TIER_PARTIAL
    assert [c.content_hash for c in queue].index("h0") == 0


def test_a_face_too_small_in_the_frame_is_not_a_prominent_face():
    row = {"scanned": True, "detect_long_side": 1024, "faces": 3,
           "face_long_sides": [200, 81, 80]}
    # 8% от 1024 это 81.92 — ровно та граница, которую порог называет.
    assert ar.prominent_faces(row) == 1
    assert ar.prominent_faces({"scanned": True, "detect_long_side": 0,
                               "faces": 1, "face_long_sides": [900]}) == 0
    assert ar.prominent_faces(None) == 0


def test_recompression_traces_push_a_photo_down_and_the_reason_says_why():
    photos, quality = _album(4)
    quality["h2"] = _quality(12.0, 9.0, 0.8, quality=55)
    queue = ar.build_album_queue(photos, album=r"D:\L\2018", quality=quality).queue
    squeezed = next(c for c in queue if c.content_hash == "h2")
    assert squeezed.tier == ar.TIER_PARTIAL
    assert "качеством ≈55" in squeezed.reason
    assert queue[-1].content_hash == "h2"


def test_sharpness_is_compared_with_the_album_not_with_an_absolute_threshold():
    photos, quality = _album(5)
    quality["h0"] = _quality(12.0, 1.0, 0.1)
    queue = ar.build_album_queue(photos, album=r"D:\L\2018", quality=quality)
    soft = next(c for c in queue.queue if c.content_hash == "h0")
    assert soft.tier == ar.TIER_PARTIAL
    assert "мягче альбома более чем на четверть" in soft.reason
    assert queue.sharpness_reference is not None


def test_a_sharpness_difference_inside_the_tolerance_is_not_softness():
    """Найдено прогоном на настоящем конвейере, а не придумано: без
    допуска снимок 3000×2250 q95 уходил из первой ступени из-за 2.7%
    разницы с медианой и оказывался позади снимка 2000×1500 — разрешение,
    единственный факт из трёх (Р2), проигрывало шуму самой шумной оценки."""
    photos, quality = _album(5)
    reference_sample = [5.0, 5.2, 5.4, 5.6, 5.8]
    for (photo, value) in zip(photos, reference_sample):
        quality[photo["content_hash"]] = _quality(12.0, value, 0.1)
    queue = ar.build_album_queue(photos, album="D:/L", quality=quality)
    assert queue.sharpness_reference == 5.4
    # 5.0 против медианы 5.4 — разница 7%, внутри допуска: ступень не теряется.
    assert all(c.tier == ar.TIER_MEASURED for c in queue.queue)
    assert "резкость в пределах альбома" in queue.queue[0].reason

    # А вот вдвое мягче — уже мягче.
    quality[photos[0]["content_hash"]] = _quality(12.0, 2.0, 0.1)
    queue = ar.build_album_queue(photos, album="D:/L", quality=quality)
    soft = next(c for c in queue.queue if c.content_hash == photos[0]["content_hash"])
    assert soft.tier == ar.TIER_PARTIAL


def test_the_sharpness_tolerance_matches_the_one_task_17_measured():
    """Общего кода с `best_copy` нет намеренно (на это есть тест в пункте
    17), но число одно: это одна и та же метрика с той же погрешностью, и
    разъехаться они не должны молча."""
    from dupecleaner.best_copy import SHARPNESS_TIE_RATIO

    assert ar.SHARPNESS_SOFT_RATIO == SHARPNESS_TIE_RATIO


def test_too_few_measured_photos_means_no_sharpness_reference_at_all():
    """«Резче половины альбома» из двух снимков означало бы «резче, чем
    другой» — это уже не про альбом. Аудит пункта 17 намерил, насколько
    резкость шумная; выдумывать из неё точку отсчёта на двух снимках
    нельзя."""
    photos, quality = _album(2)
    queue = ar.build_album_queue(photos, album=r"D:\L\2018", quality=quality)
    assert queue.sharpness_reference is None
    assert all(c.tier == ar.TIER_MEASURED for c in queue.queue)
    assert "сравнивать не с чем" in queue.queue[0].reason


def test_unfit_photos_live_in_their_own_list_not_at_the_end_of_the_queue():
    photos, quality = _album(3)
    quality["h1"] = _quality(1.0, 7.0, 0.1)
    queue = ar.build_album_queue(photos, album=r"D:\L\2018", quality=quality)
    assert [c.content_hash for c in queue.unfit] == ["h1"]
    assert "h1" not in [c.content_hash for c in queue.queue]
    assert queue.summary()["unfit"] == 1
    assert queue.summary()["queue"] == 2
    # Снимок из фильтра всё равно получает три состояния: «мелко для
    # печати» не значит «убрать».
    assert queue.unfit[0].state is None


def test_unmeasured_photos_stay_in_the_queue_at_the_end():
    photos, quality = _album(3)
    del quality["h1"]
    queue = ar.build_album_queue(photos, album=r"D:\L\2018", quality=quality).queue
    assert [c.content_hash for c in queue][-1] == "h1"
    assert queue[-1].tier == ar.TIER_UNMEASURED
    assert "не измерялось" in queue[-1].reason
    assert "разрешение" in queue[-1].reason


def test_every_card_carries_one_line_of_why():
    photos, quality = _album(5)
    quality["h4"] = _quality(1.0, 3.0, 0.1)
    del quality["h3"]
    queue = ar.build_album_queue(photos, album=r"D:\L\2018", quality=quality)
    for card in queue.cards:
        assert card.reason
        assert "\n" not in card.reason
        assert card.reason_kind in ("ready", "measured", "partial", "unmeasured", "unfit")


def test_queue_order_is_stable_between_runs():
    """Одни данные в любом порядке — один порядок на выходе. Ключ
    сортировки заканчивается хэшем содержимого, а не позицией во входе."""
    photos, quality = _album(12)
    quality["h5"] = _quality(12.0, 5.0, 0.1)  # ничья по всем трём числам
    quality["h7"] = _quality(12.0, 5.0, 0.1)
    first = [
        c.content_hash
        for c in ar.build_album_queue(photos, album="D:/L", quality=quality).queue
    ]
    rng = random.Random(24)
    for _ in range(5):
        shuffled = photos[:]
        rng.shuffle(shuffled)
        again = [
            c.content_hash
            for c in ar.build_album_queue(shuffled, album="D:/L", quality=quality).queue
        ]
        assert again == first


# --- решения: по хэшу содержимого ------------------------------------------


def test_three_states_are_recorded_and_read_back(tmp_path: Path):
    with ScanIndex(tmp_path / "index.db") as index:
        for content_hash, state in (("h1", "keep"), ("h2", "print"), ("h3", "drop")):
            index.record_album_state(content_hash, state)
        states = index.album_states_for_hashes(["h1", "h2", "h3", "h4"])

    assert {h: row["album_state"] for h, row in states.items()} == {
        "h1": "keep",
        "h2": "print",
        "h3": "drop",
    }
    # «Не просмотрено» — это отсутствие решения, а не четвёртое решение.
    assert "h4" not in states


def test_an_unknown_state_is_refused(tmp_path: Path):
    with ScanIndex(tmp_path / "index.db") as index:
        with pytest.raises(ValueError):
            index.record_album_state("h1", "beautiful")


def test_a_decision_follows_the_content_not_the_path(tmp_path: Path):
    """Р10 целиком: решение — факт про содержимое. Файл переехал в другой
    альбом, путь другой, хэш тот же — решение на месте, и снимок приходит
    уже решённым."""
    db = tmp_path / "index.db"
    with ScanIndex(db) as index:
        index.record_album_state("hASH", "print")

    def queue_for(path: str):
        with ScanIndex(db) as index:
            states = index.album_states_for_hashes(["hASH"])
        return ar.build_album_queue(
            [{"path": path, "content_hash": "hASH", "size": 10}],
            album=ar.folder_of(path),
            states=states,
        ).queue[0]

    before = queue_for(r"D:\Photos\Pictures\IMG_1.jpg")
    after = queue_for(r"D:\Library\2018\2018 Novosibirsk\IMG_1.jpg")
    assert before.state == after.state == "print"
    assert before.album != after.album


def test_clearing_a_state_leaves_no_row_behind(tmp_path: Path):
    with ScanIndex(tmp_path / "index.db") as index:
        index.record_album_state("h1", "drop")
        assert index.album_state_counts()["drop"] == 1
        index.clear_album_state("h1")
        assert index.album_states_for_hashes(["h1"]) == {}
        assert index.album_state_counts() == {
            "keep": 0, "print": 0, "drop": 0, "drop_pending": 0
        }


def test_the_two_decision_axes_share_a_row_but_not_a_decision(tmp_path: Path):
    """Один снимок может быть и хранителем группы дублей (Р8), и
    отобранным в печать. Одна строка на хэш содержимого — это Р10; одно
    решение на две оси было бы потерей одной из них."""
    with ScanIndex(tmp_path / "index.db") as index:
        index.record_album_state("h1", "print")
        index.record_decision("h1", "quarantine", "D:/Photos/Краснодар/a.jpg")

        assert index.decisions_for_hashes(["h1"])["h1"]["action"] == "quarantine"
        assert index.album_states_for_hashes(["h1"])["h1"]["album_state"] == "print"

        # `U` на экране дублей снимает решение по группе и не трогает «в печать».
        index.clear_decision("h1")
        assert index.decisions_for_hashes(["h1"]) == {}
        assert index.album_states_for_hashes(["h1"])["h1"]["album_state"] == "print"

        # И наоборот.
        index.record_decision("h1", "keep")
        index.clear_album_state("h1")
        assert index.album_states_for_hashes(["h1"]) == {}
        assert index.decisions_for_hashes(["h1"])["h1"]["action"] == "keep"


def test_an_album_only_row_is_invisible_to_the_duplicate_group_axis(tmp_path: Path):
    """Строка, существующая только ради альбомной оси, несёт
    `action = 'unset'`. Экран дублей не должен увидеть в ней решение —
    иначе «в печать» читалось бы там как «просмотрено»."""
    with ScanIndex(tmp_path / "index.db") as index:
        index.record_album_state("h1", "keep")
        assert index.decisions_for_hashes(["h1"]) == {}
        assert index.pending_decision_count() == 0


def test_a_new_decision_resets_applied(tmp_path: Path):
    with ScanIndex(tmp_path / "index.db") as index:
        index.record_album_state("h1", "drop")
        index.mark_album_states_applied(["h1"])
        assert index.album_states_for_hashes(["h1"])["h1"]["album_applied_at"]
        assert index.pending_drop_hashes() == []

        index.record_album_state("h1", "keep")
        assert index.album_states_for_hashes(["h1"])["h1"]["album_applied_at"] is None


def test_pending_drops_are_the_ones_not_yet_moved(tmp_path: Path):
    with ScanIndex(tmp_path / "index.db") as index:
        index.record_album_state("h1", "drop")
        index.record_album_state("h2", "drop")
        index.record_album_state("h3", "print")
        index.mark_album_states_applied(["h1"])
        assert index.pending_drop_hashes() == ["h2"]
        assert index.album_state_counts() == {
            "keep": 0, "print": 1, "drop": 2, "drop_pending": 1
        }


def test_review_decisions_gains_the_album_columns_on_an_index_built_before_task_24(
    tmp_path: Path,
):
    """Миграция 13 на базе, проштампованной двенадцатой: колонки
    добавляются, уже лежащие решения по группам не трогаются."""
    import sqlite3

    db = tmp_path / "legacy.db"
    with ScanIndex(db) as index:
        index.record_decision("h1", "quarantine", "D:/a.jpg")

    conn = sqlite3.connect(db)
    for column in ("album_state", "album_decided_at", "album_applied_at"):
        conn.execute(f"ALTER TABLE review_decisions DROP COLUMN {column}")
    conn.execute(
        "UPDATE meta SET value = '2,3,4,5,6,7,8,9,10,11,12' "
        "WHERE key = 'applied_migrations'"
    )
    conn.execute("UPDATE meta SET value = '12' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with ScanIndex(db) as index:
        # Решение пункта 12 на месте, альбомная ось доступна.
        assert index.decisions_for_hashes(["h1"])["h1"]["action"] == "quarantine"
        index.record_album_state("h1", "print")
        assert index.album_states_for_hashes(["h1"])["h1"]["album_state"] == "print"


# --- границы формы ----------------------------------------------------------


def test_module_imports_nothing_that_can_delete():
    """Р0, ось B: у «ценности» структурно нет полномочий двигать файлы, и
    это свойство формы, а не дисциплины. В `album_review` нет ни
    `quarantine`, ни `shutil`, ни `os`, ни `pathlib` — то есть ни одного
    объекта, которым можно удалить или переместить файл. Та же защита,
    которой `best_copy` держит вкладку похожих (задача 17)."""
    source = Path(ar.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])

    assert imported <= {"dataclasses", "enum", "typing", "__future__"}, imported
    for forbidden in ("os", "shutil", "pathlib", "subprocess", "quarantine", "executor"):
        assert forbidden not in imported

    for dangerous in ("rmtree", "unlink", "os.remove", "shutil.move", "rename("):
        assert dangerous not in source


# --- CLI: читает, печатает, и двигает только по --confirm ------------------


def _library_index(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    """Собранная библиотека с настоящими файлами и записями задачи 22."""
    from dupecleaner.models import FileRecord, MediaKind

    album = tmp_path / "Library" / "2018" / "2018 Novosibirsk"
    album.mkdir(parents=True)
    paths = {}
    db = tmp_path / "index.db"
    with ScanIndex(db) as index:
        records = []
        for content_hash, name in (("hbig", "big.jpg"), ("hmid", "mid.jpg")):
            path = album / name
            path.write_bytes(b"\xff\xd8\xff" + content_hash.encode() * 400)
            paths[content_hash] = path
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
        for content_hash, name in (("hbig", "big.jpg"), ("hmid", "mid.jpg")):
            index.set_full_hash(str(album / name), content_hash)
            index.record_library_move(
                op_id=f"op-{content_hash}",
                content_key=content_hash,
                source=str(tmp_path / "Photos" / name),
                destination=str(album / name),
                album="Новосибирск",
            )
        index.commit()
    return db, album, paths


def test_cli_without_a_library_says_what_is_missing(tmp_path: Path, capsys):
    from dupecleaner.cli import main

    code = main(["--db", str(tmp_path / "blank.db"), "review"])
    out = capsys.readouterr().out
    assert code == 1
    assert "библиотек" in out


def test_cli_lists_albums_and_prints_the_print_thresholds(tmp_path: Path, capsys):
    from dupecleaner.cli import main

    db, album, _ = _library_index(tmp_path)
    assert main(["--db", str(db), "review"]) == 0
    out = capsys.readouterr().out
    assert str(album) in out
    assert "8.7 МП" in out
    assert "2.09 МП" in out


def test_cli_prints_one_albums_queue_with_a_reason_per_photo(tmp_path: Path, capsys):
    from dupecleaner.cli import main

    db, album, _ = _library_index(tmp_path)
    with ScanIndex(db) as index:
        index.record_album_state("hbig", "print")
    assert main(["--db", str(db), "review", "--album", str(album)]) == 0
    out = capsys.readouterr().out
    assert "big.jpg" in out and "mid.jpg" in out
    assert "в печать" in out
    assert "не измерялось" in out
    # Инструмент обязан сказать вслух, чего он не делает.
    assert "«Красиво» инструмент не определяет" in out


def test_cli_apply_needs_confirm_and_moves_nothing_without_it(tmp_path: Path, capsys):
    from dupecleaner.cli import main

    db, album, paths = _library_index(tmp_path)
    with ScanIndex(db) as index:
        index.record_album_state("hmid", "drop")
    quarantine = tmp_path / "Quarantine"

    assert main([
        "--db", str(db), "review", "--apply",
        "--quarantine-dir", str(quarantine),
    ]) == 0
    out = capsys.readouterr().out
    assert "нужен --confirm" in out
    assert paths["hmid"].is_file()
    assert not quarantine.exists()

    assert main([
        "--db", str(db), "review", "--apply",
        "--quarantine-dir", str(quarantine), "--confirm",
    ]) == 0
    out = capsys.readouterr().out
    assert "Перемещено в карантин: 1" in out
    assert not paths["hmid"].exists()
    assert paths["hbig"].is_file()
    with ScanIndex(db) as index:
        assert index.pending_drop_hashes() == []


def test_cli_apply_refuses_without_a_quarantine_directory(tmp_path: Path, capsys):
    from dupecleaner.cli import main

    db, _, _ = _library_index(tmp_path)
    assert main(["--db", str(db), "review", "--apply", "--confirm"]) == 2
    assert "--quarantine-dir" in capsys.readouterr().out


def test_a_decision_with_no_content_hash_is_refused_at_the_storage_layer(tmp_path: Path):
    """SQLite не считает NULL нарушением `TEXT PRIMARY KEY`, поэтому пустой
    ключ завёл бы строку, которую уже ничем не найти. Найдено на сквозном
    прогоне: после переноса в библиотеку `resolve_content_hash` по новому
    пути отвечает None — `files` помнит путь **до** переноса, а хэш
    собранной библиотеки живёт в `library_moves`."""
    with ScanIndex(tmp_path / "index.db") as index:
        for empty in ("", None):
            with pytest.raises(ValueError):
                index.record_album_state(empty, "drop")
        assert index.album_state_counts()["drop"] == 0


def test_library_contents_is_where_a_moved_photos_hash_lives(tmp_path: Path):
    """Состав альбомов берётся из `library_moves`, а не из `files`, и это
    не вкусовщина: `files` помнит путь **до** переноса, поэтому
    `resolve_content_hash` по пути в библиотеке отвечает None. Найдено на
    сквозном прогоне настоящего конвейера, а не придумано."""
    from dupecleaner.models import FileRecord, MediaKind

    album = tmp_path / "Library" / "2018" / "2018 Novosibirsk"
    album.mkdir(parents=True)
    source = tmp_path / "Photos" / "Краснодар" / "big.jpg"
    source.parent.mkdir(parents=True)
    destination = album / "big.jpg"
    destination.write_bytes(b"\xff\xd8\xff" + b"photo" * 300)

    db = tmp_path / "index.db"
    with ScanIndex(db) as index:
        index.upsert_files(
            [
                FileRecord(
                    display_path=str(source),
                    real_path=str(source),
                    size=destination.stat().st_size,
                    mtime=destination.stat().st_mtime,
                    media_kind=MediaKind.PHOTO,
                )
            ],
            "scan-1",
        )
        index.set_full_hash(str(source), "hbig")
        index.record_library_move(
            op_id="op-1",
            content_key="hbig",
            source=str(source),
            destination=str(destination),
            album="Новосибирск",
        )
        index.commit()

        assert index.resolve_content_hash(str(destination)) is None
        rows = index.library_contents()

    assert [(r["destination"], r["content_key"]) for r in rows] == [
        (str(destination), "hbig")
    ]
    # Размер добирается по хэшу содержимого: байт-в-байт равные копии равны
    # и по размеру, поэтому одной строки из `files` достаточно.
    assert rows[0]["size"] == destination.stat().st_size

    # А откатанное перемещение из состава библиотеки исчезает: файла по
    # этому пути больше нет.
    with ScanIndex(db) as index:
        index.mark_library_move_rolled_back("op-1")
        assert index.library_contents() == []
