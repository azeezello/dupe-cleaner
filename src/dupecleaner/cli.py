"""Command-line entry point: `dupecleaner scan|quarantine|restore|serve|index`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .archive_classify import classify_archives
from .dedupe import verify_group
from .jobs import ScanJob
from .models import ArchiveClass, MediaKind, ScanMode, ScanReport
from .quarantine import quarantine_archives, restore_from_journal, run_quarantine
from .storage import DEFAULT_DB_PATH, ScanIndex

MODE_LABEL = {ScanMode.QUICK: "быстрый", ScanMode.FULL: "полный"}


def _fmt_bytes(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ПБ"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} мин {seconds % 60} сек"
    return f"{seconds // 3600} ч {(seconds % 3600) // 60} мин"


def _render_progress_line(progress) -> str:
    data = progress.to_dict()
    parts = [data["phase_label"]]

    if data["phase_files_total"]:
        parts.append(f"{data['phase_files_done']}/{data['phase_files_total']} файлов")
    else:
        parts.append(f"{data['files_seen']} файлов")

    if data["percent"] is not None and data["phase_files_total"]:
        parts.append(f"{data['percent']:.1f}%")
    if data["bytes_per_second"]:
        parts.append(f"{_fmt_bytes(data['bytes_per_second'])}/с")
    if data["eta_seconds"] is not None:
        parts.append(f"осталось ~{_fmt_duration(data['eta_seconds'])}")

    line = " · ".join(parts)
    current = data["current_path"]
    if current:
        # Keep the line within a typical terminal width, trimming the middle
        # of long paths rather than the end (the filename is the useful part).
        budget = max(20, 110 - len(line))
        if len(current) > budget:
            current = current[: budget // 2 - 2] + "..." + current[-(budget // 2 - 1):]
        line = f"{line} | {current}"

    if data["is_stalled"]:
        line += f"  [не отвечает {_fmt_duration(data['seconds_since_update'])}]"
    return line


def _mode_banner(job: ScanJob) -> str:
    """One line, before anything runs, saying what this run will and will
    not look at.

    Worth printing even though it reads as obvious: the modes differ only
    in coverage, and coverage is invisible in the result. A quick run over
    a folder of archives finds fewer duplicates than a full one and looks
    exactly like a folder with fewer duplicates in it. Finding A1 is the
    same mistake one level down.
    """
    if job.mode is ScanMode.QUICK:
        return (
            "Режим: быстрый — точные дубликаты среди обычных файлов "
            "(размер → быстрый хэш → полный хэш). Внутрь архивов не "
            "заглядываем, превью и метрики качества не считаем. В карантин, "
            "как и в полном режиме, уходит только подтверждённое "
            "байт-в-байт."
        )
    if not job.include_archives:
        return (
            "Режим: полный, но с --no-archives — архивы будут перечислены "
            "как непроверенные."
        )
    return (
        "Режим: полный — то же самое плюс содержимое архивов (Р1), превью "
        "для просмотра и метрики качества (Р2: считаются и показываются, "
        "на выбор копий не влияют)."
    )


def _cmd_scan(args: argparse.Namespace) -> int:
    mode = ScanMode(args.mode)
    job = ScanJob(
        roots=args.paths,
        db_path=args.db,
        # None means "whatever the mode says"; --no-archives may only
        # narrow that, never widen it (see ScanJob.__init__).
        include_archives=False if args.no_archives else None,
        mode=mode,
    )
    print(_mode_banner(job))
    job.start()
    # Live single-line progress only makes sense on a terminal. When output
    # is redirected to a file or a CI log, escape codes would just be noise,
    # so fall back to occasional plain status lines.
    interactive = sys.stdout.isatty()
    last_status_line = 0.0

    try:
        while job.is_running:
            if interactive:
                sys.stdout.write("\r\033[K" + _render_progress_line(job.progress))
                sys.stdout.flush()
            elif time.time() - last_status_line > 10:
                print(_render_progress_line(job.progress), flush=True)
                last_status_line = time.time()
            time.sleep(0.4)
    except KeyboardInterrupt:
        # Ctrl+C stops the scan cleanly instead of killing it mid-write:
        # everything hashed so far is already committed to the index.
        if interactive:
            sys.stdout.write("\r\033[K")
        print("Останавливаю...")
        job.cancel()
        job.join(timeout=30)
        print("Скан остановлен. Посчитанные хэши сохранены — "
              "запустите ту же команду снова, чтобы продолжить с этого места.")
        return 130

    job.join()
    if interactive:
        sys.stdout.write("\r\033[K")

    progress = job.progress
    if progress.status == "failed":
        print(f"Скан прерван ошибкой: {progress.error}", file=sys.stderr)
        return 1
    if job.report is None:
        print("Скан не завершился.", file=sys.stderr)
        return 1

    report = job.report
    Path(args.report).write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"Просмотрено файлов: {report.total_files_seen}")
    print(f"Прочитано в этом запуске: {progress.files_hashed}")
    print(f"Взято из кэша (не перечитывалось): {progress.files_from_cache}")
    print(f"Групп дублей: {len(report.groups)}")
    print(f"Потенциально освободится: {_fmt_bytes(report.total_wasted_bytes)}")
    print(f"Время: {_fmt_duration(time.time() - progress.started_at)}")
    # `skipped_archives` now holds two different kinds of "not checked":
    # archives the mode never opened, and archives that refused to be read.
    # Printing one count for both would have made the second kind sound
    # like a setting the user chose.
    by_mode = [a for a in report.skipped_archives if a.reason == "excluded_by_mode"]
    unread = [a for a in report.skipped_archives if a.reason != "excluded_by_mode"]
    if by_mode:
        print(
            f"Не проверено в этом режиме: {len(by_mode)} архив(ов), "
            f"{_fmt_bytes(sum(a.size for a in by_mode))} (см. отчёт, "
            "skipped_archives) — внутрь не заглядывали, дубли внутри не найдены бы"
        )
    if unread:
        print(
            f"Не прочитано: {len(unread)} архив(ов), "
            f"{_fmt_bytes(sum(a.size for a in unread))} — содержимое неизвестно, "
            "к таким архивам инструмент не притрагивается"
        )
    # In quick mode every archive is UNREAD by construction, so printing
    # the Р1 breakdown would just repeat the "не проверено в этом режиме"
    # line above in four different words.
    if report.mode is ScanMode.FULL:
        _print_archive_verdicts(report)
        _print_quality_metrics(report, args.db)
    if report.warnings:
        print(f"Предупреждений: {len(report.warnings)} (см. отчёт)")
    print(f"Отчёт сохранён в {args.report}")
    if report.mode is ScanMode.QUICK:
        print(
            "Досчитать полностью: та же команда с --mode full и тем же --db. "
            "Уже посчитанные хэши берутся из индекса — заново читаются только "
            "архивы и недостающие превью."
        )
    return 0


def _print_quality_metrics(report: ScanReport, db_path: str) -> None:
    """One line on the task-9 metrics: how many of this run's photo groups
    have them.

    Worth printing even though nothing consumes the numbers yet (Р2 forbids
    them from doing anything on their own until task 17 ranks near-duplicate
    copies). The line is what tells you whether re-running a full scan over
    an index built before task 9 actually backfilled it, which is the one
    thing about this that can silently not happen.
    """
    photo_groups = [
        g for g in report.groups
        if any(r.media_kind is MediaKind.PHOTO and not r.is_archive_member
               for r in g.records)
    ]
    if not photo_groups:
        return
    with ScanIndex(db_path) as index:
        measured = len(index.quality_for_hashes(g.content_hash for g in photo_groups))
    print(
        f"Метрики качества: {measured} из {len(photo_groups)} фото-групп "
        "(разрешение, резкость, признаки пережатия — в индексе, на выбор "
        "копий пока не влияют)"
    )


_VERDICT_LABEL = {
    ArchiveClass.FULLY_REDUNDANT: "полностью избыточны",
    ArchiveClass.PARTIALLY_REDUNDANT: "частично избыточны",
    ArchiveClass.UNIQUE: "уникальны",
    ArchiveClass.UNREAD: "не прочитаны",
}


def _print_archive_verdicts(report: ScanReport) -> None:
    """Р1 gives every archive one verdict; this prints them grouped by
    verdict, with the fully redundant ones listed individually because
    those are the only ones any command will offer to act on.
    """
    verdicts = classify_archives(report)
    if not verdicts:
        return

    by_class: dict[ArchiveClass, list] = {}
    for verdict in verdicts:
        by_class.setdefault(verdict.verdict, []).append(verdict)

    parts = [
        f"{_VERDICT_LABEL[kind]} — {len(by_class[kind])}"
        for kind in (
            ArchiveClass.FULLY_REDUNDANT,
            ArchiveClass.PARTIALLY_REDUNDANT,
            ArchiveClass.UNIQUE,
            ArchiveClass.UNREAD,
        )
        if kind in by_class
    ]
    print(f"Архивов просмотрено: {len(verdicts)} ({', '.join(parts)})")

    full = by_class.get(ArchiveClass.FULLY_REDUNDANT, [])
    if full:
        freed = sum(v.size for v in full)
        print(
            f"  Полностью избыточны ({_fmt_bytes(freed)}) — всё их содержимое "
            "уже лежит на диске отдельными файлами:"
        )
        for verdict in full[:10]:
            print(f"    - {verdict.path} ({_fmt_bytes(verdict.size)}, "
                  f"участников: {verdict.members_total})")
        if len(full) > 10:
            print(f"    ... и ещё {len(full) - 10} (см. отчёт)")
        print("  Переместить их целиком: dupecleaner quarantine --archives "
              "(двойники будут перечитаны и сверены заново перед перемещением)")

    # Partially redundant archives get their numbers printed too. The
    # verdict alone ("1 archive, partially redundant") is the least useful
    # true statement available: on the real Takeout it hides that 59% of
    # the archive is already on disk. Nothing will be done about it either
    # way — Р1 defers dissolving — but how much is duplicated is the fact
    # that decides whether dissolving is worth building.
    partial = by_class.get(ArchiveClass.PARTIALLY_REDUNDANT, [])
    for verdict in partial[:5]:
        share = (
            f", {100 * verdict.members_redundant / verdict.members_total:.0f}%"
            if verdict.members_total
            else ""
        )
        print(
            f"  Частично избыточен: {verdict.path} — уже есть на диске "
            f"{verdict.members_redundant} из {verdict.members_total} "
            f"участников{share}, {_fmt_bytes(verdict.redundant_bytes)}. "
            "Архив не трогаем: вынуть часть нельзя, не пересобрав его."
        )
    if len(partial) > 5:
        print(f"    ... и ещё {len(partial) - 5} (см. отчёт)")

    unread = by_class.get(ArchiveClass.UNREAD, [])
    for verdict in unread[:5]:
        print(f"  Не прочитан: {verdict.path} — {verdict.reason}")


def _cmd_quarantine(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.report).read_text(encoding="utf-8"))
    report = ScanReport.from_dict(data)

    result = run_quarantine(
        report.groups,
        Path(args.quarantine_dir),
        confirm_media=args.confirm_media,
    )

    print(f"Перемещено файлов: {len(result.moved)}")
    if result.failed:
        print(f"Не удалось переместить: {len(result.failed)} (см. журнал)")
        for item in result.failed:
            print(f"  - {item['original']}: {item['error']}")
    if result.pending_media_review:
        print(
            f"Пропущено медиа-групп (нужна ручная проверка, затем запуск с "
            f"--confirm-media): {len(result.pending_media_review)}"
        )
    if result.archive_only_notes:
        print(f"Групп внутри архивов (не тронуты): {len(result.archive_only_notes)}")

    if args.archives:
        if report.mode is not ScanMode.FULL:
            # `quarantine_archives` refuses this too, one archive at a
            # time. Catching it here says it once, before anything runs,
            # and names the command that fixes it.
            print(
                "Архивы не перемещены: отчёт получен в быстром режиме "
                f"({MODE_LABEL[report.mode]}), внутрь архивов никто не "
                "заглядывал. Перезапустите скан с --mode full — и с тем же "
                "--db, тогда обычные файлы не будут перечитываться.",
                file=sys.stderr,
            )
        else:
            _quarantine_archives_step(
                report, Path(args.quarantine_dir), args.confirm_media
            )

    print(f"Журнал (источник истины для restore): {Path(args.quarantine_dir) / 'journal.jsonl'}")
    print(f"Манифест (сводка): {Path(args.quarantine_dir) / 'manifest.json'}")
    return 0


def _quarantine_archives_step(
    report: ScanReport, quarantine_dir: Path, confirm_media: bool
) -> None:
    """The Р1 pass, run after the file pass on purpose: by now every copy a
    file-level quarantine intended to move has moved, so re-verifying the
    twins checks the disk as it will actually look once this command is
    done — not as it looked when the scan ran.
    """
    archive_result = quarantine_archives(
        classify_archives(report),
        report,
        quarantine_dir,
        confirm_media=confirm_media,
    )
    print(
        f"Архивов перемещено целиком: {len(archive_result.moved)} "
        f"({_fmt_bytes(archive_result.freed_bytes)})"
    )
    for item in archive_result.moved:
        print(f"  - {item['archive']}: сверено двойников {item['twins_verified']}")
    for item in archive_result.refused:
        print(f"  ! не перемещён {item['archive']}: {item['reason']}")
    for item in archive_result.failed:
        print(f"  ! ошибка перемещения {item['archive']}: {item['error']}")
    if archive_result.pending_media_review:
        print(
            f"  Архивов с медиа, ожидают ручной проверки "
            f"(затем --confirm-media): {len(archive_result.pending_media_review)}"
        )


def _cmd_verify(args: argparse.Namespace) -> int:
    """Re-read one or more groups against the disk as it is right now.

    The command-line half of Р7's «сверить полностью». See
    `dedupe.GroupVerification` for what this does and does not prove: it
    is a freshness check on an ageing report, not a promotion from a
    weaker kind of match — there is no weaker kind of match in either mode.
    """
    data = json.loads(Path(args.report).read_text(encoding="utf-8"))
    report = ScanReport.from_dict(data)
    by_hash = {g.content_hash: g for g in report.groups}

    exit_code = 0
    for wanted in args.group:
        group = by_hash.get(wanted)
        if group is None:
            print(f"Группа {wanted}: в отчёте не найдена.", file=sys.stderr)
            exit_code = 1
            continue

        result = verify_group(group)
        print(f"Группа {wanted} ({_fmt_bytes(group.size)}):")
        for check in result.checked:
            mark = "  ✓" if check.ok else "  ✗"
            suffix = "" if check.ok else f" — {check.reason}"
            print(f"{mark} {check.display_path}{suffix}")
        if result.ok:
            print(
                f"  Подтверждено копий: {len(result.confirmed_paths)} из "
                f"{len(result.checked)} — байты совпадают прямо сейчас."
            )
        else:
            exit_code = 1
            confirmed = len(result.confirmed_paths)
            print(
                f"  Не подтверждено: живых одинаковых копий {confirmed}. "
                "Дубликатом это больше не является — перезапустите скан.",
                file=sys.stderr,
            )
    return exit_code


def _cmd_restore(args: argparse.Namespace) -> int:
    result = restore_from_journal(Path(args.quarantine_dir))

    print(f"Вернул: {len(result.restored)}")
    print(f"Пропустил: {len(result.skipped)}")
    for item in result.skipped:
        print(f"  - {item['original']}: {item['reason']}")
    return 0


def _cmd_index(args: argparse.Namespace) -> int:
    with ScanIndex(args.db) as index:
        if args.prune:
            removed = index.prune_missing()
            print(f"Удалено записей об исчезнувших файлах: {removed}")
        stats = index.stats()

    print(f"База индекса: {stats['db_path']}")
    print(f"Файлов в индексе: {stats['files_indexed']} ({_fmt_bytes(stats['bytes_indexed'])})")
    print(f"С действительным хэшем: {stats['files_with_valid_hash']}")
    print("Эти файлы при следующем скане перечитываться не будут.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web import app as web_app

    web_app.DB_PATH = args.db
    print(f"Индекс: {args.db}")
    print(f"Откройте http://{args.host}:{args.port}")
    uvicorn.run(web_app.app, host=args.host, port=args.port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dupecleaner")
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"Файл индекса (по умолчанию {DEFAULT_DB_PATH}). "
        "Хранит вычисленные хэши, чтобы повторный и прерванный скан не "
        "перечитывали всё заново.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_p = subparsers.add_parser("scan", help="Найти дубликаты в указанных папках")
    scan_p.add_argument("paths", nargs="+", help="Папки или диски для сканирования")
    scan_p.add_argument("--report", default="report.json", help="Куда сохранить отчёт (JSON)")
    scan_p.add_argument(
        "--mode",
        choices=[ScanMode.QUICK.value, ScanMode.FULL.value],
        default=ScanMode.QUICK.value,
        help="quick (по умолчанию) — точные дубликаты среди обычных файлов, без "
        "архивов, без превью и без метрик качества; full — то же плюс "
        "содержимое архивов (Р1), превью и метрики. Разница только в "
        "охвате: в карантин в обоих режимах уходит "
        "только подтверждённое байт-в-байт.",
    )
    scan_p.add_argument(
        "--no-archives",
        action="store_true",
        help="Не заглядывать внутрь архивов даже в режиме full "
        "(в quick они и так не открываются).",
    )
    scan_p.set_defaults(func=_cmd_scan)

    quarantine_p = subparsers.add_parser(
        "quarantine", help="Переместить дубликаты из отчёта в карантин"
    )
    quarantine_p.add_argument("--report", default="report.json", help="Отчёт команды scan")
    quarantine_p.add_argument("--quarantine-dir", required=True, help="Папка карантина")
    quarantine_p.add_argument(
        "--confirm-media",
        action="store_true",
        help="Также переместить медиа-дубликаты — только после ручного просмотра",
    )
    quarantine_p.add_argument(
        "--archives",
        action="store_true",
        help="Также переместить целиком архивы, всё содержимое которых уже "
        "лежит на диске отдельными файлами (Р1). Перед перемещением каждый "
        "двойник перечитывается и сверяется заново; любое несовпадение "
        "отменяет перемещение всего архива.",
    )
    quarantine_p.set_defaults(func=_cmd_quarantine)

    verify_p = subparsers.add_parser(
        "verify",
        help="Сверить конкретную группу заново: перечитать все копии и "
        "сравнить байты прямо сейчас",
    )
    verify_p.add_argument("--report", default="report.json", help="Отчёт команды scan")
    verify_p.add_argument(
        "--group",
        action="append",
        required=True,
        metavar="HASH",
        help="content_hash группы из отчёта; можно указать несколько раз",
    )
    verify_p.set_defaults(func=_cmd_verify)

    restore_p = subparsers.add_parser(
        "restore", help="Вернуть файлы из карантина обратно, по журналу"
    )
    restore_p.add_argument("--quarantine-dir", required=True, help="Папка карантина")
    restore_p.set_defaults(func=_cmd_restore)

    index_p = subparsers.add_parser("index", help="Показать состояние индекса хэшей")
    index_p.add_argument(
        "--prune", action="store_true", help="Убрать записи о файлах, которых больше нет"
    )
    index_p.set_defaults(func=_cmd_index)

    serve_p = subparsers.add_parser("serve", help="Запустить веб-интерфейс")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8765)
    serve_p.set_defaults(func=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
