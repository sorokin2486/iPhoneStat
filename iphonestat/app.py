"""Приложение целиком: фоновое сканирование, локальный сервер и окно с картой."""

from __future__ import annotations

import logging
import threading
import webbrowser
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from iphonestat import dialogs
from iphonestat.device import ScanResult
from iphonestat.jobs import JobRunner
from iphonestat.paths import APP_NAME, reports_dir
from iphonestat.report import render_report, render_waiting, write_report
from iphonestat.server import AppServer, DeviceActions
from iphonestat.session import PHASE_READY, Scanner, ScanSession

logger = logging.getLogger(__name__)

MODE_WINDOW = "window"
MODE_BROWSER = "browser"
MODE_SERVER_ONLY = "server-only"
JOB_SHUTDOWN_TIMEOUT_SECONDS = 10
COMPANION_TEXT = (
    "iPhoneStat открыт в браузере.\n\n"
    "Не закрывайте это окно, пока работаете с приложением: вместе с ним завершится и программа."
)


class Window(Protocol):
    """Окно приложения (см. iphonestat.window.AppWindow)."""

    def run(self, url: str) -> None: ...

    def pick_folder(self, title: str) -> Path | None: ...


WindowFactory = Callable[[str, Callable[[], bool]], Window | None]


class Pages:
    """Собирает страницу под текущее состояние: экран ожидания или карту (карта кэшируется до следующего скана)."""

    def __init__(self, session: ScanSession, live: bool) -> None:
        self._session = session
        self._live = live
        self._lock = threading.Lock()
        self._cached: tuple[int, str] | None = None

    def render(self, token: str) -> str:
        state = self._session.snapshot()
        if state.phase != PHASE_READY or state.scan is None or state.scanned_at is None:
            return render_waiting(token)
        with self._lock:
            if self._cached is None or self._cached[0] != state.revision:
                page = render_report(state.scan, state.scanned_at, live_token=token if self._live else None)
                self._cached = (state.revision, page)
            return self._cached[1]


def save_report(scan: ScanResult, scanned_at: datetime) -> None:
    """Каждый скан сохраняется самостоятельной HTML-страницей — её можно открыть позже без iPhone."""
    path = write_report(scan, reports_dir() / f"iphone-{scanned_at:%Y-%m-%d_%H-%M-%S}.html", scanned_at)
    logger.info("Отчёт сохранён: %s", path)


def create_window(title: str, is_busy: Callable[[], bool]) -> Window | None:
    """Окно приложения; None — если встроенный просмотрщик недоступен (тогда страница откроется в браузере)."""
    try:
        from iphonestat.window import AppWindow
    except Exception:
        logger.exception("Встроенное окно недоступно, открываю страницу в браузере")
        return None
    return AppWindow(title, is_busy)


def run_app(
    scanner: Scanner,
    live: bool = True,
    mode: str = MODE_WINDOW,
    window_factory: WindowFactory = create_window,
) -> None:
    """Запускает приложение и возвращает управление, когда пользователь его закрыл."""
    runner = JobRunner()
    session = ScanSession(scanner, on_ready=save_report if live else None)
    window = window_factory(APP_NAME, runner.is_running) if mode == MODE_WINDOW else None
    pick_folder = window.pick_folder if window is not None else dialogs.pick_folder
    actions = DeviceActions(session, runner, pick_folder=pick_folder)
    with AppServer(Pages(session, live).render, actions) as server:
        threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
        session.start()
        logger.info("Приложение запущено: %s", server.url)
        try:
            if window is not None:
                window.run(server.url)
            elif mode == MODE_SERVER_ONLY:
                threading.Event().wait()
            else:
                webbrowser.open(server.url)
                dialogs.run_companion_window(APP_NAME, COMPANION_TEXT, lambda: webbrowser.open(server.url))
        except KeyboardInterrupt:
            logger.info("Завершение…")
        finally:
            if runner.cancel():
                runner.wait(JOB_SHUTDOWN_TIMEOUT_SECONDS)
            server.shutdown()
