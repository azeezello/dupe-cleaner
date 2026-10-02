r"""Экранный слой похожих снимков (задача 14).

Что здесь действительно проверяется — не «функция вернула словарь», а три
утверждения, на которых держится вкладка:

1. **Ручка порога не умеет врать.** Расстояния между отпечатками чётны по
   построению, поэтому нечётное значение округляется вниз, и порог 7 — это
   порог 6. Тест держит это, потому что ошибка здесь невидима: ручка
   двигается, число меняется, результат нет.
2. **Цепочка называется цепочкой.** Группа, собранная через A∼B∼C с
   разбросом больше порога, — это «похожая сцена», а не группа копий. Это
   главная честная ошибка метода (замеры задачи 13: 13 кадров одной свадьбы
   в одной группе при пороге 12), и единственное, что отличает её от
   находки, — подпись.
3. **У похожих нет хранителя.** Ни `keeper`, ни «освободится N байт» — Р0
   (ось B) и Р2. Проверяется не чтением кода, а поиском подстроки в
   сериализованной выдаче: добавить такое поле «на будущее» нельзя, не
   уронив тест.
"""

from __future__ import annotations

import json

import pytest

from dupecleaner import similar
from dupecleaner.similar_review import (
    BURST_WINDOW_SECONDS,
    DEFAULT_UI_THRESHOLD,
    KIND_COPIES,
    KIND_SCENE,
    MEASURED_REFERENCE,
    UI_THRESHOLDS,
    build_review,
    group_kind,
    member_labels,
    normalise_threshold,
    threshold_block,
    thresholds_for,
)


def bits_of(*positions: int) -> int:
    value = 0
    for p in positions:
        value |= 1 << p
    return value


def entry(name: str, bits: int, *, size: int = 1000, aspect: float = 1.5, paths=None):
    return similar.PhashEntry(
        content_hash=name,
        bits=bits,
        aspect=aspect,
        paths=tuple(paths or (f"D:/Photos/{name}.jpg",)),
        size=size,
    )


def cluster(entries, max_distance=6):
    return similar.find_similar_groups(entries, thresholds_for(max_distance))


# --- ручка порога ---------------------------------------------------------


def test_every_notch_of_the_handle_is_even_and_measured():
    assert all(value % 2 == 0 for value in UI_THRESHOLDS)
    assert UI_THRESHOLDS[0] == 0 and UI_THRESHOLDS[-1] == 16
    # Подсказка под ручкой обязана существовать для каждого деления: деление
    # без замера оставляло бы человека без ответа на вопрос «а что будет».
    assert set(MEASURED_REFERENCE) == set(UI_THRESHOLDS)


def test_odd_threshold_rounds_down_because_seven_is_six():
    assert normalise_threshold(7) == 6
    assert normalise_threshold(6) == 6
    assert normalise_threshold(1) == 0
    assert normalise_threshold(15) == 14


def test_threshold_outside_the_handle_is_clamped_not_honoured():
    assert normalise_threshold(99) == 16
    assert normalise_threshold(-5) == 0
    assert normalise_threshold("не число") == DEFAULT_UI_THRESHOLD
    assert normalise_threshold(None) == DEFAULT_UI_THRESHOLD


def test_default_is_the_six_that_was_measured():
    assert DEFAULT_UI_THRESHOLD == 6
    assert MEASURED_REFERENCE[6] == {
        "groups": 665,
        "contents": 1638,
        "largest": 8,
        "chains": 81,
    }


def test_handle_changes_only_the_distance_not_the_shape_guard():
    base = similar.SimilarThresholds()
    picked = thresholds_for(10)
    assert picked.max_distance == 10
    assert picked.max_aspect_log_ratio == base.max_aspect_log_ratio
    assert picked.max_bucket == base.max_bucket


