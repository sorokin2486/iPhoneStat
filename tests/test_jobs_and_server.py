"""Фоновые задания и HTTP-интерфейс приложения (без настоящего iPhone и без системных окон)."""

import asyncio
import dataclasses
import json
import threading
import urllib.error
import urllib.request

import pytest

from iphonestat import server as server_module
from iphonestat.demo import build_demo_scan
from iphonestat.device import DeviceError
from iphonestat.jobs import STATE_CANCELLED, STATE_DONE, STATE_FAILED, JobBusyError, JobRunner
from iphonestat.server import TOKEN_HEADER, AppServer, DeviceActions
from iphonestat.session import PHASE_FAILED, PHASE_READY, ScanSession

WAIT_SECONDS = 5


def _run_to_end(runner, factory):
    runner.start("export", "Тест", factory)
    runner.wait(WAIT_SECONDS)
    return runner.snapshot()


def test_job_reports_progress_and_final_message(tmp_path):
    async def job(progress):
        progress.set_total(200)
        progress.set_destination(tmp_path)
        progress.add_bytes(50, "/DCIM/a.jpg")
        return "Скопировано файлов: 1."

    status = _run_to_end(JobRunner(), job)

    assert (status.state, status.message, status.percent) == (STATE_DONE, "Скопировано файлов: 1.", 100.0)
    assert (status.done_bytes, status.total_bytes, status.destination) == (50, 200, str(tmp_path))


def test_job_failure_shows_device_error_text():
    async def job(progress):
        raise DeviceError("iPhone не найден.")

    status = _run_to_end(JobRunner(), job)

    assert (status.state, status.message) == (STATE_FAILED, "iPhone не найден.")


def test_unexpected_job_error_is_reported_not_raised():
    async def job(progress):
        raise RuntimeError("boom")

    status = _run_to_end(JobRunner(), job)

    assert status.state == STATE_FAILED
    assert "RuntimeError: boom" in status.message


