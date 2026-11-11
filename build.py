"""Сборка dist/iPhoneStat.exe — один файл, без консольного окна.

Запуск: python build.py (нужны зависимости из requirements-dev.txt).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "iPhoneStat"
ICON = ROOT / "assets" / "icon.ico"
PAGES = ("template.html", "waiting.html")


def pyinstaller_command() -> list[str]:
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", APP_NAME,
        "--icon", str(ICON),
        "--collect-data", "pymobiledevice3",
        "--log-level", "WARN",
    ]  # fmt: skip
    for page in PAGES:
        command += ["--add-data", f"{ROOT / 'iphonestat' / page};iphonestat"]
    return [*command, str(ROOT / "iphonestat_app.py")]


def main() -> int:
    result = subprocess.run(pyinstaller_command(), cwd=ROOT, check=False)
    if result.returncode == 0:
        sys.stdout.write(f"Готово: {ROOT / 'dist' / (APP_NAME + '.exe')}\n")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