def test_threshold_block_shows_the_live_number_beside_the_measured_one():
    block = threshold_block(thresholds_for(4), live_groups=12)
    assert block["max_distance"] == 4
    assert block["live_groups"] == 12
    assert block["measured"]["4"]["groups"] == 529
    assert "порог 7 — это тот же порог 6" in block["even_only_note"]
    assert block["allowed"] == list(UI_THRESHOLDS)


# --- тип группы: копии против сцены ---------------------------------------


def test_a_clique_is_a_group_of_copies():
    clustering = cluster(
        [
            entry("a", 0),
            entry("b", bits_of(0, 1)),
            entry("c", bits_of(0, 1, 2, 3)),
        ]
    )
    assert len(clustering.groups) == 1
    group = clustering.groups[0]
    assert group.spread == 4
    assert group_kind(group, 6) == KIND_COPIES

    review = build_review(clustering)
    assert review["groups"][0]["kind"] == KIND_COPIES
    assert review["summary"]["copy_groups"] == 1
    assert review["summary"]["chain_groups"] == 0


def test_a_chain_is_a_similar_scene_and_says_so_in_words():
    # a∼b = 4, b∼c = 4, a∼c = 8 — крайние дальше порога 6, то есть группа
    # существует только за счёт середины. Ровно та ошибка, которую
    # одноцепочечное объединение не может исключить по построению.
    clustering = cluster(
        [
            entry("a", 0, size=3_000_000),
            entry("b", bits_of(0, 1, 2, 3)),
            entry("c", bits_of(0, 1, 2, 3, 4, 5, 6, 7)),
        ]
    )
    assert len(clustering.groups) == 1
    group = clustering.groups[0]
    assert group.spread == 8 > 6

    review = build_review(clustering)
    shown = review["groups"][0]
    assert shown["kind"] == KIND_SCENE
    assert review["summary"]["chain_groups"] == 1
    assert review["summary"]["copy_groups"] == 0

    text = " ".join(shown["explanation"])
    assert "цепочкой" in text
    assert "похожая сцена" in text
    # И пара, которая дальше порога, помечена внутри группы, а не только в
    # общей фразе: человек должен видеть, какой именно снимок «не свой».
    far = [m for m in shown["members"] if "дальше порога" in m["labels"]]
    assert [m["content_hash"] for m in far] == ["c"]


def test_a_group_too_large_to_measure_is_treated_as_a_scene():
    group = similar.SimilarGroup(
        members=[entry("a", 0), entry("b", bits_of(1))], spread=None
    )
    assert group_kind(group, 6) == KIND_SCENE


# --- опорный снимок и объяснение ------------------------------------------


def test_reference_is_the_heaviest_file_and_distances_start_from_it():
    clustering = cluster(
        [
            entry("light", bits_of(0, 1), size=200_000),
            entry("heavy", 0, size=4_000_000),
        ]
    )
    shown = build_review(clustering)["groups"][0]
    reference = [m for m in shown["members"] if m["is_reference"]]
    assert [m["content_hash"] for m in reference] == ["heavy"]
    by_hash = {m["content_hash"]: m for m in shown["members"]}
    assert by_hash["heavy"]["distance"] == 0
    assert by_hash["light"]["distance"] == 2
    assert by_hash["light"]["size_ratio"] == 20.0


def test_a_much_lighter_file_reads_as_a_recompressed_copy():
    labels = member_labels(
        bits_distance=2,
        max_distance=6,
        size_ratio=4.0,
        resolution=None,
        reference_resolution=None,
        seconds_apart=None,
    )
    assert labels == ["пережатая копия"]


def test_fewer_pixels_reads_as_a_downscaled_copy_not_merely_a_lighter_one():
    labels = member_labels(
        bits_distance=2,
        max_distance=6,
        size_ratio=4.0,
        resolution=(600, 450),
        reference_resolution=(1200, 900),
        seconds_apart=None,
    )
    assert labels == ["уменьшенная копия"]


