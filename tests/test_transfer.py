import asyncio
from datetime import datetime

import pytest
from fakes import MODIFIED_AT, FakeAfc, FakeLockdown, RecordingProgress

from iphonestat import transfer
from iphonestat.device import DeviceError
from iphonestat.transfer import (
    Source,
    backup_device,
    copy_from_device,
    export_source,
    parse_source,
    sanitize_component,
    unique_child,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("afc:/DCIM/100APPLE", Source("", "/DCIM/100APPLE")),
        ("afc:/", Source("", "/")),
        ("afc:/DCIM/", Source("", "/DCIM")),
        ("app:com.example.player:/Documents", Source("com.example.player", "/Documents")),
        ("app:com.example.player:/Documents/My Films/a.mp4", Source("com.example.player", "/Documents/My Films/a.mp4")),
    ],
)
def test_parse_source_accepts_media_and_app_addresses(text, expected):
    assert parse_source(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "C:\\Windows",
        "file:///etc/passwd",
        "afc:DCIM",
        "afc:/DCIM/../../private",
        "afc:/DCIM/\x00",
        "app::/Documents",
        "app:com.example.player",
        "app:com.example.player:/Library/Preferences",
        "app:com.example.player:/Documents/../Library",
        None,
        42,
    ],
)
def test_parse_source_rejects_malformed_or_escaping_addresses(text):
    with pytest.raises(ValueError):
        parse_source(text)


def test_source_folder_name_describes_what_is_copied():
    assert Source("", "/DCIM").folder_name == "DCIM"
    assert Source("", "/").folder_name == "Медиа iPhone"
    assert Source("com.example.player", "/Documents").folder_name == "com.example.player — Documents"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("IMG_0001.HEIC", "IMG_0001.HEIC"),
        ("a/b\\c:d*e?.txt", "a_b_c_d_e_.txt"),
        ("..", "_"),
        ("", "_"),
        ("trailing dot. ", "trailing dot"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("x" * 300, "x" * 200),
    ],
)
def test_sanitize_component_produces_safe_windows_names(name, expected):
    assert sanitize_component(name) == expected


