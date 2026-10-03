r"""Задача 21: планировщик библиотеки — план строится, файлы не двигаются.

Данные в тестах настоящие там, где это что-то значит: имена папок взяты из
`D:\Photos` (`Wedding 16042017`, `Краснодар`, `Новосибирск 2021`), а кадр
`20170416_145106.jpg`, лежащий одновременно в четырёх папках, — это находка
пилота, ради которой Р5 и написано. Выдуманы только временные каталоги в
тесте «ничего не записано на диск».
"""

from __future__ import annotations

import ast
import collections
import datetime as dt
import inspect
import os

import pytest

from dupecleaner import library as lib
from dupecleaner import events as ev
from dupecleaner.albums import AlbumNaming, AlbumSuggestion, NameCandidate
from dupecleaner.keeper import choose_keeper
from dupecleaner.models import FileRecord


# --- строительные блоки -----------------------------------------------------


def wall(year=2017, month=4, day=16, hour=14, minute=51, second=6) -> float:
    return dt.datetime(year, month, day, hour, minute, second).timestamp()


def moment(path: str, when: float | None) -> ev.PhotoMoment:
    return ev.PhotoMoment(
        display_path=path,
        taken_at=when,
        time_source=ev.TimeSource.EXIF if when is not None else ev.TimeSource.NONE,
    )


def record(path: str, size: int = 2_000_000, mtime: float | None = None) -> FileRecord:
    return FileRecord(
        display_path=path,
        real_path=path,
        size=size,
        mtime=mtime if mtime is not None else wall(),
    )


def member(path: str, archive: str, size: int = 2_000_000) -> FileRecord:
    return FileRecord(
        display_path=path,
        real_path=archive,
        size=size,
        mtime=wall(),
        is_archive_member=True,
        archive_path=archive,
        member_name=path.split("::", 1)[-1],
    )


def suggestion(
    anchor: str,
    subject: str,
    *,
    source: str = "place",
    confirmed: str | None = None,
    size: int = 1,
) -> AlbumSuggestion:
    text = f"{subject}, 16 апреля 2017" if subject else "16 апреля 2017"
    return AlbumSuggestion(
        anchor=anchor,
        date_range="16 апреля 2017",
        candidates=[
            NameCandidate(
                source=source,
                subject=subject,
                text=text,
                evidence="тестовые данные",
                strength=1.0,
            )
        ],
        confirmed=confirmed,
        size=size,
    )


def one_event(paths: list[str], *, when: float | None = None) -> ev.EventClustering:
    stamp = when if when is not None else wall()
    moments = [moment(p, stamp + i) for i, p in enumerate(paths)]
    return ev.EventClustering(events=[ev.EventCluster(moments=moments)])


def layout(root: str = r"D:\Library", **kw) -> lib.LibraryLayout:
    return lib.LibraryLayout(root=root, **kw)


def plan_one(
    paths: list[str],
    *,
    subject: str = "Душанбе",
    records: dict[str, FileRecord] | None = None,
    content_keys: dict[str, str] | None = None,
    probe: lib.PlanProbe | None = None,
    root: str = r"D:\Library",
    **layout_kw,
) -> lib.LibraryPlan:
    clustering = one_event(paths)
    naming = AlbumNaming(suggestions=[suggestion(paths[0], subject, size=len(paths))])
    return lib.plan_library(
        clustering,
        naming,
        records=records or {p: record(p) for p in paths},
        layout=layout(root, **layout_kw),
        content_keys=content_keys,
        probe=probe,
    )


class Probe:
    """Пробник с заданными ответами — те же три вопроса, что у настоящего."""

    blind = False

    def __init__(self, *, existing: set[str] | None = None, busy: set[str] | None = None,
                 volumes: dict[str, str] | None = None):
        self.existing = {p.lower() for p in (existing or set())}
        self.busy = busy or set()
        self.volumes = volumes or {}
        self.asked_write = []

    def exists(self, path: str) -> bool:
        return path.lower() in self.existing

    def is_busy(self, path: str) -> bool:
        return path in self.busy

    def volume_of(self, path: str) -> str:
        for prefix, volume in self.volumes.items():
            if path.startswith(prefix):
                return volume
        return lib.volume_from_path(path)


# --- 1. ничего не двигается -------------------------------------------------