def test_the_same_weight_reads_as_the_same_frame_under_another_name():
    # Находка пилота: `20210927_073914.jpg` и `2021-09-27 07-39-14.JPG` —
    # один кадр, переименованный конвейером.
    labels = member_labels(
        bits_distance=0,
        max_distance=6,
        size_ratio=1.0,
        resolution=None,
        reference_resolution=None,
        seconds_apart=None,
    )
    assert labels == ["тот же кадр под другим именем"]


def test_seconds_apart_reads_as_a_burst_frame():
    labels = member_labels(
        bits_distance=4,
        max_distance=6,
        size_ratio=1.0,
        resolution=None,
        reference_resolution=None,
        seconds_apart=1.0,
    )
    assert labels[0] == "кадр серии"


def test_resolution_comes_from_the_index_when_it_is_there_and_is_absent_when_not():
    clustering = cluster([entry("a", 0, size=900), entry("b", bits_of(0, 1), size=300)])
    review = build_review(
        clustering,
        quality={"a": {"source_width": 1200, "source_height": 900, "megapixels": 1.08}},
    )
    by_hash = {m["content_hash"]: m for m in review["groups"][0]["members"]}
    assert by_hash["a"]["resolution"] == "1200×900"
    # У снимка, который не попадал в группу дублей, миниатюры и метрик нет —
    # и выдача это признаёт, а не подставляет ноль.
    assert by_hash["b"]["resolution"] is None
    assert by_hash["b"]["megapixels"] is None


def test_capture_times_turn_a_group_into_a_named_burst():
    entries = [
        entry("a", 0, paths=["D:/Photos/IMG_3155.jpg"]),
        entry("b", bits_of(0, 1, 2, 3), paths=["D:/Photos/IMG_3156.jpg"]),
    ]
    taken = {"D:/Photos/IMG_3155.jpg": 1_700_000_000.0,
             "D:/Photos/IMG_3156.jpg": 1_700_000_001.0}
    shown = build_review(cluster(entries), taken_at=taken)["groups"][0]
    assert shown["burst"] is True
    assert any("соседние кадры серии" in line for line in shown["explanation"])

    apart = {"D:/Photos/IMG_3155.jpg": 1_700_000_000.0,
             "D:/Photos/IMG_3156.jpg": 1_700_000_000.0 + BURST_WINDOW_SECONDS + 60}
    shown_apart = build_review(cluster(entries), taken_at=apart)["groups"][0]
    assert shown_apart["burst"] is False


def test_a_missing_capture_time_costs_a_label_not_an_error():
    entries = [entry("a", 0), entry("b", bits_of(0, 1))]
    shown = build_review(cluster(entries), taken_at={})["groups"][0]
    assert shown["burst"] is False
    assert all(m["seconds_from_reference"] is None for m in shown["members"])


# --- граница, которую нельзя перейти (Р0, Р2) -----------------------------


def test_the_review_payload_has_no_keeper_and_no_freed_bytes():
    clustering = cluster(
        [entry("a", 0, size=4_000), entry("b", bits_of(0, 1), size=1_000)]
    )
    blob = json.dumps(build_review(clustering), ensure_ascii=False)
    for forbidden in ("keeper", "wasted", "освободится", "applied_at", "decision"):
        assert forbidden not in blob, forbidden


def test_every_group_says_out_loud_that_it_moves_nothing():
    clustering = cluster([entry("a", 0), entry("b", bits_of(0, 1))])
    for shown in build_review(clustering)["groups"]:
        text = " ".join(shown["explanation"])
        assert "никогда не уходят в карантин" in text


def test_review_module_does_not_import_quarantine():
    """Граница держится формой, а не вниманием: модуль, который собирает
    выдачу для экрана похожих, не имеет доступа к карантину вовсе.

    Проверяется по дереву импортов, а не поиском подстроки: слово
    «карантин» в этом модуле встречается часто — именно потому, что он
    объясняет человеку, что похожие туда не уходят.
    """
    import ast
    import inspect

    import dupecleaner.similar_review as module

    tree = ast.parse(inspect.getsource(module))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported += [alias.name for alias in node.names]

    assert not any("quarantine" in name for name in imported), imported
    assert not any("models" in name for name in imported), (
        "DuplicateGroup сюда не должен попадать даже как тип", imported)