def test_unique_child_never_points_at_existing_entry(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM (2)").mkdir()
    (tmp_path / "a.jpg").write_bytes(b"1")

    assert unique_child(tmp_path, "DCIM") == tmp_path / "DCIM (3)"
    assert unique_child(tmp_path, "a.jpg") == tmp_path / "a (2).jpg"
    assert unique_child(tmp_path, "new") == tmp_path / "new"


def test_copy_from_device_copies_tree_with_sizes_progress_and_dates(tmp_path):
    afc = FakeAfc(
        directories={"/DCIM": ["100APPLE", "note.txt"], "/DCIM/100APPLE": ["IMG_1.HEIC", "IMG_2.MOV"]},
        files={"/DCIM/note.txt": 3, "/DCIM/100APPLE/IMG_1.HEIC": 1000, "/DCIM/100APPLE/IMG_2.MOV": 5_000_000},
    )
    progress = RecordingProgress()

    summary = asyncio.run(copy_from_device(afc, Source("", "/DCIM"), tmp_path, progress))

    video = tmp_path / "DCIM" / "100APPLE" / "IMG_2.MOV"
    assert summary.destination == tmp_path / "DCIM"
    assert (summary.files_copied, summary.bytes_copied, summary.failures_total) == (3, 5_001_003, 0)
    assert video.stat().st_size == 5_000_000
    assert video.stat().st_mtime == MODIFIED_AT.timestamp()
    assert (tmp_path / "DCIM" / "note.txt").read_bytes() == b"xxx"
    assert (progress.total, progress.bytes, progress.destination) == (5_001_003, 5_001_003, tmp_path / "DCIM")
    assert afc.open_handles == 0


def test_copy_from_device_does_not_overwrite_existing_folder(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "keep.txt").write_text("мои файлы", encoding="utf-8")
    afc = FakeAfc(directories={"/DCIM": ["a.jpg"]}, files={"/DCIM/a.jpg": 4})

    summary = asyncio.run(copy_from_device(afc, Source("", "/DCIM"), tmp_path, RecordingProgress()))

    assert summary.destination == tmp_path / "DCIM (2)"
    assert (tmp_path / "DCIM" / "keep.txt").read_text(encoding="utf-8") == "мои файлы"
    assert [p.name for p in (tmp_path / "DCIM").iterdir()] == ["keep.txt"]


def test_copy_from_device_keeps_hostile_names_inside_destination(tmp_path):
    afc = FakeAfc(
        directories={"/Downloads": ["..", "..\\..\\evil.txt", "ok.txt"]},
        files={"/Downloads/..\\..\\evil.txt": 5, "/Downloads/ok.txt": 2},
    )

    summary = asyncio.run(copy_from_device(afc, Source("", "/Downloads"), tmp_path, RecordingProgress()))

    copied = sorted(p.name for p in (tmp_path / "Downloads").iterdir())
    assert copied == [".._.._evil.txt", "ok.txt"]
    assert summary.files_copied == 2
    assert [p.name for p in tmp_path.iterdir()] == ["Downloads"]


def test_copy_from_device_skips_unreadable_files_and_reports_them(tmp_path):
    afc = FakeAfc(
        directories={"/DCIM": ["good.jpg", "bad.jpg", "link"]},
        files={"/DCIM/good.jpg": 10, "/DCIM/bad.jpg": 10},
        symlinks=["/DCIM/link"],
        broken=["/DCIM/bad.jpg"],
    )

    summary = asyncio.run(copy_from_device(afc, Source("", "/DCIM"), tmp_path, RecordingProgress()))

    assert sorted(p.name for p in (tmp_path / "DCIM").iterdir()) == ["good.jpg"]
    assert (summary.files_copied, summary.failures_total, summary.failures) == (1, 1, ("bad.jpg",))
    assert "Не удалось скопировать: 1" in summary.describe()


def test_copy_from_device_removes_partial_file_when_read_fails(tmp_path):
    afc = FakeAfc(directories={"/DCIM": ["big.mov"]}, files={"/DCIM/big.mov": 6_000_000})

    async def failing_read(handle, size):
        raise OSError("device went away")

    afc.fread = failing_read

    summary = asyncio.run(copy_from_device(afc, Source("", "/DCIM"), tmp_path, RecordingProgress()))

    assert list((tmp_path / "DCIM").iterdir()) == []
    assert summary.failures_total == 1
    assert afc.open_handles == 0


def test_copy_from_device_copies_single_file(tmp_path):
    afc = FakeAfc(directories={"/DCIM": ["a.jpg"]}, files={"/DCIM/a.jpg": 7})

    summary = asyncio.run(copy_from_device(afc, Source("", "/DCIM/a.jpg"), tmp_path, RecordingProgress()))

    assert summary.destination == tmp_path / "a.jpg"
    assert (tmp_path / "a.jpg").stat().st_size == 7


def test_copy_from_device_refuses_when_disk_is_too_small(tmp_path, monkeypatch):
    afc = FakeAfc(directories={"/DCIM": ["a.jpg"]}, files={"/DCIM/a.jpg": 10 * 1000**3})
    monkeypatch.setattr(transfer.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 1000**3})())

    with pytest.raises(DeviceError, match="недостаточно места"):
        asyncio.run(copy_from_device(afc, Source("", "/DCIM"), tmp_path, RecordingProgress()))

    assert list(tmp_path.iterdir()) == []


def test_export_source_uses_app_container_for_app_addresses(tmp_path, monkeypatch):
    documents = FakeAfc(directories={"/Documents": ["film.mp4"]}, files={"/Documents/film.mp4": 9})
    opened = []

    async def fake_connect(udid):
        return FakeLockdown()

    async def fake_house_arrest(lockdown, bundle_id, documents_only):
        opened.append((bundle_id, documents_only))
        return documents

    monkeypatch.setattr(transfer, "connect", fake_connect)
    monkeypatch.setattr(transfer.HouseArrestService, "create", fake_house_arrest)

    message = asyncio.run(export_source("FAKE-UDID", Source("com.example.player", "/Documents"), tmp_path, RecordingProgress()))

    assert opened == [("com.example.player", True)]
    assert (tmp_path / "com.example.player — Documents" / "film.mp4").stat().st_size == 9
    assert message == "Скопировано файлов: 1."


def test_backup_device_writes_into_new_dated_folder_and_reports_progress(tmp_path, monkeypatch):
    calls = {}

    class FakeBackupService:
        def __init__(self, lockdown):
            pass

        async def get_will_encrypt(self):
            return True

        async def backup(self, full, backup_directory, progress_callback):
            calls["full"] = full
            calls["directory"] = backup_directory
            progress_callback(12.5)
            progress_callback("garbage")
            progress_callback(250)

    async def fake_connect(udid):
        return FakeLockdown()

    monkeypatch.setattr(transfer, "connect", fake_connect)
    monkeypatch.setattr(transfer, "Mobilebackup2Service", FakeBackupService)
    progress = RecordingProgress()

    message = asyncio.run(backup_device("FAKE-UDID", tmp_path, progress, datetime(2026, 11, 10, 18, 5)))

    expected = tmp_path / "Резервная копия iPhone 2026-11-10 18-05"
    assert calls == {"full": True, "directory": expected}
    assert expected.is_dir()
    assert progress.destination == expected
    assert progress.percents == [0.0, 12.5, 0.0, 100.0]
    assert "зашифрована" in message
