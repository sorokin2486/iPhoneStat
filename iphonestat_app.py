"""Точка входа для сборки iPhoneStat.exe (PyInstaller). Из исходников запускайте: python -m iphonestat"""

import sys

from iphonestat.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