def test_planning_does_not_touch_a_single_file(tmp_path):
    """Главное обещание задачи 21, проверенное на настоящем каталоге.

    Состав, размеры и `mtime_ns` снимаются до и после; побайтное сравнение
    поймало бы и перемещение, и перезапись, и «безобидное» открытие на
    запись, которое обнуляет файл.
    """
    folder = tmp_path / "Wedding 16042017"
    folder.mkdir()
    for name in ("20170416_145106.jpg", "20170416_145107.jpg"):
        (folder / name).write_bytes(b"\xff\xd8\xff" + name.encode())

    def snapshot():
        out = {}
        for root, _, files in os.walk(tmp_path):
            for name in files:
                p = os.path.join(root, name)
                st = os.stat(p)
                out[p] = (st.st_size, st.st_mtime_ns, open(p, "rb").read())
        return out

    before = snapshot()
    paths = [str(folder / "20170416_145106.jpg"), str(folder / "20170416_145107.jpg")]
    plan = plan_one(paths, root=str(tmp_path / "Library"), probe=lib.RealProbe())

    assert plan.moves, "план должен быть построен, иначе тест ничего не проверяет"
    assert snapshot() == before


def test_the_module_has_no_way_to_move_a_file():
    """Запрет держится формой, а не дисциплиной: инструментов нет под рукой.

    Проверяется дерево импортов, а не поиск подстроки: докстрока модуля
    законно упоминает `quarantine` — она объясняет, что перенос это задача
    22 и что журнал берётся оттуда. Упоминание в прозе и импорт в коде —
    разные вещи, и спутать их значит написать тест, который нельзя
    удовлетворить, не испортив документацию.
    """
    tree = ast.parse(inspect.getsource(lib))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
            imported.update(a.name for a in node.names)
    assert "shutil" not in imported
    assert "quarantine" not in imported
    for forbidden in ("shutil", "quarantine"):
        assert not hasattr(lib, forbidden)
    calls = {
        f"{n.func.value.id}.{n.func.attr}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
    }
    assert not {"os.rename", "os.replace", "os.remove", "os.unlink"} & calls


# --- 2. отпечаток плана -----------------------------------------------------


def test_fingerprint_is_stable_across_two_identical_runs():
    paths = [r"D:\Photos\Краснодар\IMG_1.jpg", r"D:\Photos\Краснодар\IMG_2.jpg"]
    first = plan_one(paths)
    second = plan_one(paths)
    assert first.fingerprint() == second.fingerprint()


def test_fingerprint_ignores_the_order_moves_were_built_in():
    paths = [r"D:\Photos\Краснодар\IMG_1.jpg", r"D:\Photos\Краснодар\IMG_2.jpg"]
    plan = plan_one(paths)
    shuffled = lib.LibraryPlan(layout=plan.layout, moves=list(reversed(plan.moves)))
    assert shuffled.fingerprint() == plan.fingerprint()


def test_fingerprint_changes_when_a_destination_changes():
    paths = [r"D:\Photos\Краснодар\IMG_1.jpg"]
    assert plan_one(paths, subject="Краснодар").fingerprint() != plan_one(
        paths, subject="Новосибирск"
    ).fingerprint()


def test_fingerprint_changes_when_a_move_is_added():
    base = [r"D:\Photos\Краснодар\IMG_1.jpg"]
    more = base + [r"D:\Photos\Краснодар\IMG_2.jpg"]
    assert plan_one(base).fingerprint() != plan_one(more).fingerprint()


# --- 3. сериализация --------------------------------------------------------


def test_plan_survives_a_round_trip_through_a_dict():
    paths = [r"D:\Photos\Краснодар\IMG_1.jpg", r"D:\Photos\Краснодар\IMG_2.jpg"]
    plan = plan_one(paths)
    restored = lib.plan_from_dict(plan.to_dict())

    assert restored.fingerprint() == plan.fingerprint()
    assert [(m.source, m.destination, m.transfer, m.bucket, m.size) for m in restored.moves] == [
        (m.source, m.destination, m.transfer, m.bucket, m.size) for m in plan.moves
    ]
    assert restored.layout == plan.layout
    assert [a.folder for a in restored.albums] == [a.folder for a in plan.albums]
    assert restored.summary() == plan.summary()


# --- 4. один кадр в четырёх папках -> один канонический путь ----------------


FOUR_PLACES = [
    r"D:\Photos\Pictures\20170416_145106.jpg",
    r"D:\Photos\Pictures\Wedding Day\20170416_145106.jpg",
    r"D:\Photos\Pictures\Diljon\20170416_145106.jpg",
    r"D:\Photos\Wedding 16042017\20170416_145106.jpg",
]