# --- пустые и вырожденные случаи ------------------------------------------


def test_nothing_similar_is_a_valid_answer_not_a_blank():
    clustering = cluster([entry("a", 0), entry("b", bits_of(*range(20)))])
    review = build_review(clustering, coverage={"photos": 2, "with_phash": 2})
    assert review["groups"] == []
    assert review["summary"]["groups"] == 0
    assert review["coverage"]["photos"] == 2


def test_oversized_bucket_warning_reaches_the_screen():
    entries = [entry(f"e{i}", bits_of(0, 1)) for i in range(5)]
    thresholds = similar.SimilarThresholds(max_distance=6, max_bucket=2)
    clustering = similar.find_similar_groups(entries, thresholds)
    review = build_review(clustering)
    assert review["warnings"], "корзина пропущена молча — это находка A1 заново"
    assert any("корзин" in w for w in review["warnings"])


@pytest.mark.parametrize("threshold", UI_THRESHOLDS)
def test_every_notch_produces_a_usable_payload(threshold: int):
    entries = [
        entry("a", 0, size=4_000),
        entry("b", bits_of(0, 1, 2, 3), size=2_000),
        entry("c", bits_of(*range(10)), size=1_000),
    ]
    review = build_review(cluster(entries, threshold))
    assert review["threshold"]["max_distance"] == threshold
    assert review["threshold"]["live_groups"] == len(review["groups"])
    for shown in review["groups"]:
        assert shown["kind"] in (KIND_COPIES, KIND_SCENE)
        assert sum(1 for m in shown["members"] if m["is_reference"]) == 1


# --- задача 17: лучшая копия внутри группы --------------------------------
#
# Деление на «копии» и «похожую сцену» задача 17 берёт готовым и своего не
# вводит: в клике лучшая копия — осмысленный ответ, в цепочке кадры разные,
# и там тот же блок подписан как сравнение технического качества. Опорный
# снимок задачи 14 (самый тяжёлый файл) при этом остаётся точкой отсчёта
# для расстояний и лучшей копией не становится — это два разных понятия, и
# они обязаны уметь разойтись.


def _quality(width, height, *, sharp=50.0, recomp=0.1, jq=92, basis="jpeg_quant_tables"):
    return {
        "source_width": width,
        "source_height": height,
        "megapixels": round(width * height / 1_000_000, 2),
        "sharpness": sharp,
        "recompression": recomp,
        "recompression_basis": basis,
        "jpeg_quality": jq,
    }


def test_a_copy_group_gets_a_best_copy_with_a_reason_in_metric_terms():
    clustering = cluster([entry("big", bits_of(*range(31))),
                          entry("small", bits_of(*range(2, 33)))])
    quality = {
        "big": _quality(4000, 3000),
        "small": _quality(1000, 750),
    }
    payload = build_review(clustering, quality=quality)
    group = payload["groups"][0]
    assert group["kind"] == KIND_COPIES
    block = group["best_copy"]
    assert block["is_choice"] is True
    assert block["best"] == "big"
    assert "выше разрешение" in block["reason"]
    # И каждая копия знает своё место, чтобы панель деталей не считала
    # порядок второй раз и не разошлась с ним.
    ranks = {m["content_hash"]: m["quality_rank"] for m in group["members"]}
    assert ranks == {"big": 1, "small": 2}
    assert [m["is_best_copy"] for m in group["members"] if m["content_hash"] == "big"] == [True]


