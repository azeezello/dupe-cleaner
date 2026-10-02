r"""Названия альбомов (задача 20, Р4): цепочка, её порядок и её границы.

Что здесь проверяется, и почему именно это
------------------------------------------
Р4 задаёт цепочку источников названия — GPS через офлайн-геокодер, диапазон
дат, доминирующий кластер лиц, папка, которую человек назвал сам, — и
`albums.py` её реализует. Цепочка тем и ценна, что **падает на следующее
звено честно**: событие без координат получает не ближайший город, а дату.
Поэтому почти каждый тест ниже — это одно и то же событие, у которого
отбирают одно звено, и утверждение о том, кто заговорил следующим.

Второе, что проверяется здесь по существу, — что название остаётся
**предложением**. Модуль не переименовывает, не перемещает и не трогает ни
один файл; единственная запись в проекте — явный `confirm_album_name`. На
это есть тест с настоящими файлами на диске, а не только договорённость в
докстроке.

Третье — Р8. Четвёртое звено цепочки существует только потому, что Р8
перестал отправлять именованные папки в карантин, и значит имя, придуманное
человеком, не должно проигрывать месту, которое геокодер **выдумал** из
меньшинства снимков. Папки в тестах настоящие: `Wedding 16042017` (1790
файлов), `Краснодар` (1575), `Новосибирск 2021` (1695), `Грузия 2022-2023`
(442), `Pictures` (10 847) — всё из `D:\Photos`, а не придумано для теста.
"""

from __future__ import annotations

import datetime as dt

import pytest

from dupecleaner import albums as albums_module
from dupecleaner import keeper as keeper_module
from dupecleaner.albums import (
    DEFAULT_POLICY,
    NamePolicy,
    deepest_named_segment,
    format_date_range,
    format_month,
    moments_by_folder,
    path_segments,
    suggest_for_event,
    suggest_names,
)
from dupecleaner.events import (
    EventCluster,
    EventClustering,
    GeoSource,
    PhotoMoment,
    TimeSource,
)
from dupecleaner.geocode import Gazetteer, Place
from dupecleaner.storage import ScanIndex

# Настоящие координаты и населения — чтобы «правильно ли назвало» был
# вопрос про существующие места, а не про данные теста. Те же записи, что
# в tests/test_geocode.py, плюс Батуми: в `D:\Photos` есть папка
# «Грузия 2022-2023».
KRASNODAR = Place("Краснодар", 45.04484, 38.97603, 899541, "RU")
NOVOSIBIRSK = Place("Новосибирск", 55.0415, 82.9346, 1612833, "RU")
BATUMI = Place("Батуми", 41.64228, 41.63392, 152839, "GE")
VLADIVOSTOK = Place("Владивосток", 43.10562, 131.87353, 604901, "RU")

WEDDING_FOLDER = r"D:\Photos\Wedding 16042017"
GEORGIA_FOLDER = r"D:\Photos\Грузия 2022-2023"
DUMP_FOLDER = r"D:\Photos\Pictures"


@pytest.fixture
def gazetteer() -> Gazetteer:
    return Gazetteer([KRASNODAR, NOVOSIBIRSK, BATUMI, VLADIVOSTOK])


