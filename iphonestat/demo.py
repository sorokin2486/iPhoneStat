"""Правдоподобные вымышленные данные — чтобы посмотреть отчёт без подключённого iPhone."""

from __future__ import annotations

import random

from iphonestat.device import MEDIA_FOLDER_NOTES, MEDIA_ROOT_NAME, DeviceInfo, ScanResult
from iphonestat.tree import (
    KIND_AUDIO,
    KIND_DIR,
    KIND_DOC,
    KIND_IMAGE,
    KIND_OTHER,
    KIND_VIDEO,
    DiskUsage,
    Node,
    build_app_node,
    build_root,
    collapse_children,
    group,
    leaf,
)

MB = 1000**2
GB = 1000**3
DEMO_SEED = 42

# (название, размер программы в МБ, размер данных в МБ)
_DEMO_APPS = (
    ("Telegram", 190, 9400),
    ("YouTube", 310, 6100),
    ("Genshin Impact", 4200, 21800),
    ("Spotify", 240, 5200),
    ("WhatsApp", 180, 3900),
    ("Instagram", 420, 2300),
    ("TikTok", 510, 4700),
    ("Яндекс Карты", 380, 1900),
    ("VLC", 120, 7300),
    ("Chrome", 260, 840),
    ("Discord", 210, 1250),
    ("Steam", 150, 310),
    ("Notion", 130, 420),
    ("Банк", 340, 95),
    ("Калькулятор+", 18, 2),
)
_DEMO_SYSTEM_APPS = (("Safari", 1400), ("Сообщения", 3800), ("Почта", 650), ("Заметки", 210), ("Музыка", 2900))


def build_demo_scan() -> ScanResult:
    rng = random.Random(DEMO_SEED)
    user_apps = [node for name, static, dynamic in _DEMO_APPS if (node := _demo_app(name, static, dynamic, rng))]
    system_apps = [
        node
        for name, dynamic in _DEMO_SYSTEM_APPS
        if (node := build_app_node({"CFBundleDisplayName": name, "ApplicationType": "System", "DynamicDiskUsage": dynamic * MB}))
    ]
    disk = DiskUsage(total=256 * GB, system=11 * GB, free=38 * GB)
    root = build_root("iPhone (демо)", disk, user_apps, system_apps, _demo_media(rng))
    return ScanResult(device=DeviceInfo(name="iPhone (демо)", model="iPhone15,2", ios_version="18.1"), root=root)


def _demo_app(name: str, static_mb: int, dynamic_mb: int, rng: random.Random) -> Node | None:
    info = {
        "CFBundleDisplayName": name,
        "CFBundleIdentifier": f"com.example.{name.lower().replace(' ', '')}",
        "ApplicationType": "User",
        "StaticDiskUsage": static_mb * MB,
        "DynamicDiskUsage": dynamic_mb * MB,
    }
    documents = _demo_vlc_documents(rng) if name == "VLC" else None
    return build_app_node(info, documents)


def _demo_vlc_documents(rng: random.Random) -> Node:
    movies = [leaf(f"Фильм {i}.mkv", rng.randint(700, 1900) * MB, KIND_VIDEO) for i in range(1, 5)]
    return group("Documents", KIND_DIR, movies)


def _demo_media(rng: random.Random) -> Node:
    camera_rolls = [_demo_camera_roll(100 + index, rng) for index in range(6)]
    folders = (
        group("DCIM", KIND_DIR, camera_rolls, note=MEDIA_FOLDER_NOTES["DCIM"]),
        group(
            "PhotoData",
            KIND_DIR,
            (
                leaf("Photos.sqlite", 410 * MB, KIND_OTHER),
                group("Thumbnails", KIND_DIR, [leaf(f"thumb_{i}.ithmb", rng.randint(40, 320) * MB, KIND_IMAGE) for i in range(8)]),
                group("Mutations", KIND_DIR, [leaf(f"IMG_{4000 + i}.MOV", rng.randint(60, 900) * MB, KIND_VIDEO) for i in range(9)]),
            ),
            note=MEDIA_FOLDER_NOTES["PhotoData"],
        ),
        group(
            "Books",
            KIND_DIR,
            [leaf(f"Книга {i}.epub", rng.randint(2, 90) * MB, KIND_DOC) for i in range(1, 15)],
            note=MEDIA_FOLDER_NOTES["Books"],
        ),
        group(
            "Recordings",
            KIND_DIR,
            [leaf(f"Запись {i}.m4a", rng.randint(1, 140) * MB, KIND_AUDIO) for i in range(1, 22)],
            note=MEDIA_FOLDER_NOTES["Recordings"],
        ),
        group(
            "Downloads",
            KIND_DIR,
            (leaf("архив.zip", 1300 * MB, KIND_OTHER), leaf("инструкция.pdf", 48 * MB, KIND_DOC)),
            note=MEDIA_FOLDER_NOTES["Downloads"],
        ),
    )
    return group(MEDIA_ROOT_NAME, KIND_DIR, folders, note="Раздел, доступный компьютеру: фото, видео, музыка, книги, загрузки")


def _demo_camera_roll(number: int, rng: random.Random) -> Node:
    photos = [leaf(f"IMG_{number}{i:03d}.HEIC", rng.randint(900, 4200) * 1000, KIND_IMAGE) for i in range(700)]
    videos = [leaf(f"IMG_{number}{700 + i:03d}.MOV", rng.randint(30, 2400) * MB, KIND_VIDEO) for i in range(rng.randint(5, 14))]
    return group(f"{number}APPLE", KIND_DIR, collapse_children((*photos, *videos)))
