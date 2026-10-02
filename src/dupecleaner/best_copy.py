r"""Какая из похожих копий лучше по качеству — и почему именно она.

Задача 17. Это **не** Р8 и не `keeper.py`. Там копии байт-в-байт равны,
выбор идёт по расположению файла, и у выбора есть право на действие: одна
копия остаётся, остальные уезжают. Здесь копии разного качества, у выбора
права на действие нет и не будет (Р0 ось B, Р2), а сам выбор опирается не
на путь, а на три числа из индекса (задача 9, `quality.py`): разрешение,
резкость, сила сжатия.

Что здесь считается подсказкой, а не решением
----------------------------------------------
Р2 разрешает качеству быть только ранжированием внутри группы похожих —
«оставить лучшую копию», и ничего больше. Модуль возвращает словарь с
хэшем лучшей копии и фразой-объяснением; он не производит ни
`DuplicateGroup`, ни пути для перемещения, ни оценку освобождаемого
места, и не импортирует ни `models`, ни `quarantine`. Это та же защита
формой, которой задача 14 держит всю вкладку: пути в карантин не
существует, а не «он закрыт проверкой».

Лестница признаков, и почему в этом порядке
--------------------------------------------
Порядок взят из таблицы Р2 «чего каждое число стоит», от факта к оценке:

1. **Разрешение** — читается из заголовка файла до всякого
   декодирования. Р2 называет его фактом, единственным из трёх.
2. **Сила сжатия** — у JPEG берётся из таблиц квантования, то есть из
   настроек компрессора, записанных в сам файл: это **чтение**, а не
   вывод. Но сравнивать можно только оценки с одним основанием
   (`recompression_basis`), и это требование Р2, а не предосторожность:
   HEIC при 0.5 бит/пиксель выглядит хорошо, JPEG при тех же 0.5 — нет,
   а `lossless` 0.0 у PNG значит «в этом файле ничего не терялось», а не
   «это оригинал». Если у измеренных копий основания разные, ступень
   выключается целиком и об этом сказано в выдаче.
3. **Резкость** — дисперсия лапласиана, нормированная по размеру
   (`quality.measure_sharpness`). Величина относительная: она сравнима
   между файлами, прошедшими один конвейер, и не имеет абсолютного
   смысла. Поэтому она ниже сжатия, хотя отвечает на самый интересный
   вопрос — «в какой копии больше настоящей детали».
4. **Вес файла** — последний довод, ровно как «короче путь» в Р8. Больше
   байт на ту же картинку обычно значит меньше сжатия, но это косвенный
   признак, который легко обмануть (пустые метаданные, превью внутри
   JPEG), и ставить его выше измеренного было бы странно.

Порядок — строгий лексикографический, как `keeper.keeper_key`:
допуски в сравнение не вмешиваются, иначе отношение «примерно равно»
перестало бы быть транзитивным и порядок зависел бы от того, с какой
копии начать. Допуски работают там, где им место, — в **объяснении**:
они решают, назвать ли перевес убедительным (`confident`) и не выигрывает
ли какая-то другая копия по другому признаку (`contested`).

Чего этот модуль не знает
--------------------------
Что на снимке. Из двух кадров серии «лучший» по этим трём числам — это
самый резкий и меньше всех сжатый кадр, а не тот, где все открыли глаза.
Поэтому `rank_copies` обязан получать тип группы: в группе-цепочке
(«похожая сцена», задача 14) лучшей копии не существует, и выдача там
подписана как сравнение технического качества, а не как выбор копии.
"""

from __future__ import annotations

from dataclasses import dataclass

# Насколько должен быть перевес, чтобы называться перевесом, а не шумом.
# Эти числа ничего не меняют в порядке копий — только в словах о нём.
#
# Разрешение: 10% по числу пикселей это ~5% по длинной стороне. Пара
# 4032x3024 против 4000x3000 (та же камера, другая прошивка) остаётся
# ничьёй, уменьшение вдвое — нет.
RESOLUTION_TIE_RATIO = 1.10

# Сжатие: 0.10 по шкале 0..1 это примерно 4 пункта фактора качества JPEG
# в середине шкалы (92..50 растянуты на 0..1). Пересохранение из
# мессенджера даёт заметно больше.
RECOMPRESSION_TIE = 0.10

