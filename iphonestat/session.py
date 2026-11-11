"""Сканирование iPhone в фоне: окно приложения открывается сразу, а карта появляется, когда скан готов."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from iphonestat.device import DeviceError, ScanResult

logger = logging.getLogger(__name__)

PHASE_SCANNING = "scanning"
PHASE_READY = "ready"
PHASE_FAILED = "failed"
# Сканер пишет о ходе работы в этот журнал; последние строки показываются на экране ожидания.
PROGRESS_LOGGER_NAME = "iphonestat.device"
START_MESSAGE = "Подключение к iPhone…"

Scanner = Callable[[], Coroutine[Any, Any, ScanResult]]
ReadyHook = Callable[[ScanResult, datetime], None]


@dataclass(frozen=True)
class ScanState:
    """Неизменяемый снимок состояния сканирования; при каждом изменении создаётся новый."""

    phase: str = PHASE_SCANNING
    message: str = START_MESSAGE
    revision: int = 0  # растёт с каждым запуском сканирования
    scan: ScanResult | None = None
    scanned_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"phase": self.phase, "message": self.message, "revision": self.revision}


class _ProgressHandler(logging.Handler):
    """Передаёт информационные сообщения сканера на экран ожидания."""

    def __init__(self, report: Callable[[str], None]) -> None:
        super().__init__(level=logging.INFO)
        self._report = report

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno == logging.INFO:
            self._report(record.getMessage().strip())


class ScanSession:
    """Запускает сканирование в отдельном потоке и хранит его последнее состояние."""

    def __init__(
        self,
        scanner: Scanner,
        on_ready: ReadyHook | None = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._scanner = scanner
        self._on_ready = on_ready
        self._now = now
        self._lock = threading.Lock()
        self._state: ScanState | None = None
        self._thread: threading.Thread | None = None

    def snapshot(self) -> ScanState:
        with self._lock:
            return self._state or ScanState()

    def start(self) -> bool:
        """Запускает (или повторяет) сканирование. False — если оно уже идёт."""
        with self._lock:
            if self._state is not None and self._state.phase == PHASE_SCANNING:
                return False
            revision = self._state.revision + 1 if self._state else 1
            self._state = ScanState(revision=revision)
            self._thread = threading.Thread(target=self._run, args=(revision,), name=f"scan-{revision}", daemon=True)
            self._thread.start()
        return True

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _set(self, revision: int, state: ScanState) -> None:
        with self._lock:
            if self._state is not None and self._state.revision == revision:
                self._state = state

    def _run(self, revision: int) -> None:
        progress_logger = logging.getLogger(PROGRESS_LOGGER_NAME)
        handler = _ProgressHandler(lambda message: self._set(revision, ScanState(message=message, revision=revision)))
        progress_logger.addHandler(handler)
        try:
            scan = asyncio.run(self._scanner())
        except DeviceError as error:
            logger.warning("Сканирование не удалось: %s", error)
            self._set(revision, ScanState(PHASE_FAILED, str(error), revision))
            return
        except Exception as error:  # приложение не должно падать: показываем причину и даём повторить
            logger.exception("Сканирование завершилось непредвиденной ошибкой")
            self._set(revision, ScanState(PHASE_FAILED, f"Непредвиденная ошибка: {type(error).__name__}: {error}", revision))
            return
        finally:
            progress_logger.removeHandler(handler)
        scanned_at = self._now()
        self._set(revision, ScanState(PHASE_READY, "", revision, scan, scanned_at))
        if self._on_ready is not None:
            try:
                self._on_ready(scan, scanned_at)
            except Exception:  # сохранение отчёта — побочное действие, карта уже готова
                logger.exception("Не удалось выполнить действие после сканирования")