def test_one_content_in_four_folders_gets_exactly_one_destination():
    """Находка пилота. Р5 требует один канонический путь на файл."""
    keys = {p: "hash-wedding" for p in FOUR_PLACES}
    plan = plan_one(FOUR_PLACES, content_keys=keys)

    assert len(plan.moves) == 1
    assert len(plan.redundant) == 3
    assert {r.source for r in plan.redundant} == set(FOUR_PLACES) - {plan.moves[0].source}
    assert plan.moves[0].also_at == tuple(sorted(set(FOUR_PLACES) - {plan.moves[0].source}))


def test_the_mover_is_the_one_Р8_chooses_not_a_second_ranking():
    keys = {p: "hash-wedding" for p in FOUR_PLACES}
    records = {p: record(p) for p in FOUR_PLACES}
    plan = plan_one(FOUR_PLACES, content_keys=keys, records=records)
    assert plan.moves[0].source == choose_keeper(list(records.values())).display_path


def test_redundant_copies_are_not_quarantined_only_reported():
    keys = {p: "hash-wedding" for p in FOUR_PLACES}
    plan = plan_one(FOUR_PLACES, content_keys=keys)
    assert all(r.source not in {m.source for m in plan.moves} for r in plan.redundant)
    assert all("Р0" in r.reason or "Р8" in r.reason for r in plan.redundant)


# --- 5. конфликты имён ------------------------------------------------------


def test_two_files_with_one_basename_both_get_a_destination():
    paths = [r"D:\Photos\A\IMG_1234.jpg", r"D:\Photos\B\IMG_1234.jpg"]
    plan = plan_one(paths, content_keys={paths[0]: "h1", paths[1]: "h2"})

    assert len(plan.moves) == 2
    destinations = {m.destination for m in plan.moves}
    assert len(destinations) == 2, "два файла не должны получить один путь"
    renamed = [m for m in plan.moves if m.renamed_from]
    assert len(renamed) == 1 and renamed[0].renamed_from == "IMG_1234.jpg"


def test_an_occupied_destination_is_reported_and_never_overwritten():
    paths = [r"D:\Photos\Краснодар\IMG_1.jpg"]
    probe = Probe()
    plan = plan_one(paths, probe=probe)
    target = plan.moves[0].destination

    busy_probe = Probe(existing={target})
    second = plan_one(paths, probe=busy_probe)
    assert all(m.destination != target for m in second.moves)
    assert second.moves[0].renamed_from == "IMG_1.jpg"


def test_two_events_claiming_one_named_folder_is_a_problem_for_a_human():
    """«Wedding 16042017, 23 декабря 2016» из отчёта пункта 20 — ровно этот
    случай: одно имя папки, два разных события."""
    first = ev.EventCluster(moments=[moment(r"D:\Photos\W\a.jpg", wall())])
    second = ev.EventCluster(moments=[moment(r"D:\Photos\W\b.jpg", wall(hour=20))])
    clustering = ev.EventClustering(events=[first, second])
    naming = AlbumNaming(
        suggestions=[
            suggestion(r"D:\Photos\W\a.jpg", "Душанбе"),
            suggestion(r"D:\Photos\W\b.jpg", "Душанбе"),
        ]
    )
    plan = lib.plan_library(
        clustering,
        naming,
        records={p: record(p) for p in (r"D:\Photos\W\a.jpg", r"D:\Photos\W\b.jpg")},
        layout=layout(),
        content_keys={r"D:\Photos\W\a.jpg": "h1", r"D:\Photos\W\b.jpg": "h2"},
    )

    collisions = [p for p in plan.problems if p.kind is lib.Problem.ALBUM_COLLISION]
    assert len(collisions) == 1
    assert collisions[0].kind.needs_human
    assert len({a.folder for a in plan.albums}) == 2, "папки должны различаться"
    assert len(plan.moves) == 2