# Резкость — самая шумная из трёх: она снимается с картинки, уже
# уменьшенной `draft()` до 240 px, и считается в 8 битах с насыщением
# (см. `quality.measure_sharpness`). Четверть — намеренно щедрый допуск:
# лучше промолчать о перевесе, чем назвать убедительным то, что получено
# округлением.
SHARPNESS_TIE_RATIO = 1.25

# И у этого шума есть направление, измеренное при написании задачи 17.
# Один кадр, сохранённый с качеством 96 и пересохранённый с качеством 40:
# резкость 3.7 против **6.6**, то есть сильнее сжатая копия оказалась в
# 1.8 раза «резче». Это блочность JPEG — края блоков 8x8 это и есть
# высокочастотные края, а лапласиан меряет именно их, и уменьшение до
# 240 px их не убирает. Уменьшение тоже добавляет энергии на краях
# (LANCZOS звенит): та же картинка в 640 px дала 4.8 против 3.5 в
# 1200 px.
#
# Замер синтетический и гладкий, то есть завышает эффект: на снимке с
# настоящей деталью артефакт делит знаменатель с ней. Но направление
# ошибки от этого не меняется, а оно здесь и важно:
#
#   **перевес в резкости у более сжатой копии — это не свидетельство
#   детали.** В лестнице это не мешает (резкость решает только когда
#   оценки сжатия совпали в точности), а вот предупреждать «другая копия
#   резче» в таком случае значило бы звать человека смотреть на
#   артефакты. См. `contested_by`.

# Вес: 5%, то же число, которым задача 14 считает «тот же кадр под другим
# именем».
SIZE_TIE_RATIO = 1.05

# Фактор качества JPEG — вторая половина ступени сжатия, и она нужна
# потому, что оценка 0..1 **насыщается с двух концов**: всё, что сохранено
# с качеством 92 и выше, получает 0.0, всё, что с 50 и ниже — 1.0
# (`quality._QUALITY_CLEAN/_QUALITY_SQUEEZED`). Пара q95 и q92 это одна и
# та же оценка 0.000, и без этой ступени выбор падал бы на резкость —
# самую шумную метрику из трёх. Проверено аудитом задачи 17: именно так
# оно и падало, и в двух случаях из трёх выбирало пересохранение.
#
# Число читается из таблиц квантования, то есть из настроек компрессора,
# записанных в сам файл, — поэтому оно стоит выше резкости, а не ниже. И
# только при основании `jpeg_quant_tables`: у двух копий с разными
# основаниями сравнивать нечего (Р2), а HEIC фактора качества не имеет
# вовсе.
JPEG_QUALITY_TIE = 3

LEVEL_RESOLUTION = "resolution"
LEVEL_RECOMPRESSION = "recompression"
LEVEL_SHARPNESS = "sharpness"
LEVEL_SIZE = "size"
LEVEL_SINGLE = "single"
LEVEL_EQUAL = "equal"

# Как назвать основание шкалы сжатия человеку. Ключи — значения
# `quality.BASIS_*`; они здесь строками, чтобы модуль не тянул за собой
# Pillow ради трёх подписей.
_BASIS_LABEL = {
    "jpeg_quant_tables": "таблицы квантования JPEG",
    "bits_per_pixel": "бит на пиксель",
    "lossless": "сжатие без потерь",
}


@dataclass(frozen=True)
class Candidate:
    """Одна копия похожего снимка глазами ранжирования.

    `pixels is None` значит «разрешение не измерялось» — такая копия в
    ранжирование не попадает вовсе. Это не ноль и не «плохая копия»:
    поставить её последней означало бы соврать о том, чего мы не знаем
    (та же логика, по которой задача 14 пишет «разрешение не
    измерялось», а не «0×0»).
    """

    content_hash: str
    size: int
    pixels: int | None = None
    width: int | None = None
    height: int | None = None
    sharpness: float | None = None
    recompression: float | None = None
    recompression_basis: str | None = None
    jpeg_quality: int | None = None

    @property
    def measured(self) -> bool:
        return self.pixels is not None and self.recompression_basis is not None

    @property
    def resolution_text(self) -> str:
        if not self.width or not self.height:
            return "разрешение не измерялось"
        return f"{self.width}×{self.height}"


