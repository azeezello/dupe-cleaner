r"""Задача 22: исполнение плана библиотеки и откат.

Все тесты работают на **настоящих файлах в собственном временном каталоге**
(`tmp_path`), а не на моках файловой системы: проверять «перенос прошёл»
по вызовам `shutil.move` значит проверять, что мы вызвали то, что вызвали.
Здесь после каждого прогона сверяются байты.

Имена папок и кадр `20170416_145106.jpg`, лежащий в двух папках
одновременно, взяты из `D:\Photos` — это находка пилота, ради которой Р5 и
написано. Настоящую библиотеку Азиза эти тесты не видят и видеть не могут:
ни один путь здесь не выходит за `tmp_path`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dupecleaner import executor as ex
from dupecleaner.library import Bucket, LibraryLayout, LibraryPlan, PlanProblem
from dupecleaner.library import PlannedMove, Problem, Transfer
from dupecleaner.quarantine import read_journal
from dupecleaner.storage import ScanIndex


# --- строительные блоки -----------------------------------------------------


class Probe:
    """Зонд исполнения: существование спрашивает у настоящей файловой
    системы, занятость и тома задаёт тест. Занятость иначе не проверить —
    на Linux обязательных блокировок нет, и `RealProbe.is_busy` там почти
    всегда отвечает «свободен» (это свойство ОС, а не пробел в коде)."""

    blind = False

    def __init__(self, busy: set[str] | None = None, volumes: dict[str, str] | None = None):
        self._busy = {str(p) for p in (busy or set())}
        self._volumes = {str(k): v for k, v in (volumes or {}).items()}

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def is_busy(self, path: str) -> bool:
        return str(path) in self._busy

    def volume_of(self, path: str) -> str:
        for prefix, volume in self._volumes.items():
            if str(path).startswith(prefix):
                return volume
        return "/"


class RecordingOps(ex.FileOps):
    """Настоящие операции, но с протоколом вызовов — им проверяется
    порядок «скопировали → сверили → только потом удалили источник»."""

    def __init__(self):
        self.calls: list[str] = []

    def rename(self, source, destination):
        self.calls.append(f"rename {source.name}")
        super().rename(source, destination)

    def copy(self, source, destination):
        self.calls.append(f"copy {source.name}")
        super().copy(source, destination)

    def hash_file(self, path):
        self.calls.append(f"hash {path.name}")
        return super().hash_file(path)

    def replace(self, source, destination):
        self.calls.append(f"replace {source.name}")
        super().replace(source, destination)

    def remove(self, path):
        self.calls.append(f"remove {path.name}")
        super().remove(path)


class Boom(BaseException):
    """Смерть процесса, а не штатная ошибка файловой системы.

    Унаследовано от `BaseException` намеренно: `journalled_move` ловит
    `OSError` и `MoveRefused`, то есть всё, что считается неудачной
    операцией. Исключение, которое оно не ловит, — единственный способ
    изобразить «процесс убили посреди переноса», а не «перенос не удался».
    Тот же приём, что в тесте прерванного карантина (задача 5).
    """


class DyingOps(ex.FileOps):
    def __init__(self, die_on: int):
        self.moves = 0
        self._die_on = die_on

    def rename(self, source, destination):
        self.moves += 1
        if self.moves == self._die_on:
            raise Boom("процесс убит посреди переноса")
        super().rename(source, destination)


class CorruptingOps(ex.FileOps):
    """Копия приезжает не той, какой уехала — обрыв сети на NAS, сбойный
    USB. Единственное, что стоит между этим и потерей файла, — сверка
    хэша до удаления источника."""

    def copy(self, source, destination):
        destination.write_bytes(b"\x00" * max(1, source.stat().st_size))


class Tree:
    def __init__(self, root: Path, lib: Path, plan: LibraryPlan, contents: dict[str, bytes]):
        self.root = root
        self.lib = lib
        self.plan = plan
        self.contents = contents

    @property
    def fingerprint(self) -> str:
        return self.plan.fingerprint()

    def source_bytes_intact(self) -> bool:
        return all(
            Path(path).exists() and Path(path).read_bytes() == data
            for path, data in self.contents.items()
        )

    def files_in_library(self) -> list[Path]:
        return sorted(p for p in self.lib.rglob("*") if p.is_file())


def _move(
    source: Path,
    destination: Path,
    *,
    bucket: Bucket = Bucket.EVENT,
    album: str = "",
    also_at: tuple[Path, ...] = (),
    content_key: str | None = None,
) -> PlannedMove:
    return PlannedMove(
        source=str(source),
        destination=str(destination),
        bucket=bucket,
        transfer=Transfer.RENAME,
        size=source.stat().st_size,
        content_key=content_key or hashlib.sha256(source.read_bytes()).hexdigest()[:16],
        album=album,
        also_at=tuple(str(p) for p in also_at),
    )


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    """Четыре файла, два из которых — один кадр в двух папках."""
    root = tmp_path / "src"
    lib = tmp_path / "Library"
    paths = {
        "wedding_named": root / "Photos" / "Wedding 16042017" / "20170416_145106.jpg",
        "wedding_dump": root / "Pictures" / "Wedding Day" / "20170416_145106.jpg",
        "krasnodar": root / "Photos" / "Краснодар" / "IMG_0001.jpg",
        "novosibirsk": root / "Photos" / "Новосибирск 2021" / "IMG_0002.jpg",
        "screenshot": root / "Pictures" / "Screenshot_20190603_110020.png",
    }
    contents: dict[str, bytes] = {}
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (name.encode("utf-8") + b"-pixels") * 97
        if name == "wedding_dump":
            data = (b"wedding_named" + b"-pixels") * 97  # байт-в-байт та же копия
        path.write_bytes(data)
        contents[str(path)] = data

    plan = LibraryPlan(layout=LibraryLayout(root=str(lib)))
    plan.moves = [
        _move(
            paths["wedding_named"],
            lib / "2017" / "2017-04-16 Душанбе" / "20170416_145106.jpg",
            album="Душанбе, 16 апреля 2017",
            also_at=(paths["wedding_dump"],),
            content_key="wedding",
        ),
        _move(
            paths["krasnodar"],
            lib / "2022" / "2022-01-05 Краснодар" / "IMG_0001.jpg",
            album="Краснодар, 5 января 2022",
        ),
        _move(
            paths["novosibirsk"],
            lib / "2021" / "2021-05-25 Новосибирск" / "IMG_0002.jpg",
            album="Новосибирск, 25 мая 2021",
        ),
        _move(
            paths["screenshot"],
            lib / "_screenshots" / "2019" / "Screenshot_20190603_110020.png",
            bucket=Bucket.SCREENSHOTS,
        ),
    ]
    return Tree(root=root, lib=lib, plan=plan, contents=contents)


def run(tree: Tree, tmp_path: Path, **kwargs):
    kwargs.setdefault("probe", Probe())
    kwargs.setdefault("journal_path", tmp_path / "journal.jsonl")
    kwargs.setdefault("move_files", True)
    return ex.execute_plan(tree.plan, expected_fingerprint=tree.fingerprint, **kwargs)


def events_of(journal: Path) -> list[str]:
    return [e.get("event") for e in read_journal(journal)]


# --- перенос ----------------------------------------------------------------


def test_tree_is_built_and_every_byte_survives(tree: Tree, tmp_path: Path):
    """Первый пункт «готово, когда»: дерево строится, файлы на местах,
    содержимое побайтно совпадает с исходным."""
    outcome = run(tree, tmp_path)

    assert outcome.refusal is None
    assert len(outcome.moved) == 4
    assert outcome.failed == []
    assert outcome.skipped == []

    for move in tree.plan.moves:
        destination = Path(move.destination)
        assert destination.is_file(), destination
        assert destination.read_bytes() == tree.contents[move.source]
        assert not Path(move.source).exists()

    # Лишняя копия того же кадра не тронута: убрать её — решение об
    # избыточности (Р0), а не об организации.
    dump = tree.root / "Pictures" / "Wedding Day" / "20170416_145106.jpg"
    assert dump.is_file()
    assert dump.read_bytes() == tree.contents[str(dump)]

    # Р5: дата впереди имени, год отдельным уровнем, скриншоты своей веткой.
    assert (tree.lib / "2017" / "2017-04-16 Душанбе").is_dir()
    assert (tree.lib / "_screenshots" / "2019").is_dir()


def test_sidecar_and_index_both_remember_where_the_file_came_from(
    tree: Tree, tmp_path: Path
):
    """«Исходный путь сохранён и в базе, и в XMP» — буквально обе копии,
    и в XMP ещё и остальные пути того же содержимого (`also_at`), которые
    после переноса не восстановимы ниоткуда."""
    with ScanIndex(tmp_path / "index.db") as index:
        outcome = run(tree, tmp_path, index=index)

        wedding = tree.plan.moves[0]
        sidecar = Path(wedding.destination).with_suffix(".xmp")
        assert sidecar.is_file()
        text = sidecar.read_text(encoding="utf-8")
        assert wedding.source in text
        assert wedding.also_at[0] in text
        assert "Душанбе, 16 апреля 2017" in text
        assert "originalPath" in text and "alsoAt" in text

        row = index.library_origin_of(wedding.destination)
        assert row is not None
        assert row["source"] == wedding.source
        assert row["also_at"] == list(wedding.also_at)
        assert row["sidecar"] == str(sidecar)
        assert row["rolled_back_at"] is None
        assert len(index.library_moves()) == 4

    assert len(outcome.sidecars) == 4


def test_sidecar_journal_digest_matches_the_bytes_on_disk(tree: Tree, tmp_path: Path):
    journal = tmp_path / "journal.jsonl"
    run(tree, tmp_path, journal_path=journal)

    sidecars = [e for e in read_journal(journal) if e.get("event") == "sidecar_written"]
    assert len(sidecars) == 4
    for entry in sidecars:
        data = Path(entry["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["digest"]


def test_sidecar_keeps_the_full_name_when_two_files_share_a_stem(tmp_path: Path):
    """`a.jpg` и `a.png` в одной папке оба хотят `a.xmp`. Один сайдкар на
    два файла — это сайдкар ни про один из них, поэтому второй берёт
    полное имя, и это записано, а не умолчано."""
    src = tmp_path / "src"
    src.mkdir()
    lib = tmp_path / "lib"
    first, second = src / "a.jpg", src / "a.png"
    first.write_bytes(b"first" * 20)
    second.write_bytes(b"second" * 20)
    plan = LibraryPlan(layout=LibraryLayout(root=str(lib)))
    plan.moves = [
        _move(first, lib / "2020" / "2020-01-01 Город" / "a.jpg"),
        _move(second, lib / "2020" / "2020-01-01 Город" / "a.png"),
    ]
    ex.execute_plan(
        plan,
        expected_fingerprint=plan.fingerprint(),
        journal_path=tmp_path / "j.jsonl",
        move_files=True,
        probe=Probe(),
    )
    album = lib / "2020" / "2020-01-01 Город"
    assert (album / "a.xmp").is_file()
    assert (album / "a.png.xmp").is_file()
    assert first.name in (album / "a.xmp").read_text(encoding="utf-8")
    assert second.name in (album / "a.png.xmp").read_text(encoding="utf-8")


# --- сверка отпечатка -------------------------------------------------------


def test_fingerprint_mismatch_refuses_and_moves_nothing(tree: Tree, tmp_path: Path):
    """Весь смысл отпечатка: выполняется то, что человек прочитал, а не
    то, что пересчиталось после сдвига порога."""
    journal = tmp_path / "journal.jsonl"
    with pytest.raises(ex.PlanFingerprintMismatch) as caught:
        ex.execute_plan(
            tree.plan,
            expected_fingerprint="0000000000000000",
            journal_path=journal,
            move_files=True,
            probe=Probe(),
        )

    assert caught.value.expected == "0000000000000000"
    assert caught.value.actual == tree.fingerprint
    assert "порог" in str(caught.value) and "--subject-order" in str(caught.value)

    assert tree.source_bytes_intact()
    assert not tree.lib.exists()
    assert not journal.exists()


def test_a_plan_file_edited_after_it_was_read_is_refused(tree: Tree, tmp_path: Path):
    """Тот же отказ на том пути, по которому задача 22 план и получает:
    через файл, сохранённый `library --json`."""
    payload = tree.plan.to_dict()
    payload["moves"][1]["destination"] = str(
        tree.lib / "2022" / "2022-01-05 Краснодар" / "ПОДМЕНА.jpg"
    )
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    plan, recorded = ex.load_plan(path)
    assert recorded == tree.fingerprint
    with pytest.raises(ex.PlanFingerprintMismatch):
        ex.execute_plan(
            plan,
            expected_fingerprint=recorded,
            journal_path=tmp_path / "j.jsonl",
            move_files=True,
            probe=Probe(),
        )
    assert tree.source_bytes_intact()


def test_a_plan_with_no_fingerprint_is_not_executed(tree: Tree, tmp_path: Path):
    """План, чью личность никто не может подтвердить, — ровно тот план, от
    которого эта проверка и защищает."""
    with pytest.raises(ex.PlanFingerprintMismatch):
        ex.execute_plan(
            tree.plan,
            expected_fingerprint=None,
            journal_path=tmp_path / "j.jsonl",
            move_files=True,
            probe=Probe(),
        )
    assert tree.source_bytes_intact()


def test_fingerprint_survives_the_plan_file_round_trip(tree: Tree, tmp_path: Path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(tree.plan.to_dict(), ensure_ascii=False), encoding="utf-8")
    plan, recorded = ex.load_plan(path)
    assert plan.fingerprint() == recorded == tree.fingerprint


# --- сухой прогон и вопросы к человеку --------------------------------------


def test_dry_run_is_the_default_and_touches_nothing(tree: Tree, tmp_path: Path):
    journal = tmp_path / "journal.jsonl"
    outcome = ex.execute_plan(
        tree.plan,
        expected_fingerprint=tree.fingerprint,
        journal_path=journal,
        probe=Probe(),
    )
    assert outcome.dry_run is True
    assert outcome.moved == []
    assert tree.source_bytes_intact()
    assert not tree.lib.exists()
    assert not journal.exists()
    assert any("--move-files-for-real" in w for w in outcome.warnings)


def test_unanswered_questions_stop_the_run_until_they_are_accepted(
    tree: Tree, tmp_path: Path
):
    tree.plan.problems.append(
        PlanProblem(
            kind=Problem.ALBUM_COLLISION,
            source="anchor",
            destination=str(tree.lib / "2024" / "2024-07-06 Измир"),
            detail="два события просят одну папку",
        )
    )
    outcome = run(tree, tmp_path)
    assert outcome.refusal is not None
    assert "album_collision" in outcome.refusal
    assert outcome.moved == []
    assert tree.source_bytes_intact()

    accepted = run(tree, tmp_path, accept_unresolved=True)
    assert accepted.refusal is None
    assert len(accepted.moved) == 4


# --- занятые файлы ----------------------------------------------------------


def test_a_busy_file_is_skipped_and_the_rest_of_the_plan_goes_through(
    tree: Tree, tmp_path: Path
):
    """Р5 это уже решила: занятый файл — предупреждение и пропуск, а не
    вопрос. Занятость спрашивается в момент исполнения: план мог строиться
    слепым зондом, да и редактор могли открыть между планом и прогоном."""
    busy = tree.plan.moves[2].source
    journal = tmp_path / "journal.jsonl"
    outcome = run(tree, tmp_path, probe=Probe(busy={busy}), journal_path=journal)

    assert len(outcome.moved) == 3
    assert len(outcome.skipped) == 1
    assert outcome.skipped[0]["source"] == busy
    assert "открыт другим процессом" in outcome.skipped[0]["reason"]

    assert Path(busy).read_bytes() == tree.contents[busy]
    assert not Path(tree.plan.moves[2].destination).exists()

    skips = [e for e in read_journal(journal) if e.get("event") == "move_skipped"]
    assert len(skips) == 1
    assert skips[0]["original"] == busy
    assert skips[0]["reason"] == "busy"


# --- между томами -----------------------------------------------------------


def _cross_volume_probe(tree: Tree, **kwargs) -> Probe:
    return Probe(volumes={str(tree.root): "D:", str(tree.lib): "E:"}, **kwargs)


def test_cross_volume_move_verifies_the_copy_before_the_source_dies(
    tree: Tree, tmp_path: Path
):
    ops = RecordingOps()
    outcome = run(tree, tmp_path, probe=_cross_volume_probe(tree), ops=ops)

    assert len(outcome.moved) == 4
    assert all(m["transfer"] == "copy_verify" for m in outcome.moved)
    for move in tree.plan.moves:
        assert Path(move.destination).read_bytes() == tree.contents[move.source]
        assert not Path(move.source).exists()

    first = ops.calls[:5]
    assert first[0].startswith("hash ")      # источник
    assert first[1].startswith("copy ")
    assert first[2].startswith("hash ")      # копия
    assert first[3].startswith("replace ")
    assert first[4].startswith("remove ")    # и только теперь источник
    assert not list(tree.lib.rglob(f"*{ex.PART_SUFFIX}"))


def test_cross_volume_copy_that_does_not_match_keeps_the_source(
    tree: Tree, tmp_path: Path
):
    """Не сошлись — источник остаётся, операция в журнале помечена
    неудачной, в библиотеку не попадает ничего."""
    journal = tmp_path / "journal.jsonl"
    outcome = run(
        tree,
        tmp_path,
        probe=_cross_volume_probe(tree),
        ops=CorruptingOps(),
        journal_path=journal,
    )

    assert outcome.moved == []
    assert len(outcome.failed) == 4
    assert all("не сошлась" in f["reason"] for f in outcome.failed)

    assert tree.source_bytes_intact()
    assert tree.files_in_library() == []
    assert not list(tree.lib.rglob(f"*{ex.PART_SUFFIX}"))

    journal_events = events_of(journal)
    assert journal_events.count("move_pending") == 4
    assert journal_events.count("move_failed") == 4
    assert "move_done" not in journal_events


def test_a_rename_that_turns_out_to_cross_volumes_falls_back_to_verifying(
    tree: Tree, tmp_path: Path
):
    """План обещал переименование, а ядро ответило EXDEV. Р5 — правило, а
    не предпочтение: переходим на копирование со сверкой, а не отдаём
    перенос `shutil.move`, который скопирует и удалит, не сравнив ни
    байта."""
    import errno

    class ExdevOps(ex.FileOps):
        def rename(self, source, destination):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

    ops = ExdevOps()
    outcome = run(tree, tmp_path, ops=ops)
    assert len(outcome.moved) == 4
    assert all(m["transfer"] == "copy_verify" for m in outcome.moved)
    assert any("другом томе" in w for w in outcome.warnings)
    for move in tree.plan.moves:
        assert Path(move.destination).read_bytes() == tree.contents[move.source]


# --- ничего не перезаписывается ---------------------------------------------


def test_a_file_that_appeared_at_the_destination_is_never_overwritten(
    tree: Tree, tmp_path: Path
):
    """Между планом и исполнением по целевому пути что-то появилось —
    отказ по этому файлу, а не затирание."""
    target = Path(tree.plan.moves[1].destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"CHUZHOY FAYL")
    journal = tmp_path / "journal.jsonl"

    outcome = run(tree, tmp_path, journal_path=journal)

    assert target.read_bytes() == b"CHUZHOY FAYL"
    source = tree.plan.moves[1].source
    assert Path(source).read_bytes() == tree.contents[source]
    assert len(outcome.failed) == 1
    assert "не затираю" in outcome.failed[0]["reason"]
    assert len(outcome.moved) == 3
    assert "move_refused" in events_of(journal)


# --- журнал пишется до операции ---------------------------------------------


def test_the_journal_line_is_written_before_the_move_happens(
    tree: Tree, tmp_path: Path
):
    """Упасть между записью и операцией — и убедиться, что на диске
    осталась запись о намерении без отметки о результате. Это та самая
    половина Р5, из которой откат вообще возможен."""
    journal = tmp_path / "journal.jsonl"
    with pytest.raises(Boom):
        run(tree, tmp_path, ops=DyingOps(die_on=3), journal_path=journal)

    entries = [e for e in read_journal(journal) if e.get("op_id")]
    last = entries[-1]
    assert last["event"] == "move_pending"
    assert last["original"] == tree.plan.moves[2].source
    assert last["kind"] == ex.KIND
    # Ни одной отметки о результате у этой операции нет — и файл не тронут.
    assert [e["event"] for e in entries if e["op_id"] == last["op_id"]] == ["move_pending"]
    assert Path(tree.plan.moves[2].source).exists()
    assert not Path(tree.plan.moves[2].destination).exists()

    pairs = [e["event"] for e in entries]
    assert pairs == ["move_pending", "move_done", "move_pending", "move_done", "move_pending"]


# --- откат ------------------------------------------------------------------


def test_a_finished_run_rolls_back_byte_for_byte(tree: Tree, tmp_path: Path):
    journal = tmp_path / "journal.jsonl"
    with ScanIndex(tmp_path / "index.db") as index:
        run(tree, tmp_path, journal_path=journal, index=index)
        back = ex.rollback_library(journal, move_files=True, index=index)

        assert len(back.restored) == 4
        assert back.failed == []
        assert len(back.sidecars_removed) == 4
        assert all(
            row["rolled_back_at"] is not None for row in index.library_moves()
        )

    assert tree.source_bytes_intact()
    assert tree.files_in_library() == []
    # Пустые папки, созданные переносом, тоже убраны: иначе это не откат.
    assert not (tree.lib / "2017").exists()


def test_an_interrupted_run_rolls_back_exactly_like_a_finished_one(
    tree: Tree, tmp_path: Path
):
    """Оборвать на середине и откатить по журналу — все файлы вернулись
    туда, где были, побайтно. Прерванная операция не «неизвестность»: у
    неё есть запись о намерении, и откат по ней идёт смотреть оба пути."""
    journal = tmp_path / "journal.jsonl"
    with pytest.raises(Boom):
        run(tree, tmp_path, ops=DyingOps(die_on=3), journal_path=journal)

    assert len(tree.files_in_library()) > 0  # что-то уже переехало

    back = ex.rollback_library(journal, move_files=True)
    assert len(back.restored) == 2
    assert len(back.skipped) == 1
    assert "перенос не состоялся" in back.skipped[0]["reason"]
    assert back.failed == []

    assert tree.source_bytes_intact()
    assert tree.files_in_library() == []


def test_rollback_walks_the_journal_backwards(tree: Tree, tmp_path: Path):
    """Корень библиотеки может законно лежать внутри дерева, из которого
    её собирают, — тогда порядок перемещений значим, и обратный порядок
    единственный безопасный."""
    journal = tmp_path / "journal.jsonl"
    outcome = run(tree, tmp_path, journal_path=journal)
    back = ex.rollback_library(journal, move_files=True)
    assert [r["op_id"] for r in back.restored] == [
        m["op_id"] for m in reversed(outcome.moved)
    ]


def test_rollback_dry_run_shows_what_it_would_do_and_does_nothing(
    tree: Tree, tmp_path: Path
):
    journal = tmp_path / "journal.jsonl"
    run(tree, tmp_path, journal_path=journal)
    before = {str(p): p.read_bytes() for p in tree.files_in_library()}

    back = ex.rollback_library(journal)
    assert back.dry_run is True
    assert len(back.restored) == 4
    assert {str(p): p.read_bytes() for p in tree.files_in_library()} == before
    assert not Path(tree.plan.moves[0].source).exists()


def test_rollback_is_idempotent(tree: Tree, tmp_path: Path):
    journal = tmp_path / "journal.jsonl"
    run(tree, tmp_path, journal_path=journal)
    ex.rollback_library(journal, move_files=True)
    again = ex.rollback_library(journal, move_files=True)
    assert again.restored == []
    assert tree.source_bytes_intact()


def test_rollback_refuses_to_overwrite_a_file_that_took_the_original_path(
    tree: Tree, tmp_path: Path
):
    journal = tmp_path / "journal.jsonl"
    run(tree, tmp_path, journal_path=journal)
    taken = Path(tree.plan.moves[0].source)
    taken.parent.mkdir(parents=True, exist_ok=True)
    taken.write_bytes(b"CHTO-TO NOVOE")

    back = ex.rollback_library(journal, move_files=True)
    assert taken.read_bytes() == b"CHTO-TO NOVOE"
    assert len(back.skipped) == 1
    assert "разберитесь вручную" in back.skipped[0]["reason"]
    assert Path(tree.plan.moves[0].destination).is_file()


def test_rollback_removes_only_a_sidecar_it_wrote_itself(tree: Tree, tmp_path: Path):
    """Единственное удаление на всём пути отката — файл, который написали
    мы сами и байты которого не изменились. Тронутый человеком сайдкар
    остаётся на месте, и об этом сказано."""
    journal = tmp_path / "journal.jsonl"
    run(tree, tmp_path, journal_path=journal)
    edited = Path(tree.plan.moves[0].destination).with_suffix(".xmp")
    edited.write_text("правка человека", encoding="utf-8")

    back = ex.rollback_library(journal, move_files=True)
    assert edited.is_file()
    assert edited.read_text(encoding="utf-8") == "правка человека"
    assert any("изменился" in w for w in back.warnings)
    assert len(back.restored) == 4
    assert len(back.sidecars_removed) == 3
    assert tree.source_bytes_intact()


def test_rollback_of_a_cross_volume_move_verifies_on_the_way_back(
    tree: Tree, tmp_path: Path
):
    journal = tmp_path / "journal.jsonl"
    run(tree, tmp_path, probe=_cross_volume_probe(tree), journal_path=journal)
    ops = RecordingOps()
    back = ex.rollback_library(journal, move_files=True, ops=ops)

    assert len(back.restored) == 4
    assert tree.source_bytes_intact()
    assert "copy 20170416_145106.jpg" in ops.calls
    assert ops.calls.count("rename 20170416_145106.jpg") == 0


def test_rollback_without_a_journal_says_so_instead_of_guessing(tmp_path: Path):
    back = ex.rollback_library(tmp_path / "net-takogo.jsonl", move_files=True)
    assert back.restored == []
    assert any("журнала нет" in w for w in back.warnings)


def test_rollback_leaves_quarantine_operations_alone(tree: Tree, tmp_path: Path):
    """В один файл могут попасть обе механики — они пишут один формат.
    Откат библиотеки трогает только свои операции."""
    journal = tmp_path / "journal.jsonl"
    from dupecleaner.quarantine import JournalWriter, journalled_move

    alien = tmp_path / "alien.txt"
    alien.write_bytes(b"ne moy fayl")
    with JournalWriter(journal) as writer:
        journalled_move(alien, tmp_path / "alien-moved.txt", writer, size=11)

    run(tree, tmp_path, journal_path=journal)
    back = ex.rollback_library(journal, move_files=True)
    assert back.considered == 4
    assert (tmp_path / "alien-moved.txt").is_file()
    assert not alien.exists()
