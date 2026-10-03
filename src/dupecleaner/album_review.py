r"""Разбор альбома по одному снимку: что оставить, что напечатать, что убрать.

Пункт 24. Задача сформулирована так: «мы потом пройдёмся по фоткам и решим
что оставить (то что красиво и стоит распечатки), а что можно удалить
навсегда». Модуль отвечает за ту часть этого, которую можно посчитать, и
молчит про ту, которую нельзя.

Чего здесь нет и не будет
--------------------------
**Оценки красоты.** Композиция, выражение лица, «тот ли это момент» не
вычисляются ни из пикселей, ни из EXIF. Любой «эстетический балл» был бы
техническими метриками, переодетыми в эстетические, то есть обманом — и
Р7 уже отложил оценку «достоин печати» именно по этой причине. Поэтому
модуль делает две вещи, обе арифметические:

1. **Отсекает заведомо непригодное для печати** — не «плохой снимок», а
   «на таком формате напечатается плохо», с названным форматом и
   названным числом мегапикселей. Это деление, а не мнение.
2. **Сортирует очередь**, чтобы годное шло первым, и одной строкой
   говорит, почему снимок на этом месте — тем же приёмом, которым
   `keeper.keeper_reason` объясняет выбор Р8, а `best_copy.reason` —
   выбор лучшей копии.

Вердикт выносит человек. Три состояния (`оставить` / `в печать` /
`убрать`) — его, модуль их только читает и раскладывает.

Почему здесь нет ни одного пути к удалению
-------------------------------------------
Это ось B из Р0: «стоит ли это хранить» знает только человек, и у оси B
структурно нет полномочий двигать файлы. «Убрать» в этом модуле — это
строка `"drop"` в индексе, и ничего больше. Перемещение делает
`quarantine.quarantine_review_drops` — тот же `journalled_move`, тот же
`journal.jsonl`, тот же `restore` (Р5, механика пункта 5). Опустошает
карантин человек руками.

Защита — формой, а не дисциплиной: модуль не импортирует ни `quarantine`,
ни `shutil`, ни `os`, ни `pathlib`, то есть в нём нет ни одного объекта,
которым можно удалить или переместить файл. Это та же защита, которой
`best_copy` держит вкладку похожих, и на неё есть тест
(`tests/test_album_review.py::test_module_imports_nothing_that_can_delete`).

Откуда берутся числа
---------------------
Ни одного нового декодирования. Разрешение, резкость, сила сжатия и
фактор качества JPEG лежат в индексе с пункта 9 и — для всей библиотеки,
а не только для групп дублей — рядом с перцептивным отпечатком с пункта
17 (миграция 12). Лица — пункт 18. Состав альбома — таблица
`library_moves`, которую записал пункт 22: один файл, один канонический
путь (Р5), и альбом это папка, в которой он лежит.

Чего числа не знают, сказано вслух: снимок, у которого разрешения в
индексе нет, помечается «не измерялось» и **остаётся в очереди**. Поставить
его последним или выбросить из фильтра значило бы соврать о том, чего мы
не знаем — та же причина, по которой `best_copy.Candidate` не считает
неизмеренную копию худшей, а задача 14 пишет «разрешение не измерялось»
вместо «0×0». Вес файла вместо разрешения не подставляется: это другое
число про другое.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Mapping, Sequence

# --- печать: арифметика, а не мнение ---------------------------------------

#: Разрешение печати, под которое считается порог. 300 dpi — то, что
#: фотолаборатория называет «фотографическим качеством»; при 150 dpi
#: пороги вчетверо ниже, и тогда это надо сказать, а не промолчать.
PRINT_DPI = 300

_MM_PER_INCH = 25.4


@dataclass(frozen=True)
class PrintFormat:
    """Формат печати и сколько мегапикселей он требует.

    Требование **считается**, а не берётся из таблицы: ширина в
    миллиметрах, делённая на 25.4 и умноженная на dpi, даёт пиксели, а их
    произведение — мегапиксели. Поэтому «нужно 8.7 МП на A4» в интерфейсе
    и в тесте — одно и то же деление, и поменять dpi можно в одном месте.
    """

    key: str
    label: str
    width_mm: float
    height_mm: float
    dpi: int = PRINT_DPI

    @property
    def pixels(self) -> tuple[int, int]:
        return (
            round(self.width_mm / _MM_PER_INCH * self.dpi),
            round(self.height_mm / _MM_PER_INCH * self.dpi),
        )

    @property
    def megapixels(self) -> float:
        width, height = self.pixels
        return round(width * height / 1_000_000, 2)

    def to_dict(self) -> dict:
        width, height = self.pixels
        return {
            "key": self.key,
            "label": self.label,
            "dpi": self.dpi,
            "width_px": width,
            "height_px": height,
            "megapixels": self.megapixels,
        }


A4 = PrintFormat("a4", "A4", 210.0, 297.0)
A5 = PrintFormat("a5", "A5", 148.0, 210.0)
PHOTO_10X15 = PrintFormat("10x15", "10×15 см", 100.0, 150.0)

#: От большего к меньшему. Порядок несущий: «самый большой формат, на
#: который хватает» — это первый подошедший из этого списка, а «заведомо
#: непригодно» — это не подошёл даже последний.
PRINT_FORMATS: tuple[PrintFormat, ...] = (A4, A5, PHOTO_10X15)

#: Самый мелкий формат из списка. Снимок, которому не хватает и на него,
#: и есть «заведомо непригодно для печати» — единственное, что модуль
#: позволяет себе утверждать про пригодность.
SMALLEST_FORMAT = PRINT_FORMATS[-1]


# --- три состояния ----------------------------------------------------------


class ReviewState(str, Enum):
    """Решение человека по одному снимку.

    Три, а не два: «оставить» и «в печать» — разные ответы на разные
    вопросы («держать в библиотеке» и «отобрать на печать»), и смешивать
    их значило бы потерять ровно тот список, ради которого пункт 24
    придуман.
    """

    KEEP = "keep"
    PRINT = "print"
    DROP = "drop"


STATE_LABEL: dict[str, str] = {
    ReviewState.KEEP.value: "оставить",
    ReviewState.PRINT.value: "в печать",
    ReviewState.DROP.value: "убрать",
}

ALL_STATES: tuple[str, ...] = tuple(s.value for s in ReviewState)


# --- пороги, каждый со своим смыслом ---------------------------------------

#: Порог «следов пережатия мало». Шкала `recompression` (пункт 9,
#: `quality.measure_recompression`) закреплена с двух концов: 0.0 — файл
#: сохранён с качеством 92 и выше, 1.0 — с качеством 50 и ниже, между —
#: линейно. 0.5 это ровно качество ≈71, то есть «пережат, но не
#: раздавлен». Порог назван в этих терминах, потому что качество JPEG —
#: единственное из трёх чисел, которое человек может прочитать и с
#: которым может что-то сделать. Равенство 0.5 ↔ 71 закреплено тестом
#: против самих констант `quality.py`, чтобы одно не уехало без другого.
RECOMPRESSION_CLEAN = 0.5

#: Лицо считается крупным, если его длинная сторона занимает хотя бы
#: столько от длинной стороны кадра, в котором его нашли.
#:
#: **Это не «лицо в фокусе».** Резкость отдельного лица в индексе не
#: лежит (`content_faces` хранит рамку, уверенность детектора и
#: эмбеддинг — пункт 18), и притворяться, что мы её мерили, нельзя.
#: Считается ровно то, что считается: лицо крупное в кадре, а сам кадр не
#: из мягких по альбому. Так это и подписано на экране — «крупное лицо,
#: кадр резче половины альбома», а не «лица в фокусе».
FACE_PROMINENT_SHARE = 0.08

#: Меньше этого числа измеренных снимков в альбоме — медиана резкости не
#: считается вовсе. Резкость, как намерил аудит пункта 17, величина
#: относительная и шумная: «резче, чем половина альбома» из двух снимков
#: означало бы «резче, чем другой», а это уже не про альбом.
MIN_SHARPNESS_SAMPLE = 3

#: Во сколько раз кадр должен быть мягче медианы альбома, чтобы это
#: считалось мягкостью.
#:
#: Допуск, а не порог, и он найден прогоном на настоящем конвейере. Без
#: него снимок 3000×2250, сохранённый с качеством 95, уходил из первой
#: ступени вниз из-за **2.7%** разницы с медианой (2290 против 2352) — и
#: оказывался в очереди позади снимка 2000×1500, то есть разрешение, из
#: трёх признаков единственный факт (Р2), проигрывало шуму самой шумной
#: оценки. Число — то же 1.25, которым `best_copy.SHARPNESS_TIE_RATIO`
#: уже отвечает на тот же вопрос в пункте 17: перевес в резкости меньше
#: четверти перевесом не считается. Общего кода с ним намеренно нет (см.
#: отчёт пункта 17), а совпадение числа не случайно: это одна и та же
#: метрика с той же погрешностью.
SHARPNESS_SOFT_RATIO = 1.25


# --- пригодность к печати ---------------------------------------------------


@dataclass(frozen=True)
class PrintFitness:
    """На какой формат хватает разрешения — и что значит, если неизвестно.

    `measured` отделяет «посмотрели и мелко» от «не смотрели». Второе
    никогда не становится первым: `unfit` ложно, пока разрешение не
    измерено, поэтому фильтр «не годится для печати» не выбрасывает
    снимок за то, что до него не дошла полная обработка.
    """

    measured: bool
    megapixels: float | None
    largest: PrintFormat | None
    fits: tuple[str, ...]
    too_small_for: tuple[str, ...]
    text: str

    @property
    def unfit(self) -> bool:
        return self.measured and self.largest is None

    def to_dict(self) -> dict:
        return {
            "measured": self.measured,
            "megapixels": self.megapixels,
            "largest": self.largest.key if self.largest else None,
            "largest_label": self.largest.label if self.largest else None,
            "fits": list(self.fits),
            "too_small_for": list(self.too_small_for),
            "text": self.text,
            "unfit": self.unfit,
        }


def megapixels_of(quality: Mapping | None) -> float | None:
    """Мегапиксели снимка из строки метрик, или None.

    `quality_for_hashes` уже кладёт готовое `megapixels`; принимается и
    пара `source_width`/`source_height`, чтобы вызывающий мог передать
    сырую строку таблицы. Одна формула на оба случая, чтобы «12.2 МП» на
    экране и порог в тесте считались одинаково.
    """
    if not quality:
        return None
    value = quality.get("megapixels")
    if value is not None:
        return round(float(value), 2)
    width = quality.get("source_width")
    height = quality.get("source_height")
    if width and height:
        return round(int(width) * int(height) / 1_000_000, 2)
    return None


def print_fitness(
    quality: Mapping | None, formats: Sequence[PrintFormat] = PRINT_FORMATS
) -> PrintFitness:
    """Самый большой формат, на который хватает разрешения, и фраза об этом."""
    megapixels = megapixels_of(quality)
    if megapixels is None:
        return PrintFitness(
            measured=False,
            megapixels=None,
            largest=None,
            fits=(),
            too_small_for=(),
            text="разрешение не измерялось — полная обработка его досчитает",
        )

    fits = tuple(f.key for f in formats if megapixels >= f.megapixels)
    too_small = tuple(f.key for f in formats if megapixels < f.megapixels)
    largest = next((f for f in formats if f.key in fits), None)

    if largest is None:
        smallest = formats[-1]
        text = (
            f"{megapixels:g} МП — мелко даже для {smallest.label}: "
            f"при {smallest.dpi} dpi нужно {smallest.megapixels:g} МП"
        )
    else:
        text = f"{megapixels:g} МП — хватает на {largest.label} при {largest.dpi} dpi"
    return PrintFitness(
        measured=True,
        megapixels=megapixels,
        largest=largest,
        fits=fits,
        too_small_for=too_small,
        text=text,
    )


# --- альбом как папка -------------------------------------------------------


def folder_of(path: str) -> str:
    r"""Папка, в которой лежит файл, с тем разделителем, что был в пути.

    Разделитель — свойство пути, а не машины: план пункта 21 строится на
    Windows и может читаться на Linux, поэтому `library.join_path`
    сохраняет обратные слэши, и здесь они тоже сохраняются. Иначе альбом
    `D:\Library\2018\2018 Novosibirsk`, прочитанный из индекса, не совпал
    бы сам с собой при повторном запросе.
    """
    normalised = path.replace("\\", "/").rstrip("/")
    cut = normalised.rfind("/")
    if cut <= 0:
        return ""
    head = normalised[:cut]
    return head.replace("/", "\\") if "\\" in path else head


def basename_of(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").split("/")[-1]


@dataclass(frozen=True)
class AlbumRef:
    """Одна папка собранной библиотеки и сколько в ней снимков."""

    folder: str
    name: str
    photos: int

    def to_dict(self) -> dict:
        return {"folder": self.folder, "name": self.name, "photos": self.photos}


def group_albums(rows: Iterable[Mapping]) -> list[AlbumRef]:
    """Папки библиотеки из строк `library_moves`.

    Альбом — это папка на диске, а не запись в таблице: так его видит
    человек в проводнике, и так его определяет Р5 («взаимоисключающая
    группировка материализуется папками»). Имя берётся из плана
    (`PlannedMove.album` — его придумала цепочка Р4), а если плана
    нечего сказать — из имени самой папки, что честнее пустой строки.

    Порядок — по пути, то есть хронологический: год отдельным уровнем и
    дата впереди имени ровно для этого и выбраны (см. `LibraryLayout`).
    """
    folders: dict[str, list[str]] = {}
    for row in rows:
        destination = str(row.get("destination") or "")
        if not destination:
            continue
        folder = folder_of(destination)
        folders.setdefault(folder, []).append(str(row.get("album") or ""))

    out: list[AlbumRef] = []
    for folder, names in sorted(folders.items()):
        proposed = [n for n in names if n]
        name = max(set(proposed), key=proposed.count) if proposed else basename_of(folder)
        out.append(AlbumRef(folder=folder, name=name, photos=len(names)))
    return out


def photos_in_album(rows: Iterable[Mapping], folder: str) -> list[dict]:
    """Снимки одной папки: путь, хэш содержимого, размер.

    Дедуплицируется по пути: `library_moves` — журнал, и один и тот же
    канонический путь мог быть записан дважды (прогон, откат, повторный
    прогон). Побеждает последняя запись, как в `library_origin_of`.
    """
    by_path: dict[str, dict] = {}
    for row in rows:
        destination = str(row.get("destination") or "")
        if not destination or folder_of(destination) != folder:
            continue
        by_path[destination] = {
            "path": destination,
            "content_hash": str(row.get("content_key") or ""),
            "size": int(row.get("size") or 0),
            "album": str(row.get("album") or ""),
        }
    return [by_path[p] for p in sorted(by_path)]


# --- один снимок в очереди --------------------------------------------------

TIER_READY = 0
TIER_MEASURED = 1
TIER_PARTIAL = 2
TIER_UNMEASURED = 3
#: Не ступень очереди, а отдельный список: заведомо непригодное для
#: печати не перемешивается с годным, а лежит за своим фильтром.
TIER_UNFIT = 9

TIER_LABEL: dict[int, str] = {
    TIER_READY: "годное, с лицами",
    TIER_MEASURED: "годное",
    TIER_PARTIAL: "измерено, но есть к чему придраться",
    TIER_UNMEASURED: "не измерялось",
    TIER_UNFIT: "не годится для печати",
}


@dataclass(frozen=True)
class PhotoCard:
    """Один снимок альбома глазами разбора."""

    content_hash: str
    path: str
    album: str
    size: int
    state: str | None = None
    applied_at: float | None = None
    width: int | None = None
    height: int | None = None
    megapixels: float | None = None
    sharpness: float | None = None
    recompression: float | None = None
    recompression_basis: str | None = None
    jpeg_quality: int | None = None
    faces: int = 0
    prominent_faces: int = 0
    faces_scanned: bool = False
    print_fit: PrintFitness = PrintFitness(
        measured=False,
        megapixels=None,
        largest=None,
        fits=(),
        too_small_for=(),
        text="разрешение не измерялось — полная обработка его досчитает",
    )
    tier: int = TIER_UNMEASURED
    reason: str = ""
    reason_kind: str = "unmeasured"

    @property
    def name(self) -> str:
        return basename_of(self.path)

    @property
    def fully_measured(self) -> bool:
        """Все три признака на руках: разрешение, резкость, сила сжатия.

        В индексе они приходят вместе — одна и та же функция
        `quality.measure` считает их за один декод, — но проверяются
        порознь: строка, собранная из `content_previews` до миграции 12,
        могла нести разрешение без остального.
        """
        return (
            self.megapixels is not None
            and self.sharpness is not None
            and self.recompression is not None
            and self.recompression_basis is not None
        )

    @property
    def decided(self) -> bool:
        return self.state is not None

    def to_dict(self) -> dict:
        return {
            "content_hash": self.content_hash,
            "path": self.path,
            "name": self.name,
            "album": self.album,
            "size": self.size,
            "state": self.state,
            "state_label": STATE_LABEL.get(self.state or "", ""),
            "applied_at": self.applied_at,
            "width": self.width,
            "height": self.height,
            "megapixels": self.megapixels,
            "sharpness": self.sharpness,
            "recompression": self.recompression,
            "recompression_basis": self.recompression_basis,
            "jpeg_quality": self.jpeg_quality,
            "faces": self.faces,
            "prominent_faces": self.prominent_faces,
            "faces_scanned": self.faces_scanned,
            "print": self.print_fit.to_dict(),
            "tier": self.tier,
            "tier_label": TIER_LABEL.get(self.tier, ""),
            "reason": self.reason,
            "reason_kind": self.reason_kind,
        }


def prominent_faces(face_row: Mapping | None) -> int:
    """Сколько лиц в кадре крупные — по доле длинной стороны.

    Рамки лиц лежат в координатах уменьшенной копии, в которой их нашли
    (`detect_long_side`, пункт 18), поэтому доля считается от неё, а не от
    исходного разрешения: это одно деление вместо пересчёта масштаба,
    который нигде не записан целиком.
    """
    if not face_row:
        return 0
    long_side = int(face_row.get("detect_long_side") or 0)
    if long_side <= 0:
        return 0
    threshold = long_side * FACE_PROMINENT_SHARE
    sides = face_row.get("face_long_sides") or ()
    return sum(1 for side in sides if float(side) >= threshold)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _recompression_text(card: PhotoCard) -> str:
    """Насколько пережат — в терминах, которые человек прочитает."""
    if card.jpeg_quality is not None:
        return f"сохранён с качеством ≈{card.jpeg_quality}"
    basis = card.recompression_basis or "неизвестной шкале"
    return f"оценка сжатия {card.recompression:.2f} по {basis}"


def classify(card: PhotoCard, sharpness_reference: float | None) -> tuple[int, str, str]:
    """`(ступень, текст, вид)` — почему снимок стоит на своём месте.

    Та же форма, что у `keeper.keeper_reason` и `best_copy.reason`:
    называется не вердикт, а **признак, который поставил снимок сюда**, и
    находится он теми же проверками, по которым идёт сортировка. Фраза не
    может разойтись с правилом, которое описывает.

    Порядок проверок — от факта к оценке, как в лестнице пункта 17:
    сперва арифметика печати (деление), потом сила сжатия (чтение таблиц
    квантования), потом резкость (величина относительная и шумная), и
    только после них лица.
    """
    fit = card.print_fit

    if fit.unfit:
        return TIER_UNFIT, fit.text, "unfit"

    if not card.fully_measured:
        missing = []
        if card.megapixels is None:
            missing.append("разрешение")
        if card.sharpness is None:
            missing.append("резкость")
        if card.recompression is None or card.recompression_basis is None:
            missing.append("сила сжатия")
        return (
            TIER_UNMEASURED,
            f"не измерялось: {', '.join(missing)} — полная обработка досчитает",
            "unmeasured",
        )

    if card.recompression > RECOMPRESSION_CLEAN:
        return (
            TIER_PARTIAL,
            f"{fit.text}; {_recompression_text(card)} — следы пережатия заметны",
            "partial",
        )

    if (
        sharpness_reference is not None
        and card.sharpness * SHARPNESS_SOFT_RATIO < sharpness_reference
    ):
        return (
            TIER_PARTIAL,
            f"{fit.text}; кадр мягче альбома более чем на четверть "
            f"({card.sharpness:.1f} против медианы {sharpness_reference:.1f})",
            "partial",
        )

    soft_note = (
        "резкость в пределах альбома"
        if sharpness_reference is not None
        else "резкость измерена, но сравнивать не с чем — в альбоме мало измеренных снимков"
    )

    if card.prominent_faces:
        faces = card.prominent_faces
        return (
            TIER_READY,
            f"{fit.text}; в кадре крупное лицо ({faces}), {soft_note}",
            "ready",
        )

    return TIER_MEASURED, f"{fit.text}; следов пережатия мало, {soft_note}", "measured"


def sort_key(card: PhotoCard) -> tuple:
    """Устойчивый порядок внутри ступени.

    Заканчивается хэшем содержимого, а не путём и не позицией во входе:
    хэш у снимка один на все пересканы и переносы, поэтому очередь,
    собранная дважды из одних данных в любом порядке, выходит одинаковой.
    Это то же требование, что `LibraryPlan.fingerprint` предъявляет плану.

    Внутри ступени — от большего разрешения к меньшему, затем меньше
    следов пережатия, затем резче. Названная граница: увеличенная копия
    обходит оригинал по разрешению, и это та же ошибка, которую пункт 17
    назвал вслух у своей первой ступени. Здесь она дешевле — порядок в
    очереди, а не выбор копии, — но она есть.
    """
    return (
        card.tier,
        -(card.megapixels or 0.0),
        card.recompression if card.recompression is not None else 1.0,
        -(card.sharpness or 0.0),
        card.content_hash,
    )


# --- очередь на просмотр ----------------------------------------------------


@dataclass(frozen=True)
class AlbumQueue:
    """Альбом, разложенный на очередь и на фильтр «не годится для печати».

    Два списка, а не один с флагом: пункт 24 требует, чтобы заведомо
    непригодное лежало «отдельным фильтром, а не вперемешку». Снимок из
    `unfit` всё равно получает все три состояния — «мелко для печати» не
    значит «убрать», это решает человек.
    """

    album: str
    name: str
    queue: tuple[PhotoCard, ...]
    unfit: tuple[PhotoCard, ...]
    sharpness_reference: float | None = None
    formats: tuple[PrintFormat, ...] = PRINT_FORMATS

    @property
    def cards(self) -> tuple[PhotoCard, ...]:
        return self.queue + self.unfit

    def summary(self) -> dict:
        states = {key: 0 for key in ALL_STATES}
        states["none"] = 0
        tiers: dict[str, int] = {}
        measured = 0
        faces_scanned = 0
        with_faces = 0
        drop_pending = 0
        for card in self.cards:
            states[card.state or "none"] += 1
            tiers[str(card.tier)] = tiers.get(str(card.tier), 0) + 1
            if card.fully_measured:
                measured += 1
            if card.faces_scanned:
                faces_scanned += 1
            if card.prominent_faces:
                with_faces += 1
            if card.state == ReviewState.DROP.value and card.applied_at is None:
                drop_pending += 1
        return {
            "album": self.album,
            "name": self.name,
            "photos": len(self.cards),
            "queue": len(self.queue),
            "unfit": len(self.unfit),
            "states": states,
            "reviewed": len(self.cards) - states["none"],
            "drop_pending": drop_pending,
            "tiers": tiers,
            "measured": measured,
            "unmeasured": len(self.cards) - measured,
            "faces_scanned": faces_scanned,
            "with_prominent_faces": with_faces,
            "sharpness_reference": (
                round(self.sharpness_reference, 2)
                if self.sharpness_reference is not None
                else None
            ),
        }

    def to_dict(self) -> dict:
        return {
            "summary": self.summary(),
            "formats": [f.to_dict() for f in self.formats],
            "queue": [c.to_dict() for c in self.queue],
            "unfit": [c.to_dict() for c in self.unfit],
        }


def build_album_queue(
    photos: Iterable[Mapping],
    *,
    album: str = "",
    name: str = "",
    quality: Mapping[str, Mapping] | None = None,
    faces: Mapping[str, Mapping] | None = None,
    states: Mapping[str, Mapping] | None = None,
    formats: Sequence[PrintFormat] = PRINT_FORMATS,
) -> AlbumQueue:
    """Собрать очередь альбома из того, что уже лежит в индексе.

    `photos` — снимки папки (`path`, `content_hash`, `size`),
    `quality` — метрики по хэшу содержимого (пункты 9 и 17),
    `faces` — лица по тому же ключу (пункт 18),
    `states` — решения человека (`album_state`, `album_applied_at`).

    Ни одно из трёх не обязательно: отсутствие метрик — обычный случай у
    индекса, собранного до миграции 12, отсутствие лиц — у индекса без
    установленных моделей (Р11), отсутствие решений — у альбома, который
    ещё не открывали. Всё это «не измерялось», а не «плохо».
    """
    quality = quality or {}
    faces = faces or {}
    states = states or {}
    formats = tuple(formats)

    raw: list[PhotoCard] = []
    for photo in photos:
        content_hash = str(photo.get("content_hash") or "")
        metrics = quality.get(content_hash) or {}
        face_row = faces.get(content_hash) or {}
        state_row = states.get(content_hash) or {}
        recompression = metrics.get("recompression")
        sharpness = metrics.get("sharpness")
        raw.append(
            PhotoCard(
                content_hash=content_hash,
                path=str(photo.get("path") or ""),
                album=album or str(photo.get("album") or ""),
                size=int(photo.get("size") or 0),
                state=state_row.get("album_state"),
                applied_at=state_row.get("album_applied_at"),
                width=metrics.get("source_width"),
                height=metrics.get("source_height"),
                megapixels=megapixels_of(metrics),
                sharpness=None if sharpness is None else float(sharpness),
                recompression=None if recompression is None else float(recompression),
                recompression_basis=metrics.get("recompression_basis"),
                jpeg_quality=metrics.get("jpeg_quality"),
                faces=int(face_row.get("faces") or 0),
                prominent_faces=prominent_faces(face_row),
                faces_scanned=bool(face_row.get("scanned")),
                print_fit=print_fitness(metrics, formats),
            )
        )

    # Резкость сравнивается с самим альбомом, а не с абсолютным порогом:
    # Р2 называет её оценкой, а не фактом, и аудит пункта 17 намерил, чем
    # абсолютный порог кончается — сильнее сжатая копия выходит «резче»
    # оригинала в 1.8 раза, потому что лапласиан меряет края блоков 8×8.
    # Медиана по альбому этого не исправляет, но и не делает вид, что
    # число значит больше, чем значит: «резче половины этого альбома» —
    # ровно то, что посчитано, и ровно то, что написано на экране.
    sample = [c.sharpness for c in raw if c.fully_measured and c.sharpness is not None]
    reference = _median(sample) if len(sample) >= MIN_SHARPNESS_SAMPLE else None

    graded: list[PhotoCard] = []
    for card in raw:
        tier, reason, kind = classify(card, reference)
        graded.append(replace(card, tier=tier, reason=reason, reason_kind=kind))

    queue = tuple(sorted((c for c in graded if c.tier != TIER_UNFIT), key=sort_key))
    unfit = tuple(sorted((c for c in graded if c.tier == TIER_UNFIT), key=sort_key))
    return AlbumQueue(
        album=album,
        name=name or basename_of(album),
        queue=queue,
        unfit=unfit,
        sharpness_reference=reference,
        formats=formats,
    )