def ts(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> float:
    """Отметка времени в UTC — `events` читает их через
    `utcfromtimestamp`, поэтому дата в тесте и дата в названии совпадают."""
    return dt.datetime(
        year, month, day, hour, minute, tzinfo=dt.timezone.utc
    ).timestamp()


def moment(
    path: str,
    taken_at: float,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
) -> PhotoMoment:
    return PhotoMoment(
        display_path=path,
        taken_at=taken_at,
        time_source=TimeSource.EXIF,
        latitude=latitude,
        longitude=longitude,
        geo_source=GeoSource.EXIF if latitude is not None else GeoSource.NONE,
    )


def event(*moments: PhotoMoment) -> EventCluster:
    """`EventCluster` ждёт снимки по возрастанию времени — `start` это
    `moments[0]`, а не минимум."""
    return EventCluster(moments=sorted(moments, key=lambda m: m.taken_at or 0.0))


def wedding_event(*, geo: bool = True, folder: str = WEDDING_FOLDER) -> EventCluster:
    """Настоящая свадьба 16 апреля 2017 года из `D:\\Photos`: шесть снимков
    в Краснодаре, лежащие в папке, которую человек назвал сам.

    Одно и то же событие для всей цепочки: у него есть и координаты, и
    папка-имя, и (через `faces_for`) лица, — так что отбирая по одному
    звену, можно увидеть, кто заговорит следующим.
    """
    moments = []
    for i in range(6):
        moments.append(
            moment(
                rf"{folder}\IMG_{2200 + i}.jpg",
                ts(2017, 4, 16, 14, i * 5),
                latitude=KRASNODAR.latitude + 0.002 * i if geo else None,
                longitude=KRASNODAR.longitude + 0.002 * i if geo else None,
            )
        )
    return event(*moments)


def faces_for(cluster: EventCluster, person_id: int = 7) -> tuple[dict, dict]:
    """Хэши по путям и персоны по хэшам: один человек на каждом снимке.

    Лица в проекте ключуются содержимым (Р11), а событие сделано из путей —
    поэтому третьему звену нужны обе таблицы, и тест обязан давать обе.
    """
    hashes = {m.display_path: f"hash-{i}" for i, m in enumerate(cluster.moments)}
    persons = {h: [person_id] for h in hashes.values()}
    return hashes, persons


class TestChainOrder:
    """Цепочка Р4 и её падение на следующее звено."""

    def test_place_speaks_first_when_every_link_fires(self, gazetteer):
        """Порядок по умолчанию — Р4: место важнее лица, лицо важнее папки.

        Событие нарочно такое, где звенья спорят: координаты в Краснодаре,
        папка названа «Wedding 16042017». Р4 ставит геокодер первым, и это
        видно.
        """
        cluster = wedding_event()
        hashes, persons = faces_for(cluster)
        suggestion = suggest_for_event(
            cluster,
            gazetteer=gazetteer,
            content_hashes=hashes,
            persons_by_hash=persons,
            person_labels={7: "Диljon"},
        )
        assert suggestion.primary.source == "place"
        assert suggestion.primary.text == "Краснодар, 16 апреля 2017"
        assert [c.source for c in suggestion.candidates] == [
            "place",
            "person",
            "folder",
            "dates",
        ]

    def test_without_coordinates_the_next_link_speaks(self, gazetteer):
        """Нет GPS — место не подставляется ничем, говорит лицо."""
        cluster = wedding_event(geo=False)
        hashes, persons = faces_for(cluster)
        suggestion = suggest_for_event(
            cluster,
            gazetteer=gazetteer,
            content_hashes=hashes,
            persons_by_hash=persons,
            person_labels={7: "Диljon"},
        )
        assert suggestion.primary.source == "person"
        assert suggestion.primary.text == "Диljon, 16 апреля 2017"
        assert not [c for c in suggestion.candidates if c.source == "place"]

    def test_without_place_and_person_the_folder_speaks(self, gazetteer):
        """Ни координат, ни подписанных лиц — остаётся имя, придуманное
        человеком. Это звено 4, и оно единственное невосстановимое."""
        suggestion = suggest_for_event(wedding_event(geo=False), gazetteer=gazetteer)
        assert suggestion.primary.source == "folder"
        assert suggestion.primary.text == "Wedding 16042017, 16 апреля 2017"

    def test_with_nothing_left_the_name_is_the_date_range(self, gazetteer):
        """Свалка `Pictures` вместо названной папки — и название это дата.

        Пятое звено Р4 целиком: никакого выдуманного места, никакого
        «Pictures».
        """
        suggestion = suggest_for_event(
            wedding_event(geo=False, folder=DUMP_FOLDER), gazetteer=gazetteer
        )
        assert suggestion.primary.source == "dates"
        assert suggestion.primary.subject == ""
        assert suggestion.primary.text == "16 апреля 2017"

    def test_the_chain_falls_through_in_order(self, gazetteer):
        """Четыре шага подряд, одним утверждением: отбираем звено — говорит
        следующее, и ни одно не перескакивает через соседа."""
        cluster_with_geo = wedding_event()
        cluster_no_geo = wedding_event(geo=False)
        hashes, persons = faces_for(cluster_no_geo)
        geo_hashes, geo_persons = faces_for(cluster_with_geo)

        steps = [
            suggest_for_event(
                cluster_with_geo,
                gazetteer=gazetteer,
                content_hashes=geo_hashes,
                persons_by_hash=geo_persons,
                person_labels={7: "Диljon"},
            ),
            suggest_for_event(
                cluster_no_geo,
                gazetteer=gazetteer,
                content_hashes=hashes,
                persons_by_hash=persons,
                person_labels={7: "Диljon"},
            ),
            suggest_for_event(cluster_no_geo, gazetteer=gazetteer),
            suggest_for_event(
                wedding_event(geo=False, folder=DUMP_FOLDER), gazetteer=gazetteer
            ),
        ]
        assert [s.primary.source for s in steps] == [
            "place",
            "person",
            "folder",
            "dates",
        ]

    def test_an_unnamed_cluster_of_faces_never_names_an_album(self, gazetteer):
        """«Человек №742, 8 июля 2019» — не название, и его никто не увидит.

        Доминирующий, но не подписанный кластер не называет альбом, а
        попадает в отдельный список: это измерение того, сколько стоит
        пять минут в `dupecleaner persons --label`.
        """
        cluster = wedding_event(geo=False, folder=DUMP_FOLDER)
        hashes, persons = faces_for(cluster, person_id=742)
        naming = suggest_names(
            EventClustering(events=[cluster]),
            gazetteer=gazetteer,
            content_hashes=hashes,
            persons_by_hash=persons,
            person_labels={742: None},
        )
        suggestion = naming.suggestions[0]
        assert suggestion.primary.source == "dates"
        assert "742" not in suggestion.name
        assert [p for _, p, _ in naming.unlabelled_dominant] == [742]

    def test_a_missing_gazetteer_is_silence_and_not_an_error(self):
        """Геокодер — данные, которых может не быть на машине (как модели
        лиц по Р11). Его отсутствие обязано стоить одно звено, а не прогон."""
        suggestion = suggest_for_event(wedding_event(), gazetteer=None)
        assert suggestion.primary.source == "folder"
        assert not [c for c in suggestion.candidates if c.source == "place"]


class TestNameIsOnlyASuggestion:
    """Название — предложение, подтверждаемое вручную (Р4, задача 20)."""

    def test_suggesting_names_touches_no_file_on_disk(self, tmp_path, gazetteer):
        """Настоящие файлы на диске, а не договорённость в докстроке.

        Снимаем состав каталога, размеры и mtime до и после прогона — и
        отдельно проверяем, что модуль не держит у себя ни `os`, ни
        `shutil`, то есть переименовать физически нечем.
        """
        folder = tmp_path / "Wedding 16042017"
        folder.mkdir()
        for i in range(3):
            (folder / f"IMG_{i}.jpg").write_bytes(b"not really a jpeg")
        before = {
            p.name: (p.stat().st_size, p.stat().st_mtime_ns)
            for p in folder.iterdir()
        }

        cluster = event(
            *[
                moment(str(folder / f"IMG_{i}.jpg"), ts(2017, 4, 16, 14, i))
                for i in range(3)
            ]
        )
        suggest_names(EventClustering(events=[cluster]), gazetteer=gazetteer)

        after = {
            p.name: (p.stat().st_size, p.stat().st_mtime_ns)
            for p in folder.iterdir()
        }
        assert after == before
        assert not {"os", "shutil", "quarantine"} & set(vars(albums_module))

    def test_suggesting_names_writes_nothing_to_the_index(self, gazetteer):
        """Прогон целиком — и таблица подтверждений остаётся пустой."""
        with ScanIndex(":memory:") as index:
            suggest_names(
                EventClustering(events=[wedding_event()]),
                gazetteer=gazetteer,
                confirmed=index.confirmed_album_names(),
            )
            assert index.album_name_rows() == []

    def test_a_confirmed_name_is_stored_only_by_an_explicit_call(self, gazetteer):
        """Единственная запись задачи 20 — явный `confirm_album_name`.

        И после неё предложение не исчезает: `name` отдаёт подтверждённое,
        `suggested` в отчёте по-прежнему говорит, что предлагала цепочка, —
        иначе вопрос «часто ли предложение принимают как есть» некому было
        бы задать.
        """
        cluster = wedding_event()
        with ScanIndex(":memory:") as index:
            first = suggest_for_event(cluster, gazetteer=gazetteer)
            index.confirm_album_name(
                first.anchor,
                "Свадьба Диljon",
                suggested=first.primary.text,
                source=first.primary.source,
            )
            again = suggest_for_event(
                cluster, gazetteer=gazetteer, confirmed=index.confirmed_album_names()
            )

        assert again.name == "Свадьба Диljon"
        assert again.primary.text == "Краснодар, 16 апреля 2017"
        assert again.to_dict()["suggested"] == "Краснодар, 16 апреля 2017"
        assert again.to_dict()["source"] == "place"

    def test_forgetting_a_confirmation_returns_the_event_to_its_suggestion(
        self, gazetteer
    ):
        """Снятое имя не должно стоить ничего — та же замена-а-не-накопление,
        что у метки персоны."""
        cluster = wedding_event()
        with ScanIndex(":memory:") as index:
            suggestion = suggest_for_event(cluster, gazetteer=gazetteer)
            index.confirm_album_name(suggestion.anchor, "Свадьба")
            assert index.forget_album_name(suggestion.anchor) is True
            back = suggest_for_event(
                cluster, gazetteer=gazetteer, confirmed=index.confirmed_album_names()
            )
        assert back.confirmed is None
        assert back.name == "Краснодар, 16 апреля 2017"

    def test_the_anchor_is_the_content_hash_of_the_earliest_photo(self, gazetteer):
        """Ключ подтверждения — содержимое, а не путь: имя обязано пережить
        перескан и переезд снимка в папку получше (Р9, Р10, Р12)."""
        cluster = wedding_event()
        hashes, _ = faces_for(cluster)
        suggestion = suggest_for_event(
            cluster, gazetteer=gazetteer, content_hashes=hashes
        )
        earliest = min(cluster.moments, key=lambda m: m.taken_at)
        assert suggestion.anchor == hashes[earliest.display_path]


class TestHumanNameVersusInventedPlace:
    """Р8 и его следствие: придуманное человеком имя против выдуманного
    геокодером места."""

    def test_a_place_a_minority_agrees_on_does_not_beat_your_folder(self, gazetteer):
        """Четыре снимка в Краснодаре, три в Новосибирске, три во
        Владивостоке — это не событие в Краснодаре, это папка, которую
        кто-то скопировал. Согласия нет, значит места нет, и говорит папка.
        """
        moments = []
        for i, place in enumerate(
            [KRASNODAR] * 4 + [NOVOSIBIRSK] * 3 + [VLADIVOSTOK] * 3
        ):
            moments.append(
                moment(
                    rf"{WEDDING_FOLDER}\IMG_{i}.jpg",
                    ts(2017, 4, 16, 14, i),
                    latitude=place.latitude,
                    longitude=place.longitude,
                )
            )
        suggestion = suggest_for_event(event(*moments), gazetteer=gazetteer)
        assert suggestion.primary.source == "folder"
        assert suggestion.primary.subject == "Wedding 16042017"
        assert not [c for c in suggestion.candidates if c.source == "place"]

    def test_a_place_nobody_photographed_is_never_invented(self, gazetteer):
        """Геокодер установлен, координат в событии нет — и места нет.

        Половина библиотеки без координат по построению (49% на
        `D:\\Photos`), поэтому это не краевой случай, а половина ответов.
        """
        cluster = wedding_event(geo=False, folder=GEORGIA_FOLDER)
        suggestion = suggest_for_event(cluster, gazetteer=gazetteer)
        assert suggestion.primary.subject == "Грузия 2022-2023"
        assert not [c for c in suggestion.candidates if c.source == "place"]

    def test_your_folder_survives_as_an_alternative_when_the_place_wins(
        self, gazetteer
    ):
        """Даже проиграв порядку Р4, имя человека не выбрасывается:
        подтверждение это выбор из названных вариантов, а не «да/нет»."""
        suggestion = suggest_for_event(wedding_event(), gazetteer=gazetteer)
        assert suggestion.primary.source == "place"
        assert [c.subject for c in suggestion.alternatives if c.source == "folder"] == [
            "Wedding 16042017"
        ]

    def test_folder_first_is_a_setting_and_not_a_patch(self, gazetteer):
        """Порядок звеньев — открытый вопрос задачи 20, поэтому он
        `NamePolicy`, а не `if` в коде."""
        policy = NamePolicy(subject_order=("folder", "place", "person"))
        suggestion = suggest_for_event(
            wedding_event(), gazetteer=gazetteer, policy=policy
        )
        assert suggestion.primary.source == "folder"
        assert suggestion.alternatives[0].source == "place"

    def test_a_dump_folder_is_not_offered_as_a_name(self):
        """10 847 файлов в `Pictures` не становятся альбомом «Pictures»."""
        assert deepest_named_segment(rf"{DUMP_FOLDER}\IMG_1.jpg") is None
        assert deepest_named_segment(r"D:\Photos\Photos\image-0-02-05.jpg") is None

    def test_a_dated_folder_is_not_a_name_either(self):
        """Год восстановим из EXIF, имя — нет: ровно поэтому Р8 различает
        эти два класса, и ровно поэтому звено 4 не берёт `2020`."""
        assert deepest_named_segment(r"D:\Photos\2010 -2020\2020\IMG_1.jpg") is None
        assert deepest_named_segment(r"D:\Photos\2019\IMG_1.jpg") is None

    def test_the_deepest_hand_written_name_wins(self):
        """`Грузия 2022-2023\\Батуми` — внутренняя папка описывает именно
        эти снимки."""
        assert (
            deepest_named_segment(rf"{GEORGIA_FOLDER}\Батуми\IMG_1.jpg") == "Батуми"
        )
        assert (
            deepest_named_segment(rf"{DUMP_FOLDER}\Wedding Day\IMG_1.jpg")
            == "Wedding Day"
        )

    def test_classification_is_keepers_one_and_not_a_second_copy(self):
        """Два списка слов разъехались бы, и разъехалась бы та половина,
        которая решает, имя ли `2019`."""
        assert albums_module.classify_segment is keeper_module.classify_segment

    def test_the_drive_letter_is_never_a_folder_name(self):
        assert path_segments(r"D:\Photos\Краснодар\IMG_1.jpg") == [
            "Photos",
            "Краснодар",
        ]

    def test_a_folder_only_a_minority_sits_in_is_not_the_albums_name(self, gazetteer):
        """Порог есть и у четвёртого звена: два снимка из шести в названной
        папке — это не название события."""
        moments = [
            moment(rf"{WEDDING_FOLDER}\IMG_{i}.jpg", ts(2017, 4, 16, 14, i))
            for i in range(2)
        ] + [
            moment(rf"{DUMP_FOLDER}\IMG_{i}.jpg", ts(2017, 4, 16, 15, i))
            for i in range(4)
        ]
        suggestion = suggest_for_event(event(*moments), gazetteer=gazetteer)
        assert suggestion.primary.source == "dates"

    def test_moments_by_folder_counts_only_hand_written_names(self):
        counted = moments_by_folder(
            [
                moment(rf"{WEDDING_FOLDER}\a.jpg", ts(2017, 4, 16)),
                moment(rf"{WEDDING_FOLDER}\b.jpg", ts(2017, 4, 16)),
                moment(rf"{DUMP_FOLDER}\c.jpg", ts(2017, 4, 16)),
                moment(r"D:\Photos\2019\d.jpg", ts(2019, 1, 1)),
            ]
        )
        assert counted == {"Wedding 16042017": 2}


class TestDatesWhenGeographyIsSilent:
    """Событие без геоданных получает честный диапазон дат."""

    def test_an_event_with_no_geodata_gets_an_honest_range(self, gazetteer):
        cluster = event(
            moment(rf"{DUMP_FOLDER}\IMG_1.jpg", ts(2018, 5, 14, 9)),
            moment(rf"{DUMP_FOLDER}\IMG_2.jpg", ts(2018, 5, 17, 20)),
        )
        suggestion = suggest_for_event(cluster, gazetteer=gazetteer)
        assert suggestion.primary.source == "dates"
        assert suggestion.name == "14–17 мая 2018"
        assert suggestion.primary.strength == 1.0
        assert "выдуманного места" in suggestion.primary.evidence

    @pytest.mark.parametrize(
        ("first", "last", "expected"),
        [
            ((2018, 5, 14), (2018, 5, 14), "14 мая 2018"),
            ((2018, 5, 14), (2018, 5, 17), "14–17 мая 2018"),
            ((2021, 4, 28), (2021, 5, 3), "28 апреля — 3 мая 2021"),
            ((2019, 12, 29), (2020, 1, 2), "29 декабря 2019 — 2 января 2020"),
        ],
    )
    def test_the_four_shapes_of_a_date_range(self, first, last, expected):
        """Четыре формы, потому что повторять месяц и год с обеих сторон
        тире — это как дату пишет машина."""
        assert format_date_range(dt.date(*first), dt.date(*last)) == expected

    def test_a_month_is_spelled_the_way_r4_spells_it(self):
        """Р4 приводит «август 2026» своим собственным примером."""
        assert format_month(dt.date(2026, 8, 23)) == "август 2026"

    def test_a_place_resting_on_one_or_two_photos_is_marked_as_thin(self, gazetteer):
        """Согласие полное, но согласны двое: место правдоподобно и честно
        помечено как недоказанное, а не выдано за факт."""
        cluster = event(
            moment(
                rf"{DUMP_FOLDER}\IMG_1.jpg",
                ts(2022, 8, 3, 10),
                latitude=BATUMI.latitude,
                longitude=BATUMI.longitude,
            ),
            moment(
                rf"{DUMP_FOLDER}\IMG_2.jpg",
                ts(2022, 8, 3, 11),
                latitude=BATUMI.latitude + 0.001,
                longitude=BATUMI.longitude,
            ),
        )
        suggestion = suggest_for_event(cluster, gazetteer=gazetteer)
        assert suggestion.primary.source == "place"
        assert suggestion.primary.solid is False
        assert "не доказано" in suggestion.primary.evidence

    def test_a_city_the_event_straddles_is_not_split_in_two(self, gazetteer):
        """Настоящая поездка в Батуми: половина снимков в черте города,
        половина — в получасе от него по берегу.

        Досягаемость Батуми при 152 839 жителях — около 3.9 км, поэтому
        снимки в 10 км от центра геокодер честно отдаёт как «Батуми
        (окрестности)». Но место у них **одно**, и событие обязано
        называться Батуми: согласие считается по населённому пункту, а не по
        строке подписи, иначе один и тот же город, снятый с двух сторон,
        сам у себя отбирает большинство и событие теряет место целиком.
        """
        moments = []
        for i in range(5):
            moments.append(
                moment(
                    rf"{GEORGIA_FOLDER}\IMG_in_{i}.jpg",
                    ts(2022, 8, 3, 10, i),
                    latitude=BATUMI.latitude + 0.002 * i,
                    longitude=BATUMI.longitude,
                )
            )
        for i in range(5):
            moments.append(
                moment(
                    rf"{GEORGIA_FOLDER}\IMG_near_{i}.jpg",
                    ts(2022, 8, 3, 15, i),
                    latitude=BATUMI.latitude + 0.09 + 0.002 * i,
                    longitude=BATUMI.longitude,
                )
            )
        suggestion = suggest_for_event(event(*moments), gazetteer=gazetteer)
        place = next(
            (c for c in suggestion.candidates if c.source == "place"), None
        )
        assert place is not None, "город, снятый с двух сторон, потерял место"
        assert place.subject.startswith("Батуми")
        assert place.strength == pytest.approx(1.0)

    def test_an_event_genuinely_outside_a_town_still_says_so(self, gazetteer):
        """Обратная половина той же починки: если событие и правда вне
        черты города, подпись обязана остаться «(окрестности)», а не
        тихо присвоить событию город."""
        moments = [
            moment(
                rf"{GEORGIA_FOLDER}\IMG_{i}.jpg",
                ts(2022, 8, 4, 12, i),
                latitude=BATUMI.latitude + 0.09 + 0.002 * i,
                longitude=BATUMI.longitude,
            )
            for i in range(4)
        ]
        suggestion = suggest_for_event(event(*moments), gazetteer=gazetteer)
        assert suggestion.primary.source == "place"
        assert suggestion.primary.subject == "Батуми (окрестности)"

    def test_the_summary_counts_the_events_living_on_dates_alone(self, gazetteer):
        """Цифра, которую отчёт задачи 20 обязан напечатать: сколько
        альбомов живут на одном диапазоне дат."""
        naming = suggest_names(
            EventClustering(
                events=[
                    wedding_event(),
                    wedding_event(geo=False, folder=DUMP_FOLDER),
                    event(
                        moment(rf"{DUMP_FOLDER}\x.jpg", ts(2019, 1, 1)),
                    ),
                ]
            ),
            gazetteer=gazetteer,
        )
        summary = naming.summary()
        assert summary["events"] == 3
        assert summary["dates_only"] == 2
        assert summary["by_source"]["place"] == 1
        assert summary["confirmed"] == 0


class TestPolicyIsHonest:
    """Пороги названы и передаются снаружи — как в `EventThresholds` и
    `PersonThresholds`, и по той же причине."""

    def test_the_default_order_is_the_one_r4_wrote_down(self):
        assert DEFAULT_POLICY.subject_order == ("place", "person", "folder")

    def test_lowering_the_place_threshold_brings_a_minority_place_back(
        self, gazetteer
    ):
        """Тот же спорный набор снимков, что выше: при пороге 0.6 места нет,
        при 0.3 — есть. Порог настраивается и объясним, а не вшит."""
        moments = [
            moment(
                rf"{DUMP_FOLDER}\IMG_{i}.jpg",
                ts(2017, 4, 16, 14, i),
                latitude=place.latitude,
                longitude=place.longitude,
            )
            for i, place in enumerate([KRASNODAR] * 4 + [NOVOSIBIRSK] * 3 + [VLADIVOSTOK] * 3)
        ]
        cluster = event(*moments)
        strict = suggest_for_event(cluster, gazetteer=gazetteer)
        loose = suggest_for_event(
            cluster, gazetteer=gazetteer, policy=NamePolicy(place_share=0.3)
        )
        assert strict.primary.source == "dates"
        assert loose.primary.source == "place"
        assert loose.primary.subject == "Краснодар"
