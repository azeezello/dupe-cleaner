"""Command-line entry point: `dupecleaner scan|events|quarantine|restore|serve|index`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .archive_classify import classify_archives
from .dedupe import verify_group
from .events import (
    DEFAULT_THRESHOLDS,
    EventThresholds,
    MomentPolicy,
    cluster_events,
    gap_sensitivity,
    merge_into_trips,
    moments_from_rows,
)
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
            "заглядываем, превью, метрики качества и происхождение снимков "
            "не считаем. В карантин, "
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
        "для просмотра, метрики качества (Р2: считаются и показываются, "
        "на выбор копий не влияют) и происхождение снимков (Р3: метка и "
        "будущий фильтр альбомов, не повод что-либо двигать)."
    )


def _cmd_scan(args: argparse.Namespace) -> int:
    mode = ScanMode(args.mode)
    job = ScanJob(
        moment_policy=MomentPolicy(
            utc_offset_seconds=(
                args.utc_offset_hours * 3600
                if args.utc_offset_hours is not None
                else None
            )
        ),
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
        _print_origin_breakdown(job.scan_id, args.db)
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


def _print_origin_breakdown(scan_id: str, db_path: str) -> None:
    """The task-15 summary: where this run's photos came from (Р3).

    Confidence is printed beside every count rather than folded away,
    because the two numbers mean different things to whoever reads them.
    "Скриншотов 2100" invites a bulk decision; "1300 уверенно, 800 по
    разрешению" says which part of that is worth a second look. UX-BRIEF
    asks for the evidence rather than the verdict, and this is the
    cheapest possible form of that.
    """
    from .origin import OriginClass

    try:
        with ScanIndex(db_path) as index:
            breakdown = index.origin_breakdown(scan_id)
    except Exception:  # noqa: BLE001 - a summary line must never fail a scan
        return
    if not breakdown:
        return

    labels = {
        OriginClass.CAMERA: "камера",
        OriginClass.SCREENSHOT_PHONE: "скриншот с телефона",
        OriginClass.SCREENSHOT_DESKTOP: "скриншот с компьютера",
        OriginClass.MESSENGER: "мессенджер",
        OriginClass.DOCUMENT_SCAN: "скан документа",
        OriginClass.WEB_DOWNLOAD: "загрузка из сети",
        OriginClass.UNKNOWN: "не определено",
    }
    total = sum(breakdown.values())
    print(f"Происхождение снимков ({total}):")
    for origin_class, label in labels.items():
        per_confidence = {
            conf: n for (cls, conf), n in breakdown.items() if cls == origin_class.value
        }
        count = sum(per_confidence.values())
        if not count:
            continue
        detail = ", ".join(
            f"{n} {name}"
            for name, n in (
                ("уверенно", per_confidence.get("high", 0)),
                ("вероятно", per_confidence.get("medium", 0)),
                ("слабо", per_confidence.get("low", 0)),
            )
            if n
        )
        share = 100.0 * count / total if total else 0.0
        print(f"  {label}: {count} ({share:.1f}%) — {detail}")

    screenshots = sum(
        n for (cls, _), n in breakdown.items()
        if cls in (OriginClass.SCREENSHOT_PHONE.value, OriginClass.SCREENSHOT_DESKTOP.value)
    )
    if screenshots:
        print(
            f"  Из будущих альбомов исключаются {screenshots} снимк(ов) экрана (Р3). "
            "Это метка, не действие: ни один файл от неё не двигается."
        )


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


def _cmd_events(args: argparse.Namespace) -> int:
    """Cluster a finished scan into events (Р4, task 16). Read-only.

    Reads the index and prints; it cannot move, rename or delete anything,
    and it does not want to — an event is axis C in Р0. Building the
    library out of these clusters is task 21, and that one shows its whole
    plan before touching a file.
    """
    thresholds = EventThresholds(
        session_gap_seconds=args.session_gap_hours * 3600,
        geo_gap_seconds=args.geo_gap_minutes * 60,
        place_radius_m=args.place_radius_km * 1000,
        keep_same_day_same_place=not args.no_same_day_merge,
        same_place_radius_m=args.same_place_radius_km * 1000,
        max_speed_kmh=args.max_speed_kmh,
    )
    policy = MomentPolicy(
        utc_offset_seconds=(
            args.utc_offset_hours * 3600 if args.utc_offset_hours is not None else None
        ),
        use_mtime=args.use_mtime,
    )

    with ScanIndex(args.db) as index:
        scan_id = args.scan_id or index.latest_scan_id()
        if scan_id is None:
            print(
                "В индексе нет ни одного скана. Сначала: dupecleaner scan --mode full <папки>",
                file=sys.stderr,
            )
            return 1

        coverage = index.moment_coverage(scan_id)
        rows = index.moments(scan_id)
        excluded = [] if args.include_screenshots else index.excluded_from_albums_paths(scan_id)
        moments = moments_from_rows(rows, policy=policy)

    if not rows:
        print("В этом скане нет ни фото, ни видео.", file=sys.stderr)
        return 1
    if not any(m.has_time for m in moments):
        print(
            "Ни у одного файла нет времени съёмки. Фаза метаданных выполняется "
            "только в полном режиме: dupecleaner scan --mode full с тем же --db.",
            file=sys.stderr,
        )
        return 1

    print(f"Скан: {scan_id}")
    print("Пороги:")
    for line in thresholds.describe():
        print(f"  · {line}")
    if not policy.use_mtime:
        print(
            "  · Время файла (mtime) не используется: на реальной папке он "
            "расходится с EXIF на годы. --use-mtime включает."
        )
    print("Источники времени:", ", ".join(f"{k}={v}" for k, v in sorted(coverage.items())))

    clustering = cluster_events(moments, thresholds=thresholds, excluded_paths=excluded)
    summary = clustering.summary()
    print(
        f"\nСобытий: {summary['events']} на {summary['photos_in_events']} снимков "
        f"(медиана {summary['median_event_size']}, крупнейшее {summary['largest_event']}, "
        f"одиночек {summary['singletons']})"
    )
    print(
        "Уверенность: "
        + ", ".join(f"{k}={v}" for k, v in sorted(summary["by_confidence"].items()))
    )
    print(f"Без даты (в _unsorted по Р5): {summary['undated']}")
    print(f"Исключено из альбомов (Р3, скриншоты и сканы): {summary['excluded']}")
    if clustering.warnings:
        print(f"Странных координат: {len(clustering.warnings)} (первые три)")
        for warning in clustering.warnings[:3]:
            print(f"  ! {warning}")

    if args.sensitivity:
        print("\nЧувствительность к порогу времени:")
        hours = (2, 4, 6, 8, 9, 10, 12, 16, 24)
        table = gap_sensitivity(
            [m for m in moments if m.display_path not in set(excluded)],
            [h * 3600 for h in hours],
            base=thresholds,
        )
        for hour in hours:
            mark = " ←" if abs(hour * 3600 - thresholds.session_gap_seconds) < 1 else ""
            print(f"  {hour:>2} ч: {table[hour * 3600]:6d} событий{mark}")

    limit = args.limit
    print(f"\nПервые {min(limit, len(clustering.events))} событий:")
    for event in clustering.events[:limit]:
        start, end = event.date_range
        span = f"{start}" if start == end else f"{start} → {end}"
        centre = event.centroid
        where = f", {centre[0]:.3f},{centre[1]:.3f}" if centre else ""
        print(
            f"  [{event.size:5d}] {span}  {_fmt_duration(event.duration_seconds)}"
            f", {event.confidence.value}, гео {event.geo_known}{where}"
        )
        print(f"          начало: {event.boundary.reason if event.boundary else 'первый снимок'}")
        folders = ", ".join(
            f"{name or '<корень>'}×{n}" for name, n in event.folders.most_common(3)
        )
        print(f"          папки: {folders}")

    if args.trips:
        trips = merge_into_trips(clustering, thresholds=thresholds)
        merged = [t for t in trips if len(t.events) > 1]
        print(
            f"\nВторой уровень (поездки): {len(trips)} вместо {len(clustering.events)}; "
            f"склеено из нескольких событий: {len(merged)}"
        )
        print(
            "  Какой уровень становится папкой — открытый продуктовый вопрос, "
            "см. claude/task-16-events-report.md"
        )
        for trip in sorted(merged, key=lambda t: -t.size)[:10]:
            first, last = trip.date_range
            print(f"  [{trip.size:5d}] {first} → {last}, событий {len(trip.events)}")

    if args.json:
        payload = {
            "scan_id": scan_id,
            "thresholds": {
                "session_gap_seconds": thresholds.session_gap_seconds,
                "geo_gap_seconds": thresholds.geo_gap_seconds,
                "place_radius_m": thresholds.place_radius_m,
                "same_place_radius_m": thresholds.same_place_radius_m,
                "keep_same_day_same_place": thresholds.keep_same_day_same_place,
                "max_speed_kmh": thresholds.max_speed_kmh,
                "use_mtime": policy.use_mtime,
            },
            "summary": summary,
            "coverage": coverage,
            "events": [event.to_dict() for event in clustering.events],
            "undated": [m.display_path for m in clustering.undated],
            "excluded": clustering.excluded,
            "warnings": clustering.warnings,
        }
        Path(args.json).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nСобытия сохранены в {args.json}")
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
        "--utc-offset-hours",
        type=float,
        default=None,
        help="Часовой пояс библиотеки, если он не совпадает с зоной этой машины. "
        "EXIF пишет местное время без зоны, а Google-сайдкар — настоящий UTC; "
        "смешивать их нельзя (см. events.py).",
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

    events_p = subparsers.add_parser(
        "events",
        help="Разбить снимки на события: разрыв во времени плюс смена места (Р4)",
    )
    events_p.add_argument(
        "--scan-id",
        default=None,
        help="Какой скан кластеризовать. По умолчанию последний в индексе.",
    )
    events_p.add_argument(
        "--session-gap-hours",
        type=float,
        default=DEFAULT_THRESHOLDS.session_gap_seconds / 3600,
        help="Разрыв, после которого начинается новое событие (по умолчанию "
        f"{DEFAULT_THRESHOLDS.session_gap_seconds / 3600:.0f} ч — выбрано по "
        "гистограмме разрывов и по сверке с вашими же папками, см. "
        "claude/task-16-events-report.md).",
    )
    events_p.add_argument(
        "--place-radius-km",
        type=float,
        default=DEFAULT_THRESHOLDS.place_radius_m / 1000,
        help="Насколько далеко нужно переместиться, чтобы это считалось сменой "
        f"места (по умолчанию {DEFAULT_THRESHOLDS.place_radius_m / 1000:.0f} км).",
    )
    events_p.add_argument(
        "--geo-gap-minutes",
        type=float,
        default=DEFAULT_THRESHOLDS.geo_gap_seconds / 60,
        help="Минимальный разрыв, при котором смена места вообще учитывается "
        f"(по умолчанию {DEFAULT_THRESHOLDS.geo_gap_seconds / 60:.0f} мин).",
    )
    events_p.add_argument(
        "--same-place-radius-km",
        type=float,
        default=DEFAULT_THRESHOLDS.same_place_radius_m / 1000,
        help="Радиус, внутри которого длинный разрыв в пределах одного дня не "
        "разрезает событие.",
    )
    events_p.add_argument(
        "--no-same-day-merge",
        action="store_true",
        help="Отключить правило «тот же день, то же место»: тогда длинный разрыв "
        "разрезает событие всегда.",
    )
    events_p.add_argument(
        "--max-speed-kmh",
        type=float,
        default=DEFAULT_THRESHOLDS.max_speed_kmh,
        help="Выше этой скорости пара снимков считается ошибкой координат, а не "
        "перемещением.",
    )
    events_p.add_argument(
        "--use-mtime",
        action="store_true",
        help="Использовать время файла там, где нет ни EXIF, ни даты в имени. "
        "На D:\\Photos это даёт фальшивое событие из 889 снимков «за 36 минут» "
        "в день копирования папки — поэтому по умолчанию выключено.",
    )
    events_p.add_argument(
        "--utc-offset-hours",
        type=float,
        default=None,
        help="Часовой пояс библиотеки. EXIF пишет местное время без зоны, а "
        "sidecar и mtime — настоящий UTC; смешивать их нельзя. По умолчанию "
        "берётся зона этой машины.",
    )
    events_p.add_argument(
        "--include-screenshots",
        action="store_true",
        help="Не исключать скриншоты и сканы (Р3 исключает их из альбомов).",
    )
    events_p.add_argument("--trips", action="store_true", help="Показать второй уровень: поездки")
    events_p.add_argument(
        "--sensitivity",
        action="store_true",
        help="Показать, как число событий зависит от порога времени",
    )
    events_p.add_argument("--limit", type=int, default=20, help="Сколько событий распечатать")
    events_p.add_argument("--json", default=None, help="Сохранить события в JSON")
    events_p.set_defaults(func=_cmd_events)

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
