"""Command-line entry point: `dupecleaner scan|quarantine|serve`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .dedupe import find_duplicate_groups
from .models import ScanReport
from .quarantine import run_quarantine
from .scanner import Scanner


def _cmd_scan(args: argparse.Namespace) -> int:
    scanner = Scanner(include_archives=not args.no_archives)
    records = list(scanner.iter_records(args.paths))
    groups = find_duplicate_groups(records, warnings=scanner.warnings)
    report = ScanReport(
        scanned_roots=args.paths,
        total_files_seen=scanner.total_files_seen,
        groups=groups,
        warnings=scanner.warnings,
    )

    Path(args.report).write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"Просмотрено файлов: {report.total_files_seen}")
    print(f"Групп дублей: {len(report.groups)}")
    print(f"Потенциально освободится: {report.total_wasted_bytes / (1024**2):.1f} МБ")
    if report.warnings:
        print(f"Предупреждений: {len(report.warnings)} (см. отчёт).")
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
            f"Пропущено медиа-групп (нужна ручная проверка, запустите с "
            f"--confirm-media после review): {len(result.pending_media_review)}"
        )
    if result.archive_only_notes:
        print(f"Групп только внутри архивов (не тронуты): {len(result.archive_only_notes)}")
    print(f"Манифест: {Path(args.quarantine_dir) / 'manifest.json'}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("dupecleaner.web.app:app", host=args.host, port=args.port, reload=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dupecleaner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_p = subparsers.add_parser("scan", help="Найти дубликаты в указанных папках")
    scan_p.add_argument("paths", nargs="+", help="Папки или диски для сканирования")
    scan_p.add_argument("--report", default="report.json", help="Куда сохранить отчёт (JSON)")
    scan_p.add_argument("--no-archives", action="store_true", help="Не заглядывать внутрь архивов")
    scan_p.set_defaults(func=_cmd_scan)

    quarantine_p = subparsers.add_parser(
        "quarantine", help="Переместить дубликаты из отчёта в карантин"
    )
    quarantine_p.add_argument("--report", default="report.json", help="Отчёт, созданный командой scan")
    quarantine_p.add_argument("--quarantine-dir", required=True, help="Папка для карантина")
    quarantine_p.add_argument(
        "--confirm-media",
        action="store_true",
        help="Также переместить медиа-дубликаты (фото/видео) — используйте "
        "только после того, как явно просмотрели их в веб-интерфейсе",
    )
    quarantine_p.set_defaults(func=_cmd_quarantine)

    serve_p = subparsers.add_parser("serve", help="Запустить веб-интерфейс для review")
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
