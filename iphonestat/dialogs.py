"""Системные окна Windows: выбор папки и открытие папки в Проводнике."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import tkinter
from pathlib import Path
from tkinter import filedialog

logger = logging.getLogger(__name__)

PICK_FOLDER_FLAG = "--pick-folder"
# Без этого флага при запуске без консоли на секунду появлялось бы чёрное окно.
_NO_CONSOLE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_picker_lock = threading.Lock()


class DialogBusyError(Exception):
    """Окно выбора папки уже открыто."""


def show_folder_dialog(title: str) -> str:
    """Показывает окно выбора папки в текущем процессе. Пустая строка — если окно закрыли без выбора."""
    root = tkinter.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        return filedialog.askdirectory(parent=root, title=title, mustexist=True) or ""
    finally:
        root.destroy()


def picker_command(title: str) -> list[str]:
    """Команда, запускающая это же приложение в режиме «только окно выбора папки».

    Окно показывается отдельным процессом: tkinter нельзя безопасно вызывать из потока веб-сервера.
    Собранный exe запускает сам себя, обычная установка — модуль через интерпретатор.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, PICK_FOLDER_FLAG, title]
    return [sys.executable, "-m", "iphonestat", PICK_FOLDER_FLAG, title]


def pick_folder(title: str) -> Path | None:
    """Показывает окно выбора папки. None — если пользователь закрыл окно, не выбрав папку."""
    if not _picker_lock.acquire(blocking=False):
        raise DialogBusyError("Окно выбора папки уже открыто — посмотрите на панели задач.")
    try:
        result = subprocess.run(
            picker_command(title),
            capture_output=True,
            check=False,
            creationflags=_NO_CONSOLE_WINDOW,
        )
    finally:
        _picker_lock.release()
    if result.returncode != 0:
        logger.error("Окно выбора папки не открылось: %s", result.stderr.decode("utf-8", "replace").strip())
        raise OSError("Не удалось открыть окно выбора папки.")
    chosen = result.stdout.decode("utf-8").strip()
    if not chosen:
        return None
    folder = Path(chosen)
    return folder if folder.is_dir() else None


def reveal_folder(folder: Path) -> None:
    """Открывает папку в Проводнике."""
    if not folder.is_dir():
        raise OSError("Папка не найдена.")
    os.startfile(folder)  # путь создан самим приложением, не получен извне
