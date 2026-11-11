"""Запуск: iPhoneStat.exe или python -m iphonestat"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import webbrowser
from collections.abc import Sequence
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from iphonestat import dialogs
from iphonestat.app import MODE_BROWSER, MODE_SERVER_ONLY, MODE_WINDOW, run_app
from iphonestat.demo import build_demo_scan
from iphonestat.device import DeviceError, ScanResult, scan_device
from iphonestat.dialogs import PICK_FOLDER_FLAG, show_folder_dialog
from iphonestat.paths import APP_NAME, log_path, reports_dir
from iphonestat.report import write_report

logger = logging.getLogger("iphonestat")

EXIT_OK = 0
EXIT_FAILED = 1
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="iphonestat", description="Карта занятого места на iPhone (как WinDirStat).")
    parser.add_argument("--demo", action="store_true", help="показать пример на вымышленных данных, без iPhone")
    parser.add_argument("--udid", help="серийный номер (UDID) устройства, если подключено несколько")
    parser.add_argument("--no-app-files", action="store_true", help="не обходить папки Documents приложений (быстрее)")
    parser.add_argument("--browser", action="store_true", help="открыть страницу в обычном браузере, а не в своём окне")
    parser.add_argument("--server-only", action="store_true", help="не открывать окно: только адрес страницы в журнале")
    parser.add_argument("--report-only", action="store_true", help="только сохранить HTML-отчёт в файл и выйти")
    parser.add_argument("--output", type=Path, help="куда сохранить отчёт (вместе с --report-only)")
    parser.add_argument("--verbose", action="store_true", help="подробный журнал")
    # Служебный режим: приложение запускает само себя, чтобы показать окно выбора папки.
    parser.add_argument(PICK_FOLDER_FLAG, dest="pick_folder", metavar="TITLE", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def run_folder_picker(title: str) -> int:
    """Показывает окно выбора папки и печатает выбранный путь — его читает основной процесс приложения."""
    sys.stdout.buffer.write(show_folder_dialog(title).encode("utf-8"))
    sys.stdout.buffer.flush()
    return EXIT_OK


def configure_logging(verbose: bool) -> None:
    """Журнал пишется в файл в профиле пользователя; в консоль — только если она есть (у exe её нет)."""
    handlers: list[logging.Handler] = []
    try:
        log_path().parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_path(), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"))
    except OSError:
        pass  # без файла журнала приложение всё равно работоспособно
    if sys.stdout is not None:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers or [logging.NullHandler()],
    )
    if not verbose:
        logging.getLogger("pymobiledevice3").setLevel(logging.WARNING)


async def scan_from_args(args: argparse.Namespace) -> ScanResult:
    return await scan_device(udid=args.udid, include_app_files=not args.no_app_files)


async def demo_scan() -> ScanResult:
    return build_demo_scan()


def save_report_only(args: argparse.Namespace) -> int:
    now = datetime.now()
    try:
        scan = asyncio.run(demo_scan() if args.demo else scan_from_args(args))
    except DeviceError as error:
        logger.error("%s", error)
        return EXIT_FAILED
    prefix = "demo" if args.demo else "iphone"
    output = write_report(scan, args.output or reports_dir() / f"{prefix}-{now:%Y-%m-%d_%H-%M-%S}.html", now)
    logger.info("Отчёт сохранён: %s", output.resolve())
    if sys.stdout is None:  # exe без консоли: иначе пользователь не узнал бы, куда сохранился отчёт
        webbrowser.open(output.resolve().as_uri())
    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    if args.report_only:
        return save_report_only(args)
    mode = MODE_SERVER_ONLY if args.server_only else MODE_BROWSER if args.browser else MODE_WINDOW
    if args.demo:
        run_app(demo_scan, live=False, mode=mode)
    else:
        run_app(lambda: scan_from_args(args), live=True, mode=mode)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.pick_folder is not None:
        return run_folder_picker(args.pick_folder)
    configure_logging(args.verbose)
    try:
        return run(args)
    except Exception as error:
        logger.exception("Приложение завершилось из-за ошибки")
        dialogs.show_error(APP_NAME, f"iPhoneStat не смог запуститься.\n\n{type(error).__name__}: {error}\n\nЖурнал: {log_path()}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
