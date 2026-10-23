import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from job_mail_desk import privacy_reset as reset
from job_mail_desk.config import Settings


@pytest.fixture
def vault(monkeypatch):
    values = {target: "synthetic" for target in reset.CREDENTIAL_TARGETS}
    monkeypatch.setattr(reset.credentials.keyring, "get_password",
                        lambda service, user: values.get((service, user)))
    monkeypatch.setattr(reset.credentials.keyring, "delete_password",
                        lambda service, user: values.pop((service, user)))
    return values


def pending(root, local_files=()):
    (root / reset.PENDING).write_text(json.dumps({
        "schema": 1, "parent_pid": 12345, "local_files": list(local_files),
    }), encoding="utf-8")


def finish(root, monkeypatch):
    monkeypatch.setattr(reset, "wait_for_parent", lambda pid: None)
    return reset.finish_pending_reset(root, lambda: (None, True), lambda handle: None)


def test_reset_erases_all_owned_data_and_all_three_credentials(tmp_path, monkeypatch, vault):
    root = tmp_path / "app"
    root.mkdir()
    pending(root, ["custom-progress.md", "exports/nested.md"])
    (root / "exports").mkdir()
    (root / "exports" / "nested.md").write_text("private")
    for directory in ("tasks", "applications", "unresolved", "digests", "logs",
                      "dictionaries", "webview2", ".transactions"):
        (root / directory).mkdir()
        (root / directory / "private.txt").write_text("private")
    for name in ("state.db", "state.db-wal", "state.db-shm", "private-links.json",
                 "activity-state.json", "config.toml", "custom-progress.md", "Custom.ics",
                 ".private-links-abcd.tmp", "config.toml.tmp", ".config-mail-settings-fixture.tmp"):
        (root / name).write_text("private")
    external = tmp_path / "obsidian.md"
    external.write_text("keep")
    unrelated = root / "my-unrelated-file.txt"
    unrelated.write_text("keep")
    assert finish(root, monkeypatch)
    assert not vault
    assert {p.name for p in root.iterdir()} == {reset.COMPLETE, ".data.lock", unrelated.name, "exports"}
    assert not list((root / "exports").iterdir())
    assert external.read_text() == "keep"
    assert not finish(root, monkeypatch)


def test_credential_failure_keeps_request_and_data_for_retry(tmp_path, monkeypatch, vault):
    pending(tmp_path)
    data = tmp_path / "private-links.json"
    data.write_text("encrypted")
    def denied(*args):
        raise reset.credentials.KeyringError("sensitive backend detail")
    monkeypatch.setattr(reset.credentials.keyring, "delete_password", denied)
    with pytest.raises(reset.PrivacyResetError, match="无法清除系统凭据") as error:
        finish(tmp_path, monkeypatch)
    assert "sensitive" not in str(error.value)
    assert data.exists() and (tmp_path / reset.PENDING).exists()
    assert not (tmp_path / reset.COMPLETE).exists()


def test_file_failure_is_not_success_and_retry_finishes(tmp_path, monkeypatch, vault):
    pending(tmp_path)
    data = tmp_path / "state.db"
    data.write_text("private")
    remove = reset._remove_owned
    monkeypatch.setattr(reset.time, "sleep", lambda seconds: None)
    def blocked(path):
        raise PermissionError("locked")
    monkeypatch.setattr(reset, "_remove_owned", blocked)
    with pytest.raises(reset.PrivacyResetError, match="未完成"):
        finish(tmp_path, monkeypatch)
    assert (tmp_path / reset.PENDING).exists()
    assert not vault
    monkeypatch.setattr(reset, "_remove_owned", remove)
    assert finish(tmp_path, monkeypatch)
    assert not data.exists()


def test_parent_exit_and_single_instance_are_required_before_erase(tmp_path, monkeypatch, vault):
    pending(tmp_path)
    def waiting(pid):
        raise reset.PrivacyResetError("still running")
    monkeypatch.setattr(reset, "wait_for_parent", waiting)
    with pytest.raises(reset.PrivacyResetError, match="still running"):
        reset.finish_pending_reset(tmp_path, lambda: (None, True), lambda handle: None)
    monkeypatch.setattr(reset, "wait_for_parent", lambda pid: None)
    with pytest.raises(reset.PrivacyResetError, match="另一个"):
        reset.finish_pending_reset(tmp_path, lambda: (None, False), lambda handle: None)
    assert len(vault) == 3
    assert (tmp_path / reset.PENDING).exists()