def test_the_best_copy_and_the_reference_are_allowed_to_differ():
    """Опорный — самый тяжёлый файл; лучшая копия — та, в которой больше
    картинки. Лёгкий файл с вдвое большим разрешением разводит их, и это
    не ошибка, а причина, по которой задача 17 вообще существует."""
    clustering = cluster([
        entry("heavy_small", bits_of(*range(31)), size=9_000_000),
        entry("light_big", bits_of(*range(2, 33)), size=1_000_000),
    ])
    quality = {
        "heavy_small": _quality(1000, 750),
        "light_big": _quality(4000, 3000),
    }
    group = build_review(clustering, quality=quality)["groups"][0]
    reference = [m for m in group["members"] if m["is_reference"]][0]
    assert reference["content_hash"] == "heavy_small"
    assert group["best_copy"]["best"] == "light_big"


def test_a_scene_group_says_this_is_not_a_choice_between_copies():
    # Цепочка: A∼B, B∼C, A и C дальше порога — ровно то, что задача 14
    # называет похожей сценой.
    a = bits_of(*range(31))
    b = bits_of(*range(4, 35))
    c = bits_of(*range(8, 39))
    clustering = cluster([entry("a", a), entry("b", b), entry("c", c)], max_distance=8)
    group = build_review(
        clustering,
        quality={"a": _quality(4000, 3000), "b": _quality(1000, 750), "c": _quality(900, 600)},
    )["groups"][0]
    assert group["kind"] == KIND_SCENE
    assert group["best_copy"]["is_choice"] is False
    assert "не выбор копии" in group["best_copy"]["headline"]


def test_without_metrics_the_screen_says_so_instead_of_ranking():
    """Основное состояние индекса, собранного до миграции 12. Экран обязан
    сказать «сравнить не на чем», а не разложить копии по весу, выдав это
    за качество."""
    clustering = cluster([entry("a", bits_of(*range(31))),
                          entry("b", bits_of(*range(2, 33)))])
    group = build_review(clustering)["groups"][0]
    assert group["best_copy"]["best"] is None
    assert group["best_copy"]["measured"] == 0
    assert all(m["quality_rank"] is None for m in group["members"])


def test_the_summary_counts_groups_that_could_and_could_not_be_ranked():
    ranked = [entry("big", bits_of(*range(31))), entry("small", bits_of(*range(2, 33)))]
    blind = [entry("x", bits_of(*range(40, 63), 0, 1, 2, 3, 4, 5, 6, 7)),
             entry("y", bits_of(*range(40, 63), 0, 1, 2, 3, 4, 5, 6, 9))]
    clustering = cluster(ranked + blind)
    payload = build_review(
        clustering,
        quality={"big": _quality(4000, 3000), "small": _quality(1000, 750)},
    )
    assert payload["summary"]["ranked_groups"] == 1
    assert payload["summary"]["contested_groups"] == 0


def test_contested_groups_are_counted_so_the_number_is_visible():
    clustering = cluster([entry("up", bits_of(*range(31))),
                          entry("orig", bits_of(*range(2, 33)))])
    payload = build_review(
        clustering,
        quality={
            "up": _quality(6000, 4500, sharp=30.0, recomp=0.1, jq=92),
            "orig": _quality(3000, 2250, sharp=150.0, recomp=0.1, jq=92),
        },
    )
    assert payload["summary"]["contested_groups"] == 1
    assert payload["groups"][0]["best_copy"]["contested"][0]["better_in"] == ["резкости"]


def test_the_three_metrics_travel_with_each_member_so_the_phrase_is_checkable():
    clustering = cluster([entry("a", bits_of(*range(31))),
                          entry("b", bits_of(*range(2, 33)))])
    group = build_review(
        clustering, quality={"a": _quality(4000, 3000), "b": _quality(1000, 750)}
    )["groups"][0]
    member = [m for m in group["members"] if m["content_hash"] == "a"][0]
    assert member["sharpness"] == 50.0
    assert member["jpeg_quality"] == 92
    # Оценка сжатия без основания — приглашение сравнить несравнимое (Р2),
    # поэтому они едут парой.
    assert member["recompression_basis"] == "jpeg_quant_tables"
