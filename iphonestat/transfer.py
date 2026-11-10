"""Копирование с iPhone на компьютер: отдельные папки и полная резервная копия.

На устройстве ничего не меняется и не удаляется — файлы только читаются.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pymobiledevice3.lockdown_service_provider import LockdownServiceProvider
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.services.house_arrest import HouseArrestService
from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

from iphonestat.device import (
    APP_DOCUMENTS_PATH,
    SOURCE_APP_PREFIX,
    SOURCE_MEDIA_PREFIX,
    DeviceError,
    connect,
    friendly_errors,
)

logger = logging.getLogger(__name__)

READ_CHUNK_BYTES = 2 * 1024 * 1024
MAX_REPORTED_FAILURES = 20
MAX_NAME_LENGTH = 200
MEDIA_ROOT_FOLDER_NAME = "Медиа iPhone"
BACKUP_FOLDER_PREFIX = "Резервная копия iPhone"
_FORBIDDEN_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)


class TransferProgress(Protocol):
    """Куда операция сообщает о ходе работы (реализуется в jobs.JobProgress)."""

    def set_total(self, total_bytes: int) -> None: ...
    def add_bytes(self, count: int, current: str) -> None: ...
    def set_percent(self, percent: float, current: str) -> None: ...
    def set_destination(self, destination: Path) -> None: ...


@dataclass(frozen=True)
class Source:
    """Что копировать: путь в медиа-разделе (bundle_id пуст) или в папке Documents приложения."""

    bundle_id: str
    path: str

    @property
    def folder_name(self) -> str:
        if self.path == "/":
            return MEDIA_ROOT_FOLDER_NAME
        if self.bundle_id and self.path == APP_DOCUMENTS_PATH:
            return f"{self.bundle_id} — Documents"
        return posixpath.basename(self.path)


@dataclass(frozen=True)
class ExportSummary:
    destination: Path
    files_copied: int
    bytes_copied: int
    failures: tuple[str, ...]
    failures_total: int

    def describe(self) -> str:
        text = f"Скопировано файлов: {self.files_copied}."
        if self.failures_total:
            text += f" Не удалось скопировать: {self.failures_total} (например: {'; '.join(self.failures[:3])})."
        return text


def parse_source(text: Any) -> Source:
    """Разбирает адрес вида «afc:/DCIM» или «app:com.example.app:/Documents/x». ValueError — если адрес некорректен."""
    if not isinstance(text, str):
        raise ValueError("адрес должен быть строкой")  # noqa: TRY004 — вызывающий код ждёт один тип ошибки
    if text.startswith(SOURCE_MEDIA_PREFIX):
        bundle_id, path = "", text[len(SOURCE_MEDIA_PREFIX) :]
    elif text.startswith(SOURCE_APP_PREFIX):
        bundle_id, separator, path = text[len(SOURCE_APP_PREFIX) :].partition(":")
        if not bundle_id or not separator:
            raise ValueError("в адресе не указано приложение")
    else:
        raise ValueError("неизвестный вид адреса")

    if not path.startswith("/") or "\x00" in path or ".." in path.split("/"):
        raise ValueError("недопустимый путь")
    normalized = posixpath.normpath(path)
    if bundle_id and normalized != APP_DOCUMENTS_PATH and not normalized.startswith(APP_DOCUMENTS_PATH + "/"):
        raise ValueError("у приложений доступна только папка Documents")
    return Source(bundle_id=bundle_id, path=normalized)


def sanitize_component(name: str) -> str:
    """Делает из имени с устройства безопасное имя файла Windows (без разделителей, «..» и служебных имён)."""
    cleaned = _FORBIDDEN_CHARACTERS.sub("_", name).rstrip(" .")[:MAX_NAME_LENGTH].rstrip(" .")
    if not cleaned or cleaned in (".", ".."):
        return "_"
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        return f"_{cleaned}"
    return cleaned


def unique_child(parent: Path, name: str) -> Path:
    """Путь внутри parent, который ещё не занят: «имя», «имя (2)», «имя (3)»… Существующее не перезаписывается."""
    candidate = parent / name
    stem, suffix = os.path.splitext(name)
    counter = 2
    while candidate.exists():
        candidate = parent / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


class _Copier:
    """Рекурсивно копирует файлы с устройства, накапливая итог и не останавливаясь на отдельных сбоях."""

    def __init__(self, afc: AfcService, progress: TransferProgress) -> None:
        self._afc = afc
        self._progress = progress
        self.files_copied = 0
        self.bytes_copied = 0
        self.failures: list[str] = []
        self.failures_total = 0

    async def copy(self, device_path: str, local_path: Path) -> None:
        try:
            stat = await self._afc.stat(device_path)
        except (OSError, ValueError, KeyError) as error:
            self._record_failure(device_path, error)
            return
        file_type = stat.get("st_ifmt")
        if file_type == "S_IFDIR":
            await self._copy_directory(device_path, local_path)
        elif file_type == "S_IFREG":
            await self._copy_file(device_path, local_path, stat)
        # Ссылки и прочие особые объекты пропускаются: это не данные пользователя.

    async def _copy_directory(self, device_path: str, local_path: Path) -> None:
        try:
            entries = await self._afc.listdir(device_path)
            local_path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._record_failure(device_path, error)
            return
        for entry in sorted(entries):
            if entry in ("", ".", ".."):
                continue
            target = unique_child(local_path, sanitize_component(entry))
            await self.copy(posixpath.join(device_path, entry), target)

    async def _copy_file(self, device_path: str, local_path: Path, stat: dict[str, Any]) -> None:
        completed = False
        try:
            await self._download(device_path, local_path, int(stat.get("st_size", 0)))
            self._apply_modification_time(local_path, stat.get("st_mtime"))
            completed = True
            self.files_copied += 1
        except OSError as error:  # сбой чтения одного файла не должен обрывать копирование остальных
            self._record_failure(device_path, error)
        finally:
            if not completed:  # в том числе при отмене — недокачанный файл не оставляем
                local_path.unlink(missing_ok=True)

    async def _download(self, device_path: str, local_path: Path, size: int) -> None:
        handle = await self._afc.fopen(device_path)
        try:
            # Запись идёт в отдельном потоке задания, где кроме неё ничего не выполняется.
            with open(local_path, "wb") as output:  # noqa: ASYNC230
                remaining = size
                while remaining > 0:
                    chunk = await self._afc.fread(handle, min(READ_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    output.write(chunk)
                    remaining -= len(chunk)
                    self.bytes_copied += len(chunk)
                    self._progress.add_bytes(len(chunk), device_path)
        finally:
            await self._afc.fclose(handle)

    @staticmethod
    def _apply_modification_time(local_path: Path, modified: Any) -> None:
        if isinstance(modified, datetime):
            timestamp = modified.timestamp()
            os.utime(local_path, (timestamp, timestamp))

    def _record_failure(self, device_path: str, error: Exception) -> None:
        logger.warning("Не удалось скопировать %s: %s", device_path, error)
        self.failures_total += 1
        if len(self.failures) < MAX_REPORTED_FAILURES:
            self.failures.append(posixpath.basename(device_path) or device_path)


async def copy_from_device(
    afc: AfcService, source: Source, destination_parent: Path, progress: TransferProgress
) -> ExportSummary:
    """Копирует файл или папку с устройства в новую подпапку destination_parent."""
    total = await _measure(afc, source.path)
    free = shutil.disk_usage(destination_parent).free
    if total > free:
        raise DeviceError(
            f"На диске назначения недостаточно места: нужно {_gigabytes(total)} ГБ, свободно {_gigabytes(free)} ГБ."
        )
    progress.set_total(total)
    destination = unique_child(destination_parent, sanitize_component(source.folder_name))
    progress.set_destination(destination)

    copier = _Copier(afc, progress)
    await copier.copy(source.path, destination)
    return ExportSummary(
        destination=destination,
        files_copied=copier.files_copied,
        bytes_copied=copier.bytes_copied,
        failures=tuple(copier.failures),
        failures_total=copier.failures_total,
    )


async def export_source(udid: str | None, source: Source, destination_parent: Path, progress: TransferProgress) -> str:
    """Подключается к iPhone и копирует source на компьютер. Возвращает итоговое сообщение."""
    with friendly_errors():
        async with await connect(udid) as lockdown, await _open_file_service(lockdown, source) as afc:
            summary = await copy_from_device(afc, source, destination_parent, progress)
    return summary.describe()


async def backup_device(udid: str | None, destination_parent: Path, progress: TransferProgress, now: datetime) -> str:
    """Делает полную резервную копию в формате iTunes в новой датированной папке."""
    destination = unique_child(destination_parent, f"{BACKUP_FOLDER_PREFIX} {now:%Y-%m-%d %H-%M}")
    destination.mkdir(parents=True)
    progress.set_destination(destination)
    progress.set_percent(0.0, "Если iPhone просит код-пароль — введите его на экране телефона")
    with friendly_errors():
        async with await connect(udid) as lockdown:
            service = Mobilebackup2Service(lockdown)
            encrypted = await service.get_will_encrypt()
            await service.backup(
                full=True,
                backup_directory=destination,
                progress_callback=lambda percent: progress.set_percent(_as_percent(percent), "Идёт резервное копирование…"),
            )
    if encrypted:
        return "Резервная копия создана и зашифрована паролем, который вы задавали для копий этого iPhone."
    return "Резервная копия создана без шифрования: сохранённые пароли и данные «Здоровья» в неё не входят."


async def _open_file_service(lockdown: LockdownServiceProvider, source: Source) -> AfcService:
    if source.bundle_id:
        return await HouseArrestService.create(lockdown, source.bundle_id, documents_only=True)
    return AfcService(lockdown)


async def _measure(afc: AfcService, device_path: str) -> int:
    """Размер копируемого в байтах; 0, если устройство не смогло его посчитать (прогресс будет без процентов)."""
    try:
        stat = await afc.stat(device_path)
        if stat.get("st_ifmt") != "S_IFDIR":
            return int(stat.get("st_size", 0))
        return int((await afc.get_size_of_path_contents(device_path)).get("st_size", 0))
    except (OSError, ValueError, KeyError) as error:
        logger.debug("Не удалось заранее узнать размер %s: %s", device_path, error)
        return 0


def _as_percent(value: Any) -> float:
    try:
        return min(100.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _gigabytes(size: int) -> str:
    return f"{size / 1000**3:.1f}"

