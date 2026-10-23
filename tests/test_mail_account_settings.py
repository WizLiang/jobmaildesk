"""Mailbox form changes use synthetic credentials and never contact IMAP."""

from types import SimpleNamespace
import threading

import pytest

from job_mail_desk import ui_app
from job_mail_desk.config import Settings, load_settings, write_settings
from job_mail_desk.credentials import MailCredential
from job_mail_desk.runtime_control import RuntimeControl


@pytest.fixture
def mailbox_form(tmp_path, monkeypatch):
    state = SimpleNamespace(
        credential=MailCredential("old@example.test", "synthetic-old-code"),
        writes=[], connections=[], exports=[], callbacks=[],
    )

    def read_credential():
        if state.credential is None:
            raise RuntimeError("synthetic credential absent")
        return state.credential

    def save_credential(email, code):
        state.writes.append((email, code))
        state.credential = MailCredential(email, code)

    class Reader:
        def __init__(self, settings, credential):
            state.connections.append((settings, credential))

        def mailbox_snapshot(self):
            return {"unseen": 2}

    monkeypatch.setattr(ui_app, "LOCAL_ROOT", tmp_path)
    monkeypatch.setattr(ui_app, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(ui_app, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(ui_app, "load_credential", read_credential)
    monkeypatch.setattr(ui_app, "save_credential", save_credential)
    monkeypatch.setattr(ui_app, "ImapReader", Reader)
    monkeypatch.setattr(ui_app, "write_settings", lambda settings: write_settings(settings, tmp_path / "config.toml"))
    api = object.__new__(ui_app.DesktopApi)
    api._settings = Settings(obsidian_enabled=False, progress_enabled=False)
    api._runtime_control = RuntimeControl()
    api._scan_lock = api._runtime_control.scan_lock
    api._privacy_reset_lock = threading.Lock()
    api._on_privacy_reset = None
    api._on_settings_saved = state.callbacks.append
    api._export = state.exports.append
    return api, state, tmp_path


@pytest.mark.parametrize("email", ["new@example.test", ""])
def test_changed_or_cleared_account_cannot_silently_keep_old_credentials(mailbox_form, email):
    api, state, root = mailbox_form
    with pytest.raises(ValueError, match="更换邮箱账号"):
        api.save_app_settings({"email": email, "authorization_code": "", "ui_font_scale": 120})
    assert state.writes == state.exports == state.callbacks == []
    assert state.credential.email == "old@example.test"
    assert api._settings.ui_font_scale != 120
    assert not (root / "config.toml").exists()
    assert not api._scan_lock.locked()


def test_same_account_can_keep_saved_code_and_change_local_settings(mailbox_form):
    api, state, root = mailbox_form
    result = api.save_app_settings({"email": " old@example.test ", "ui_font_scale": 120})
    assert state.writes == []
    assert result["email"] == result["active_mail_account"] == "old@example.test"
    assert result["credential_configured"] is True
    assert "authorization_code" not in result
    assert "synthetic-old-code" not in str(result)
    assert load_settings(root / "config.toml").ui_font_scale == 120
    assert len(state.callbacks) == 1


def test_switch_account_with_new_code_persists_the_new_account(mailbox_form):
    api, state, root = mailbox_form
    result = api.save_app_settings({
        "email": "new@example.test", "authorization_code": "synthetic-new-code",
        "mail_provider": "custom", "mail_host": "imap.example.test",
    })
    assert state.writes == [("new@example.test", "synthetic-new-code")]
    assert result["active_mail_account"] == "new@example.test"
    assert load_settings(root / "config.toml").mail_host == "imap.example.test"
    assert state.connections == []


def test_unconfigured_local_app_can_save_without_mailbox(mailbox_form):
    api, state, root = mailbox_form
    state.credential = None
    result = api.save_app_settings({"email": "", "authorization_code": "", "ui_font_scale": 120})
    assert result["active_mail_account"] == ""
    assert result["credential_configured"] is False
    assert state.writes == []
    assert load_settings(root / "config.toml").ui_font_scale == 120


def test_new_account_without_code_is_rejected_before_config_write(mailbox_form):
    api, state, root = mailbox_form
    state.credential = None
    with pytest.raises(ValueError, match="对应的客户端授权码"):
        api.save_app_settings({"email": "new@example.test"})
    assert not (root / "config.toml").exists()
    assert state.writes == []


@pytest.mark.parametrize("email", ["new@example.test", ""])
def test_connection_test_never_sends_old_code_for_different_account(mailbox_form, email):
    api, state, root = mailbox_form
    result = api.test_mail_settings({"email": email, "authorization_code": ""})
    assert result["ok"] is False
    assert "更换邮箱账号" in result["detail"]
    assert state.connections == state.writes == []
    assert not (root / "config.toml").exists()


def test_same_account_connection_test_uses_saved_code_without_writing(mailbox_form):
    api, state, root = mailbox_form
    result = api.test_mail_settings({"email": "old@example.test"})
    assert result["ok"] is True
    assert state.connections[0][1] == state.credential
    assert state.writes == []
    assert not (root / "config.toml").exists()


def test_new_account_connection_test_uses_only_unsaved_new_code(mailbox_form):
    api, state, root = mailbox_form
    result = api.test_mail_settings({
        "email": "new@example.test", "authorization_code": "synthetic-new-code",
        "mail_provider": "custom", "mail_host": "imap.example.test",
    })
    assert result["ok"] is True
    settings, credential = state.connections[0]
    assert settings.mail_host == "imap.example.test"
    assert credential == MailCredential("new@example.test", "synthetic-new-code")
    assert state.credential.email == "old@example.test"
    assert state.writes == []
    assert not (root / "config.toml").exists()


def test_scan_in_progress_rejects_settings_before_touching_credentials(mailbox_form):
    api, state, root = mailbox_form
    api._scan_lock.acquire()
    try:
        with pytest.raises(ValueError, match="当前修改尚未保存"):
            api.save_app_settings({"email": "new@example.test", "authorization_code": "synthetic-new-code"})
        assert api._scan_lock.locked()
        assert state.writes == state.callbacks == []
        assert not (root / "config.toml").exists()
    finally:
        api._scan_lock.release()


@pytest.mark.parametrize("code", ["short", "invalid code with spaces"])
def test_invalid_new_code_is_rejected_before_persistence_or_network(mailbox_form, code):
    api, state, root = mailbox_form
    payload = {"email": "new@example.test", "authorization_code": code}
    with pytest.raises(ValueError):
        api.save_app_settings(payload)
    assert api.test_mail_settings(payload)["ok"] is False
    assert state.writes == state.connections == []
    assert not (root / "config.toml").exists()


def test_failed_config_write_does_not_change_credentials(mailbox_form, monkeypatch):
    api, state, root = mailbox_form
    config = root / "config.toml"
    original = b"# exact original config\r\n[mail]\r\nprovider = 'qq'\r\n"
    config.write_bytes(original)
    prior_settings = api._settings

    def broken_write(_settings):
        config.write_bytes(b"partial write")
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(ui_app, "write_settings", broken_write)
    with pytest.raises(RuntimeError, match="已恢复原配置"):
        api.save_app_settings({"email": "new@example.test", "authorization_code": "synthetic-new-code"})
    assert config.read_bytes() == original
    assert state.writes == state.callbacks == state.exports == []
    assert state.credential.email == "old@example.test"
    assert api._settings is prior_settings
    assert not api._scan_lock.locked()
    assert not list(root.glob(".config-mail-settings-*.tmp"))


@pytest.mark.parametrize("existing_config", [False, True])
def test_failed_credential_write_restores_config_without_reading_old_keyring(
    mailbox_form, monkeypatch, existing_config,
):
    api, state, root = mailbox_form
    config = root / "config.toml"
    original = b"# user comment retained\r\n[mail]\r\nprovider = 'qq'\r\n"
    if existing_config:
        config.write_bytes(original)
    prior_settings = api._settings
    saved_attempts = []

    def inaccessible_old_credential():
        raise AssertionError("must not read old credentials when a new code was supplied")

    def failed_secure_write(email, code):
        assert load_settings(config).mail_host == "imap.example.test"
        saved_attempts.append((email, code))
        raise OSError("synthetic credential store unavailable")

    monkeypatch.setattr(ui_app, "load_credential", inaccessible_old_credential)
    monkeypatch.setattr(ui_app, "save_credential", failed_secure_write)
    with pytest.raises(RuntimeError, match="已恢复原配置"):
        api.save_app_settings({
            "email": "new@example.test", "authorization_code": "synthetic-new-code",
            "mail_provider": "custom", "mail_host": "imap.example.test",
        })
    assert saved_attempts == [("new@example.test", "synthetic-new-code")]
    if existing_config:
        assert config.read_bytes() == original
    else:
        assert not config.exists()
    assert state.callbacks == state.exports == []
    assert state.credential.email == "old@example.test"
    assert api._settings is prior_settings
    assert not api._scan_lock.locked()
    assert not list(root.glob(".config-mail-settings-*.tmp"))


def test_failed_config_restore_keeps_exact_recovery_copy(mailbox_form, monkeypatch):
    from job_mail_desk import mail_settings

    api, state, root = mailbox_form
    config = root / "config.toml"
    original = b"# recovery source\r\n[mail]\r\nprovider = 'qq'\r\n"
    config.write_bytes(original)
    prior_settings = api._settings

    def failed_secure_write(_email, _code):
        raise OSError("synthetic secure write failure")

    original_replace = mail_settings.os.replace

    def failed_restore(source, target):
        if str(source).replace("\\", "/").rsplit("/", 1)[-1].startswith(".config-mail-settings-"):
            raise PermissionError("synthetic replace denied")
        return original_replace(source, target)

    monkeypatch.setattr(ui_app, "save_credential", failed_secure_write)
    monkeypatch.setattr(mail_settings.os, "replace", failed_restore)
    with pytest.raises(RuntimeError, match="无法恢复原配置"):
        api.save_app_settings({"email": "new@example.test", "authorization_code": "synthetic-new-code"})
    backups = list(root.glob(".config-mail-settings-*.tmp"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert b"synthetic-new-code" not in backups[0].read_bytes()
    assert state.writes == state.callbacks == state.exports == []
    assert api._settings is prior_settings
    assert not api._scan_lock.locked()


def test_failed_export_after_save_keeps_new_scheduler_and_returns_warning(mailbox_form):
    api, state, root = mailbox_form

    def failed_export(_store):
        assert state.callbacks == [api._settings]
        assert api._settings.mail_host == "imap.example.test"
        raise OSError("synthetic-new-code must never appear in the warning")

    api._export = failed_export
    result = api.save_app_settings({
        "email": "new@example.test", "authorization_code": "synthetic-new-code",
        "mail_provider": "custom", "mail_host": "imap.example.test",
    })
    assert result["active_mail_account"] == "new@example.test"
    assert "设置已保存" in result["save_warning"] and "导出文档" in result["save_warning"]
    assert "synthetic-new-code" not in result["save_warning"]
    assert load_settings(root / "config.toml").mail_host == "imap.example.test"
    assert not api._runtime_control.stopping
    assert not api._scan_lock.locked()


def test_failed_completion_marker_cleanup_cannot_prevent_new_scheduler(mailbox_form, monkeypatch):
    from pathlib import Path

    api, state, root = mailbox_form
    marker = root / ".privacy-reset-complete"
    marker.write_text("synthetic", encoding="utf-8")
    original_unlink = Path.unlink

    def denied_marker_unlink(path, *args, **kwargs):
        if path == marker:
            assert state.callbacks == [api._settings]
            raise PermissionError("synthetic-new-code must never appear in the warning")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied_marker_unlink)
    result = api.save_app_settings({"email": "new@example.test", "authorization_code": "synthetic-new-code"})
    assert result["active_mail_account"] == "new@example.test"
    assert "设置已保存" in result["save_warning"] and "清理" in result["save_warning"]
    assert "synthetic-new-code" not in result["save_warning"]
    assert len(state.exports) == 1
    assert not api._runtime_control.stopping
    assert not api._scan_lock.locked()


def test_failed_scheduler_after_commit_blocks_old_scan_and_requests_restart(mailbox_form):
    api, state, root = mailbox_form

    def failed_runtime_apply(settings):
        state.callbacks.append(settings)
        assert api._settings is settings
        raise RuntimeError("synthetic-new-code must never appear in the warning")

    api._on_settings_saved = failed_runtime_apply
    result = api.save_app_settings({
        "email": "new@example.test", "authorization_code": "synthetic-new-code",
        "mail_provider": "custom", "mail_host": "imap.example.test",
    })
    assert result["active_mail_account"] == "new@example.test"
    assert "设置已保存" in result["save_warning"] and "重新启动" in result["save_warning"]
    assert "synthetic-new-code" not in result["save_warning"]
    assert api._runtime_control.stopping
    assert load_settings(root / "config.toml").mail_host == api._settings.mail_host == "imap.example.test"
    assert state.credential.email == "new@example.test"
    assert state.exports == []
    assert not api._scan_lock.locked()


def test_old_scan_waiting_for_lock_cannot_pair_old_settings_with_new_account(mailbox_form, monkeypatch):
    from job_mail_desk import scheduler

    api, state, _root = mailbox_form
    paused = threading.Event()
    resume = threading.Event()
    real_lock = threading.Lock()
    thread_errors = []
    credential_reads = []
    scanned = []

    class PausedWorkerLock:
        def acquire(self, *args, **kwargs):
            if threading.current_thread().name == "synthetic-old-scan":
                paused.set()
                if not resume.wait(10):
                    raise AssertionError("synthetic worker was not resumed")
            return real_lock.acquire(*args, **kwargs)

        def release(self):
            real_lock.release()

    api._scan_lock = api._runtime_control.scan_lock = PausedWorkerLock()
    old_scheduler = scheduler.create_background_scheduler(api._settings, api._runtime_control)
    old_jobs = old_scheduler.get_job("mail-poll").func.__self__

    def read_synthetic_credential():
        credential_reads.append(state.credential.email)
        return state.credential

    def fake_scan(settings, **kwargs):
        scanned.append((settings.mail_host, state.credential.email))
        return SimpleNamespace(urgent=0, to_dict=lambda: {})

    monkeypatch.setattr(scheduler, "load_credential", read_synthetic_credential)
    monkeypatch.setattr(scheduler, "scan_once", fake_scan)
    monkeypatch.setattr(scheduler, "UnresolvedStore", lambda _path: object())
    monkeypatch.setattr(scheduler, "pending_snapshot", lambda _store: {})
    monkeypatch.setattr(scheduler, "new_pending_reviews", lambda *_args: [])
    monkeypatch.setattr(scheduler, "notify_urgent", lambda _urgent: None)

    def replace_scheduled_jobs(updated):
        state.callbacks.append(updated)
        scheduler.retire_background_scheduler(old_scheduler)

    api._on_settings_saved = replace_scheduled_jobs

    def old_worker():
        try:
            old_jobs.scan()
        except Exception as exc:
            thread_errors.append(exc)

    worker = threading.Thread(target=old_worker, name="synthetic-old-scan", daemon=True)
    worker.start()
    try:
        assert paused.wait(5)
        saved = api.save_app_settings({
            "email": "new@example.test", "authorization_code": "synthetic-new-code",
            "mail_provider": "custom", "mail_host": "imap.example.test",
        })
        assert saved["active_mail_account"] == "new@example.test"
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert thread_errors == []
    assert credential_reads == scanned == []
    old_jobs.scan()  # A queued old callback must also remain inert.
    assert credential_reads == scanned == []
    scheduler.ScheduledJobs(api._settings, api._runtime_control).scan()
    assert credential_reads == ["new@example.test"]
    assert scanned == [("imap.example.test", "new@example.test")]


def test_retired_jobs_do_not_read_credentials_or_run_local_callbacks(monkeypatch):
    from job_mail_desk import scheduler

    jobs = scheduler.ScheduledJobs(Settings(calendar_sync_enabled=True))
    unexpected_calls = []

    def unexpected(*_args, **_kwargs):
        unexpected_calls.append(True)
        raise AssertionError("retired callback must be inert")

    monkeypatch.setattr(scheduler, "load_credential", unexpected)
    monkeypatch.setattr(scheduler, "MarkdownTaskStore", unexpected)
    monkeypatch.setattr(scheduler, "StateStore", unexpected)
    jobs.retire()
    jobs.scan()
    jobs.digest("morning")
    jobs.reminders()
    jobs.calendar_sync()
    assert unexpected_calls == []
