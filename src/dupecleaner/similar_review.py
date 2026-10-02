r"""Похожие снимки для экрана просмотра: тип группы, объяснение, ручка порога.

Это слой над `similar.py` и ничего в нём не меняет. Задача 13 посчитала
отпечатки и собрала группы; задача 14 обязана показать их человеку так,
чтобы он мог **не поверить** инструменту: увидеть, на каком расстоянии
снимки стоят друг от друга, чем они отличаются по размеру и разрешению, и
была ли группа собрана цепочкой. Все три ответа здесь — и ни один из них
не даёт права двигать файлы.

Почему отдельный модуль, а не функции в `similar.py`
----------------------------------------------------
`similar.py` — ядро: отпечаток, расстояние, объединение в группы. У него нет
и не должно быть представления о том, что такое «кадр серии», «эталон
сравнения» или «подсказка под ручкой порога»: это свойства экрана, а не
алгоритма, и формулируются они в терминах, которые алгоритм не знает
(момент съёмки из задачи 16, метрики качества из задачи 9). Разделение
такое же, как между `keeper.py` (правило Р8) и `keeper.keeper_reason`
(объяснение правила человеку) — только здесь объяснять приходится не
решение, а его отсутствие.

Чего здесь нет, и это проверяется тестом
-----------------------------------------
Ни `keeper`, ни «лучшая копия», ни «освободится N байт». Группа похожих —
список на просмотр (Р0, ось B; Р2), и единственный честный способ это
удержать — не производить объект, который можно было бы передать в
карантин. Поэтому модуль импортирует только `similar` — ни `models`, ни
`quarantine` — и возвращает словари, а не `DuplicateGroup`. `tests/test_similar_review.py` держит это, включая
проверку, что в выдаче нет ни одного ключа со словом keeper.

Опорный снимок — это точка отсчёта, а не рекомендация
------------------------------------------------------
Расстояние Хэмминга — свойство пары, а в группе из восьми снимков пар
двадцать восемь. Чтобы «6 бит» на экране означало что-то конкретное, нужна
одна фиксированная точка, от которой считаются все расстояния: ею взят
самый тяжёлый по байтам снимок группы (`reference`). Почему он: из всех
дешёвых признаков размер файла — единственный, который есть у каждого
снимка в индексе (разрешение есть только у тех, что попали в группы
дублей, см. ниже), и пережатая или уменьшенная копия почти всегда легче
оригинала. Но это **не** выбор «какую копию оставить» — такого выбора в
группе похожих не существует (Р2), и задача 17 будет ранжировать копии по
качеству, а не по тому, кто здесь оказался опорным. В выдаче он помечен
`is_reference`, и в интерфейсе подписан как «опорный для сравнения».

Чего не хватает, честно
------------------------
Разрешение (`source_width`/`source_height`) лежит в `content_previews` —
то есть только у снимков, которые попали в группы точных дублей
(`_preview_phase`). У остальных `_similar_phase` намеренно не пишет
миниатюру (бюджет кэша Р9), поэтому разрешения у них нет, и «разница в
разрешении» для таких пар показывается как разница в байтах. Это
измеримая дыра, а не оценка: см. отчёт задачи 14. Закрывается двумя
колонками в `content_phashes` и одной миграцией — сознательно не сделано в
этой задаче, потому что схему трогать ради подписи на экране дороже, чем
сказать правду о том, чего не знаешь.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Sequence

from .similar import (
    PHASH_BITS,
    SimilarClustering,
    SimilarGroup,
    SimilarThresholds,
    distance,
)

# --- ручка порога ----------------------------------------------------------
#
# Только чётные значения, и это не вкусовщина. В отпечатке ровно 31 единица
# по построению (бит ставится по медиане 63 коэффициентов), поэтому любые
# два отпечатка различаются чётным числом бит, и порог 7 — это в точности
# порог 6. Нечётные деления ручки не делали бы ничего: половина хода
# впустую, и человек, который подвигал ручку и не увидел изменений, делает
# из этого неверный вывод о методе, а не о ручке.
UI_THRESHOLDS: tuple[int, ...] = (0, 2, 4, 6, 8, 10, 12, 14, 16)

# Порог по умолчанию. Измерен, а не выбран: на контрольном наборе из 40
# настоящих снимков шесть бит покрывают девять вариантов пересохранения и
# уменьшения из десяти, оставаясь втрое ближе, чем ближайшая пара разных
# снимков (18 бит). Подробности и таблица — в
# claude/task-13-threshold-measurements.md.
DEFAULT_UI_THRESHOLD = 6

# Что каждый порог дал на библиотеке Азиза: 6082 уникальных содержимых из
# пяти папок `D:\Photos`. Цифры из замеров задачи 13 — они едут в интерфейс
# как ориентир («что будет, если подвинуть»), и помечены там как замер на
# конкретной библиотеке, а не как обещание. Живое число групп для
# выбранного порога считается тут же и показывается рядом: когда замер и
# факт расходятся, видно оба.
#
# `chains` — группы, собравшиеся цепочкой (разброс больше порога).
MEASURED_REFERENCE: dict[int, dict[str, int]] = {
    0: {"groups": 153, "contents": 323, "largest": 4, "chains": 0},
    2: {"groups": 347, "contents": 780, "largest": 6, "chains": 30},
    4: {"groups": 529, "contents": 1237, "largest": 6, "chains": 51},
    6: {"groups": 665, "contents": 1638, "largest": 8, "chains": 81},
    8: {"groups": 792, "contents": 2039, "largest": 9, "chains": 116},
    10: {"groups": 893, "contents": 2409, "largest": 12, "chains": 157},
    12: {"groups": 974, "contents": 2757, "largest": 13, "chains": 187},
    14: {"groups": 1011, "contents": 3055, "largest": 14, "chains": 204},
    16: {"groups": 980, "contents": 3514, "largest": 94, "chains": 264},
}

MEASURED_ON = "6082 уникальных снимка из пяти папок D:\\Photos (замер задачи 13)"


def normalise_threshold(value: object) -> int:
    """Привести запрошенный порог к значению, которое ручка может выдать.

    Нечётное округляется **вниз** до чётного, потому что 7 и 6 — один и тот
    же порог по построению (см. UI_THRESHOLDS), и округление вверх молча
    расширило бы поиск сильнее, чем просили. За границами — зажимается.
    """
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_UI_THRESHOLD
    number = max(UI_THRESHOLDS[0], min(UI_THRESHOLDS[-1], number))
    return number - (number % 2)


# --- тип группы ------------------------------------------------------------
#
# Главная честная ошибка метода, названная в замерах задачи 13: объединение
# пар в группы — одноцепочечное, поэтому A∼B и B∼C дают одну группу даже
# когда A и C дальше порога. На реальной библиотеке при пороге 12 так
# собиралась группа из 13 кадров одной свадьбы с разбросом 24: одна
# комната, один пол, те же люди — и разные фотографии.
#
# Поэтому у группы два разных типа, а не один с оговоркой:
#   COPIES — клика: каждый снимок в пределах порога от каждого. «Одна
#            картинка, снятая или сохранённая несколько раз».
#   SCENE  — цепочка: разброс больше порога. «Похожая сцена» — список на
#            просмотр, но не группа копий, и подписывать её как группу
#            копий значит соврать в том месте, где поверят.
KIND_COPIES = "copies"
KIND_SCENE = "scene"

# Группа крупнее MAX_EXACT_SPREAD приходит со spread=None: точный разброс
# стоил бы больше пар, чем он стоит. Такая группа считается сценой — не
# потому что это измерено, а потому что при неизвестном разбросе
# утверждение «это копии» ничем не подкреплено, а сама величина группы уже
# находка.
def group_kind(group: SimilarGroup, max_distance: int) -> str:
    if group.spread is None:
        return KIND_SCENE
    return KIND_COPIES if group.spread <= max_distance else KIND_SCENE


# --- «почему похожи» -------------------------------------------------------

# Снимки, сделанные в пределах этого времени друг от друга, подписываются
# как соседние кадры серии. Десять секунд — из замеров задачи 13: почти всё,
# что находится при порогах 4–12 на этой библиотеке, это серии вида
# `20211229_185451` / `20211229_185452` и `IMG_3155` / `IMG_3157`, то есть
# секунды. Подпись и ничего больше: она не меняет ни группировку, ни
# порог, и ошибка в ней стоит неверного слова на экране, а не файла.
BURST_WINDOW_SECONDS = 10.0

# Во сколько раз файл должен быть легче опорного, чтобы называться
# пережатой или уменьшенной копией. 1.6 — с запасом ниже того, что даёт
# любой реальный пересжимающий конвейер (пересохранение q70 и уменьшение
# вдвое из контрольного набора дают кратность заметно выше), и выше
# разброса между двумя кадрами одной серии, снятыми одной камерой подряд.
RECOMPRESSED_SIZE_RATIO = 1.6

# Разница в байтах, ниже которой два файла считаются «одной и той же
# картинкой под двумя именами»: находка пилота `20210927_073914.jpg` и
# `2021-09-27 07-39-14.JPG` — это один кадр, переименованный Google Photos,
# и по байтам он совпадает почти точно, но не побайтно (иначе он был бы
# точным дублем и жил бы на другой вкладке).
SAME_PICTURE_SIZE_TOLERANCE = 0.05


def _size_ratio(member_size: int, reference_size: int) -> float | None:
    if not member_size or not reference_size:
        return None
    return reference_size / member_size


def _resolution(quality: Mapping[str, object] | None) -> tuple[int, int] | None:
    if not quality:
        return None
    width = quality.get("source_width")
    height = quality.get("source_height")
    if not width or not height:
        return None
    return int(width), int(height)  # type: ignore[arg-type]


def member_labels(
    *,
    bits_distance: int,
    max_distance: int,
    size_ratio: float | None,
    resolution: tuple[int, int] | None,
    reference_resolution: tuple[int, int] | None,
    seconds_apart: float | None,
) -> list[str]:
    """Короткие ярлыки, каждый из которых человек может проверить глазами.

    Порядок намеренный: сначала то, что отвечает на вопрос «это вообще один
    кадр?», потом то, что отвечает «а чем отличается». Ни один ярлык не
    является выводом о ценности — ярлык «пережатая копия» говорит про
    байты и отпечаток, а не про то, что копию можно удалить.
    """
    labels: list[str] = []

    if bits_distance > max_distance:
        # Внутри цепочки такие пары есть по построению, и это ровно та
        # ошибка, которую группа обязана показать, а не спрятать.
        labels.append("дальше порога")

    if seconds_apart is not None and seconds_apart <= BURST_WINDOW_SECONDS:
        labels.append("кадр серии")

    smaller = size_ratio is not None and size_ratio >= RECOMPRESSED_SIZE_RATIO
    downscaled = (
        resolution is not None
        and reference_resolution is not None
        and (resolution[0] * resolution[1]) < (reference_resolution[0] * reference_resolution[1])
    )
    if downscaled:
        labels.append("уменьшенная копия")
    elif smaller:
        labels.append("пережатая копия")

    if (
        not labels
        and size_ratio is not None
        and abs(size_ratio - 1.0) <= SAME_PICTURE_SIZE_TOLERANCE
    ):
        labels.append("тот же кадр под другим именем")

    return labels


def group_explanation(
    group: SimilarGroup,
    kind: str,
    thresholds: SimilarThresholds,
    *,
    burst: bool,
) -> list[str]:
    """Почему эти снимки оказались вместе — фразами, а не числами в вакууме."""
    lines: list[str] = []
    if group.spread is None:
        lines.append(
            f"Группа из {group.size} снимков — разброс внутри не измерялся "
            "(слишком много пар). Такая группа почти наверняка собралась "
            "цепочкой: смотреть её стоит первой."
        )
    elif kind == KIND_SCENE:
        lines.append(
            f"Разброс внутри группы {group.spread} бит при пороге "
            f"{thresholds.max_distance} — группа собралась цепочкой: A похож на "
            "B, B на C, а крайние уже нет. Это похожая сцена, а не группа копий."
        )
    else:
        lines.append(
            f"Каждый снимок в пределах {group.spread} бит из {PHASH_BITS} от "
            f"остальных (порог {thresholds.max_distance}) — это одна картинка, "
            "сохранённая или снятая несколько раз."
        )

    if burst:
        lines.append(
            "Снимки сделаны в пределах "
            f"{int(BURST_WINDOW_SECONDS)} секунд друг от друга — это соседние "
            "кадры серии. На этой библиотеке так выглядит большинство находок: "
            "серии, а не пережатые копии."
        )

    lines.append(
        "Похожие никогда не уходят в карантин — ни в одном режиме и ни при "
        "каком пороге (Р0, Р2). Это список на просмотр глазами."
    )
    return lines


# --- сборка ответа для экрана ---------------------------------------------


def build_review(
    clustering: SimilarClustering,
    *,
    quality: Mapping[str, Mapping[str, object]] | None = None,
    taken_at: Mapping[str, float] | None = None,
    coverage: Mapping[str, int] | None = None,
) -> dict:
    """Кластеризацию — в то, что рисует экран.

    `quality` — метрики по хэшу содержимого (задача 9), `taken_at` — момент
    съёмки по `display_path` (задача 16). Оба необязательны и оба
    неполны по природе: без них объяснение беднее, но не врёт — ярлык, для
    которого нет данных, просто не появляется.
    """
    quality = quality or {}
    taken_at = taken_at or {}
    max_distance = clustering.thresholds.max_distance

    groups: list[dict] = []
    chains = 0
    bursts = 0

    for group in clustering.groups:
        reference = max(group.members, key=lambda m: (m.size, m.content_hash))
        ref_quality = quality.get(reference.content_hash)
        ref_resolution = _resolution(ref_quality)
        ref_time = _earliest_time(reference.paths, taken_at)

        kind = group_kind(group, max_distance)
        if kind == KIND_SCENE:
            chains += 1

        members: list[dict] = []
        times: list[float] = []
        for member in group.members:
            member_quality = quality.get(member.content_hash)
            resolution = _resolution(member_quality)
            member_time = _earliest_time(member.paths, taken_at)
            if member_time is not None:
                times.append(member_time)
            bits_distance = distance(reference.bits, member.bits)
            ratio = _size_ratio(member.size, reference.size)
            seconds_apart = (
                None
                if member_time is None or ref_time is None
                else abs(member_time - ref_time)
            )
            members.append(
                {
                    "content_hash": member.content_hash,
                    "phash": f"{member.bits:016x}",
                    "distance": bits_distance,
                    "size": member.size,
                    "size_ratio": None if ratio is None else round(ratio, 2),
                    "aspect": round(member.aspect, 3),
                    "resolution": (
                        None if resolution is None else f"{resolution[0]}×{resolution[1]}"
                    ),
                    "megapixels": (member_quality or {}).get("megapixels"),
                    "taken_at": member_time,
                    "seconds_from_reference": seconds_apart,
                    "is_reference": member.content_hash == reference.content_hash,
                    "labels": member_labels(
                        bits_distance=bits_distance,
                        max_distance=max_distance,
                        size_ratio=ratio,
                        resolution=resolution,
                        reference_resolution=ref_resolution,
                        seconds_apart=seconds_apart,
                    ),
                    # Путь нужен экрану для превью и чтобы человек нашёл
                    # файл у себя. Для обычного файла `display_path` — это
                    # и есть путь на диске (архивных участников
                    # `phash_rows` не отдаёт вовсе).
                    "paths": list(member.paths),
                }
            )

        burst = len(times) >= 2 and (max(times) - min(times)) <= BURST_WINDOW_SECONDS
        if burst:
            bursts += 1

        groups.append(
            {
                "id": group.members[0].content_hash,
                "kind": kind,
                "size": group.size,
                "file_count": group.file_count,
                "spread": group.spread,
                "burst": burst,
                "explanation": group_explanation(
                    group, kind, clustering.thresholds, burst=burst
                ),
                "members": members,
            }
        )

    summary = dict(clustering.summary)
    summary["chain_groups"] = chains
    summary["copy_groups"] = len(clustering.groups) - chains
    summary["burst_groups"] = bursts

    return {
        "threshold": threshold_block(clustering.thresholds, len(clustering.groups)),
        "summary": summary,
        "coverage": dict(coverage or {}),
        "warnings": list(clustering.warnings),
        "groups": groups,
    }


def _earliest_time(
    paths: Sequence[str], taken_at: Mapping[str, float]
) -> float | None:
    """Момент съёмки для содержимого, у которого на диске несколько копий.

    Самый ранний из известных: у копий одного снимка моменты либо
    совпадают, либо один из файлов потерял EXIF при пересохранении и
    получил время из имени, которое ему дал конвейер. Ранний — это тот,
    который ближе к съёмке.
    """
    known = [taken_at[p] for p in paths if p in taken_at and taken_at[p] is not None]
    return min(known) if known else None


def threshold_block(thresholds: SimilarThresholds, live_groups: int) -> dict:
    """Всё, что нужно подписи под ручкой порога.

    Живое число групп и замер задачи 13 лежат рядом: замер говорит, что
    будет, если подвинуть ручку (пересчитывать всю таблицу на каждое
    движение — это девять кластеризаций на 6000 снимках), живое число
    говорит, что получилось на самом деле. Расходятся они ровно настолько,
    насколько эта библиотека отличается от той, на которой мерили, и это
    полезно видеть, а не прятать.
    """
    return {
        "max_distance": thresholds.max_distance,
        "bits": PHASH_BITS,
        "allowed": list(UI_THRESHOLDS),
        "default": DEFAULT_UI_THRESHOLD,
        "even_only_note": (
            "Шаг ручки — 2. В отпечатке ровно 31 единица из "
            f"{PHASH_BITS} бит по построению, поэтому расстояния всегда "
            "чётные и порог 7 — это тот же порог 6."
        ),
        "describe": thresholds.describe(),
        "live_groups": live_groups,
        "measured_on": MEASURED_ON,
        "measured": {str(k): v for k, v in MEASURED_REFERENCE.items()},
    }


def thresholds_for(
    max_distance: object, base: SimilarThresholds | None = None
) -> SimilarThresholds:
    """Пороги для запроса с экрана: меняется только расстояние.

    Форма кадра и предел корзины остаются решениями задачи 13 — ручка
    выставляет то, что человек в состоянии проверить глазами, а не каждый
    параметр алгоритма. `--aspect-tolerance` и `--max-bucket` остаются в
    CLI, где у них есть объяснение в `--help`.
    """
    base = base or SimilarThresholds()
    return replace(base, max_distance=normalise_threshold(max_distance))
