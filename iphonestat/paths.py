"""Папки приложения на этом компьютере: отчёты и журнал лежат в профиле пользователя, а не рядом с exe."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "iPhoneStat"


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home()) / APP_NAME


def reports_dir() -> Path:
    return app_data_dir() / "reports"


def log_path() -> Path:
    return app_data_dir() / "iphonestat.log"