def candidate_from(member: dict, quality: dict | None) -> Candidate:
    """Собрать кандидата из того, что уже лежит в выдаче экрана.

    `member` — словарь участника группы (нужны только `content_hash` и
    `size`), `quality` — строка метрик по хэшу содержимого (задача 9).
    Отсутствие метрик — обычный случай, а не ошибка: см. отчёт задачи 14.
    """
    quality = quality or {}
    width = quality.get("source_width")
    height = quality.get("source_height")
    pixels = int(width) * int(height) if width and height else None
    recompression = quality.get("recompression")
    return Candidate(
        content_hash=member["content_hash"],
        size=int(member.get("size") or 0),
        pixels=pixels,
        width=int(width) if width else None,
        height=int(height) if height else None,
        sharpness=(
            None if quality.get("sharpness") is None else float(quality["sharpness"])
        ),
        recompression=None if recompression is None else float(recompression),
        recompression_basis=quality.get("recompression_basis"),
        jpeg_quality=quality.get("jpeg_quality"),
    )


def common_basis(candidates: list[Candidate]) -> str | None:
    """Основание шкалы сжатия, если оно у всех измеренных копий одно.

    Иначе None — и тогда ступень сжатия выключается для всей группы, а не
    для отдельной пары. Выключать по парам значило бы получить порядок,
    зависящий от того, какие копии сравнивались первыми.
    """
    bases = {c.recompression_basis for c in candidates if c.measured}
    return bases.pop() if len(bases) == 1 else None


def quality_key(candidate: Candidate, basis: str | None) -> tuple:
    """Ключ сортировки: меньше — лучше, как у `keeper.keeper_key`.

    Четыре ступени лестницы из докстринга модуля плюс хэш содержимого
    пятым, чтобы порядок был определён до конца и не зависел от того, в
    каком порядке база отдала строки.
    """
    return (
        -(candidate.pixels or 0),
        candidate.recompression if basis and candidate.recompression is not None else 0.0,
        -(
            candidate.jpeg_quality or 0
            if basis == "jpeg_quant_tables" and candidate.jpeg_quality is not None
            else 0
        ),
        -(candidate.sharpness or 0.0),
        -candidate.size,
        candidate.content_hash,
    )


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """Измеренные копии по убыванию качества. Неизмеренные отброшены."""
    measured = [c for c in candidates if c.measured]
    basis = common_basis(measured)
    return sorted(measured, key=lambda c: quality_key(c, basis))


def _deciding_level(best: Candidate, runner: Candidate, basis: str | None) -> str:
    """На какой ступени лучшая копия впервые обошла следующую.

    Тот же приём, что в `keeper.keeper_reason`: не описывать победителя, а
    назвать место, где сравнение впервые разошлось. Иначе объяснение
    рискует разойтись с правилом, которое оно описывает.
    """
    a = quality_key(best, basis)
    b = quality_key(runner, basis)
    # Две позиции ключа — одна ступень: оценка сжатия и фактор качества
    # JPEG под ней отвечают на один вопрос, просто вторая видит внутри
    # насыщенных концов первой.
    for index, level in enumerate(
        (
            LEVEL_RESOLUTION,
            LEVEL_RECOMPRESSION,
            LEVEL_RECOMPRESSION,
            LEVEL_SHARPNESS,
            LEVEL_SIZE,
        )
    ):
        if a[index] != b[index]:
            return level
    return LEVEL_EQUAL


def _fmt_sharpness_pair(a: float | None, b: float | None) -> tuple[str, str]:
    """Два числа резкости так, чтобы они отличались на письме, если
    отличаются на самом деле.

    Иначе объяснение читается как «резче: 7 против 7» — поймано аудитом
    задачи 17 на двух кадрах серии, и это тот сорт строки, после которого
    перестают верить всему экрану. Если они не отличаются и в третьем
    знаке, так и сказано.
    """
    if a is None or b is None:
        return ("—", "—")
    # Начиная с одного знака, а не с нуля: 6.6 и 6.5 округляются до «7» и
    # «6» и читаются как разница в единицу вместо разницы в десятую —
    # поймано на том же аудите, что и «7 против 7».
    for digits in (1, 2, 3):
        left, right = f"{a:.{digits}f}", f"{b:.{digits}f}"
        if left != right:
            return (left, right)
    return (f"{a:.3f}", "столько же до третьего знака")