def test_two_unnamed_events_on_one_day_share_a_folder_by_design():
    first = ev.EventCluster(moments=[moment(r"D:\Photos\x\a.jpg", wall(hour=9))])
    second = ev.EventCluster(moments=[moment(r"D:\Photos\x\b.jpg", wall(hour=21))])
    clustering = ev.EventClustering(events=[first, second])
    naming = AlbumNaming(
        suggestions=[
            suggestion(r"D:\Photos\x\a.jpg", "", source="dates"),
            suggestion(r"D:\Photos\x\b.jpg", "", source="dates"),
        ]
    )
    plan = lib.plan_library(
        clustering,
        naming,
        records={p: record(p) for p in (r"D:\Photos\x\a.jpg", r"D:\Photos\x\b.jpg")},
        layout=layout(),
        content_keys={r"D:\Photos\x\a.jpg": "h1", r"D:\Photos\x\b.jpg": "h2"},
    )
    assert not [p for p in plan.problems if p.kind is lib.Problem.ALBUM_COLLISION]
    assert len({a.folder for a in plan.albums}) == 1
    assert [a.merged_events for a in plan.albums] == [2]


# --- 6. занятый файл --------------------------------------------------------


def test_a_busy_file_is_skipped_with_a_reason_and_the_plan_survives():
    paths = [r"D:\Photos\Краснодар\IMG_1.jpg", r"D:\Photos\Краснодар\IMG_2.jpg"]
    probe = Probe(busy={paths[0]})
    plan = plan_one(paths, content_keys={paths[0]: "h1", paths[1]: "h2"}, probe=probe)

    busy = plan.busy
    assert [p.source for p in busy] == [paths[0]]
    assert busy[0].detail
    assert [m.source for m in plan.moves] == [paths[1]], "остальной план остаётся"


def test_busy_is_a_warning_not_a_question_for_a_human():
    """Р5 это уже решила: занятый файл пропускается с предупреждением."""
    assert not lib.Problem.BUSY.needs_human
    assert lib.Problem.DESTINATION_EXISTS.needs_human
    assert lib.Problem.ALBUM_COLLISION.needs_human
    assert lib.Problem.PATH_TOO_LONG.needs_human


# --- 7. архивы (Р1) ---------------------------------------------------------


def test_an_archive_member_is_not_planned_and_says_why():
    plain = r"D:\Photos\Краснодар\IMG_1.jpg"
    inside = r"D:\Archive\old.zip::photos\IMG_9.jpg"
    clustering = one_event([plain, inside])
    naming = AlbumNaming(suggestions=[suggestion(plain, "Краснодар", size=2)])
    plan = lib.plan_library(
        clustering,
        naming,
        records={plain: record(plain), inside: member(inside, r"D:\Archive\old.zip")},
        layout=layout(),
        content_keys={plain: "h1", inside: "h2"},
    )

    problems = [p for p in plan.problems if p.kind is lib.Problem.ARCHIVE_MEMBER]
    assert [p.source for p in problems] == [inside]
    assert [m.source for m in plan.moves] == [plain]


# --- 8. переезд между томами ------------------------------------------------


def test_a_move_inside_one_volume_is_a_rename():
    paths = [r"D:\Photos\Краснодар\IMG_1.jpg"]
    plan = plan_one(paths, root=r"D:\Library")
    assert plan.moves[0].transfer is lib.Transfer.RENAME


def test_a_move_across_volumes_is_a_separate_mode():
    paths = [r"D:\Photos\Краснодар\IMG_1.jpg"]
    plan = plan_one(paths, root=r"E:\Library")
    assert plan.moves[0].transfer is lib.Transfer.COPY_VERIFY
    assert plan.bytes_by_transfer() == {"copy_verify": plan.total_bytes}


def test_a_network_share_is_one_volume():
    assert lib.volume_from_path(r"\\nas\photos\a\b.jpg") == r"\\nas\photos"
    assert lib.volume_from_path(r"D:\Photos\a.jpg") == "D:"
    assert lib.volume_from_path("/home/aziz/a.jpg") == "/"


def test_one_library_can_straddle_two_volumes_in_one_plan():
    """Режим решается на файл, а не на прогон."""
    same = r"D:\Photos\Краснодар\IMG_1.jpg"
    other = r"E:\Takeouts\IMG_2.jpg"
    clustering = one_event([same, other])
    naming = AlbumNaming(suggestions=[suggestion(same, "Краснодар", size=2)])
    plan = lib.plan_library(
        clustering,
        naming,
        records={same: record(same), other: record(other)},
        layout=layout(r"D:\Library"),
        content_keys={same: "h1", other: "h2"},
    )
    modes = {m.source: m.transfer for m in plan.moves}
    assert modes[same] is lib.Transfer.RENAME
    assert modes[other] is lib.Transfer.COPY_VERIFY


# --- 9. честность пробника --------------------------------------------------


def test_a_plan_built_without_the_files_says_so():
    plan = plan_one([r"D:\Photos\Краснодар\IMG_1.jpg"], probe=lib.PathProbe())
    assert plan.probe_blind
    assert any("без доступа к файлам" in w for w in plan.warnings)


