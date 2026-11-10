"""Тесты сканера на поддельном устройстве: настоящие службы iPhone заменены заглушками."""

import asyncio
from typing import ClassVar

import pytest
from fakes import FakeAfc, FakeLockdown
from pymobiledevice3.exceptions import NoDeviceConnectedError, PasswordRequiredError

from iphonestat import device
from iphonestat.device import DeviceError, _AfcWalker, _build_app_nodes, _read_device_info, scan_device
from iphonestat.tree import KIND_FREE, KIND_HIDDEN, KIND_IMAGE, KIND_VIDEO

GB = 1000**3


class FakeInstallationProxy:
    apps: ClassVar[dict] = {}

    def __init__(self, lockdown):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def get_apps(self, application_type, calculate_sizes):
        return self.apps


def _fail_listing_of(listdir, broken_path):
    async def wrapped(path):
        if path == broken_path:
            raise OSError(f"cannot list {path}")
        return await listdir(path)

    return wrapped


def test_walker_builds_tree_with_sizes_and_file_kinds():
    afc = FakeAfc(
        directories={"/": ["DCIM", "note.txt"], "/DCIM": ["a.jpg", "b.mov"]},
        files={"/note.txt": 5, "/DCIM/a.jpg": 100, "/DCIM/b.mov": 900},
    )

    root = asyncio.run(_AfcWalker(afc, "тест").walk("/", "Медиа"))

    dcim = root.children[0]
    assert root.size == 1005
    assert (dcim.name, dcim.size) == ("DCIM", 1000)
    assert [(c.name, c.kind) for c in dcim.children] == [("b.mov", KIND_VIDEO), ("a.jpg", KIND_IMAGE)]


def test_walker_skips_symlinks_and_counts_unreadable_entries():
    afc = FakeAfc(
        directories={"/": ["ok.jpg", "link", "bad.bin"]},
        files={"/ok.jpg": 7, "/bad.bin": 50},
        symlinks=["/link"],
        broken=["/bad.bin"],
    )
    walker = _AfcWalker(afc, "тест")

    root = asyncio.run(walker.walk("/", "Медиа"))

    assert root.size == 7
    assert walker.entries_skipped == 1


def test_walker_treats_unreadable_directory_as_empty():
    afc = FakeAfc(directories={"/": ["secret"], "/secret": ["hidden.bin"]}, files={"/secret/hidden.bin": 9})
    afc.listdir = _fail_listing_of(afc.listdir, "/secret")
    walker = _AfcWalker(afc, "тест")

    root = asyncio.run(walker.walk("/", "Медиа"))

    assert root.size == 0
    assert walker.entries_skipped == 1


def test_build_app_nodes_separates_user_and_system_apps():
    apps = {
        "com.example.game": {"ApplicationType": "User", "CFBundleName": "Game", "StaticDiskUsage": 10, "DynamicDiskUsage": 5},
        "com.apple.safari": {"ApplicationType": "System", "CFBundleName": "Safari", "StaticDiskUsage": 99, "DynamicDiskUsage": 3},
        "com.example.empty": {"ApplicationType": "User"},
    }

    user_apps, system_apps = _build_app_nodes(apps, {})

    assert [(n.name, n.size, n.note) for n in user_apps] == [("Game", 15, "com.example.game")]
    assert [(n.name, n.size) for n in system_apps] == [("Safari", 3)]


def test_read_device_info_falls_back_to_placeholders():
    info = _read_device_info({})

    assert (info.name, info.model, info.ios_version) == ("iPhone", "неизвестная модель", "?")


def test_scan_device_reports_missing_device_in_plain_words(monkeypatch):
    async def no_device(**kwargs):
        raise NoDeviceConnectedError()

    monkeypatch.setattr(device, "create_using_usbmux", no_device)

    with pytest.raises(DeviceError, match="iPhone не найден"):
        asyncio.run(scan_device())


def test_scan_device_reports_locked_device(monkeypatch):
    async def locked(**kwargs):
        raise PasswordRequiredError()

    monkeypatch.setattr(device, "create_using_usbmux", locked)

    with pytest.raises(DeviceError, match="заблокирован"):
        asyncio.run(scan_device())


def test_scan_device_assembles_full_tree_from_device_services(monkeypatch):
    disk_usage = {"TotalDiskCapacity": 64 * GB, "TotalSystemCapacity": 10 * GB, "AmountDataAvailable": 20 * GB}
    media_afc = FakeAfc(directories={"/": ["DCIM"], "/DCIM": ["a.heic"]}, files={"/DCIM/a.heic": 4 * GB})
    documents_afc = FakeAfc(directories={"/Documents": ["film.mp4"]}, files={"/Documents/film.mp4": 2 * GB})
    FakeInstallationProxy.apps = {
        "com.example.player": {
            "ApplicationType": "User",
            "CFBundleDisplayName": "Player",
            "UIFileSharingEnabled": True,
            "StaticDiskUsage": 1 * GB,
            "DynamicDiskUsage": 3 * GB,
        }
    }

    async def fake_connect(**kwargs):
        return FakeLockdown(disk_usage)

    async def fake_house_arrest(lockdown, bundle_id, documents_only):
        return documents_afc

    monkeypatch.setattr(device, "create_using_usbmux", fake_connect)
    monkeypatch.setattr(device, "InstallationProxyService", FakeInstallationProxy)
    monkeypatch.setattr(device, "AfcService", lambda lockdown: media_afc)
    monkeypatch.setattr(device.HouseArrestService, "create", fake_house_arrest)

    result = asyncio.run(scan_device())

    sizes = {child.name: child.size for child in result.root.children}
    kinds = {child.kind: child.size for child in result.root.children}
    assert result.device.name == "Тестовый iPhone"
    assert result.root.size == 64 * GB
    assert sizes["Приложения"] == 4 * GB
    assert sizes["Медиа и файлы"] == 4 * GB
    assert kinds[KIND_FREE] == 20 * GB
    assert kinds[KIND_HIDDEN] == (64 - 10 - 20 - 4 - 4) * GB
    media = next(child for child in result.root.children if child.name == "Медиа и файлы")
    assert media.children[0].note == "Фото и видео с камеры"
    apps = next(child for child in result.root.children if child.name == "Приложения")
    data_node = apps.children[0].children[0]
    assert [c.name for c in data_node.children] == ["Файлы (Documents)", "Кэш, базы и прочие данные"]


def test_scan_device_skips_app_documents_when_disabled(monkeypatch):
    media_afc = FakeAfc(directories={"/": []}, files={})
    FakeInstallationProxy.apps = {
        "com.example.player": {"ApplicationType": "User", "UIFileSharingEnabled": True, "DynamicDiskUsage": 5}
    }

    async def fake_connect(**kwargs):
        return FakeLockdown(None)

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("house_arrest не должен вызываться")

    monkeypatch.setattr(device, "create_using_usbmux", fake_connect)
    monkeypatch.setattr(device, "InstallationProxyService", FakeInstallationProxy)
    monkeypatch.setattr(device, "AfcService", lambda lockdown: media_afc)
    monkeypatch.setattr(device.HouseArrestService, "create", must_not_be_called)

    result = asyncio.run(scan_device(include_app_files=False))

    assert result.root.size == 5
    assert all(child.kind not in (KIND_FREE, KIND_HIDDEN) for child in result.root.children)
