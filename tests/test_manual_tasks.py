from datetime import datetime

import pytest

import job_mail_desk.macos_calendar as macos_calendar
import job_mail_desk.notifier as notifier
from job_mail_desk.config import Settings
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.state import StateStore
from job_mail_desk.task_service import (
    create_manual_task,
    critical_time,
    edit_task_fields,
    legacy_application_id,
)


def test_create_and_edit_manual_markdown_task(tmp_path) -> None:
    store = MarkdownTaskStore(tmp_path)
    task = create_manual_task(
        {
            "company": "个人计划",
            "role": "产品经理",
            "stage": "模拟面试",
            "start_at": "2026-08-05T19:00:00+08:00",
            "end_at": "2026-08-05T20:00:00+08:00",
            "action_summary": "准备产品案例",
            "manual_notes": "先复盘项目，再模拟回答。",
        },
        store,
        now=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )
    assert task.status == "planned"
    assert store.path_for(task.id).exists()
    edited = edit_task_fields(
        task.id,
        {
            "start_at": "2026-08-06T20:00:00+08:00",
            "end_at": "2026-08-06T21:00:00+08:00",
            "manual_notes": "已更新准备材料。",
        },
        store,
    )
    assert edited.start_at == datetime(2026, 8, 6, 20, 0, tzinfo=SHANGHAI)
    assert "已更新" in edited.manual_notes


@pytest.mark.parametrize(
    "terminal_status",
    ["done", "expired"],
)
def test_same_manual_event_dedup_preserves_terminal_status(
    tmp_path,
    terminal_status,
) -> None:
    store = MarkdownTaskStore(tmp_path)
    payload = {
        "company": "京东",
        "role": "TET 综合方向",
        "stage": "群面",
        "start_at": "2026-08-06T14:00:00+08:00",
        "end_at": "2026-08-06T17:00:00+08:00",
        "action_summary": "参加群面",
    }
    first = create_manual_task(payload, store)
    store.update_status(first.id, terminal_status)
    payload["action_summary"] = "参加群面并提前准备案例"
    second = create_manual_task(payload, store)
    assert second.id == first.id
    assert second.status == terminal_status
    assert second.change_type == "update"
    assert len(store.all()) == 1
    assert "提前准备" in second.action_summary
    edited = edit_task_fields(
        first.id,
        {"deadline_at": "2026-08-06T13:30:00+08:00"},
        store,
    )
    assert edited.id == first.id
    assert edited.status == terminal_status


@pytest.mark.parametrize("terminal_status", ["cancelled", "irrelevant"])
def test_authoritative_manual_status_is_not_reused_or_revived(
    tmp_path,
    terminal_status,
) -> None:
    store = MarkdownTaskStore(tmp_path)
    payload = {
        "company": "京东",
        "role": "TET 综合方向",
        "stage": "群面",
        "start_at": "2026-08-06T14:00:00+08:00",
        "action_summary": "参加群面",
    }
    original = create_manual_task(payload, store)
    store.update_status(original.id, terminal_status)

    recreated = create_manual_task(payload, store)
    assert recreated.id != original.id
    assert store.load(original.id).status == terminal_status

    edited = edit_task_fields(
        original.id,
        {"deadline_at": "2026-08-06T13:30:00+08:00"},
        store,
    )
    assert edited.status == terminal_status


@pytest.mark.parametrize("time_field", ["start_at", "end_at", "deadline_at"])
def test_editing_manual_time_reconciles_planned_and_review_states(
    tmp_path,
    time_field,
) -> None:
    store = MarkdownTaskStore(tmp_path)
    task = create_manual_task(
        {
            "company": "个人计划",
            "stage": "准备材料",
            "action_summary": "整理面试案例",
        },
        store,
        now=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )
    assert task.status == "needs_review"

    scheduled = edit_task_fields(
        task.id,
        {time_field: "2026-08-08T12:00:00+08:00"},
        store,
    )
    assert scheduled.id == task.id
    assert scheduled.status == "planned"

    cleared = edit_task_fields(task.id, {time_field: None}, store)
    assert cleared.id == task.id
    assert cleared.status == "needs_review"


def test_only_end_manual_task_is_critical_and_eligible(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 8, 10, 0, tzinfo=SHANGHAI)
    end_at = datetime(2026, 8, 8, 11, 30, tzinfo=SHANGHAI)
    store = MarkdownTaskStore(tmp_path / "tasks")
    payload = {
        "company": "个人计划",
        "stage": "材料提交",
        "end_at": end_at.isoformat(),
        "action_summary": "按时提交材料",
    }
    task = create_manual_task(payload, store, now=now)

    assert task.status == "planned"
    assert task.priority == "urgent"
    assert critical_time(task) == end_at
    duplicate = create_manual_task(payload, store, now=now)
    assert duplicate.id == task.id
    assert len(store.all()) == 1
    _script, calendar_active = macos_calendar._event_script(task, "JobMailDesk")
    assert calendar_active is True

    sent: list[int] = []
    monkeypatch.setattr(
        notifier,
        "notify_task",
        lambda _task, offset: sent.append(offset) or True,
    )
    assert (
        notifier.send_due_reminders(
            [task],
            Settings(),
            StateStore(tmp_path / "state.db"),
            now=now,
        )
        == 1
    )
    assert sent == [120]


def test_manual_task_can_bind_and_rebind_application_chain(tmp_path) -> None:
    store = MarkdownTaskStore(tmp_path)
    first_key = "app-" + "a" * 24
    second_key = "app-" + "b" * 24
    task = create_manual_task(
        {
            "application_key": first_key,
            "company": "样例公司",
            "role": "后端工程师",
            "stage": "笔试",
            "start_at": "2026-08-10T19:00:00+08:00",
            "action_summary": "参加笔试",
        },
        store,
    )
    assert task.application_key == first_key
    assert task.application_id == legacy_application_id(first_key)

    updated = edit_task_fields(
        task.id,
        {"application_key": second_key},
        store,
    )
    assert updated.application_key == second_key
    assert updated.application_id == legacy_application_id(second_key)

    detached = edit_task_fields(task.id, {"application_key": ""}, store)
    assert detached.application_key is None
    assert detached.application_id not in {
        legacy_application_id(first_key),
        legacy_application_id(second_key),
    }


def test_task_recycle_restore_and_permanent_tombstone(tmp_path) -> None:
    store = MarkdownTaskStore(tmp_path)
    task = create_manual_task(
        {
            "company": "样例公司",
            "role": "产品经理",
            "stage": "面试",
            "start_at": "2026-08-12T14:00:00+08:00",
            "action_summary": "参加面试",
        },
        store,
        now=datetime(2026, 8, 10, 9, 0, tzinfo=SHANGHAI),
    )

    trashed = store.trash(
        task.id,
        now=datetime(2026, 8, 10, 10, 0, tzinfo=SHANGHAI),
    )
    assert trashed.deleted_at is not None
    assert trashed.deleted_status == "planned"
    assert trashed.status == "cancelled"

    restored = store.restore(
        task.id,
        now=datetime(2026, 8, 10, 11, 0, tzinfo=SHANGHAI),
    )
    assert restored.deleted_at is None
    assert restored.status == "planned"

    store.trash(task.id)
    tombstone = store.permanently_delete(task.id)
    assert tombstone.tombstoned is True
    assert tombstone.source_message_hash == "manual"
    assert tombstone.company == "已永久删除"
    assert tombstone.title == ""