@pytest.mark.parametrize("name", ["../outside.md", "..", ".data.lock", reset.PENDING])
def test_invalid_request_never_deletes(tmp_path, monkeypatch, vault, name):
    pending(tmp_path, [name])
    with pytest.raises(reset.PrivacyResetError, match="请求损坏"):
        finish(tmp_path, monkeypatch)
    assert len(vault) == 3


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reset flow")
def test_request_requires_confirmation_and_records_only_local_exports(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(reset.subprocess, "Popen", lambda *args, **kw: calls.append((args, kw)))
    settings = Settings(obsidian_output=tmp_path.parent / "outside.md",
                        progress_output=tmp_path / "custom.md")
    with pytest.raises(reset.PrivacyResetError, match="确认"):
        reset.request_reset(tmp_path, "yes", settings)
    assert not calls and not (tmp_path / reset.PENDING).exists()
    reset.request_reset(tmp_path, "清除", settings)
    request = json.loads((tmp_path / reset.PENDING).read_text(encoding="utf-8"))
    assert request["local_files"] == ["custom.md"]
    assert calls[0][1]["creationflags"] == subprocess.CREATE_NO_WINDOW
    with pytest.raises(reset.PrivacyResetError, match="请求已存在"):
        reset.request_reset(tmp_path, "清除", settings)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reset flow")
def test_spawn_failure_rolls_back_request(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("cannot start")
    monkeypatch.setattr(reset.subprocess, "Popen", fail)
    with pytest.raises(reset.PrivacyResetError, match="尚未删除"):
        reset.request_reset(tmp_path, "清除", Settings())
    assert not (tmp_path / reset.PENDING).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_nested_junction_is_unlinked_without_touching_external_data(tmp_path, monkeypatch, vault):
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("keep")
    root = tmp_path / "app"
    (root / "tasks").mkdir(parents=True)
    junction = root / "tasks" / "linked"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                   check=True, capture_output=True)
    pending(root)
    assert finish(root, monkeypatch)
    assert (external / "keep.txt").read_text() == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles")
def test_windows_wait_rejects_live_process_then_accepts_exited_process():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"],
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with pytest.raises(reset.PrivacyResetError, match="仍未退出"):
            reset.wait_for_parent(process.pid, timeout=0.01)
        process.wait(timeout=5)
        reset.wait_for_parent(process.pid, timeout=1)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()


def test_cli_blocks_non_ui_commands_before_config_or_scan(tmp_path, monkeypatch):
    from job_mail_desk import cli
    pending(tmp_path)
    monkeypatch.setattr(cli, "LOCAL_ROOT", tmp_path)
    def forbidden():
        pytest.fail("logging must not start while reset is pending")
    monkeypatch.setattr(cli, "configure_logging", forbidden)
    assert cli.main(["scan", "--once"]) == 1
    assert cli.main(["configure"]) == 1


def test_pending_request_blocks_other_writers(tmp_path):
    from job_mail_desk.data_lock import data_directory_lease, DataLockBusyError
    pending(tmp_path)
    with pytest.raises(DataLockBusyError, match="正在清除"):
        with data_directory_lease(tmp_path / ".data.lock"):
            pytest.fail("writer entered during reset")


def test_desktop_request_stops_runtime_and_schedules_exit(monkeypatch):
    from job_mail_desk.ui_app import DesktopApi
    from job_mail_desk.runtime_control import RuntimeControl, RuntimeStopping
    events = []
    monkeypatch.setattr(reset, "request_reset", lambda *args: events.append("requested"))
    monkeypatch.setattr("job_mail_desk.ui_app.threading.Timer", lambda delay, fn:
                        SimpleNamespace(start=lambda: fn(), daemon=False))
    api = object.__new__(DesktopApi)
    import threading
    api._privacy_reset_lock = threading.Lock()
    api._github_updater = SimpleNamespace(snapshot=lambda: {"busy": False})
    api._runtime_control = RuntimeControl()
    api._settings = Settings()
    api._on_privacy_reset = lambda: events.append("exit")
    assert api.clear_personal_information("清除") == {"status": "pending"}
    assert events == ["requested", "exit"] and api._runtime_control.stopping
    with pytest.raises(RuntimeStopping):
        api.clear_personal_information("清除")
