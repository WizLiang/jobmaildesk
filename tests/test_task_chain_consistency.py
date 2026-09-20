"""Task cards, application chains and follow-up mails must stay consistent.

Covers the three user-reported drifts (completing/rescheduling a task did not
reach the application chain; a follow-up mail could only add a second task)
plus the performance fixes that made every click take seconds.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from job_mail_desk import frontmatter_cache, scan_alerts, ui_app
from job_mail_desk.application_lifecycle import (
    reconcile_application,
    sync_application_progress_from_task,
)
from job_mail_desk.application_registry import (
    ApplicationRegistry,
    application_from_user_payload,
)
from job_mail_desk.config import Settings, load_settings, settings_from_payload, write_settings
from job_mail_desk.file_transaction import FileTransaction
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.models import JobTask, ParsedEvent
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.progress import build_application_timeline, progress_payload
from job_mail_desk.task_service import edit_task_fields, task_from_event
from job_mail_desk.ui_app import DesktopApi
from job_mail_desk.unresolved_store import UnresolvedRecord, UnresolvedStore

NOW = datetime(2026, 9, 10, 10, 0, tzinfo=SHANGHAI)


def _configure(monkeypatch, tmp_path) -> DesktopApi:
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr("job_mail_desk.ui_app.APPLICATIONS_DIR", tmp_path / "applications")
    monkeypatch.setattr("job_mail_desk.ui_app.UNRESOLVED_DIR", tmp_path / "unresolved")
    monkeypatch.setattr("job_mail_desk.ui_app.STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr("job_mail_desk.ui_app.DASHBOARD_FILE", tmp_path / "dashboard.md")
    monkeypatch.setattr("job_mail_desk.agent_bridge.TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr("job_mail_desk.agent_bridge.DASHBOARD_FILE", tmp_path / "dashboard.md")
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)
    monkeypatch.setattr(api, "_refresh_task_runtime", lambda *_a, **_k: None)
    return api


def _application(tmp_path, *, stage="一面", status="pending"):
    registry = ApplicationRegistry(tmp_path / "applications")
    record = application_from_user_payload(
        {
            "company": "样例科技",
            "role": "后端工程师",
            "manual_stage": stage,
            "manual_stage_status": status,
            "next_stage": "二面",
        },
        now=NOW,
    )
    registry.save(record)
    return registry, record


def _mail_task(record, *, task_id="a" * 24, stage="一面", start=NOW + timedelta(days=3), status="planned",
               source_hash="c" * 32) -> JobTask:
    return JobTask(
        id=task_id,
        application_id=record.legacy_application_ids[0] if record.legacy_application_ids else "b" * 20,
        company=record.company,
        role=record.role,
        recruiting_project=None,
        event_type="interview",
        stage=stage,
        round=None,
        received_at=NOW,
        start_at=None,
        end_at=None,
        deadline_at=start,
        priority="high",
        status=status,  # type: ignore[arg-type]
        change_type="new",
        source_message_hash=source_hash,
        research_status="not_queued",
        confidence=0.9,
        title="面试邀请",
        action_summary="参加一面",
        application_key=record.application_key,
    )


def _node(task, *, status="pending"):
    return {
        "id": "pgn2_" + "1" * 24,
        "source_hash": task.source_message_hash,
        "source_hashes": [task.source_message_hash],
        "has_mail_locator": False,
        "event_at": NOW.isoformat(),
        "stage": task.stage,
        "round": None,
        "status": status,
        "next_stage": "二面",
        "lifecycle_status": "active",
        "source_task_id": task.id,
        "operation_id": "op1_" + "0" * 32,
    }


# --------------------------------------------------------------------------- bug 1: done propagates to the chain


def test_completing_a_task_completes_the_chain_stage_and_node(tmp_path, monkeypatch) -> None:
    api = _configure(monkeypatch, tmp_path)
    registry, record = _application(tmp_path)
    task = _mail_task(record)
    record.progress_nodes.append(_node(task))
    registry.save(record)
    MarkdownTaskStore(tmp_path / "tasks").save(task)

    payload = api.update_status(task.id, "done")

    refreshed = registry.load(record.application_key)
    assert refreshed.manual_stage_status == "completed"
    assert refreshed.progress_nodes[0]["status"] == "completed"
    assert refreshed.workflow_status == "waiting_next_step"
    card = next(
        application
        for group in payload["progress"]
        for application in group.get("applications", [group])
        if application.get("application_key") == record.application_key
    )
    assert card["manual_stage_status"] == "completed"
    # Reopening the task reopens the stage; nothing else regresses.
    api.update_status(task.id, "planned")
    reopened = registry.load(record.application_key)
    assert reopened.manual_stage_status == "pending"
    assert reopened.progress_nodes[0]["status"] == "pending"


def test_task_sync_advances_stage_and_never_touches_closed_chains() -> None:
    record = application_from_user_payload(
        {"company": "样例科技", "role": "后端", "manual_stage": "一面", "manual_stage_status": "completed"},
        now=NOW,
    )
    deeper = _mail_task(record, stage="二面", status="planned")
    assert sync_application_progress_from_task(record, deeper) is True
    assert (record.manual_stage, record.manual_stage_status) == ("二面", "pending")
    assert record.next_stage is None  # the announced next stage has arrived

    ended = application_from_user_payload(
        {"company": "样例科技", "role": "后端", "manual_stage": "已拒绝", "manual_stage_status": "completed"},
        now=NOW,
    )
    ended.status = "ended"
    old_task = _mail_task(ended, stage="一面", status="done")
    revision = ended.revision
    assert sync_application_progress_from_task(ended, old_task) is False
    assert ended.revision == revision and ended.status == "ended"

    # No change -> no revision churn (optimistic locks stay stable).
    same = application_from_user_payload(
        {"company": "样例科技", "role": "后端", "manual_stage": "一面", "manual_stage_status": "completed"},
        now=NOW,
    )
    done = _mail_task(same, status="done")
    assert sync_application_progress_from_task(same, done) is False


def test_card_save_without_status_change_keeps_a_completed_task_done(tmp_path, monkeypatch) -> None:
    api = _configure(monkeypatch, tmp_path)
    registry, record = _application(tmp_path)
    task = _mail_task(record)
    MarkdownTaskStore(tmp_path / "tasks").save(task)
    api.update_status(task.id, "done")
    current = registry.load(record.application_key)
    assert current.manual_stage_status == "completed"

    # The card re-posts whatever it was rendered with; here a stale 'pending'
    # while the user only fixed the role. The done task must survive.
    api.edit_application(
        record.application_key,
        {"role": "后端开发工程师", "manual_stage_status": "completed", "expected_revision": current.revision},
    )
    api.edit_application(
        record.application_key,
        {"role": "后端开发", "expected_revision": registry.load(record.application_key).revision},
    )
    assert MarkdownTaskStore(tmp_path / "tasks").load(task.id).status == "done"


# --------------------------------------------------------------------------- bug 2/3: follow-up mail updates the existing task


def _pending(record, *, source_hash="d" * 32, deadline=NOW + timedelta(days=5)) -> UnresolvedRecord:
    return UnresolvedRecord(
        id=source_hash,
        status="pending",
        resolution_status="matched",
        reason="review-first",
        company=record.company,
        role=record.role,
        recruiting_project=None,
        event_type="interview",
        stage="一面",
        round=None,
        received_at=NOW + timedelta(days=1),
        start_at=None,
        end_at=None,
        deadline_at=deadline,
        action_summary="面试时间调整，请按新时间参加",
        title="面试时间变更通知",
        requirements=(),
        confidence=0.95,
        change_type="update",
        candidate_application_keys=(record.application_key,),
        resolved_application_key=None,
        resolved_task_id=None,
        rule_version="test",
        revision=2,
        duration_minutes=None,
        recommended_application_key=record.application_key,
    )


def test_review_update_task_mode_moves_the_existing_task_instead_of_adding_one(tmp_path, monkeypatch) -> None:
    api = _configure(monkeypatch, tmp_path)
    registry, record = _application(tmp_path)
    task = _mail_task(record)
    record.progress_nodes.append(_node(task))
    registry.save(record)
    store = MarkdownTaskStore(tmp_path / "tasks")
    store.save(task)
    pending = _pending(record)
    UnresolvedStore(tmp_path / "unresolved").save(pending)

    recommendation = api.get_review_recommendation(pending.id, record.application_key)
    assert recommendation["suggested_task_id"] == task.id
    assert recommendation["suggest_update_task"] is True
    assert "existing_task" in recommendation["reasons"]
    target = api.get_review_target(record.application_key)
    assert [item["task_id"] for item in target["tasks"]] == [task.id]

    api.resolve_unresolved_workflow(
        pending.id,
        {
            "mode": "update_task",
            "application_key": record.application_key,
            "task_id": task.id,
            "expected_task_revision": target["tasks"][0]["revision"],
            "stage": "一面",
            "manual_stage_status": "pending",
            "expected_review_revision": 2,
            "expected_application_revision": registry.load(record.application_key).revision,
            "deadline_at": (NOW + timedelta(days=5)).isoformat(),
            "start_at": None,
            "end_at": None,
        },
    )

    tasks = store.all()
    assert [item.id for item in tasks] == [task.id], "no second task for the same interview"
    updated = tasks[0]
    assert updated.deadline_at == NOW + timedelta(days=5)
    assert updated.change_type == "update" and updated.status == "planned"
    assert updated.source_message_hash == task.source_message_hash  # stable id and source
    resolved = UnresolvedStore(tmp_path / "unresolved").load(pending.id)
    assert resolved.status == "resolved" and resolved.resolved_task_id == task.id
    refreshed = registry.load(record.application_key)
    assert all(node["source_task_id"] == task.id for node in refreshed.progress_nodes)
    timeline = build_application_timeline(tasks, refreshed)
    assert [row["stage"] for row in timeline].count("一面") == 1
    assert timeline[0]["time"] == (NOW + timedelta(days=5)).isoformat()


def test_review_update_task_reopens_a_done_task_and_rejects_stale_or_foreign_tasks(tmp_path, monkeypatch) -> None:
    api = _configure(monkeypatch, tmp_path)
    registry, record = _application(tmp_path)
    task = _mail_task(record, status="done")
    store = MarkdownTaskStore(tmp_path / "tasks")
    store.save(task)
    other_registry, other = _application(tmp_path)  # a second chain
    other.company = "别家公司"
    other_registry.save(other)
    pending = _pending(record)
    UnresolvedStore(tmp_path / "unresolved").save(pending)
    base = {
        "mode": "update_task",
        "application_key": record.application_key,
        "stage": "一面",
        "manual_stage_status": "pending",
        "expected_review_revision": 2,
    }
    with pytest.raises(ValueError, match="请选择要更新的待办"):
        api.resolve_unresolved_workflow(pending.id, base)
    with pytest.raises(ValueError, match="不属于所选申请链"):
        api.resolve_unresolved_workflow(pending.id, {**base, "task_id": "f" * 24})
    with pytest.raises(ValueError, match="刚被修改过"):
        api.resolve_unresolved_workflow(
            pending.id, {**base, "task_id": task.id, "expected_task_revision": "stale"}
        )
    api.resolve_unresolved_workflow(pending.id, {**base, "task_id": task.id})
    reopened = store.load(task.id)
    assert reopened.status == "planned" and reopened.completed_at is None


def test_review_update_task_requires_an_active_chain(tmp_path, monkeypatch) -> None:
    api = _configure(monkeypatch, tmp_path)
    registry, record = _application(tmp_path, stage="已拒绝", status="completed")
    record.status = "ended"
    registry.save(record)
    task = _mail_task(record)
    MarkdownTaskStore(tmp_path / "tasks").save(task)
    pending = _pending(record)
    UnresolvedStore(tmp_path / "unresolved").save(pending)
    with pytest.raises(ValueError, match="进行中"):
        api.resolve_unresolved_workflow(
            pending.id,
            {"mode": "update_task", "application_key": record.application_key, "task_id": task.id,
             "stage": "一面", "manual_stage_status": "pending", "expected_review_revision": 2},
        )


def test_confirming_a_new_mail_for_a_stage_with_a_trashed_task_creates_a_visible_task(tmp_path) -> None:
    store = MarkdownTaskStore(tmp_path / "tasks")
    event = ParsedEvent(
        company="样例科技", role="后端工程师", location=None, recruiting_project=None,
        event_type="interview", stage="一面", round=None, title="一面邀请",
        start_at=None, end_at=None, deadline_at=NOW + timedelta(days=2),
        source_message_id="<first@example.invalid>", source_received_at=NOW, source_sender="",
        source_url=None, action_summary="参加一面", requirements=(), matched_keywords=(),
        confidence=0.9, change_type="new",
    )
    first = task_from_event(event, store, application_key="app-" + "a" * 24, source_hash_override="1" * 32)
    store.save(first)
    store.trash(first.id)
    second = task_from_event(
        replace(event, deadline_at=NOW + timedelta(days=4)),
        store,
        application_key="app-" + "a" * 24,
        source_hash_override="2" * 32,
    )
    assert second.id != first.id and second.deleted_at is None
    assert second.source_message_hash == "2" * 32


def test_rescheduling_an_expired_mail_task_brings_it_back(tmp_path) -> None:
    store = MarkdownTaskStore(tmp_path / "tasks")
    record = application_from_user_payload({"company": "样例科技", "role": "后端"}, now=NOW)
    task = _mail_task(record, status="expired", start=NOW - timedelta(days=2))
    store.save(task)
    future = datetime.now(SHANGHAI) + timedelta(days=3)
    edited = edit_task_fields(task.id, {"deadline_at": future.isoformat()}, store)
    assert edited.status == "planned"


# --------------------------------------------------------------------------- timeline: no duplicate rows


def test_timeline_folds_review_nodes_into_matching_task_rows_and_hides_trashed_ones(tmp_path) -> None:
    record = application_from_user_payload({"company": "样例科技", "role": "后端", "manual_stage": "测评"}, now=NOW)
    task = _mail_task(record, stage="测评", status="done")
    task.completed_at = NOW + timedelta(days=1)
    # A node from a *different* mail at the same stage (no hash / task link).
    stray = {**_node(task, status="completed"), "id": "pgn2_" + "2" * 24,
             "source_hash": "e" * 32, "source_hashes": ["e" * 32], "source_task_id": None}
    record.progress_nodes.append(stray)
    timeline = build_application_timeline([task], record)
    rows = [row for row in timeline if row["stage"] == "测评"]
    assert len(rows) == 1 and rows[0]["status"] == "done"
    assert any(source["source_type"] == "mail-review" for source in rows[0]["sources"])

    # Node whose task was trashed: hidden from the card history too.
    store = MarkdownTaskStore(tmp_path / "tasks")
    linked = _mail_task(record, task_id="b" * 24, stage="一面", source_hash="f" * 32)
    record.progress_nodes.append({**_node(linked), "id": "pgn2_" + "3" * 24})
    store.save(linked)
    store.trash(linked.id)
    payload = progress_payload(store.all(), None, [record])
    card = next(item for item in payload if item["application_key"] == record.application_key)
    assert not any(row["stage"] == "一面" for row in card["history"])


# --------------------------------------------------------------------------- performance regressions


def test_frontmatter_cache_reuses_parses_until_the_file_changes(tmp_path) -> None:
    frontmatter_cache.clear_cache()
    store = MarkdownTaskStore(tmp_path / "tasks")
    record = application_from_user_payload({"company": "样例科技", "role": "后端"}, now=NOW)
    task = _mail_task(record)
    path = store.save(task)
    calls = {"count": 0}
    original = frontmatter_cache.fast_safe_load

    def counting(text):
        calls["count"] += 1
        return original(text)

    frontmatter_cache.fast_safe_load = counting  # type: ignore[assignment]
    try:
        first = store.load(task.id)
        second = store.load(task.id)
        assert calls["count"] == 1 and first.id == second.id
        second.company = "改动不应泄漏"
        assert store.load(task.id).company == "样例科技"
        # A rewrite (new mtime/size) invalidates the entry.
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 5_000_000))
        store.load(task.id)
        assert calls["count"] == 2
    finally:
        frontmatter_cache.fast_safe_load = original  # type: ignore[assignment]
        frontmatter_cache.clear_cache()


def test_frontmatter_cache_uses_libyaml_when_available() -> None:
    import yaml

    if yaml.__with_libyaml__:
        assert frontmatter_cache._LOADER is yaml.CSafeLoader
    assert frontmatter_cache.fast_safe_load("a: 1\nb: [x, y]") == {"a": 1, "b": ["x", "y"]}


def test_file_transaction_flushes_backups_once_and_markers(tmp_path, monkeypatch) -> None:
    facts = tmp_path / "facts"
    facts.mkdir()
    for index in range(30):
        (facts / f"{index}.md").write_text("x", encoding="utf-8")
    flushed: list[int] = []
    real_fsync = os.fsync

    def counting_fsync(fd):
        flushed.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr("job_mail_desk.fs_utils.os.fsync", counting_fsync)
    with FileTransaction(tmp_path / ".transactions", (facts,)) as transaction:
        transaction.commit()
    # 30 backups + manifest flushed once before `prepared`, then the two
    # markers and directories: durable, but not three passes over every copy.
    assert 31 <= len(flushed) <= 45


# --------------------------------------------------------------------------- new-mail notifications and settings


def test_scan_alerts_detect_new_or_updated_pending_reviews(tmp_path) -> None:
    store = UnresolvedStore(tmp_path / "unresolved")
    record = application_from_user_payload({"company": "样例科技", "role": "后端"}, now=NOW)
    first = _pending(record, source_hash="1" * 32)
    store.save(first)
    before = scan_alerts.pending_snapshot(store)
    assert scan_alerts.new_pending_reviews(before, store) == []
    store.save(_pending(record, source_hash="2" * 32))
    store.save(replace(first, revision=first.revision + 1))
    fresh = scan_alerts.new_pending_reviews(before, store)
    assert sorted(item.id for item in fresh) == ["1" * 32, "2" * 32]
    message = scan_alerts.describe_new_reviews(fresh)
    assert "样例科技" in message and "面试时间变更通知" not in message  # never the subject

    shown: list[tuple[str, str]] = []
    assert scan_alerts.announce_new_reviews(fresh, Settings(), lambda t, m: shown.append((t, m)) or True)
    assert shown and shown[0][0] == scan_alerts.NEW_MAIL_TITLE
    assert scan_alerts.announce_new_reviews(fresh, Settings(notify_new_mail=False), lambda t, m: True) is False
    assert scan_alerts.announce_new_reviews([], Settings(), lambda t, m: True) is False


def test_scheduler_scan_announces_new_reviews(monkeypatch, tmp_path) -> None:
    from job_mail_desk import scheduler

    store = UnresolvedStore(tmp_path / "unresolved")
    record = application_from_user_payload({"company": "样例科技", "role": "后端"}, now=NOW)
    monkeypatch.setattr(scheduler, "UNRESOLVED_DIR", tmp_path / "unresolved")
    monkeypatch.setattr(scheduler, "load_credential", lambda: object())
    shown: list[str] = []
    monkeypatch.setattr(scheduler, "notify_message", lambda title, message: shown.append(message) or True)
    monkeypatch.setattr(scheduler, "notify_urgent", lambda _count: None)

    class Summary:
        urgent = 0

        def to_dict(self):
            return {}

    def fake_scan(settings, runtime_control=None):
        store.save(_pending(record, source_hash="9" * 32))  # the scan files a new pending review
        return Summary()

    monkeypatch.setattr(scheduler, "scan_once", fake_scan)
    scheduler.ScheduledJobs(Settings()).scan()
    assert len(shown) == 1 and "样例科技" in shown[0]
    # Nothing new on the next scan -> silence.
    monkeypatch.setattr(scheduler, "scan_once", lambda settings, runtime_control=None: Summary())
    scheduler.ScheduledJobs(Settings()).scan()
    assert len(shown) == 1


def test_new_settings_round_trip_and_payload(tmp_path) -> None:
    settings = replace(Settings(), taskbar_button=False, notify_new_mail=False)
    path = write_settings(settings, tmp_path / "config.toml")
    loaded = load_settings(path)
    assert loaded.taskbar_button is False and loaded.notify_new_mail is False
    assert load_settings(Path(write_settings(Settings(), tmp_path / "d.toml"))).taskbar_button is True
    updated = settings_from_payload(Settings(), {"taskbar_button": False, "notify_new_mail": "0", "calendar_name": "JobMailDesk"})
    assert updated.taskbar_button is False and updated.notify_new_mail is False


def test_notification_click_handler_and_tray_subclass(monkeypatch) -> None:
    from job_mail_desk import tray_badge, windows_notify

    clicks: list[str] = []
    windows_notify.register_notification_click_handler(lambda title: clicks.append(title or "open"))
    try:
        assert windows_notify.notification_clicked() is True
        icon = tray_badge.ClickableTrayIcon("test-tray")  # dummy backend under pytest
        icon._on_notify(0, windows_notify.NIN_BALLOONUSERCLICK)
        assert clicks == ["open", "open"]
        icon._on_notify(0, 0x0202)  # WM_LBUTTONUP: no base handler on the dummy backend, no crash
    finally:
        windows_notify.register_notification_click_handler(None)
    assert windows_notify.notification_clicked() is False


def test_desktop_chrome_helpers_are_inert_off_windows(monkeypatch) -> None:
    class Window:
        native = None

        def evaluate_js(self, script):
            raise AssertionError("must not be reached off Windows")

    monkeypatch.setattr(ui_app.sys, "platform", "linux")
    assert ui_app.install_close_to_tray(Window(), {"requested": False}) is False
    ui_app._hide_from_task_switcher(Window(), enabled=False)
    ui_app._hide_from_task_switcher(Window(), enabled=True)  # linux: no-op


def test_desktop_settings_payload_and_ui_expose_the_new_controls(tmp_path, monkeypatch) -> None:
    api = _configure(monkeypatch, tmp_path)
    payload = api.get_app_settings()
    assert payload["taskbar_button"] is True and payload["notify_new_mail"] is True
    assert "close_hides_to_tray" in payload
    ui_dir = Path(ui_app.__file__).parent / "ui"
    html = (ui_dir / "index.html").read_text(encoding="utf-8")
    javascript = (ui_dir / "app.js").read_text(encoding="utf-8")
    review_html = (ui_dir / "review.html").read_text(encoding="utf-8")
    review_js = (ui_dir / "review.js").read_text(encoding="utf-8")
    for needle in ('name="notify_new_mail"', 'name="taskbar_button"', 'id="closeHidesHint"',
                   'value="update_task"', 'id="ownershipTaskRow"'):
        assert needle in html, needle
    for needle in ("window.setBackgroundMode", "list_application_tasks", "update_task", "reviewWindowCompleted"):
        assert needle in javascript, needle
    assert 'value="update_task"' in review_html and 'id="existingTaskPicker"' in review_html
    assert "existingTaskSelect" in review_js and "expected_task_revision" in review_js
    startup = Path(ui_app.__file__).read_text(encoding="utf-8").split("def _run_ui_primary", 1)[1]
    assert "install_close_to_tray(window, quit_state)" in startup
    assert 'quit_state["requested"] = True' in startup
    assert "register_notification_click_handler(notification_clicked)" in startup
    assert "if title == NEW_MAIL_TITLE:" in startup
    assert "window.events.closing" not in Path(ui_app.__file__).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- round-2 review follow-ups


def test_review_window_bootstrap_fallback_target_carries_tasks(tmp_path, monkeypatch) -> None:
    api = _configure(monkeypatch, tmp_path)
    registry, record = _application(tmp_path)
    task = _mail_task(record)
    MarkdownTaskStore(tmp_path / "tasks").save(task)
    pending = replace(_pending(record), recommended_application_key=None, candidate_application_keys=())
    UnresolvedStore(tmp_path / "unresolved").save(pending)

    bootstrap = api.get_review_window_bootstrap(pending.id)
    target = bootstrap["target"]
    assert target and target["application_key"] == record.application_key
    # The exact-match fallback used to hand over the bare search payload
    # (no ``tasks``) so the review window disabled 更新已有待办 and fell back to
    # creating a new chain for a company/role it already tracks.
    assert [item["task_id"] for item in target["tasks"]] == [task.id]
    assert bootstrap["recommendation"]["suggested_task_id"] == task.id


def test_update_task_keeps_notes_and_schedule_when_the_mail_has_none(tmp_path, monkeypatch) -> None:
    api = _configure(monkeypatch, tmp_path)
    registry, record = _application(tmp_path)
    task = _mail_task(record)
    task.manual_notes = "带作品集，提前 10 分钟到"
    store = MarkdownTaskStore(tmp_path / "tasks")
    store.save(task)
    pending = replace(_pending(record, deadline=None), action_summary="面试地点改为 B 座 3 层")
    UnresolvedStore(tmp_path / "unresolved").save(pending)

    api.resolve_unresolved_workflow(
        pending.id,
        {
            "mode": "update_task",
            "application_key": record.application_key,
            "task_id": task.id,
            "stage": "一面",
            "manual_stage_status": "pending",
            "expected_review_revision": 2,
            "task_notes": "",  # both forms always post the (empty) notes box
        },
    )
    updated = store.load(task.id)
    assert updated.manual_notes == "带作品集，提前 10 分钟到"
    assert updated.deadline_at == NOW + timedelta(days=3), "a mail without a time keeps the old one"
    assert updated.status == "planned" and updated.action_summary == "面试地点改为 B 座 3 层"


def test_card_stage_advance_does_not_reopen_the_previous_stage_task(tmp_path, monkeypatch) -> None:
    api = _configure(monkeypatch, tmp_path)
    registry, record = _application(tmp_path)
    task = _mail_task(record)
    store = MarkdownTaskStore(tmp_path / "tasks")
    store.save(task)
    api.update_status(task.id, "done")
    current = registry.load(record.application_key)
    assert (current.manual_stage, current.manual_stage_status) == ("一面", "completed")

    # The inline card posts both selects: moving to 二面 arrives as pending.
    api.edit_application(
        record.application_key,
        {"manual_stage": "二面", "manual_stage_status": "pending", "expected_revision": current.revision},
    )
    assert store.load(task.id).status == "done", "the finished 一面 task belongs to the old stage"
    advanced = registry.load(record.application_key)
    assert (advanced.manual_stage, advanced.manual_stage_status) == ("二面", "pending")


def test_suggestion_respects_the_round_named_by_the_mail(tmp_path, monkeypatch) -> None:
    api = _configure(monkeypatch, tmp_path)
    registry, record = _application(tmp_path, stage="面试", status="completed")
    first = _mail_task(record, stage="面试", status="done")
    first.round = "第1轮"
    MarkdownTaskStore(tmp_path / "tasks").save(first)
    pending = replace(_pending(record), stage="面试", round="第2轮", change_type="new",
                      recommendation_reasons=("explicit_deadline",))
    UnresolvedStore(tmp_path / "unresolved").save(pending)

    recommendation = api.get_review_recommendation(pending.id, record.application_key)
    assert recommendation["suggested_task_id"] is None, "a 第2轮 mail must not overwrite the 第1轮 task"
    assert recommendation["suggest_update_task"] is False
    assert recommendation["default_create_task"] is True


def test_irrelevant_task_node_stays_hidden_on_the_progress_card(tmp_path, monkeypatch) -> None:
    api = _configure(monkeypatch, tmp_path)
    registry, record = _application(tmp_path, stage="测评")
    task = _mail_task(record, stage="一面")
    record.progress_nodes.append(_node(task))
    registry.save(record)
    store = MarkdownTaskStore(tmp_path / "tasks")
    store.save(task)
    api.update_status(task.id, "irrelevant")
    payload = progress_payload(store.all(), None, [registry.load(record.application_key)])
    card = next(item for item in payload if item["application_key"] == record.application_key)
    assert not any(row["stage"] == "一面" for row in card["history"])


def test_update_status_and_snooze_refresh_calendar_and_reminders(tmp_path, monkeypatch) -> None:
    api = _configure(monkeypatch, tmp_path)
    registry, record = _application(tmp_path)
    task = _mail_task(record)
    MarkdownTaskStore(tmp_path / "tasks").save(task)
    calls: list[str] = []
    monkeypatch.setattr(api, "_refresh_task_runtime", lambda task, *_a, **_k: calls.append(task.id))
    api.update_status(task.id, "done")
    api.snooze(task.id, (NOW + timedelta(days=1)).isoformat())
    assert calls == [task.id, task.id]


def test_frontmatter_cache_invalidates_on_same_size_rewrite(tmp_path) -> None:
    path = tmp_path / "doc.md"
    path.write_text("---\nvalue: 1\n---\nbody\n", encoding="utf-8")
    extract = lambda text: text.split("---\n")[1]  # noqa: E731
    assert frontmatter_cache.load_document(path, extract)[1] == {"value": 1}
    stat = path.stat()
    path.write_text("---\nvalue: 2\n---\nbody\n", encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same size, same mtime tick
    assert frontmatter_cache.load_document(path, extract)[1] == {"value": 1}, "the key alone cannot see this"
    frontmatter_cache.invalidate(path)
    assert frontmatter_cache.load_document(path, extract)[1] == {"value": 2}


def test_manual_scan_announces_new_reviews(monkeypatch, tmp_path) -> None:
    api = _configure(monkeypatch, tmp_path)
    unresolved = UnresolvedStore(tmp_path / "unresolved")
    registry, record = _application(tmp_path)
    announced: list[tuple[str, str]] = []

    class Summary:
        def to_dict(self):
            return {}

    def fake_scan_once(settings, *, runtime_control):
        unresolved.save(_pending(record))
        return Summary()

    monkeypatch.setattr("job_mail_desk.ui_app.scan_once", fake_scan_once)
    monkeypatch.setattr("job_mail_desk.ui_app.notify_message", lambda title, message: announced.append((title, message)))
    assert api.trigger_scan()["status"] == "ok"
    assert announced and announced[0][0] == scan_alerts.NEW_MAIL_TITLE
    assert "样例科技" in announced[0][1]
