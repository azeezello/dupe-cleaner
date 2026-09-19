"""Command-line entry point: `dupecleaner scan|quarantine|serve|index`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .jobs import ScanJob
from .models import ScanReport
from .quarantine import run_quarantine
from .storage import DEFAULT_DB_PATH, ScanIndex


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


def _cmd_scan(args: argparse.Namespace) -> int:
    job = ScanJob(
        roots=args.paths,
        db_path=args.db,
        include_archives=not args.no_archives,
    )
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
    if report.skipped_archives:
        skipped_bytes = sum(a.size for a in report.skipped_archives)
        print(
            f"Не проверено в этом режиме: {len(report.skipped_archives)} "
            f"архив(ов), {_fmt_bytes(skipped_bytes)} (см. отчёт, "
            "skipped_archives) — внутрь не заглядывали, дубли внутри не найдены бы"
        )
    if report.warnings:
        print(f"Предупреждений: {len(report.warnings)} (см. отчёт)")
    print(f"Отчёт сохранён в {args.report}")
    return 0


def _cmd_quarantine(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.report).read_text(encoding="utf-8"))
    report = ScanReport.from_dict(data)

    result = run_quarantine(
        report.groups,
        Path(args.quarantine_dir),
        confirm_media=args.confirm_media,
    )

    print(f"Перемещено файлов: {len(result.moved)}")
    if result.pending_media_review:
        print(
            f"Пропущено медиа-групп (нужна ручная проверка, затем запуск с "
            f"--confirm-media): {len(result.pending_media_review)}"
        )
    if result.archive_only_notes:
        print(f"Групп внутри архивов (не тронуты): {len(result.archive_only_notes)}")
    print(f"Манифест: {Path(args.quarantine_dir) / 'manifest.json'}")
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
    scan_p.add_argument("--no-archives", action="store_true", help="Не заглядывать внутрь архивов")
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
    quarantine_p.set_defaults(func=_cmd_quarantine)

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
