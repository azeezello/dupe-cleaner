r"""Задача 17: ранжирование копий внутри группы похожих по качеству.

Что здесь проверяется, помимо арифметики:

1. **Лестница именно в этом порядке** и каждая ступень отдельно — порядок
   взят из таблицы Р2 «чего каждое число стоит», от факта (разрешение) к
   относительной оценке (резкость) и к косвенному признаку (вес).
2. **Оценки сжатия с разными основаниями не сравниваются** — требование
   Р2 прямым текстом, а не предосторожность: PNG со `lossless` 0.0 не
   обходит JPEG, сохранённый с качеством 95.
3. **Объяснение не может разойтись с правилом**: оно называет ступень, на
   которой победитель впервые обошёл следующую копию, и находит её
   сравнением тех же ключей, по которым идёт сортировка (тот же приём, что
   в `keeper.keeper_reason`).
4. **Спорный случай назван спорным.** Увеличенная копия обходит оригинал
   по разрешению и проигрывает по резкости и сжатию; подсказка остаётся
   подсказкой, но говорит, что признаки спорят.
5. **Ни одного права на действие.** В выдаче нет ни пути, ни объёма, ни
   хранителя группы — тот же тест по существу, что
   `test_similar_review.py::test_the_review_payload_has_no_keeper_and_no_freed_bytes`,
   только на уровне модуля.
6. **На настоящих пережатых файлах, а не только на числах** — последний
   раздел прогоняет оригинал, его пересохранение q40 и уменьшенную копию
   через тот самый конвейер `thumbnails.generate`, которым их меряет скан.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from dupecleaner import best_copy, thumbnails
from dupecleaner.storage import ScanIndex


# --- helpers ---------------------------------------------------------------


def member(content_hash: str, size: int = 1000) -> dict:
    return {"content_hash": content_hash, "size": size}


def q(
    width=4000,
    height=3000,
    sharpness=100.0,
    recompression=0.1,
    basis="jpeg_quant_tables",
    jpeg_quality=95,
) -> dict:
    return {
        "source_width": width,
        "source_height": height,
        "sharpness": sharpness,
        "recompression": recompression,
        "recompression_basis": basis,
        "jpeg_quality": jpeg_quality,
    }


def photo_like(seed: int, size=(1200, 900)) -> Image.Image:
    """Фотографическая структура (низкие частоты), детерминированно — тот
    же помощник, что в `tests/test_similar.py`, и по той же причине:
    шумовая картинка не похожа на фотографию ни для отпечатка, ни для
    резкости."""
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


# --- лестница, по одной ступени ------------------------------------------


def test_resolution_decides_first_and_says_so():
    members = [member("small"), member("big")]
    quality = {
        "small": q(width=1600, height=1200),
        "big": q(width=4000, height=3000),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["best"] == "big"
    assert block["level"] == best_copy.LEVEL_RESOLUTION
    assert "выше разрешение" in block["reason"]
    assert "4000×3000" in block["reason"] and "1600×1200" in block["reason"]
    assert block["confident"] is True


def test_recompression_decides_when_resolution_ties():
    """И в терминах фактора качества JPEG, а не абстрактной оценки: это
    число компрессор записал в сам файл, и человеку оно знакомо."""
    members = [member("squeezed"), member("clean")]
    quality = {
        "squeezed": q(recompression=0.76, jpeg_quality=60),
        "clean": q(recompression=0.00, jpeg_quality=96),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["best"] == "clean"
    assert block["level"] == best_copy.LEVEL_RECOMPRESSION
    assert "меньше следов пережатия" in block["reason"]
    assert "96" in block["reason"] and "60" in block["reason"]


def test_the_jpeg_quality_factor_breaks_a_tie_the_0_1_score_cannot_see():
    """Оценка сжатия насыщается: всё с качеством 92 и выше — это 0.000.
    Без фактора качества пара q95/q92 падала бы на резкость, и аудит
    задачи 17 показал, что там она в двух случаях из трёх выбирала
    пересохранение."""
    members = [member("q92"), member("q95")]
    quality = {
        "q92": q(recompression=0.0, jpeg_quality=92, sharpness=7.0),
        "q95": q(recompression=0.0, jpeg_quality=95, sharpness=6.6),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["best"] == "q95"
    assert block["level"] == best_copy.LEVEL_RECOMPRESSION
    assert "качеством JPEG 95 против 92" in block["reason"]
    assert block["confident"] is True


def test_the_quality_factor_does_not_cross_bases():
    """У HEIC фактора качества нет вовсе, а сравнивать оценки с разными
    основаниями запрещает Р2 — поэтому ступень молчит, а не достраивается
    из того, что под руку попалось."""
    members = [member("heic"), member("jpeg", size=2_000_000)]
    quality = {
        "heic": q(recompression=0.3, basis="bits_per_pixel", jpeg_quality=None),
        "jpeg": q(recompression=0.3, basis="jpeg_quant_tables", jpeg_quality=95),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["basis"] is None
    assert block["level"] == best_copy.LEVEL_SIZE


def test_sharpness_numbers_do_not_print_as_seven_against_seven():
    """Поймано аудитом: два кадра серии давали «резче: 7 против 7». Строка,
    после которой перестают верить всему экрану."""
    members = [member("a"), member("b")]
    quality = {"a": q(sharpness=6.64), "b": q(sharpness=6.56)}
    reason = best_copy.rank_copies(members, quality, is_copy_group=True)["reason"]
    assert "6.64 против 6.56" in reason

    same = best_copy.rank_copies(
        [member("a"), member("b")],
        {"a": q(sharpness=6.6001), "b": q(sharpness=6.6002)},
        is_copy_group=True,
    )
    assert "столько же до третьего знака" in same["reason"]


def test_sharpness_decides_when_resolution_and_compression_tie():
    members = [member("soft"), member("sharp")]
    quality = {
        "soft": q(sharpness=40.0),
        "sharp": q(sharpness=160.0),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["best"] == "sharp"
    assert block["level"] == best_copy.LEVEL_SHARPNESS
    assert "резче" in block["reason"]
    # Числа без абсолютного смысла обязаны быть названы относительными —
    # иначе «160» прочитается как измерение оптики.
    assert "относительная" in block["reason"]


def test_file_size_is_the_last_argument_and_is_labelled_as_weak():
    members = [member("light", size=1_000_000), member("heavy", size=3_000_000)]
    quality = {"light": q(), "heavy": q()}
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["best"] == "heavy"
    assert block["level"] == best_copy.LEVEL_SIZE
    assert "косвенный признак" in block["reason"]


def test_fully_identical_metrics_fall_back_to_the_content_hash():
    """Порядок обязан быть устойчивым между запросами: экран показывает
    «2-я по качеству», и это не должно меняться от того, в каком порядке
    база отдала строки."""
    members = [member("bbb"), member("aaa")]
    quality = {"aaa": q(), "bbb": q()}
    first = best_copy.rank_copies(members, quality, is_copy_group=True)
    second = best_copy.rank_copies(list(reversed(members)), quality, is_copy_group=True)
    assert first["order"] == second["order"] == ["aaa", "bbb"]
    assert first["level"] == best_copy.LEVEL_EQUAL


def test_resolution_beats_compression_even_when_compression_disagrees():
    """Лестница лексикографическая, а не сумма баллов. Проверяется прямо,
    потому что это самое спорное решение задачи: Р2 называет разрешение
    единственным фактом из трёх, и именно поэтому оно сверху."""
    members = [member("big_squeezed"), member("small_clean")]
    quality = {
        "big_squeezed": q(width=4000, height=3000, recompression=0.9, jpeg_quality=45),
        "small_clean": q(width=2000, height=1500, recompression=0.0, jpeg_quality=98),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["best"] == "big_squeezed"
    assert block["level"] == best_copy.LEVEL_RESOLUTION


# --- основание шкалы сжатия (Р2) ------------------------------------------


def test_mixed_bases_switch_the_compression_rung_off_entirely():
    """PNG получает 0.0 потому, что в нём ничего не терялось, — а не
    потому, что он оригинал: он вполне может быть скриншотом плохого
    JPEG. Сравнить его 0.0 с 0.1 у JPEG значит сравнить несравнимое, и
    ступень обязана выключиться, а не выключиться для одной пары."""
    members = [member("png"), member("jpeg", size=5_000_000)]
    quality = {
        "png": q(recompression=0.0, basis="lossless", jpeg_quality=None, sharpness=50.0),
        "jpeg": q(recompression=0.2, basis="jpeg_quant_tables", sharpness=50.0),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["basis"] is None
    # Победил JPEG — но по весу, а не по мнимому превосходству над PNG.
    assert block["best"] == "jpeg"
    assert block["level"] == best_copy.LEVEL_SIZE
    assert any("несопоставимы" in note for note in block["notes"])


def test_one_basis_everywhere_keeps_the_rung_on():
    members = [member("a"), member("b")]
    quality = {
        "a": q(recompression=0.0, basis="bits_per_pixel", jpeg_quality=None),
        "b": q(recompression=0.8, basis="bits_per_pixel", jpeg_quality=None),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["basis"] == "bits_per_pixel"
    assert block["best"] == "a"
    assert block["level"] == best_copy.LEVEL_RECOMPRESSION
    # Без фактора качества JPEG объяснение обязано назвать основание —
    # иначе «0.00 против 0.80» это число в вакууме.
    assert "бит на пиксель" in block["reason"]


# --- спорный случай -------------------------------------------------------


def test_an_upscaled_copy_wins_on_resolution_and_is_called_contested():
    """Главная ошибка правила, названная вслух: апскейл обходит оригинал
    по первой ступени, и настоящих деталей в нём не больше. Подсказка
    остаётся подсказкой, но перестаёт быть уверенной."""
    members = [member("upscaled"), member("original")]
    quality = {
        "upscaled": q(width=6000, height=4500, sharpness=30.0, recompression=0.7, jpeg_quality=62),
        "original": q(width=3000, height=2250, sharpness=150.0, recompression=0.05, jpeg_quality=95),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["best"] == "upscaled"
    assert [c["content_hash"] for c in block["contested"]] == ["original"]
    assert set(block["contested"][0]["better_in"]) == {"резкости", "следам пережатия"}
    assert any("спорят" in note for note in block["notes"])


def test_a_sharpness_win_held_by_a_more_compressed_copy_is_not_reported():
    """Измеренная ошибка метрики: пересохранение с качеством 40 вышло в
    1.8 раза «резче» оригинала с качеством 96 — это блочность JPEG, а не
    деталь. Звать человека смотреть на артефакты нельзя, поэтому такой
    перевес в спорные не попадает."""
    members = [member("clean", size=900_000), member("squeezed", size=300_000)]
    quality = {
        "clean": q(sharpness=3.7, recompression=0.0, jpeg_quality=96),
        "squeezed": q(sharpness=6.6, recompression=1.0, jpeg_quality=40),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["best"] == "clean"
    assert block["level"] == best_copy.LEVEL_RECOMPRESSION
    assert block["contested"] == []


def test_a_sharpness_win_at_equal_compression_is_reported():
    """Обратная сторона того же правила: если сжатие одинаковое, объяснить
    перевес артефактами нечем, и о нём надо сказать."""
    members = [member("a"), member("b")]
    quality = {
        "a": q(width=4000, height=3000, sharpness=40.0),
        "b": q(width=2000, height=1500, sharpness=160.0),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["best"] == "a"  # разрешение — первая ступень
    assert [c["better_in"] for c in block["contested"]] == [["резкости"]]


def test_a_clean_win_is_not_contested():
    members = [member("good"), member("resave")]
    quality = {
        "good": q(width=4000, height=3000, sharpness=150.0, recompression=0.05, jpeg_quality=95),
        "resave": q(width=1000, height=750, sharpness=60.0, recompression=0.8, jpeg_quality=55),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["contested"] == []
    assert block["confident"] is True


def test_a_margin_inside_the_tolerance_is_not_called_convincing():
    """4032×3024 против 4000×3000 — одна камера, другая прошивка. Порядок
    определён (он обязан быть), но называть такой перевес перевесом
    нечестно."""
    members = [member("a"), member("b")]
    quality = {
        "a": q(width=4032, height=3024),
        "b": q(width=4000, height=3000),
    }
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["best"] == "a"
    assert block["confident"] is False
    assert any("погрешности" in note for note in block["notes"])


# --- чего мы не знаем -----------------------------------------------------


def test_copies_without_metrics_are_left_out_not_ranked_last():
    members = [member("measured"), member("unknown")]
    block = best_copy.rank_copies(members, {"measured": q()}, is_copy_group=True)
    assert block["order"] == ["measured"]
    assert block["measured"] == 1 and block["unmeasured"] == 1
    assert block["level"] == best_copy.LEVEL_SINGLE
    assert "сравнивать не с чем" in block["reason"]


def test_a_group_with_no_metrics_at_all_says_so_and_picks_nobody():
    members = [member("x"), member("y")]
    block = best_copy.rank_copies(members, {}, is_copy_group=True)
    assert block["best"] is None
    assert block["order"] == []
    assert any("нет метрик качества" in note for note in block["notes"])


def test_an_unmeasured_copy_is_mentioned_rather_than_silently_dropped():
    members = [member("a"), member("b"), member("nope")]
    quality = {"a": q(width=4000, height=3000), "b": q(width=1000, height=750)}
    block = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert block["unmeasured"] == 1
    assert any("без метрик" in note for note in block["notes"])


def test_resolution_present_but_metrics_absent_is_still_unmeasured():
    """Строка метрик без основания оценки сжатия — это строка из индекса,
    собранного до задачи 9. Половина ответа не ранжируется."""
    half = {"source_width": 4000, "source_height": 3000, "recompression_basis": None}
    block = best_copy.rank_copies(
        [member("half"), member("full")], {"half": half, "full": q()}, is_copy_group=True
    )
    assert block["order"] == ["full"]


# --- сцена против копий (деление задачи 14) -------------------------------


def test_a_scene_group_is_labelled_as_a_technical_comparison():
    """У серии кадров лучшей копии не существует, и подписывать её так
    значило бы соврать там, где поверят. Деление на копии и сцену берётся
    готовым из задачи 14 — здесь меняются только слова."""
    members = [member("a"), member("b")]
    quality = {"a": q(sharpness=160.0), "b": q(sharpness=40.0)}
    scene = best_copy.rank_copies(members, quality, is_copy_group=False)
    assert scene["is_choice"] is False
    assert "не выбор копии" in scene["headline"]
    assert any("разные кадры" in note for note in scene["notes"])
    # Порядок всё равно посчитан: «какой кадр резче» — осмысленный вопрос.
    assert scene["best"] == "a"

    copies = best_copy.rank_copies(members, quality, is_copy_group=True)
    assert copies["is_choice"] is True
    assert copies["headline"] == best_copy.HEADLINE_CHOICE


# --- границы, которые держат Р0/Р2 ----------------------------------------


def test_the_block_carries_no_path_no_volume_and_no_group_owner():
    members = [member("a"), member("b")]
    block = best_copy.rank_copies(members, {"a": q(), "b": q(width=100, height=100)}, is_copy_group=True)
    blob = json.dumps(block, ensure_ascii=False)
    for forbidden in ("keeper", "wasted", "освободится", "applied_at", "decision", "C:\\", "/"):
        assert forbidden not in blob, forbidden


def test_the_module_imports_nothing_that_can_move_a_file():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(best_copy))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names += [a.name for a in node.names]
    assert not any("quarantine" in n for n in names), names
    assert not any("models" in n for n in names), names
    assert not any("keeper" in n for n in names), (
        "Р8 и задача 17 отвечают на разные вопросы; общий код между ними "
        "означал бы, что кто-то из двух объясняет не своё правило",
        names,
    )


# --- на настоящих файлах --------------------------------------------------


def _measure(path: Path) -> dict:
    """Через тот же конвейер, которым меряет скан."""
    preview = thumbnails.generate(path)
    m = preview.metrics
    assert m is not None
    return {
        "source_width": m.source_width,
        "source_height": m.source_height,
        "sharpness": m.sharpness,
        "recompression": m.recompression,
        "recompression_basis": m.recompression_basis,
        "jpeg_quality": m.jpeg_quality,
    }


def test_on_real_files_the_original_beats_its_resave_and_its_downscale(tmp_path: Path):
    """Практический случай целиком: оригинал, пересохранение из
    мессенджера и уменьшенная копия — три файла, один кадр."""
    photo = photo_like(5, size=(1600, 1200)).convert("RGB")
    original = tmp_path / "original.jpg"
    photo.save(original, "JPEG", quality=96)
    resave = tmp_path / "resave_q40.jpg"
    photo.save(resave, "JPEG", quality=40)
    smaller = tmp_path / "w640.jpg"
    photo.resize((640, 480), Image.Resampling.LANCZOS).save(smaller, "JPEG", quality=85)

    quality = {
        "original": _measure(original),
        "resave": _measure(resave),
        "smaller": _measure(smaller),
    }
    members = [
        member("original", original.stat().st_size),
        member("resave", resave.stat().st_size),
        member("smaller", smaller.stat().st_size),
    ]
    block = best_copy.rank_copies(members, quality, is_copy_group=True)

    assert block["best"] == "original"
    # Уменьшенная копия последняя — её обходят по первой же ступени.
    assert block["order"][-1] == "smaller"
    # И причина названа той, которой она на самом деле является: с
    # пересохранением разрешение совпадает, расходится сжатие.
    assert block["level"] == best_copy.LEVEL_RECOMPRESSION
    assert quality["original"]["jpeg_quality"] > quality["resave"]["jpeg_quality"]
    assert block["contested"] == []


def test_the_metrics_a_scan_stores_are_the_metrics_the_ranking_reads(tmp_path: Path):
    """Сквозная проверка миграции 12: метрики, посчитанные на декоде для
    перцептивного хэша, доезжают до ранжирования через индекс, а не через
    кэш превью. Это и есть то, что задача 17 добавила в схему — без этого
    в большинстве групп похожих ранжировать не на чем (отчёт задачи 14)."""
    photo = photo_like(7, size=(1400, 1050)).convert("RGB")
    good = tmp_path / "good.jpg"
    photo.save(good, "JPEG", quality=95)
    bad = tmp_path / "bad.jpg"
    photo.save(bad, "JPEG", quality=35)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        for name, path in (("good", good), ("bad", bad)):
            preview = thumbnails.generate(path, encode=False)
            assert not preview.data, "encode=False не должен кодировать миниатюру"
            thumbnails.store_phash(index, name, preview)
        index.commit()
        quality = index.quality_for_hashes(["good", "bad"])

    assert set(quality) == {"good", "bad"}
    block = best_copy.rank_copies(
        [member("good", good.stat().st_size), member("bad", bad.stat().st_size)],
        quality,
        is_copy_group=True,
    )
    assert block["best"] == "good"
    assert block["level"] == best_copy.LEVEL_RECOMPRESSION