def reason(
    best: Candidate, runner: Candidate | None, basis: str | None
) -> tuple[str, str, bool]:
    """`(текст, ступень, убедительно ли)` — почему лучшей названа эта копия.

    `confident` отвечает на вопрос, который человек задаст следующим:
    перевес настоящий или в пределах погрешности метрики. Формулировки —
    в терминах метрик, как требует задача: «выше разрешение», «меньше
    следов пережатия», «резче».
    """
    if runner is None:
        return (
            "единственная копия с измеренными метриками — сравнивать не с чем",
            LEVEL_SINGLE,
            False,
        )

    level = _deciding_level(best, runner, basis)

    if level == LEVEL_RESOLUTION:
        ratio = (best.pixels or 0) / max(1, runner.pixels or 1)
        return (
            f"выше разрешение: {best.resolution_text} против "
            f"{runner.resolution_text} у следующей копии",
            level,
            ratio >= RESOLUTION_TIE_RATIO,
        )

    if level == LEVEL_RECOMPRESSION:
        gap = abs((runner.recompression or 0.0) - (best.recompression or 0.0))
        confident = gap >= RECOMPRESSION_TIE
        if (
            basis == "jpeg_quant_tables"
            and best.jpeg_quality is not None
            and runner.jpeg_quality is not None
        ):
            detail = (
                f"сохранена с качеством JPEG {best.jpeg_quality} против "
                f"{runner.jpeg_quality}"
            )
            # Внутри насыщенного конца шкалы (q >= 92 -> оценка 0.000)
            # убедительность обязана считаться по фактору качества, иначе
            # любой перевес там называется ничьёй.
            confident = confident or (
                best.jpeg_quality - runner.jpeg_quality >= JPEG_QUALITY_TIE
            )
        else:
            detail = (
                f"{best.recompression:.2f} против {runner.recompression:.2f} "
                f"по шкале «{_BASIS_LABEL.get(basis or '', basis or '?')}»"
            )
        return (f"меньше следов пережатия: {detail}", level, confident)

    if level == LEVEL_SHARPNESS:
        ratio = (best.sharpness or 0.0) / max(1e-9, runner.sharpness or 1e-9)
        left, right = _fmt_sharpness_pair(best.sharpness, runner.sharpness)
        return (
            f"резче при одинаковом размере показа: {left} против {right} "
            "по дисперсии лапласиана (величина относительная, сравнима "
            "только внутри группы)",
            level,
            ratio >= SHARPNESS_TIE_RATIO,
        )

    if level == LEVEL_SIZE:
        ratio = best.size / max(1, runner.size)
        return (
            "разрешение, сжатие и резкость неотличимы — выбрана как более "
            "тяжёлый файл на ту же картинку (косвенный признак, последний "
            "довод)",
            level,
            ratio >= SIZE_TIE_RATIO,
        )

    return (
        "копии неотличимы по всем трём метрикам и по весу — выбрана по хэшу "
        "содержимого, чтобы порядок был устойчив между запросами",
        LEVEL_EQUAL,
        False,
    )


def contested_by(best: Candidate, others: list[Candidate], basis: str | None) -> list[dict]:
    """Копии, которые заметно обходят лучшую хотя бы по одному признаку.

    Лестница — лексикографическая, поэтому победитель верхней ступени
    выигрывает группу даже проиграв две нижние. Это сознательно: иначе
    пришлось бы складывать метрики с разными смыслами и разной
    надёжностью в один балл, а Р2 прямо говорит, чего каждое число стоит.
    Но молчать об этом нельзя, потому что так выглядит главная ошибка
    этого правила: увеличенная копия (разрешение выше, настоящих деталей
    не больше) обходит оригинал на первой же ступени, а оригинал остаётся
    резче и менее сжатым. Здесь это становится видно, а не прячется.

    По разрешению лучшую копию обойти нельзя по построению — она и есть
    копия с наибольшим числом пикселей, это первая ступень лестницы.
    Спорить могут только две нижние.
    """
    out: list[dict] = []
    for other in others:
        wins: list[str] = []
        if (
            basis
            and other.recompression is not None
            and best.recompression is not None
            and best.recompression - other.recompression >= RECOMPRESSION_TIE
        ):
            wins.append("следам пережатия")
        # Перевес в резкости считается только у копии, которая не сжата
        # заметно сильнее выбранной: иначе это почти наверняка блочность,
        # а не деталь (замер — у SHARPNESS_TIE_RATIO выше). Когда
        # основания разные и сжатие сравнить нельзя, исключить эту
        # причину нечем, и тогда перевес называется — предупреждение
        # ценой ложной тревоги дешевле молчания.
        artefact_suspect = (
            basis is not None
            and other.recompression is not None
            and best.recompression is not None
            and other.recompression - best.recompression >= RECOMPRESSION_TIE
        )
        if (
            other.sharpness is not None
            and best.sharpness is not None
            and other.sharpness >= best.sharpness * SHARPNESS_TIE_RATIO
            and not artefact_suspect
        ):
            wins.append("резкости")
        if wins:
            out.append({"content_hash": other.content_hash, "better_in": wins})
    return out


