"""Сканирование подключённого iPhone через службы Apple Mobile Device (те же, что использует iTunes)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import posixpath
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from pymobiledevice3.exceptions import (
    ConnectionFailedToUsbmuxdError,
    ConnectionTerminatedError,
    DeviceNotFoundError,
    NoDeviceConnectedError,
    PairingDialogResponsePendingError,
    PasswordRequiredError,
    PyMobileDevice3Exception,
    UserDeniedPairingError,
)
from pymobiledevice3.lockdown import UsbmuxLockdownClient, create_using_usbmux
from pymobiledevice3.lockdown_service_provider import LockdownServiceProvider
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.services.house_arrest import HouseArrestService
from pymobiledevice3.services.installation_proxy import InstallationProxyService

from iphonestat.tree import (
    KIND_DIR,
    Node,
    build_app_node,
    build_root,
    collapse_children,
    group,
    kind_for_filename,
    leaf,
    parse_disk_usage,
)

logger = logging.getLogger(__name__)

AFC_CONCURRENCY = 16
PAIR_TIMEOUT_SECONDS = 60
PROGRESS_EVERY_FILES = 2000
DISK_USAGE_DOMAIN = "com.apple.disk_usage"
MEDIA_ROOT_NAME = "Медиа и файлы"
APP_DOCUMENTS_PATH = "/Documents"
SOURCE_MEDIA_PREFIX = "afc:"  # адрес в медиа-разделе: «afc:/DCIM/100APPLE»
SOURCE_APP_PREFIX = "app:"  # адрес в папке приложения: «app:com.example.player:/Documents/video»

MEDIA_FOLDER_NOTES: Mapping[str, str] = {
    "DCIM": "Фото и видео с камеры",
    "PhotoData": "База медиатеки, миниатюры и изменённые версии снимков",
    "Books": "Книги",
    "Downloads": "Загрузки",
    "iTunes_Control": "Музыка и видео, синхронизированные с компьютера",
    "Recordings": "Записи диктофона",
    "Podcasts": "Подкасты",
    "Purchases": "Покупки iTunes Store",
    "PublicStaging": "Временные файлы установки приложений",
    "MediaAnalysis": "Служебные данные анализа фото",
}


class DeviceError(Exception):
    """Операция с iPhone не удалась; текст сообщения предназначен пользователю."""


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    model: str
    ios_version: str


@dataclass(frozen=True)
class ScanResult:
    device: DeviceInfo
    root: Node
    udid: str = ""  # нужен, чтобы последующие операции шли к тому же устройству; в отчёт не попадает


@contextlib.contextmanager
def friendly_errors() -> Iterator[None]:
    """Переводит ошибки связи с устройством в DeviceError с понятным человеку текстом."""
    try:
        yield
    except DeviceError:
        raise
    except ConnectionFailedToUsbmuxdError as error:
        raise DeviceError(
            "Не найдена служба Apple Mobile Device. Установите iTunes или «Устройства Apple» и повторите."
        ) from error
    except (NoDeviceConnectedError, DeviceNotFoundError) as error:
        raise DeviceError("iPhone не найден. Подключите его кабелем USB и разблокируйте экран.") from error
    except PasswordRequiredError as error:
        raise DeviceError("iPhone заблокирован. Разблокируйте экран и повторите.") from error
    except PairingDialogResponsePendingError as error:
        raise DeviceError("На экране iPhone нажмите «Доверять» и повторите.") from error
    except UserDeniedPairingError as error:
        raise DeviceError("На iPhone был выбран ответ «Не доверять». Переподключите кабель и нажмите «Доверять».") from error
    except ConnectionTerminatedError as error:
        raise DeviceError("Связь с iPhone прервалась. Проверьте кабель и повторите.") from error
    except (PyMobileDevice3Exception, OSError) as error:
        raise DeviceError(f"Ошибка связи с iPhone: {type(error).__name__}: {error}") from error


async def connect(udid: str | None) -> UsbmuxLockdownClient:
    """Открывает соединение с iPhone по USB (при первом подключении ждёт нажатия «Доверять»)."""
    return await create_using_usbmux(serial=udid, autopair=True, pair_timeout=PAIR_TIMEOUT_SECONDS)


class _AfcWalker:
    """Рекурсивный обход файловой системы AFC с ограничением числа одновременных запросов."""

    def __init__(self, afc: AfcService, label: str) -> None:
        self._afc = afc
        self._label = label
        self._semaphore = asyncio.Semaphore(AFC_CONCURRENCY)
        self.files_seen = 0
        self.entries_skipped = 0

    async def walk(self, path: str, name: str) -> Node:
        try:
            async with self._semaphore:
                entries = await self._afc.listdir(path)
        except OSError as error:  # AfcException наследуется от OSError
            logger.debug("Не удалось прочитать папку %s: %s", path, error)
            self.entries_skipped += 1
            return leaf(name, 0, KIND_DIR)

        stats = await asyncio.gather(*(self._stat(posixpath.join(path, entry)) for entry in entries))
        files: list[Node] = []
        directories: list[str] = []
        for entry, stat in zip(entries, stats):
            file_type = stat.get("st_ifmt") if stat else None
            if file_type == "S_IFDIR":
                directories.append(entry)
            elif file_type == "S_IFREG":
                files.append(leaf(entry, stat.get("st_size", 0), kind_for_filename(entry)))
        self._report_progress(len(files))

        subtrees = await asyncio.gather(*(self.walk(posixpath.join(path, d), d) for d in directories))
        return group(name, KIND_DIR, collapse_children((*files, *subtrees)))

    async def _stat(self, path: str) -> dict[str, Any] | None:
        try:
            async with self._semaphore:
                return await self._afc.stat(path)
        except (OSError, ValueError, KeyError) as error:
            logger.debug("Не удалось получить сведения о %s: %s", path, error)
            self.entries_skipped += 1
            return None

    def _report_progress(self, new_files: int) -> None:
        before = self.files_seen
        self.files_seen += new_files
        if before // PROGRESS_EVERY_FILES != self.files_seen // PROGRESS_EVERY_FILES:
            logger.info("  %s: просмотрено файлов — %d…", self._label, self.files_seen)


async def scan_device(udid: str | None = None, include_app_files: bool = True) -> ScanResult:
    """Подключается к iPhone по USB и строит дерево занятого места."""
    with friendly_errors():
        return await _scan(udid, include_app_files)


async def _scan(udid: str | None, include_app_files: bool) -> ScanResult:
    logger.info("Подключение к iPhone…")
    lockdown = await connect(udid)
    async with lockdown:
        device_udid = str(lockdown.udid or udid or "")
        device = _read_device_info(lockdown.all_values)
        logger.info("Устройство: %s (%s, iOS %s)", device.name, device.model, device.ios_version)

        disk = parse_disk_usage(await _read_disk_usage(lockdown))
        if disk is None:
            logger.warning("iPhone не сообщил сводку по накопителю: скрытые данные и свободное место не будут показаны.")

        logger.info("Запрос размеров приложений (может занять до минуты)…")
        apps = await _read_apps(lockdown)

        logger.info("Обход медиафайлов…")
        media = await _scan_media(lockdown)

        documents: Mapping[str, Node] = {}
        if include_app_files:
            logger.info("Обход файлов приложений с открытым доступом…")
            documents = await _scan_app_documents(lockdown, apps)

    user_apps, system_apps = _build_app_nodes(apps, documents)
    root = build_root(device.name, disk, user_apps, system_apps, media)
    return ScanResult(device=device, root=root, udid=device_udid)


def _read_device_info(values: Mapping[str, Any]) -> DeviceInfo:
    return DeviceInfo(
        name=str(values.get("DeviceName") or "iPhone"),
        model=str(values.get("ProductType") or "неизвестная модель"),
        ios_version=str(values.get("ProductVersion") or "?"),
    )


async def _read_disk_usage(lockdown: LockdownServiceProvider) -> Mapping[str, Any] | None:
    try:
        values = await lockdown.get_value(domain=DISK_USAGE_DOMAIN)
    except PyMobileDevice3Exception as error:
        logger.debug("Не удалось прочитать %s: %s", DISK_USAGE_DOMAIN, error)
        return None
    return values if isinstance(values, Mapping) else None


async def _read_apps(lockdown: LockdownServiceProvider) -> Mapping[str, Mapping[str, Any]]:
    async with InstallationProxyService(lockdown) as installation_proxy:
        return await installation_proxy.get_apps(application_type="Any", calculate_sizes=True)


async def _scan_media(lockdown: LockdownServiceProvider) -> Node:
    async with AfcService(lockdown) as afc:
        walker = _AfcWalker(afc, "медиа")
        media = await walker.walk("/", MEDIA_ROOT_NAME)
    if walker.entries_skipped:
        logger.warning("Медиа: не удалось прочитать объектов — %d (они не учтены).", walker.entries_skipped)
    annotated = tuple(
        dataclasses.replace(child, note=MEDIA_FOLDER_NOTES.get(child.name, child.note)) for child in media.children
    )
    return dataclasses.replace(
        media,
        children=annotated,
        note="Раздел, доступный компьютеру: фото, видео, музыка, книги, загрузки",
        source=f"{SOURCE_MEDIA_PREFIX}/",
    )


async def _scan_app_documents(
    lockdown: LockdownServiceProvider, apps: Mapping[str, Mapping[str, Any]]
) -> Mapping[str, Node]:
    """Обходит папку Documents приложений, которые сами открыли её для компьютера (UIFileSharingEnabled)."""
    shared = [bundle_id for bundle_id, info in apps.items() if info.get("UIFileSharingEnabled")]
    documents: dict[str, Node] = {}
    for bundle_id in shared:
        node = await _scan_one_app_documents(lockdown, bundle_id)
        if node is not None and node.size > 0:
            documents[bundle_id] = node
    return documents


async def _scan_one_app_documents(lockdown: LockdownServiceProvider, bundle_id: str) -> Node | None:
    try:
        async with await HouseArrestService.create(lockdown, bundle_id, documents_only=True) as house_arrest:
            documents = await _AfcWalker(house_arrest, bundle_id).walk(APP_DOCUMENTS_PATH, "Documents")
            return dataclasses.replace(documents, source=f"{SOURCE_APP_PREFIX}{bundle_id}:{APP_DOCUMENTS_PATH}")
    except (PyMobileDevice3Exception, OSError) as error:
        logger.debug("Файлы приложения %s недоступны: %s", bundle_id, error)
        return None


def _build_app_nodes(
    apps: Mapping[str, Mapping[str, Any]], documents: Mapping[str, Node]
) -> tuple[list[Node], list[Node]]:
    user_apps: list[Node] = []
    system_apps: list[Node] = []
    for bundle_id, info in apps.items():
        node = build_app_node({"CFBundleIdentifier": bundle_id, **info}, documents.get(bundle_id))
        if node is None:
            continue
        target = system_apps if info.get("ApplicationType") == "System" else user_apps
        target.append(node)
    return user_apps, system_apps
