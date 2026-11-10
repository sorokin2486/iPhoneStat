"""Запуск: python -m iphonestat [--demo]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import webbrowser
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from iphonestat.demo import build_demo_scan
from iphonestat.device import DeviceError, ScanResult, scan_device
from iphonestat.dialogs import PICK_FOLDER_FLAG, show_folder_dialog
from iphonestat.jobs import JobRunner
from iphonestat.report import render_report, write_report
from iphonestat.server import AppServer, DeviceActions

logger = logging.getLogger("iphonestat")

REPORTS_DIR = Path("reports")
EXIT_OK = 0
EXIT_SCAN_FAILED = 1
JOB_SHUTDOWN_TIMEOUT_SECONDS = 10


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="iphonestat", description="Карта занятого места на iPhone (как WinDirStat).")
    parser.add_argument("--demo", action="store_true", help="показать отчёт на вымышленных данных, без iPhone")
    parser.add_argument("--udid", help="серийный номер (UDID) устройства, если подключено несколько")
    parser.add_argument("--no-app-files", action="store_true", help="не обходить папки Documents приложений (быстрее)")
    parser.add_argument("--output", type=Path, help="куда сохранить HTML-отчёт")
    parser.add_argument("--no-open", action="store_true", help="не открывать отчёт в браузере")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="только сохранить отчёт в файл, без кнопок копирования и резервной копии",
    )
    parser.add_argument("--verbose", action="store_true", help="подробный журнал")
    # Служебный режим: приложение запускает само себя, чтобы показать окно выбора папки.
    parser.add_argument(PICK_FOLDER_FLAG, dest="pick_folder", metavar="TITLE", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def run_folder_picker(title: str) -> int:
    """Показывает окно выбора папки и печатает выбранный путь — его читает основной процесс приложения."""
    sys.stdout.buffer.write(show_folder_dialog(title).encode("utf-8"))
    sys.stdout.buffer.flush()
    return EXIT_OK


def wait_before_closing(exit_code: int) -> None:
    """У exe, запущенного двойным щелчком, окно закрылось бы раньше, чем удастся прочитать ошибку."""
    if exit_code != EXIT_OK and getattr(sys, "frozen", False) and sys.stdin is not None and sys.stdin.isatty():
        input("Нажмите Enter, чтобы закрыть окно…")


def default_output_path(now: datetime, demo: bool) -> Path:
    prefix = "demo" if demo else "iphone"
    return REPORTS_DIR / f"{prefix}-{now:%Y-%m-%d_%H-%M-%S}.html"


def run(args: argparse.Namespace) -> int:
    now = datetime.now()
    try:
        scan: ScanResult = (
            build_demo_scan()
            if args.demo
            else asyncio.run(scan_device(udid=args.udid, include_app_files=not args.no_app_files))
        )
    except DeviceError as error:
        logger.error("%s", error)
        return EXIT_SCAN_FAILED

    output_path = write_report(scan, args.output or default_output_path(now, args.demo), now)
    logger.info("Отчёт сохранён: %s", output_path.resolve())
    if args.demo or args.report_only:
        if not args.no_open:
            webbrowser.open(output_path.resolve().as_uri())
        return EXIT_OK
    serve(scan, now, open_browser=not args.no_open)
    return EXIT_OK


def serve(scan: ScanResult, scanned_at: datetime, open_browser: bool) -> None:
    """Запускает приложение: страница с картой и рабочими кнопками копирования, пока окно не закроют."""
    runner = JobRunner()
    actions = DeviceActions(scan.udid, runner)
    with AppServer(lambda token: render_report(scan, scanned_at, live_token=token), actions) as server:
        logger.info("Приложение открыто: %s", server.url)
        logger.info("Не закрывайте это окно, пока работаете с приложением. Выход — Ctrl+C.")
        if open_browser:
            webbrowser.open(server.url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Завершение…")
        finally:
            if runner.cancel():
                runner.wait(JOB_SHUTDOWN_TIMEOUT_SECONDS)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.pick_folder is not None:
        return run_folder_picker(args.pick_folder)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s", stream=sys.stdout)
    if not args.verbose:
        logging.getLogger("pymobiledevice3").setLevel(logging.WARNING)
    exit_code = run(args)
    wait_before_closing(exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
