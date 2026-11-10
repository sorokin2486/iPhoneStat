"""Чистые функции построения дерева «что занимает место» (без обращения к устройству)."""

from __future__ import annotations

import posixpath
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

KIND_APP = "app"
KIND_BUNDLE = "bundle"  # приложение целиком: программа + её данные
KIND_APPDATA = "appdata"
KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_DOC = "doc"
KIND_OTHER = "other"
KIND_HIDDEN = "hidden"
KIND_SYSTEM = "system"
KIND_FREE = "free"
KIND_DIR = "dir"

DEFAULT_KEEP_CHILDREN = 200

_EXTENSION_KINDS: Mapping[str, str] = {
    **dict.fromkeys(
        ("jpg", "jpeg", "heic", "heif", "png", "gif", "dng", "tif", "tiff", "webp", "bmp", "aae", "thm"),
        KIND_IMAGE,
    ),
    **dict.fromkeys(("mov", "mp4", "m4v", "avi", "mkv", "3gp", "webm"), KIND_VIDEO),
    **dict.fromkeys(("mp3", "m4a", "aac", "wav", "flac", "aiff", "caf", "ogg", "m4b", "m4r"), KIND_AUDIO),
    **dict.fromkeys(("pdf", "epub", "ibooks", "txt", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "fb2"), KIND_DOC),
}


@dataclass(frozen=True)
class Node:
    """Узел дерева: файл, папка, приложение или категория."""

    name: str
    size: int
    kind: str
    children: tuple[Node, ...] = ()
    note: str = ""
    source: str = ""  # адрес на устройстве («afc:/DCIM»), если узел можно скопировать на ПК
    synthetic: bool = False  # свёрнутый остаток «… ещё N», которому не соответствует реальный объект


@dataclass(frozen=True)
class DiskUsage:
    """Сводка по накопителю в байтах."""

    total: int
    system: int
    free: int


def leaf(name: str, size: int, kind: str, note: str = "") -> Node:
    return Node(name=name, size=max(0, int(size)), kind=kind, note=note)


def group(name: str, kind: str, children: Iterable[Node], note: str = "") -> Node:
    """Собирает папку: пустые узлы отбрасываются, остальные сортируются по убыванию размера."""
    kept = tuple(sorted((c for c in children if c.size > 0), key=lambda c: c.size, reverse=True))
    return Node(name=name, size=sum(c.size for c in kept), kind=kind, children=kept, note=note)


def kind_for_filename(name: str) -> str:
    extension = posixpath.splitext(name)[1].lstrip(".").lower()
    return _EXTENSION_KINDS.get(extension, KIND_OTHER)


def collapse_children(children: Sequence[Node], keep: int = DEFAULT_KEEP_CHILDREN) -> tuple[Node, ...]:
    """Оставляет `keep` самых крупных узлов, остальные сворачивает в один узел «… ещё N»."""
    ordered = sorted(children, key=lambda c: c.size, reverse=True)
    if len(ordered) <= keep:
        return tuple(ordered)
    head, tail = ordered[:keep], ordered[keep:]
    bytes_by_kind: dict[str, int] = defaultdict(int)
    for node in tail:
        bytes_by_kind[node.kind] += node.size
    dominant_kind = max(bytes_by_kind, key=lambda k: bytes_by_kind[k])
    rest = Node(
        name=f"… ещё {len(tail)} мелких объектов",
        size=sum(n.size for n in tail),
        kind=dominant_kind,
        synthetic=True,
    )
    return (*head, rest)


def parse_disk_usage(values: Mapping[str, Any] | None) -> DiskUsage | None:
    """Разбирает ответ lockdown-домена com.apple.disk_usage. Возвращает None, если данных нет."""
    if not values:
        return None
    total = _as_int(values.get("TotalDiskCapacity"))
    if total <= 0:
        return None
    data_capacity = _as_int(values.get("TotalDataCapacity"))
    system = _as_int(values.get("TotalSystemCapacity")) or max(0, total - data_capacity)
    free = _as_int(values.get("AmountDataAvailable")) or _as_int(values.get("TotalDataAvailable"))
    return DiskUsage(total=total, system=min(system, total), free=min(free, total))


def build_app_node(info: Mapping[str, Any], documents: Node | None = None) -> Node | None:
    """Строит узел приложения из записи installation_proxy. None — если приложение ничего не занимает."""
    bundle_id = str(info.get("CFBundleIdentifier") or "")
    name = str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle_id or "Без названия")
    is_system = info.get("ApplicationType") == "System"
    # Системные приложения лежат на системном разделе — их код уже учтён в «iOS».
    static_size = 0 if is_system else _as_int(info.get("StaticDiskUsage"))
    dynamic_size = _as_int(info.get("DynamicDiskUsage"))

    node = group(
        name,
        KIND_BUNDLE,
        (leaf("Программа", static_size, KIND_APP), _build_app_data_node(dynamic_size, documents)),
        note=bundle_id,
    )
    return node if node.size > 0 else None


def _build_app_data_node(dynamic_size: int, documents: Node | None) -> Node:
    title = "Документы и данные"
    if documents is None or documents.size <= 0:
        return leaf(title, dynamic_size, KIND_APPDATA)
    remainder = max(0, dynamic_size - documents.size)
    return group(
        title,
        KIND_APPDATA,
        (
            Node("Файлы (Documents)", documents.size, KIND_DIR, documents.children, source=documents.source),
            leaf("Кэш, базы и прочие данные", remainder, KIND_APPDATA),
        ),
    )


def build_root(
    device_name: str,
    disk: DiskUsage | None,
    user_apps: Sequence[Node],
    system_apps: Sequence[Node],
    media: Node | None,
) -> Node:
    """Собирает корень дерева. Невидимый остаток («прочее») вычисляется из сводки по диску."""
    visible = [
        group("Приложения", KIND_APP, user_apps),
        group(
            "Данные встроенных приложений",
            KIND_APPDATA,
            system_apps,
            note="Данные Safari, Почты, Сообщений и других встроенных приложений",
        ),
    ]
    if media is not None:
        visible.append(media)
    if disk is None:
        return group(device_name, KIND_DIR, visible)

    accounted = sum(node.size for node in visible)
    used_data = max(0, disk.total - disk.system - disk.free)
    hidden = max(0, used_data - accounted)
    return group(
        device_name,
        KIND_DIR,
        (
            *visible,
            leaf(
                "Системные данные и прочее",
                hidden,
                KIND_HIDDEN,
                note="Кэши, журналы, данные служб и всё, что iOS не показывает компьютеру",
            ),
            leaf("iOS (система)", disk.system, KIND_SYSTEM),
            leaf("Свободно", disk.free, KIND_FREE),
        ),
    )


def to_dict(node: Node) -> dict[str, Any]:
    """Компактное представление для JSON.

    n — имя, s — размер, k — тип, t — пояснение, c — дети, p — адрес на устройстве, x — свёрнутый остаток.
    Адрес задаётся только у корня обхода: адреса вложенных узлов страница получает, дописывая имена.
    """
    result: dict[str, Any] = {"n": node.name, "s": node.size, "k": node.kind}
    if node.note:
        result["t"] = node.note
    if node.source:
        result["p"] = node.source
    if node.synthetic:
        result["x"] = 1
    if node.children:
        result["c"] = [to_dict(child) for child in node.children]
    return result


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
