@echo off
rem Builds dist\iPhoneStat.exe (single file, console window shows scan progress).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv || goto :error
)
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt || goto :error
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --console --name iPhoneStat ^
    --add-data "iphonestat\template.html;iphonestat" --collect-data pymobiledevice3 ^
    --log-level WARN iphonestat_app.py || goto :error
echo.
echo Built: dist\iPhoneStat.exe
exit /b 0

:error
echo Build failed.
exit /b 1