# --- выдача для экрана -----------------------------------------------------

# Подписи зависят от типа группы (задача 14): в клике «копии» лучшая копия
# это осмысленный ответ, в цепочке «похожая сцена» — нет, там кадры разные.
HEADLINE_CHOICE = "Лучшая копия по качеству"
HEADLINE_SCENE = (
    "Кадры в группе разные — это сравнение технического качества, а не "
    "выбор копии"
)


def rank_copies(
    members: list[dict],
    quality: dict,
    *,
    is_copy_group: bool,
) -> dict:
    """Блок «лучшая копия» для одной группы похожих.

    `is_copy_group` — тип группы из задачи 14: True для клики («копии»),
    False для цепочки («похожая сцена»). Деление берётся готовым и своё
    здесь не вводится: у сцены лучшей копии не существует, и единственное,
    что меняется, — подпись и слово «лучшая».

    Ничего из возвращённого не является правом на действие: ни пути, ни
    объёма, ни отметки о применении. См. докстринг модуля.
    """
    candidates = [candidate_from(m, quality.get(m["content_hash"])) for m in members]
    ordered = rank(candidates)
    basis = common_basis(ordered)
    unmeasured = len(candidates) - len(ordered)

    block: dict = {
        "is_choice": is_copy_group,
        "headline": HEADLINE_CHOICE if is_copy_group else HEADLINE_SCENE,
        "measured": len(ordered),
        "unmeasured": unmeasured,
        "basis": basis,
        "basis_label": _BASIS_LABEL.get(basis or "", basis),
        "order": [c.content_hash for c in ordered],
        "best": None,
        "reason": None,
        "level": None,
        "confident": False,
        "contested": [],
        "notes": [],
    }

    if len(ordered) < 2:
        if not ordered:
            block["notes"].append(
                "Ранжировать не на чем: ни у одной из "
                f"{len(candidates)} копий нет метрик качества. Разрешение, "
                "резкость и сила сжатия считаются при полной обработке и "
                "живут в индексе по хэшу содержимого."
            )
        else:
            block["notes"].append(
                f"Метрики есть только у одной копии из {len(candidates)} — "
                "сравнивать не с чем."
            )
        if ordered:
            text, level, confident = reason(ordered[0], None, basis)
            block.update(
                best=ordered[0].content_hash,
                reason=text,
                level=level,
                confident=confident,
            )
        return block

    best, runner = ordered[0], ordered[1]
    text, level, confident = reason(best, runner, basis)
    block.update(best=best.content_hash, reason=text, level=level, confident=confident)
    block["contested"] = contested_by(best, ordered[1:], basis)

    if basis is None:
        block["notes"].append(
            "Силу сжатия сравнить нельзя: у копий разные основания оценки "
            "(JPEG по таблицам квантования, HEIC по битам на пиксель, PNG "
            "как сжатие без потерь) — такие числа несопоставимы между собой "
            "(Р2). Сравнивались разрешение, резкость и вес. Резкости в такой "
            "группе стоит верить меньше обычного: отличить настоящую деталь "
            "от блочности сжатия здесь нечем."
        )
    if unmeasured:
        block["notes"].append(
            f"Ещё {unmeasured} копий без метрик — они не участвуют в "
            "сравнении, а не оказываются худшими."
        )
    if not confident and level in (LEVEL_RESOLUTION, LEVEL_RECOMPRESSION, LEVEL_SHARPNESS):
        block["notes"].append(
            "Перевес в пределах погрешности этой метрики — порядок устойчив, "
            "но разницу глазами вы, скорее всего, не увидите."
        )
    if block["contested"]:
        block["notes"].append(
            "Другая копия обходит выбранную по "
            + ", ".join(
                sorted({w for item in block["contested"] for w in item["better_in"]})
            )
            + ": признаки спорят между собой, и это тот случай, когда стоит "
            "посмотреть на снимки самому."
        )
    if not is_copy_group:
        block["notes"].append(
            "Группа собралась цепочкой: снимки в ней — разные кадры, поэтому "
            "«лучшая копия» здесь означала бы выбор между фотографиями, а не "
            "между сохранениями одной. Числа ниже сравнивают только "
            "техническое качество."
        )
    return block
