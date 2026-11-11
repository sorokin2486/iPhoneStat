"""Сборка автономного HTML-отчёта с картой занятого места."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from iphonestat.device import ScanResult
from iphonestat.tree import to_dict

TEMPLATE_PATH = Path(__file__).with_name("template.html")
DATA_PLACEHOLDER = '"__REPORT_DATA__"'


def render_report(scan: ScanResult, scanned_at: datetime, live_token: str | None = None) -> str:
    """Подставляет данные сканирования в шаблон и возвращает готовую HTML-страницу.

    live_token задаётся только для страницы, которую отдаёт локальный сервер приложения: с ним на
    странице появляются кнопки копирования и резервной копии. В сохранённом файле отчёта их нет.
    """
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if DATA_PLACEHOLDER not in template:
        raise ValueError(f"В шаблоне {TEMPLATE_PATH.name} нет метки для данных")
    payload = {
        "device": {"name": scan.device.name, "model": scan.device.model, "ios": scan.device.ios_version},
        "scannedAt": scanned_at.strftime("%d.%m.%Y %H:%M"),
        "tree": to_dict(scan.root),
        "live": {"token": live_token} if live_token else None,
    }
    return template.replace(DATA_PLACEHOLDER, _to_script_safe_json(payload), 1)


def write_report(scan: ScanResult, output_path: Path, scanned_at: datetime) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report(scan, scanned_at), encoding="utf-8")
    return output_path


def _to_script_safe_json(payload: object) -> str:
    """JSON, который безопасно вставлять внутрь <script>: имена файлов с устройства — недоверенные данные."""
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


WAITING_TEMPLATE_PATH = Path(__file__).with_name("waiting.html")
TOKEN_PLACEHOLDER = '"__APP_TOKEN__"'


def render_waiting(token: str) -> str:
    """Экран ожидания: его приложение показывает, пока iPhone сканируется или если сканирование не удалось."""
    template = WAITING_TEMPLATE_PATH.read_text(encoding="utf-8")
    if TOKEN_PLACEHOLDER not in template:
        raise ValueError(f"В шаблоне {WAITING_TEMPLATE_PATH.name} нет метки для токена")
    return template.replace(TOKEN_PLACEHOLDER, _to_script_safe_json(token), 1)
