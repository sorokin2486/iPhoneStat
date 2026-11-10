import subprocess
import sys

import pytest

from iphonestat import __main__ as entry
from iphonestat import dialogs
from iphonestat.dialogs import PICK_FOLDER_FLAG, pick_folder, picker_command, reveal_folder


def _fake_run(stdout=b"", returncode=0, stderr=b""):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    return run


def test_picker_command_runs_module_from_sources():
    assert picker_command("Куда?") == [sys.executable, "-m", "iphonestat", PICK_FOLDER_FLAG, "Куда?"]


def test_picker_command_runs_exe_itself_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert picker_command("Куда?") == [sys.executable, PICK_FOLDER_FLAG, "Куда?"]


def test_pick_folder_returns_chosen_existing_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(dialogs.subprocess, "run", _fake_run(stdout=str(tmp_path).encode("utf-8")))

    assert pick_folder("Куда?") == tmp_path


@pytest.mark.parametrize("stdout", [b"", b"Z:/no/such/folder/anywhere"])
def test_pick_folder_returns_none_when_nothing_usable_was_chosen(stdout, monkeypatch):
    monkeypatch.setattr(dialogs.subprocess, "run", _fake_run(stdout=stdout))

    assert pick_folder("Куда?") is None


def test_pick_folder_reports_dialog_that_failed_to_open(monkeypatch):
    monkeypatch.setattr(dialogs.subprocess, "run", _fake_run(returncode=1, stderr=b"no display"))

    with pytest.raises(OSError, match="Не удалось открыть окно"):
        pick_folder("Куда?")


def test_reveal_folder_refuses_missing_folder(tmp_path):
    with pytest.raises(OSError, match="Папка не найдена"):
        reveal_folder(tmp_path / "missing")


def test_pick_folder_mode_prints_chosen_path_and_skips_scanning(tmp_path, monkeypatch, capfdbinary):
    monkeypatch.setattr(entry, "show_folder_dialog", lambda title: str(tmp_path))
    monkeypatch.setattr(entry, "run", lambda args: pytest.fail("сканирование не должно запускаться"))

    exit_code = entry.main([PICK_FOLDER_FLAG, "Куда?"])

    assert exit_code == 0
    assert capfdbinary.readouterr().out.decode("utf-8") == str(tmp_path)