def test_only_one_job_runs_at_a_time_and_it_can_be_cancelled():
    started = threading.Event()

    async def endless(progress):
        started.set()
        await asyncio.sleep(3600)
        return "не должно завершиться"

    runner = JobRunner()
    runner.start("backup", "Долгое", endless)
    assert started.wait(WAIT_SECONDS)

    with pytest.raises(JobBusyError):
        runner.start("export", "Второе", endless)
    assert runner.cancel() is True
    runner.wait(WAIT_SECONDS)

    assert runner.snapshot().state == STATE_CANCELLED
    assert runner.cancel() is False


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Запущенный сервер с подменёнными операциями устройства и окном выбора папки."""
    calls = {"export": [], "backup": [], "revealed": []}

    async def fake_export(udid, source, folder, progress):
        calls["export"].append((udid, source.path, folder))
        progress.set_destination(folder / "DCIM")
        return "Скопировано файлов: 3."

    async def fake_backup(udid, folder, progress, now):
        calls["backup"].append((udid, folder))
        return "Резервная копия создана."

    monkeypatch.setattr(server_module, "export_source", fake_export)
    monkeypatch.setattr(server_module, "backup_device", fake_backup)
    runner = JobRunner()
    chosen = {"folder": tmp_path}
    scans = {"count": 0, "error": None}

    async def fake_scan():
        scans["count"] += 1
        if scans["error"]:
            raise DeviceError(scans["error"])
        return dataclasses.replace(build_demo_scan(), udid="FAKE-UDID")

    session = ScanSession(fake_scan)
    session.start()
    session.wait(WAIT_SECONDS)
    actions = DeviceActions(
        session,
        runner,
        pick_folder=lambda title: chosen["folder"],
        reveal_folder=lambda folder: calls["revealed"].append(folder),
    )
    app_server = AppServer(lambda token: f"<html>{token}</html>", actions)
    thread = threading.Thread(target=app_server.serve_forever, daemon=True)
    thread.start()
    yield type("App", (), {"server": app_server, "runner": runner, "calls": calls, "chosen": chosen, "session": session, "scans": scans})
    app_server.shutdown()
    app_server.server_close()


def _request(app, path, body=None, token=True, host=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers[TOKEN_HEADER] = app.server.token if token is True else token
    if host:
        headers["Host"] = host
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(app.server.url.rstrip("/") + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=WAIT_SECONDS) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


def test_page_is_served_with_its_token(app):
    status, text = _request(app, "/", token=False)

    assert status == 200
    assert app.server.token in text


def test_export_starts_job_in_folder_chosen_by_user(app, tmp_path):
    status, text = _request(app, "/api/export", {"source": "afc:/DCIM"})
    app.runner.wait(WAIT_SECONDS)

    assert status == 200
    assert json.loads(text)["data"]["started"] is True
    assert app.calls["export"] == [("FAKE-UDID", "/DCIM", tmp_path)]
    job = json.loads(_request(app, "/api/status")[1])["data"]["job"]
    assert (job["state"], job["message"], job["destination"]) == (STATE_DONE, "Скопировано файлов: 3.", str(tmp_path / "DCIM"))


def test_export_does_nothing_when_folder_dialog_is_dismissed(app):
    app.chosen["folder"] = None

    status, text = _request(app, "/api/export", {"source": "afc:/DCIM"})

    assert (status, json.loads(text)["data"]) == (200, {"started": False})
    assert app.calls["export"] == []


def test_export_rejects_address_outside_shared_areas(app):
    status, text = _request(app, "/api/export", {"source": "app:com.example.x:/Library"})

    assert status == 400
    assert json.loads(text)["ok"] is False
    assert app.calls["export"] == []


def test_backup_starts_job_and_reveal_opens_its_folder(app, tmp_path):
    status, _ = _request(app, "/api/backup", {})
    app.runner.wait(WAIT_SECONDS)

    assert status == 200
    assert app.calls["backup"] == [("FAKE-UDID", tmp_path)]
    assert _request(app, "/api/reveal", {})[0] == 409  # у этого задания папка не объявлена

    _request(app, "/api/export", {"source": "afc:/DCIM"})
    app.runner.wait(WAIT_SECONDS)
    assert _request(app, "/api/reveal", {})[0] == 200
    assert app.calls["revealed"] == [tmp_path / "DCIM"]


@pytest.mark.parametrize("token", [False, "wrong-token"])
def test_api_requires_the_page_token(app, token):
    status, text = _request(app, "/api/backup", {}, token=token)

    assert status == 403
    assert json.loads(text)["ok"] is False
    assert app.calls["backup"] == []


def test_requests_for_foreign_host_names_are_refused(app):
    assert _request(app, "/", token=False, host="evil.example:80")[0] == 403
    assert _request(app, "/api/backup", {}, host="evil.example:80")[0] == 403
    assert app.calls["backup"] == []


def test_unknown_paths_and_bad_bodies_get_clear_errors(app):
    assert _request(app, "/api/nope", {})[0] == 404
    assert _request(app, "/other", token=False)[0] == 404
    assert _request(app, "/api/export", ["not", "an", "object"])[0] == 400
    assert _request(app, "/api/cancel", {})[1].count('"cancelled": false') == 1


def test_status_reports_scan_state_and_rescan_starts_a_new_scan(app):
    scan = json.loads(_request(app, "/api/status")[1])["data"]["scan"]
    assert (scan["phase"], scan["revision"]) == (PHASE_READY, 1)

    app.scans["error"] = "iPhone не найден."
    status, text = _request(app, "/api/rescan", {})
    app.session.wait(WAIT_SECONDS)

    assert (status, json.loads(text)["data"]) == (200, {"started": True})
    scan = json.loads(_request(app, "/api/status")[1])["data"]["scan"]
    assert (scan["phase"], scan["message"], scan["revision"]) == (PHASE_FAILED, "iPhone не найден.", 2)


def test_copy_is_refused_until_the_phone_is_scanned(app):
    app.scans["error"] = "iPhone не найден."
    _request(app, "/api/rescan", {})
    app.session.wait(WAIT_SECONDS)

    assert _request(app, "/api/export", {"source": "afc:/DCIM"})[0] == 409
    assert _request(app, "/api/backup", {})[0] == 409
    assert app.calls["export"] == [] and app.calls["backup"] == []


def test_rescan_is_refused_while_a_job_is_running(app, monkeypatch):
    started = threading.Event()

    async def endless(udid, folder, progress, now):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(server_module, "backup_device", endless)
    _request(app, "/api/backup", {})
    assert started.wait(WAIT_SECONDS)

    assert _request(app, "/api/rescan", {})[0] == 409
    assert app.scans["count"] == 1
    app.runner.cancel()
    app.runner.wait(WAIT_SECONDS)


def test_page_render_failure_is_reported_as_server_error(app):
    def broken(token):
        raise RuntimeError("boom")

    app.server.render_page = broken

    assert _request(app, "/", token=False)[0] == 500
