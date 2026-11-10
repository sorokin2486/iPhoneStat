"""Фоновые задания (копирование, резервная копия): одно за раз, с прогрессом и отменой."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iphonestat.device import DeviceError

logger = logging.getLogger(__name__)

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"


class JobBusyError(Exception):
    """Уже выполняется другое задание."""


@dataclass(frozen=True)
class JobStatus:
    """Неизменяемый снимок состояния задания; при каждом изменении создаётся новый."""

    id: int
    kind: str
    title: str
    state: str = STATE_RUNNING
    done_bytes: int = 0
    total_bytes: int = 0
    percent: float | None = None
    current: str = ""
    destination: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class JobProgress:
    """Через этот объект задание сообщает о ходе работы (реализует transfer.TransferProgress)."""

    def __init__(self, update: Callable[..., None]) -> None:
        self._update = update
        self._done_bytes = 0
        self._total_bytes = 0

    def set_total(self, total_bytes: int) -> None:
        self._total_bytes = max(0, total_bytes)
        self._update(total_bytes=self._total_bytes)

    def add_bytes(self, count: int, current: str) -> None:
        self._done_bytes += count
        percent = min(100.0, self._done_bytes * 100 / self._total_bytes) if self._total_bytes else None
        self._update(done_bytes=self._done_bytes, percent=percent, current=current)

    def set_percent(self, percent: float, current: str) -> None:
        self._update(percent=percent, current=current)

    def set_destination(self, destination: Path) -> None:
        self._update(destination=str(destination))


JobFactory = Callable[[JobProgress], Coroutine[Any, Any, str]]


class JobRunner:
    """Запускает задание в отдельном потоке со своим циклом asyncio и хранит его последний статус."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: JobStatus | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[str] | None = None
        self._thread: threading.Thread | None = None
        self._next_id = 1

    def snapshot(self) -> JobStatus | None:
        with self._lock:
            return self._status

    def is_running(self) -> bool:
        status = self.snapshot()
        return status is not None and status.state == STATE_RUNNING

    def start(self, kind: str, title: str, factory: JobFactory) -> JobStatus:
        """Запускает задание. JobBusyError — если предыдущее ещё не завершилось."""
        with self._lock:
            if self._status is not None and self._status.state == STATE_RUNNING:
                raise JobBusyError("Дождитесь завершения текущей операции или отмените её.")
            status = JobStatus(id=self._next_id, kind=kind, title=title)
            self._next_id += 1
            self._status = status
            self._thread = threading.Thread(target=self._run, args=(status.id, factory), name=f"job-{status.id}", daemon=True)
            self._thread.start()
        return status

    def cancel(self) -> bool:
        """Просит текущее задание остановиться. False — если отменять нечего."""
        with self._lock:
            loop, task = self._loop, self._task
            running = self._status is not None and self._status.state == STATE_RUNNING
        if not running or loop is None or task is None:
            return False
        loop.call_soon_threadsafe(task.cancel)
        return True

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _update(self, job_id: int, **changes: Any) -> None:
        with self._lock:
            if self._status is not None and self._status.id == job_id:
                self._status = dataclasses.replace(self._status, **changes)

    def _run(self, job_id: int, factory: JobFactory) -> None:
        progress = JobProgress(lambda **changes: self._update(job_id, **changes))
        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(factory(progress))
            with self._lock:
                self._loop, self._task = loop, task
            message = loop.run_until_complete(task)
            self._update(job_id, state=STATE_DONE, message=message, percent=100.0, current="")
        except asyncio.CancelledError:
            self._update(job_id, state=STATE_CANCELLED, message="Операция отменена. Уже скопированные файлы остались на диске.", current="")
        except DeviceError as error:
            self._update(job_id, state=STATE_FAILED, message=str(error), current="")
        except Exception as error:  # задание не должно ронять приложение: показываем причину пользователю
            logger.exception("Задание %s завершилось ошибкой", job_id)
            self._update(job_id, state=STATE_FAILED, message=f"Непредвиденная ошибка: {type(error).__name__}: {error}", current="")
        finally:
            with self._lock:
                self._loop, self._task = None, None
            loop.close()
