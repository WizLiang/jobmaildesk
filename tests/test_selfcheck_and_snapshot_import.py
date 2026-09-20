"""Installer-facing commands: in-package self-check and data snapshot import."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from job_mail_desk import cli, selfcheck
from job_mail_desk.config import load_settings
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.models import JobTask
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.snapshot_import import SnapshotImportError, import_snapshot


def _task(identifier: str) -> JobTask:
    return JobTask(
        id=identifier,
        application_id="b" * 20,
        company="样例科技",
        role="产品经理",
        recruiting_project=None,
        event_type="assessment",
        stage="在线笔试",
        round=None,
        received_at=datetime(2026, 8, 1, 10, 0, tzinfo=SHANGHAI),
        start_at=datetime(2026, 8, 2, 19, 0, tzinfo=SHANGHAI),
        end_at=None,
        deadline_at=None,
        priority="high",
        status="planned",
        change_type="new",
        source_message_hash="c" * 32,
        research_status="not_queued",
        confidence=0.9,
        title="在线笔试通知",
        action_summary="请按时参加在线笔试",
    )


MAC_ROOT = "/Users/someone/Library/Application Support/JobMailDesk"
MAC_CONFIG = f"""[mail]
provider = "163"
host = "imap.163.com"
port = 993
ssl = true
folder = "INBOX"
lookback_days = 30
poll_minutes = 10

[schedule]
hourly_minute = 0
digest_times = ["08:00", "13:00", "20:00"]
timezone = "Asia/Shanghai"

[obsidian]
enabled = true
output_path = "{MAC_ROOT}/求职硬截止待办集.md"
include_sender = false
include_private_links = false

[research]
enabled = false
queue_path = "{MAC_ROOT}/research-queue.jsonl"

[progress]
enabled = true
output_path = "{MAC_ROOT}/求职当前进展.md"
source_path = "/Users/someone/Documents/Vault/台账.md"

[ui]
width = 480
height = 740
font_scale = 100
always_on_top = true
start_hidden = false

[updates]
enabled = false
channel = "preview"

[reminders]
enabled = true
offsets_minutes = [1440, 120, 30]