def test_a_plan_built_with_the_files_makes_no_such_claim():
    plan = plan_one([r"D:\Photos\Краснодар\IMG_1.jpg"], probe=Probe())
    assert not plan.probe_blind
    assert not any("без доступа к файлам" in w for w in plan.warnings)


# --- 10. длинный путь -------------------------------------------------------


def test_a_path_too_long_for_windows_is_a_problem_not_a_move():
    deep = r"D:\Photos\Краснодар\IMG_1.jpg"
    # 150 + 1 + 4 + 1 + 120 + 1 + 9 > 260: Windows refuses to open it, and a
    # name that cannot be opened is worse than a long one.
    plan = plan_one([deep], subject="Д" * 110, root="D:\\" + "L" * 150)
    problems = [p for p in plan.problems if p.kind is lib.Problem.PATH_TOO_LONG]
    assert problems and problems[0].kind.needs_human
    assert not plan.moves


# --- 11. недатированное и исключённое Р3 ------------------------------------


def test_a_photo_with_no_timestamp_goes_to_unsorted_not_into_a_guessed_month():
    path = r"D:\Photos\Pictures\no-exif.jpg"
    clustering = ev.EventClustering(undated=[moment(path, None)])
    plan = lib.plan_library(
        clustering,
        AlbumNaming(),
        records={path: record(path)},
        layout=layout(),
    )
    assert len(plan.moves) == 1
    assert plan.moves[0].bucket is lib.Bucket.UNSORTED
    assert plan.moves[0].destination.startswith(layout().unsorted_folder())


def test_a_screenshot_is_filed_by_its_year_when_the_date_is_known():
    shot = r"D:\Photos\Pictures\Screenshot_20210704.png"
    clustering = ev.EventClustering(excluded=[shot])
    plan = lib.plan_library(
        clustering,
        AlbumNaming(),
        records={shot: record(shot)},
        layout=layout(),
        origins={shot: "screenshot_phone"},
        capture_dates={shot: dt.date(2021, 7, 4)},
    )
    assert plan.moves[0].bucket is lib.Bucket.SCREENSHOTS
    assert "2021" in plan.moves[0].destination


def test_a_screenshot_with_no_date_says_undated_rather_than_guessing():
    shot = r"D:\Photos\Pictures\Screenshot.png"
    clustering = ev.EventClustering(excluded=[shot])
    plan = lib.plan_library(
        clustering,
        AlbumNaming(),
        records={shot: record(shot)},
        layout=layout(),
        origins={shot: "screenshot_phone"},
    )
    assert plan.moves[0].bucket is lib.Bucket.SCREENSHOTS
    assert lib.UNDATED_DIR in plan.moves[0].destination


# --- 12. повторный план после прерванного прогона ---------------------------


def test_a_file_already_at_its_canonical_path_is_not_moved_twice():
    """Нормальный случай после прерванного пункта 22."""
    paths = [r"D:\Photos\Краснодар\IMG_1.jpg"]
    first = plan_one(paths)
    settled = first.moves[0].destination

    plan = plan_one([settled], root=r"D:\Library")
    assert not plan.moves
    assert plan.already_in_place == [settled]


# --- 13. файл без записи в индексе ------------------------------------------


def test_a_path_with_no_record_cannot_be_planned():
    path = r"D:\Photos\Краснодар\IMG_1.jpg"
    clustering = one_event([path])
    naming = AlbumNaming(suggestions=[suggestion(path, "Краснодар")])
    plan = lib.plan_library(clustering, naming, records={}, layout=layout())
    assert [p.kind for p in plan.problems] == [lib.Problem.NO_RECORD]
    assert not plan.moves


# --- 14. подтверждённое имя человека --------------------------------------


def test_a_confirmed_name_is_used_verbatim():
    path = r"D:\Photos\Wedding 16042017\a.jpg"
    clustering = one_event([path])
    naming = AlbumNaming(
        suggestions=[
            suggestion(path, "Душанбе", confirmed="Свадьба Wedding 16042017")
        ]
    )
    plan = lib.plan_library(
        clustering, naming, records={path: record(path)}, layout=layout()
    )
    assert "Свадьба Wedding 16042017" in plan.albums[0].folder
    assert plan.albums[0].confirmed


