from datetime import datetime, timedelta
from dataclasses import replace
from pathlib import Path
import threading

import pytest

from job_mail_desk import scanner, ui_app
from job_mail_desk.config import Settings, load_settings, settings_from_payload, write_settings
from job_mail_desk.credentials import MailCredential
from job_mail_desk.mail_reader import ImapReader
from job_mail_desk.runtime_control import RuntimeControl, RuntimeStopping
from job_mail_desk.scan_progress import ScanProgress


def summary(**changes):
    return scanner.ScanSummary(**dict(
        fetched=0, skipped=0, candidates=0, tasks_updated=0, parse_failed=0,
        research_queued=0, urgent=0, exported=0, shadow=False,
    ) | changes)


@pytest.mark.parametrize("error,stage", [(RuntimeError("secret-placeholder"), "error"),
                                         (RuntimeStopping("secret-placeholder"), "cancelled")])
def test_early_failures_end_progress_without_exposing_exception(monkeypatch, error, stage):
    runtime = RuntimeControl()
    def fail(*args, **kwargs):
        assert runtime.scan_progress.snapshot()["running"]
        raise error
    monkeypatch.setattr(scanner, "_scan_once_impl", fail)
    with pytest.raises(type(error)):
        scanner.scan_once(Settings(), runtime_control=runtime)
    snapshot = runtime.scan_progress.snapshot()
    assert snapshot["stage"] == stage
    assert not snapshot["running"]
    assert "secret-placeholder" not in str(snapshot)


@pytest.mark.parametrize("failures,stage", [(0, "done"), (2, "partial")])
def test_empty_and_partial_runs_finish_and_reset_for_next_scan(monkeypatch, failures, stage):
    runtime = RuntimeControl()
    monkeypatch.setattr(scanner, "_scan_once_impl", lambda *a, **k: summary(fetch_failed=failures))
    scanner.scan_once(Settings(), runtime_control=runtime)
    first = runtime.scan_progress.snapshot()
    assert first["stage"] == stage and not first["running"]
    runtime.scan_progress.report("reading", 12, 20, lookback_days=30)
    scanner.scan_once(Settings(), runtime_control=runtime)
    second = runtime.scan_progress.snapshot()
    assert second["run_id"] == first["run_id"] + 1
    assert second["completed"] == 0 and second["total"] is None
    assert second["lookback_days"] is None


def test_imap_and_parser_progress_counts_attempts_without_private_fields(tmp_path, monkeypatch):
    runtime = RuntimeControl()
    history = []
    original_report = runtime.scan_progress.report
    def report(*args, **kwargs):
        original_report(*args, **kwargs)
        history.append(runtime.scan_progress.snapshot())
    monkeypatch.setattr(runtime.scan_progress, "report", report)
    raw = b"Subject: Ordinary newsletter\r\nFrom: fictional@example.invalid\r\n\r\nHello"

    class Client:
        def login(self, *args): return "OK", []
        def select(self, folder, readonly):
            assert readonly
            return "OK", []
        def response(self, name): return "OK", [b"1"]
        def logout(self): return "BYE", []
        def shutdown(self): pass
        def uid(self, command, *args):
            if command == "search":
                assert runtime.scan_progress.snapshot()["stage"] == "searching"
                return "OK", [b"1 2 3"]
            uid = int(args[0])
            assert "BODY.PEEK" in args[1]
            snapshot = runtime.scan_progress.snapshot()
            assert (snapshot["stage"], snapshot["completed"], snapshot["total"]) == ("reading", uid - 1, 3)
            if uid == 2: return "NO", []
            date = datetime.now(scanner.SHANGHAI) - timedelta(days=40 if uid == 3 else 1)
            stamp = date.strftime("%d-%b-%Y %H:%M:%S %z")
            header = f'1 (UID {uid} INTERNALDATE "{stamp}" RFC822.SIZE {len(raw)} BODY[] {{{len(raw)}}}'.encode()
            return "OK", [(header, raw), b")"]

    monkeypatch.setattr(ImapReader, "_client", lambda self: Client())
    monkeypatch.setattr(scanner, "load_credential", lambda: MailCredential("fictional@example.invalid", "dummy-secret"))
    monkeypatch.setattr(scanner, "ensure_directories", lambda: None)
    for name, path in {"TASKS_DIR": "tasks", "STATE_DB": "state.db", "DASHBOARD_FILE": "dashboard.md", "DICTIONARIES_DIR": "dict"}.items():
        monkeypatch.setattr(scanner, name, tmp_path / path)
    result = scanner.scan_once(Settings(progress_enabled=False), runtime_control=runtime)
    assert result.fetched == 1 and result.fetch_failed == 1
    assert any(row["stage"] == "reading" and row["completed"] == 3 for row in history)
    assert any(row["stage"] == "parsing" and row["completed"] == 1 and row["total"] == 1 for row in history)
    assert history[-1]["stage"] == "partial" and not history[-1]["running"]
    assert set(history[-1]) == {"run_id", "revision", "stage", "running", "completed", "total", "lookback_days"}
    assert "dummy-secret" not in str(history) and "fictional" not in str(history)


def test_progress_bridge_reads_while_scan_lock_is_held():
    runtime = RuntimeControl()
    api = object.__new__(ui_app.DesktopApi)
    api._runtime_control = runtime
    ready = threading.Event()
    result = []
    def read():
        result.append(api.get_scan_progress())
        ready.set()
    with runtime.scan_lock:
        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        assert ready.wait(1), "Progress bridge blocked behind a scan"
    worker.join(1)
    result[0]["stage"] = "changed"
    assert runtime.scan_progress.snapshot()["stage"] == "idle"


def test_legacy_update_settings_cannot_enable_removed_component(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[updates]\nenabled = true\nchannel = "preview"\n', encoding="utf-8")
    assert load_settings(config).updates_enabled is False
    assert settings_from_payload(Settings(), {"updates_enabled": True}).updates_enabled is False
    write_settings(replace(Settings(), updates_enabled=True), config)
    assert load_settings(config).updates_enabled is False
    for name in ("get_update_status", "check_for_updates", "maybe_check_for_updates", "open_update_release"):
        assert not hasattr(ui_app.DesktopApi, name)
    root = Path(ui_app.__file__).parent
    assert not (root / "updates.py").exists()
    for filename in ("index.html", "app.js"):
        text = (root / "ui" / filename).read_text(encoding="utf-8")
        assert "github" not in text.lower() and "checkForUpdates" not in text