[calendar]
enabled = true
name = "JobMailDesk"
"""


@pytest.fixture
def mac_snapshot(tmp_path, monkeypatch) -> Path:
    # Fixture data is isolated; an unrelated live desktop must not affect it.
    # The explicit running-desktop regression below overrides this to True.
    monkeypatch.setattr("job_mail_desk.snapshot_import.desktop_instance_running", lambda: False)
    source = tmp_path / "snapshot"
    source.mkdir()
    (source / "config.toml").write_text(MAC_CONFIG, encoding="utf-8")
    store = MarkdownTaskStore(source / "tasks")
    store.save(_task("a" * 24))
    store.save(_task("b" * 24))
    (source / "applications").mkdir()
    (source / "unresolved").mkdir()
    (source / "digests").mkdir()
    (source / "digests" / "2026-09-16.md").write_text("# digest", encoding="utf-8")
    (source / "dictionaries" / "manual").mkdir(parents=True)
    (source / "dictionaries" / "manual" / "identity-learning.key").write_text("k", encoding="utf-8")
    (source / "state.db").write_bytes(b"sqlite placeholder")
    (source / "activity-state.json").write_text("{}", encoding="utf-8")
    (source / "private-links.json").write_text('{"schema": 1, "links": {}}', encoding="utf-8")
    (source / "求职硬截止待办集.md").write_text("<!-- managed -->", encoding="utf-8")
    (source / "dashboard-cache.json").write_text("{}", encoding="utf-8")
    (source / ".transactions").mkdir()
    (source / ".transactions" / "op1_x").mkdir()
    (source / ".data.lock").write_bytes(b"\0")
    (source / "logs").mkdir()
    (source / "logs" / "jobmaildesk.log").write_text("subject leak", encoding="utf-8")
    (source / "updates").mkdir()
    (source / "updates" / "state.json").write_text("{}", encoding="utf-8")
    return source


def test_import_copies_facts_and_leaves_machine_bound_files_behind(mac_snapshot, tmp_path) -> None:
    destination = tmp_path / "LOCALAPPDATA" / "JobMailDesk"

    report = import_snapshot(mac_snapshot, destination)

    assert report.counts == {"tasks": 2, "applications": 0, "unresolved": 0}
    assert set(report.skipped_entries) == {
        ".transactions", ".data.lock", "logs", "dashboard-cache.json", "updates",
    }
    assert (destination / "tasks").is_dir() and len(list((destination / "tasks").glob("*.md"))) == 2
    assert (destination / "state.db").read_bytes() == b"sqlite placeholder"
    assert (destination / "dictionaries" / "manual" / "identity-learning.key").exists()
    assert (destination / "private-links.json").exists()
    assert not (destination / "logs").exists()
    assert not (destination / ".transactions").exists()
    assert not (destination / "dashboard-cache.json").exists()
    assert report.backup is None
    assert any("private-links.json" in warning for warning in report.warnings)


def test_import_rewrites_macos_paths_into_the_new_root(mac_snapshot, tmp_path) -> None:
    destination = tmp_path / "LOCALAPPDATA" / "JobMailDesk"

    report = import_snapshot(mac_snapshot, destination)
    settings = load_settings(destination / "config.toml")

    assert settings.obsidian_output == destination / "求职硬截止待办集.md"
    assert settings.progress_output == destination / "求职当前进展.md"
    assert settings.research_queue == destination / "research-queue.jsonl"
    # The ledger lived outside the data directory and cannot be mapped: reset, warned.
    assert settings.progress_source is None
    assert any("progress.source_path" in warning for warning in report.warnings)
    assert len(report.config_rewrites) == 4
    # Everything else survives the round trip.
    assert settings.mail_host == "imap.163.com"
    assert settings.lookback_days == 30
    assert settings.calendar_sync_enabled is True
    assert settings.reminder_offsets_minutes == (1440, 120, 30)
    text = (destination / "config.toml").read_text(encoding="utf-8")
    assert "/Users/" not in text
    if os.name == "nt":
        assert "\\\\" in text  # TOML-escaped backslashes


def test_import_refuses_to_overwrite_existing_facts_unless_replace(mac_snapshot, tmp_path) -> None:
    destination = tmp_path / "JobMailDesk"
    MarkdownTaskStore(destination / "tasks").save(_task("e" * 24))

    with pytest.raises(SnapshotImportError, match="已有本地数据"):
        import_snapshot(mac_snapshot, destination)
    assert (destination / "tasks" / ("e" * 24 + ".md")).exists()

    report = import_snapshot(
        mac_snapshot,
        destination,
        replace_existing=True,
        now=datetime(2026, 9, 17, 8, 30),
    )
    assert report.backup == str(tmp_path / "JobMailDesk.bak-20260917-083000")
    assert (Path(report.backup) / "tasks" / ("e" * 24 + ".md")).exists()
    assert report.counts["tasks"] == 2


def test_import_rejects_directories_that_are_not_snapshots(tmp_path) -> None:
    (tmp_path / "junk").mkdir()
    with pytest.raises(SnapshotImportError, match="不像"):
        import_snapshot(tmp_path / "junk", tmp_path / "dest")
    with pytest.raises(SnapshotImportError, match="不存在"):
        import_snapshot(tmp_path / "missing", tmp_path / "dest")


def test_cli_import_snapshot_verifies_expected_counts(mac_snapshot, tmp_path, monkeypatch) -> None:
    destination = tmp_path / "LOCALAPPDATA" / "JobMailDesk"
    monkeypatch.setattr(cli, "LOCAL_ROOT", destination)
    report = tmp_path / "import-report.json"

    code = cli.main([
        "import-snapshot", str(mac_snapshot), "--report", str(report),
        "--expect-tasks", "2", "--expect-applications", "0", "--expect-unresolved", "0",
    ])
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert code == 0 and payload["ok"] is True
    assert payload["counts"] == {"tasks": 2, "applications": 0, "unresolved": 0}

    shutil.rmtree(destination)
    code = cli.main([
        "import-snapshot", str(mac_snapshot), "--report", str(report), "--expect-tasks", "94",
    ])
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert code == 1 and payload["ok"] is False and "94" in payload["error"]


def test_selfcheck_passes_from_source_and_reports_structured_checks() -> None:
    # The .NET/WebView2 import probe belongs to the installer's smoke run
    # against the frozen EXE; loading the CLR inside pytest is not hermetic.
    payload = selfcheck.run_selfcheck()
    names = {check["name"] for check in payload["checks"]}
    assert payload["ok"] is True, [c for c in payload["checks"] if not c["ok"]]
    assert {"identity dictionaries", "identity golden vectors", "fact store roundtrips",
            "zoneinfo Asia/Shanghai", "packaged resources"} <= names
    assert payload["frozen"] is False


def test_selfcheck_counts_facts_under_the_given_root(mac_snapshot, tmp_path) -> None:
    destination = tmp_path / "root"
    import_snapshot(mac_snapshot, destination)
    good = selfcheck.run_selfcheck(expect_counts={"tasks": 2}, local_root=destination)
    bad = selfcheck.run_selfcheck(expect_counts={"tasks": 94}, local_root=destination)
    assert good["ok"] is True
    assert bad["ok"] is False
    failing = [c for c in bad["checks"] if not c["ok"]]
    assert failing and failing[0]["name"] == "fact counts" and "94" in failing[0]["detail"]


def test_cli_smoke_writes_a_report_and_exit_code(tmp_path, monkeypatch) -> None:
    report = tmp_path / "smoke.json"
    assert cli.main(["smoke", "--report", str(report), "--skip-dotnet"]) == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] is True and payload["version"]


def test_selfcheck_keyring_roundtrip_uses_a_probe_entry(monkeypatch) -> None:
    import keyring

    vault: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: vault.__setitem__((s, u), p))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: vault.get((s, u)))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: vault.pop((s, u), None))
    payload = selfcheck.run_selfcheck(keyring_roundtrip=True)
    check = next(c for c in payload["checks"] if c["name"] == "system credential store")
    assert check["ok"] is True
    assert vault == {}  # probe entry cleaned up
    assert all(service == selfcheck.KEYRING_PROBE_SERVICE for service, _ in vault)


REAL_SNAPSHOT = os.environ.get("JOBMAILDESK_SNAPSHOT_DIR")


@pytest.mark.skipif(not REAL_SNAPSHOT, reason="set JOBMAILDESK_SNAPSHOT_DIR to the user-data snapshot")
def test_real_snapshot_imports_with_baseline_counts(tmp_path) -> None:
    destination = tmp_path / "JobMailDesk"
    report = import_snapshot(Path(REAL_SNAPSHOT), destination)
    assert report.counts == {"tasks": 94, "applications": 59, "unresolved": 126}
    settings = load_settings(destination / "config.toml")
    assert settings.obsidian_output == destination / "求职硬截止待办集.md"
    assert settings.progress_output == destination / "求职当前进展.md"
    assert "/Users/" not in (destination / "config.toml").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- review regressions


def test_import_never_copies_destructive_reset_request(mac_snapshot, tmp_path):
    from job_mail_desk.snapshot_import import import_snapshot
    (mac_snapshot / ".privacy-reset.json").write_text('{"schema":1,"parent_pid":12345}')
    (mac_snapshot / ".privacy-reset-complete").write_text("complete")
    (mac_snapshot / "webview2").mkdir()
    (mac_snapshot / "webview2" / "private-cache").write_text("private")
    destination = tmp_path / "reset-safe-import"
    import_snapshot(mac_snapshot, destination)
    assert not (destination / ".privacy-reset.json").exists()
    assert not (destination / ".privacy-reset-complete").exists()
    assert not (destination / "webview2").exists()


def test_replace_backup_is_atomic_or_nothing(mac_snapshot, tmp_path, monkeypatch) -> None:
    """Windows refuses to rename a directory with an open handle inside; the
    live data must then stay untouched instead of degrading to copy+delete."""
    from job_mail_desk import fs_utils, snapshot_import

    destination = tmp_path / "JobMailDesk"
    MarkdownTaskStore(destination / "tasks").save(_task("e" * 24))
    (destination / "applications").mkdir()
    (destination / "applications" / "app-keep.md").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(fs_utils.time, "sleep", lambda _delay: None)

    def refused_rename(src, dst):
        raise PermissionError(5, "Access is denied", str(src))

    monkeypatch.setattr(snapshot_import.os, "rename", refused_rename)

    with pytest.raises(SnapshotImportError, match="整体备份"):
        import_snapshot(mac_snapshot, destination, replace_existing=True)

    assert (destination / "tasks" / ("e" * 24 + ".md")).exists()
    assert (destination / "applications" / "app-keep.md").read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob("JobMailDesk.bak-*"))
    assert not (destination / "state.db").exists()  # nothing from the snapshot was copied


def test_replace_refuses_while_the_desktop_app_is_running(mac_snapshot, tmp_path, monkeypatch) -> None:
    from job_mail_desk import snapshot_import

    destination = tmp_path / "JobMailDesk"
    MarkdownTaskStore(destination / "tasks").save(_task("e" * 24))
    monkeypatch.setattr(snapshot_import, "desktop_instance_running", lambda: True)

    with pytest.raises(SnapshotImportError, match="正在运行"):
        import_snapshot(mac_snapshot, destination, replace_existing=True)
    assert (destination / "tasks" / ("e" * 24 + ".md")).exists()


def test_desktop_instance_probe_is_false_off_windows(monkeypatch) -> None:
    from job_mail_desk import snapshot_import

    monkeypatch.setattr(snapshot_import.sys, "platform", "linux")
    assert snapshot_import.desktop_instance_running() is False


def test_existing_private_links_or_state_db_count_as_facts(mac_snapshot, tmp_path) -> None:
    destination = tmp_path / "JobMailDesk"
    destination.mkdir()
    (destination / "private-links.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SnapshotImportError, match="已有本地数据"):
        import_snapshot(mac_snapshot, destination)
    # A freshly initialised directory (default config + empty folders) is not.
    fresh = tmp_path / "fresh"
    (fresh / "tasks").mkdir(parents=True)
    (fresh / "config.toml").write_text("[mail]\n", encoding="utf-8")
    report = import_snapshot(mac_snapshot, fresh)
    assert report.counts["tasks"] == 2


def test_import_warns_about_uncommitted_transactions_in_the_snapshot(mac_snapshot, tmp_path) -> None:
    pending = mac_snapshot / ".transactions" / "op1_pending"
    pending.mkdir()
    (pending / "prepared").write_text("prepared\n", encoding="ascii")
    report = import_snapshot(mac_snapshot, tmp_path / "dest")
    assert any("op1_pending" in warning for warning in report.warnings)
    assert not (tmp_path / "dest" / ".transactions").exists()


def test_cli_import_snapshot_reports_unexpected_errors_instead_of_tracebacks(mac_snapshot, tmp_path, monkeypatch) -> None:
    from job_mail_desk import cli

    destination = tmp_path / "LOCALAPPDATA" / "JobMailDesk"
    monkeypatch.setattr(cli, "LOCAL_ROOT", destination)

    def boom(*_args, **_kwargs):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(cli, "import_snapshot", boom)
    report = tmp_path / "import-report.json"
    code = cli.main(["import-snapshot", str(mac_snapshot), "--report", str(report)])
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert code == 1 and payload["ok"] is False
    assert payload["error"].startswith("PermissionError") and "cannot access" in payload["error"]


def test_cli_import_snapshot_does_not_open_a_log_file_under_the_data_root(mac_snapshot, tmp_path, monkeypatch) -> None:
    """An open log handle under LOCAL_ROOT would make Windows refuse the backup rename."""
    from job_mail_desk import cli

    destination = tmp_path / "LOCALAPPDATA" / "JobMailDesk"
    monkeypatch.setattr(cli, "LOCAL_ROOT", destination)
    calls: list[str] = []
    monkeypatch.setattr(cli, "configure_logging", lambda: calls.append("configure_logging"))
    assert cli.main(["import-snapshot", str(mac_snapshot), "--report", str(tmp_path / "r.json")]) == 0
    assert calls == []
    assert not (destination / "logs").exists()


def test_frozen_console_twin_defaults_to_help(monkeypatch) -> None:
    from job_mail_desk import cli

    monkeypatch.setattr(cli.sys, "executable", r"C:\Programs\JobMailDesk\JobMailDesk-cli.exe")
    assert cli._frozen_default_command() == ["--help"]
    monkeypatch.setattr(cli.sys, "executable", r"C:\Programs\JobMailDesk\JobMailDesk.exe")
    assert cli._frozen_default_command() == ["ui"]