# --- 15. мелкий день уезжает в месяц, а не получает свою папку --------------
#
# Это поведение добавлено после того, как раскладка была посчитана на
# настоящей библиотеке: при пороге события 6 часов получалось 2163 папки, и
# 866 из них держали один-два снимка. Порог размера альбома — ответ на это,
# и он выключен по умолчанию (0), потому что на маленькой библиотеке делить
# нечего.


def month_plan(sizes: list[int], *, floor: int, confirmed: dict[int, str] | None = None):
    """План по нескольким событиям заданных размеров, все в апреле 2017."""
    confirmed = confirmed or {}
    events, suggestions, records, keys = [], [], {}, {}
    for n, size in enumerate(sizes):
        paths = [rf"D:\Photos\src\e{n}_{i}.jpg" for i in range(size)]
        stamp = wall(day=2 + n * 3)          # разные дни, одна и та же весна
        events.append(ev.EventCluster(moments=[moment(p, stamp + i) for i, p in enumerate(paths)]))
        suggestions.append(
            suggestion(paths[0], f"Город{n}", size=size, confirmed=confirmed.get(n))
        )
        for i, p in enumerate(paths):
            records[p] = record(p)
            keys[p] = f"h{n}_{i}"
    return lib.plan_library(
        ev.EventClustering(events=events),
        AlbumNaming(suggestions=suggestions),
        records=records,
        layout=layout(min_album_photos=floor),
        content_keys=keys,
    )


def test_an_event_below_the_floor_is_filed_under_its_month():
    plan = month_plan([3], floor=20)
    assert {m.bucket for m in plan.moves} == {lib.Bucket.MONTH}
    assert all("2017-04 Апрель" in m.destination for m in plan.moves)
    assert [a.source for a in plan.albums] == ["month"]
    assert plan.albums[0].name == "2017-04 Апрель"


def test_an_event_at_the_floor_keeps_its_own_folder():
    plan = month_plan([20], floor=20)
    assert {m.bucket for m in plan.moves} == {lib.Bucket.EVENT}
    assert all("2017-04-02 Город0" in m.destination for m in plan.moves)


def test_small_days_of_one_month_share_one_folder_and_are_counted():
    plan = month_plan([2, 3, 4], floor=20)
    months = [a for a in plan.albums if a.source == "month"]
    assert len(months) == 1
    assert months[0].merged_events == 3
    assert months[0].photos == 9
    assert len({m.destination.rsplit("\\", 1)[0] for m in plan.moves}) == 1


def test_big_and_small_live_side_by_side():
    plan = month_plan([30, 2], floor=20)
    by_bucket = collections.Counter(m.bucket for m in plan.moves)
    assert by_bucket[lib.Bucket.EVENT] == 30
    assert by_bucket[lib.Bucket.MONTH] == 2
    folders = {a.folder for a in plan.albums}
    assert any("2017-04-02 Город0" in f for f in folders)
    assert any("2017-04 Апрель" in f for f in folders)


def test_the_floor_is_off_by_default():
    plan = month_plan([1, 2], floor=0)
    assert {m.bucket for m in plan.moves} == {lib.Bucket.EVENT}
    assert not [a for a in plan.albums if a.source == "month"]


def test_a_name_you_confirmed_keeps_its_folder_however_small():
    """Вы сказали, что это было. Размер события — не мера того, важно ли оно."""
    plan = month_plan([2, 2], floor=20, confirmed={1: "День рождения Карима"})
    kept = [m for m in plan.moves if m.bucket is lib.Bucket.EVENT]
    assert len(kept) == 2
    assert all("День рождения Карима" in m.destination for m in kept)
    assert len([m for m in plan.moves if m.bucket is lib.Bucket.MONTH]) == 2


def test_two_small_days_of_one_month_are_not_a_collision():
    """До порога это были бы два события, просящие одну папку по дате."""
    plan = month_plan([2, 2], floor=20)
    assert not [p for p in plan.problems if p.kind is lib.Problem.ALBUM_COLLISION]


def test_the_month_move_says_why_in_words():
    plan = month_plan([2], floor=20)
    assert "слишком мал" in plan.moves[0].reason
    assert "2017-04 Апрель" in plan.moves[0].reason


def test_the_floor_survives_the_round_trip():
    plan = month_plan([2, 30], floor=20)
    restored = lib.plan_from_dict(plan.to_dict())
    assert restored.layout.min_album_photos == 20
    assert restored.fingerprint() == plan.fingerprint()
    assert [m.bucket for m in restored.moves] == [m.bucket for m in plan.moves]
