@echo off
rem iPhoneStat launcher: creates the virtual environment on first run, then scans the connected iPhone.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: installing dependencies...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :error
)
".venv\Scripts\python.exe" -m iphonestat %*
set "EXIT_CODE=%errorlevel%"
echo.
pause
exit /b %EXIT_CODE%

:error
echo Setup failed. Make sure Python 3.10+ is installed and available as "python".
pause
exit /b 1
