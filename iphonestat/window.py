"""Собственное окно приложения (WebView2 через pywebview): карта открывается в нём, а не в браузере."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

import webview

from iphonestat.dialogs import DialogBusyError

logger = logging.getLogger(__name__)

WINDOW_SIZE = (1280, 820)
WINDOW_MIN_SIZE = (760, 520)
QUIT_QUESTION = "Копирование или резервная копия ещё выполняется.\nЗакрыть iPhoneStat и прервать операцию?"


class AppWindow:
    """Окно приложения. Системное окно выбора папки открывается поверх него."""

    def __init__(self, title: str, is_busy: Callable[[], bool]) -> None:
        self._title = title
        self._is_busy = is_busy
        self._window: webview.Window | None = None
        self._dialog_lock = threading.Lock()

    def run(self, url: str) -> None:
        """Показывает окно и возвращает управление, когда пользователь его закрыл. Вызывать из главного потока."""
        window = webview.create_window(self._title, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=WINDOW_MIN_SIZE)
        window.events.closing += self._on_closing
        self._window = window
        try:
            webview.start(localization={"global.quitConfirmation": QUIT_QUESTION})
        finally:
            self._window = None

    def pick_folder(self, title: str) -> Path | None:
        """Окно выбора папки. None — если пользователь закрыл его, ничего не выбрав."""
        window = self._window
        if window is None:
            raise OSError("Окно приложения закрыто.")
        if not self._dialog_lock.acquire(blocking=False):
            raise DialogBusyError("Окно выбора папки уже открыто.")
        try:
            logger.debug("Выбор папки: %s", title)
            chosen = window.create_file_dialog(webview.FileDialog.FOLDER)
        finally:
            self._dialog_lock.release()
        if not chosen:
            return None
        folder = Path(chosen[0])
        return folder if folder.is_dir() else None

    def _on_closing(self) -> None:
        # Переспрашиваем только тогда, когда закрытие окна прервало бы идущую операцию.
        if self._window is not None:
            self._window.confirm_close = self._is_busy()
