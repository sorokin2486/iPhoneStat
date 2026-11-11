"""Фоновое сканирование, выбор страницы и сборка приложения целиком (без iPhone и без настоящего окна)."""

import asyncio
import dataclasses
import logging
import threading
import urllib.request
from datetime import datetime

import pytest

from iphonestat import __main__ as entry
from iphonestat import app as app_module
from iphonestat import paths
from iphonestat.app import MODE_WINDOW, Pages, run_app, save_report
from iphonestat.demo import build_demo_scan
from iphonestat.device import DeviceError
from iphonestat.session import PHASE_FAILED, PHASE_READY, PHASE_SCANNING, PROGRESS_LOGGER_NAME, ScanSession

WAIT_SECONDS = 5
SCANNED_AT = datetime(2026, 11, 10, 12, 30)


async def _demo_scan():
    return build_demo_scan()


def _finished(session):
    session.start()
    session.wait(WAIT_SECONDS)
    return session.snapshot()


def test_session_is_scanning_before_the_first_result():
    assert ScanSession(_demo_scan).snapshot().phase == PHASE_SCANNING


def test_session_keeps_scan_result_and_calls_ready_hook():
    saved = []
    session = ScanSession(_demo_scan, on_ready=lambda scan, at: saved.append((scan, at)), now=lambda: SCANNED_AT)

    state = _finished(session)

    assert (state.phase, state.revision, state.scanned_at) == (PHASE_READY, 1, SCANNED_AT)
    assert saved == [(state.scan, SCANNED_AT)]


def test_session_shows_scanner_progress_messages_while_scanning(caplog):
    caplog.set_level(logging.INFO, logger=PROGRESS_LOGGER_NAME)
    reached, release = threading.Event(), threading.Event()

    async def slow_scan():
        logging.getLogger(PROGRESS_LOGGER_NAME).info("  Обход медиафайлов…")
        reached.set()
        await asyncio.get_running_loop().run_in_executor(None, release.wait, WAIT_SECONDS)
        return build_demo_scan()

    session = ScanSession(slow_scan)
    session.start()
    assert reached.wait(WAIT_SECONDS)
    during = session.snapshot()
    second_start = session.start()
    release.set()
    session.wait(WAIT_SECONDS)

    assert (during.phase, during.message) == (PHASE_SCANNING, "Обход медиафайлов…")
    assert second_start is False
    assert session.snapshot().phase == PHASE_READY


@pytest.mark.parametrize(
    ("error", "expected"),
    [(DeviceError("iPhone не найден."), "iPhone не найден."), (RuntimeError("boom"), "Непредвиденная ошибка: RuntimeError: boom")],
)
def test_session_failure_is_shown_and_scan_can_be_repeated(error, expected):
    outcomes = [error, None]

    async def flaky_scan():
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome
        return build_demo_scan()

    session = ScanSession(flaky_scan)
    failed = _finished(session)
    repeated = _finished(session)

    assert (failed.phase, failed.message, failed.scan) == (PHASE_FAILED, expected, None)
    assert (repeated.phase, repeated.revision) == (PHASE_READY, 2)


def test_failing_ready_hook_does_not_lose_the_scan():
    def broken_hook(scan, at):
        raise OSError("disk full")

    assert _finished(ScanSession(_demo_scan, on_ready=broken_hook)).phase == PHASE_READY


def test_pages_show_waiting_screen_until_scan_is_ready_then_the_map():
    session = ScanSession(_demo_scan)
    pages = Pages(session, live=True)

    waiting = pages.render("tok")
    _finished(session)
    ready = pages.render("tok")

    assert "Подключение к iPhone" in waiting and "const REPORT" not in waiting
    assert '"live":{"token":"tok"}' in ready
    assert pages.render("tok") is ready  # карта собирается один раз на скан


def test_pages_without_live_actions_hide_the_token():
    session = ScanSession(_demo_scan)
    _finished(session)

    assert '"live":null' in Pages(session, live=False).render("tok")


def test_save_report_writes_timestamped_file_into_app_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    save_report(build_demo_scan(), SCANNED_AT)

    assert (tmp_path / "iPhoneStat" / "reports" / "iphone-2026-11-10_12-30-00.html").is_file()
    assert paths.log_path() == tmp_path / "iPhoneStat" / "iphonestat.log"


class FakeWindow:
    """Вместо настоящего окна: загружает страницу, как это сделал бы встроенный просмотрщик, и «закрывается»."""

    def __init__(self):
        self.pages = []

    def run(self, url):
        with urllib.request.urlopen(url, timeout=WAIT_SECONDS) as response:
            self.pages.append(response.read().decode("utf-8"))

    def pick_folder(self, title):
        return None


def test_app_serves_its_page_to_the_window_and_stops_when_it_closes(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    window = FakeWindow()

    run_app(_demo_scan, live=True, mode=MODE_WINDOW, window_factory=lambda title, is_busy: window)

    assert len(window.pages) == 1 and "iPhoneStat" in window.pages[0]


def test_app_opens_browser_with_companion_window_when_own_window_is_unavailable(monkeypatch):
    opened, companions = [], []
    monkeypatch.setattr(app_module.webbrowser, "open", opened.append)
    monkeypatch.setattr(app_module.dialogs, "run_companion_window", lambda title, text, open_page: companions.append(title))

    run_app(_demo_scan, live=False, mode=MODE_WINDOW, window_factory=lambda title, is_busy: None)

    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:")
    assert companions == ["iPhoneStat"]


def test_main_starts_the_app_in_requested_mode(monkeypatch):
    started = []
    monkeypatch.setattr(entry, "configure_logging", lambda verbose: None)
    monkeypatch.setattr(entry, "run_app", lambda scanner, live, mode: started.append((live, mode)))

    assert entry.main([]) == 0
    assert entry.main(["--demo", "--browser"]) == 0
    assert entry.main(["--server-only"]) == 0

    assert started == [(True, "window"), (False, "browser"), (True, "server-only")]


def test_main_shows_startup_failure_in_a_message_box(monkeypatch):
    shown = []

    def broken(scanner, live, mode):
        raise RuntimeError("boom")

    monkeypatch.setattr(entry, "configure_logging", lambda verbose: None)
    monkeypatch.setattr(entry, "run_app", broken)
    monkeypatch.setattr(entry.dialogs, "show_error", lambda title, message: shown.append(message))

    assert entry.main([]) == 1
    assert "RuntimeError: boom" in shown[0]


def test_report_only_reports_device_error_as_failure(monkeypatch):
    async def no_device(udid, include_app_files):
        raise DeviceError("iPhone не найден.")

    monkeypatch.setattr(entry, "configure_logging", lambda verbose: None)
    monkeypatch.setattr(entry, "scan_device", no_device)

    assert entry.main(["--report-only"]) == 1


def test_scan_result_is_not_mutated_by_session():
    scan = dataclasses.replace(build_demo_scan(), udid="X")

    async def scanner():
        return scan

    assert _finished(ScanSession(scanner)).scan is scan
