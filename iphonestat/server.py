"""Локальный веб-сервер приложения: отдаёт страницу с картой и принимает команды «скопировать» и «резервная копия».

Сервер слушает только 127.0.0.1. Каждый запрос к /api должен нести секретный токен из страницы и
обращаться по локальному адресу — так посторонний сайт в браузере не сможет запустить копирование.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from iphonestat import dialogs
from iphonestat.dialogs import DialogBusyError
from iphonestat.jobs import JobBusyError, JobRunner
from iphonestat.transfer import backup_device, export_source, parse_source

logger = logging.getLogger(__name__)

LOCAL_HOST = "127.0.0.1"
TOKEN_HEADER = "X-IPhoneStat-Token"
MAX_BODY_BYTES = 16 * 1024
JOB_EXPORT = "export"
JOB_BACKUP = "backup"


class ApiError(Exception):
    """Ошибка запроса с кодом HTTP и текстом для пользователя."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class DeviceActions:
    """Команды, доступные странице. Папку назначения выбирает сам пользователь в системном окне."""

    def __init__(
        self,
        udid: str,
        runner: JobRunner,
        pick_folder: Callable[[str], Path | None] = dialogs.pick_folder,
        reveal_folder: Callable[[Path], None] = dialogs.reveal_folder,
    ) -> None:
        self._udid = udid
        self._runner = runner
        self._pick_folder = pick_folder
        self._reveal_folder = reveal_folder

    def status(self) -> dict[str, Any]:
        snapshot = self._runner.snapshot()
        return {"job": snapshot.to_dict() if snapshot else None}

    def start_export(self, source_text: Any) -> dict[str, Any]:
        try:
            source = parse_source(source_text)
        except ValueError as error:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Этот объект нельзя скопировать: {error}.") from error
        folder = self._choose_folder(f"Куда скопировать «{source.folder_name}»?")
        if folder is None:
            return {"started": False}
        return self._start(
            JOB_EXPORT,
            f"Копирование «{source.folder_name}»",
            lambda progress: export_source(self._udid, source, folder, progress),
        )

    def start_backup(self) -> dict[str, Any]:
        folder = self._choose_folder("Где сохранить резервную копию iPhone?")
        if folder is None:
            return {"started": False}
        return self._start(
            JOB_BACKUP,
            "Резервная копия iPhone",
            lambda progress: backup_device(self._udid, folder, progress, datetime.now()),
        )

    def cancel(self) -> dict[str, Any]:
        return {"cancelled": self._runner.cancel()}

    def reveal(self) -> dict[str, Any]:
        snapshot = self._runner.snapshot()
        if snapshot is None or not snapshot.destination:
            raise ApiError(HTTPStatus.CONFLICT, "Пока нечего открывать.")
        try:
            self._reveal_folder(Path(snapshot.destination))
        except OSError as error:
            raise ApiError(HTTPStatus.CONFLICT, str(error)) from error
        return {"revealed": True}

    def _choose_folder(self, title: str) -> Path | None:
        if self._runner.is_running():
            raise ApiError(HTTPStatus.CONFLICT, "Дождитесь завершения текущей операции или отмените её.")
        try:
            return self._pick_folder(title)
        except DialogBusyError as error:
            raise ApiError(HTTPStatus.CONFLICT, str(error)) from error
        except OSError as error:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, str(error)) from error

    def _start(self, kind: str, title: str, factory: Any) -> dict[str, Any]:
        try:
            status = self._runner.start(kind, title, factory)
        except JobBusyError as error:
            raise ApiError(HTTPStatus.CONFLICT, str(error)) from error
        return {"started": True, "job": status.to_dict()}


class AppServer(ThreadingHTTPServer):
    """HTTP-сервер приложения; порт выбирается системой, токен создаётся на каждый запуск."""

    daemon_threads = True

    def __init__(self, render_page: Callable[[str], str], actions: DeviceActions, port: int = 0) -> None:
        super().__init__((LOCAL_HOST, port), _Handler)
        self.token = secrets.token_urlsafe(32)
        self.actions = actions
        self.page = render_page(self.token).encode("utf-8")

    @property
    def url(self) -> str:
        return f"http://{LOCAL_HOST}:{self.server_address[1]}/"

    def allowed_hosts(self) -> frozenset[str]:
        port = self.server_address[1]
        return frozenset({f"{LOCAL_HOST}:{port}", f"localhost:{port}"})


class _Handler(BaseHTTPRequestHandler):
    server: AppServer

    def do_GET(self) -> None:
        if not self._host_is_local():
            self._send_error(HTTPStatus.FORBIDDEN, "Доступ только с этого компьютера.")
        elif self.path == "/":
            self._send(HTTPStatus.OK, self.server.page, "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._handle_api(lambda _body: self.server.actions.status())
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "Страница не найдена.")

    def do_POST(self) -> None:
        actions = self.server.actions
        routes: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "/api/export": lambda body: actions.start_export(body.get("source")),
            "/api/backup": lambda _body: actions.start_backup(),
            "/api/cancel": lambda _body: actions.cancel(),
            "/api/reveal": lambda _body: actions.reveal(),
        }
        if not self._host_is_local():
            self._send_error(HTTPStatus.FORBIDDEN, "Доступ только с этого компьютера.")
        elif self.path in routes:
            self._handle_api(routes[self.path])
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "Команда не найдена.")

    def _handle_api(self, action: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        try:
            self._require_token()
            data = action(self._read_json_body())
        except ApiError as error:
            self._send_error(error.status, error.message)
        except Exception:  # сервер не должен падать из-за одного запроса
            logger.exception("Ошибка при обработке %s", self.path)
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Внутренняя ошибка приложения.")
        else:
            self._send_json(HTTPStatus.OK, {"ok": True, "data": data, "error": None})

    def _host_is_local(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts()

    def _require_token(self) -> None:
        supplied = self.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied.encode("utf-8"), self.server.token.encode("utf-8")):
            raise ApiError(HTTPStatus.FORBIDDEN, "Страница устарела — обновите её.")

    def _read_json_body(self) -> dict[str, Any]:
        if self.command != "POST":
            return {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as error:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Некорректный запрос.") from error
        if length < 0 or length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Слишком большой запрос.")
        if length == 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError) as error:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Некорректный запрос.") from error
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Некорректный запрос.")
        return body

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"ok": False, "data": None, "error": message})

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s — %s", self.address_string(), format % args)
