from datetime import datetime, timedelta

import pytest

from job_mail_desk.application_lifecycle import (
    reconcile_all_applications,
    reconcile_application,
)
from job_mail_desk.application_registry import (
    ApplicationRegistry,
    update_application_from_user_payload,
)
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.models import ApplicationRecord, JobTask
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.stages import is_terminal_stage, stage_depth


def application(now: datetime) -> ApplicationRecord:
    return ApplicationRecord(
        application_key="app-" + "a" * 24,
        company_key="sample",
        company="样例公司",
        recruiting_project="2027 校招",
        recruiting_year=2027,
        business_unit=None,
        role="产品经理",
        role_aliases=[],
        job_code=None,
        submitted_at=now,
        status="active",
        source="test",
        confirmed_by_user=True,
        identity_locked=True,
    )


def task(now: datetime, *, status: str = "planned", offset: int = 0) -> JobTask:
    return JobTask(
        id=str(offset + 1) * 24,
        application_id="b" * 20,
        application_key="app-" + "a" * 24,
        company="样例公司",
        role="产品经理",
        recruiting_project="2027 校招",
        event_type="assessment",
        stage="在线笔试",
        round=None,
        received_at=now + timedelta(hours=offset),
        start_at=now + timedelta(days=1, hours=offset),
        end_at=None,
        deadline_at=None,
        priority="high",
        status=status,  # type: ignore[arg-type]
        change_type="new",
        source_message_hash=str(offset + 1) * 32,
        research_status="not_queued",
        confidence=1,
        title="笔试",
        action_summary="参加笔试",
        completed_at=now + timedelta(hours=offset) if status == "done" else None,
    )


def test_completed_latest_task_moves_application_to_waiting(tmp_path) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    registry = ApplicationRegistry(tmp_path / "applications")
    store = MarkdownTaskStore(tmp_path / "tasks")
    registry.save(application(now))
    completed = task(now, status="done")
    store.save(completed)

    record = reconcile_application(completed.application_key, registry, store)

    assert record is not None
    assert record.workflow_status == "waiting_next_step"
    assert record.current_task_id == completed.id


def test_newer_actionable_task_reopens_waiting_application(tmp_path) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    registry = ApplicationRegistry(tmp_path / "applications")
    store = MarkdownTaskStore(tmp_path / "tasks")
    record = application(now)
    record.workflow_status = "waiting_next_step"
    registry.save(record)
    store.save(task(now, status="done"))
    upcoming = task(now, status="planned", offset=2)
    store.save(upcoming)

    updated = reconcile_application(upcoming.application_key, registry, store)

    assert updated is not None
    assert updated.workflow_status == "action_required"
    assert updated.current_task_id == upcoming.id


def test_newer_manual_reopen_beats_replayed_terminal_task(tmp_path) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    registry = ApplicationRegistry(tmp_path / "applications")
    store = MarkdownTaskStore(tmp_path / "tasks")
    record = application(now)
    record.status = "ended"
    record.manual_stage = "二面"
    record.manual_progress_history = [
        {
            "event_at": (now + timedelta(hours=2)).isoformat(),
            "stage": "二面",
            "status": "pending",
            "next_stage": None,
        }
    ]
    registry.save(record)
    rejection = task(now)
    rejection.event_type = "rejection"
    rejection.stage = "未通过"
    rejection.title = "很遗憾"
    rejection.updated_at = now + timedelta(days=20)
    store.save(rejection)

    updated = reconcile_application(record.application_key, registry, store)

    assert updated is not None
    assert updated.status == "active"
    assert updated.workflow_status == "action_required"


def test_later_terminal_mail_ends_manually_reopened_application(tmp_path) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    registry = ApplicationRegistry(tmp_path / "applications")
    store = MarkdownTaskStore(tmp_path / "tasks")
    record = application(now)
    record.manual_stage = "二面"
    record.manual_progress_history = [
        {
            "event_at": (now + timedelta(hours=2)).isoformat(),
            "stage": "二面",
            "status": "pending",
            "next_stage": None,
        }
    ]
    registry.save(record)
    rejection = task(now, offset=4)
    rejection.event_type = "rejection"
    rejection.stage = "流程结束"
    rejection.title = "招聘流程结束"
    store.save(rejection)

    updated = reconcile_application(record.application_key, registry, store)

    assert updated is not None
    assert updated.status == "ended"
    assert updated.workflow_status == "ended"


def test_latest_artificial_end_overrides_older_manual_reopen(tmp_path) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    registry = ApplicationRegistry(tmp_path / "applications")
    store = MarkdownTaskStore(tmp_path / "tasks")
    record = application(now)
    record.manual_stage = "网申"
    record.manual_progress_history = [
        {
            "event_at": (now + timedelta(hours=1)).isoformat(),
            "stage": "二面",
            "status": "pending",
            "next_stage": None,
            "lifecycle_status": "active",
        },
        {
            "event_at": (now + timedelta(hours=2)).isoformat(),
            "stage": "网申",
            "status": "pending",
            "next_stage": None,
            "lifecycle_status": "ended",
        },
    ]
    registry.save(record)
    store.save(task(now, status="planned"))

    updated = reconcile_application(record.application_key, registry, store)

    assert updated is not None
    assert updated.status == "ended"
    assert updated.workflow_status == "ended"
    assert updated.current_task_id is None
    assert store.load("1" * 24).application_paused is True


def test_irrelevant_rejection_does_not_end_application(tmp_path) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    registry = ApplicationRegistry(tmp_path / "applications")
    store = MarkdownTaskStore(tmp_path / "tasks")
    record = application(now)
    registry.save(record)
    rejection = task(now, status="irrelevant")
    rejection.event_type = "rejection"
    rejection.stage = "未通过"
    rejection.title = "很遗憾"
    store.save(rejection)

    updated = reconcile_application(record.application_key, registry, store)

    assert updated is not None
    assert updated.status == "active"
    assert updated.workflow_status == "waiting_next_step"


@pytest.mark.parametrize(
    "phrase",
    ("即将关闭", "关闭前提交", "如需撤回", "流程结束前提交"),
)
def test_instructional_terminal_words_do_not_end_application(
    tmp_path,
    phrase: str,
) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    registry = ApplicationRegistry(tmp_path / "applications")
    store = MarkdownTaskStore(tmp_path / "tasks")
    registry.save(application(now))
    notice = task(now)
    notice.stage = "在线测评"
    notice.title = phrase
    notice.action_summary = phrase
    store.save(notice)

    updated = reconcile_application(notice.application_key, registry, store)

    assert not is_terminal_stage(phrase)
    assert updated is not None
    assert updated.status == "active"
    assert store.load(notice.id).application_paused is False


def test_structured_terminal_status_still_ends_application(tmp_path) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    registry = ApplicationRegistry(tmp_path / "applications")
    store = MarkdownTaskStore(tmp_path / "tasks")
    registry.save(application(now))
    closed = task(now)
    closed.event_type = "notice"
    closed.stage = "申请已关闭"
    store.save(closed)

    updated = reconcile_application(closed.application_key, registry, store)

    assert is_terminal_stage(closed.stage)
    assert updated is not None
    assert updated.status == "ended"
    assert store.load(closed.id).application_paused is True


def test_taskless_reconciliation_is_idempotent(tmp_path) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    registry = ApplicationRegistry(tmp_path / "applications")
    store = MarkdownTaskStore(tmp_path / "tasks")
    record = application(now)
    registry.save(record)

    first = reconcile_application(record.application_key, registry, store)
    assert first is not None
    first_state = (first.waiting_since, first.revision, first.updated_at)

    second = reconcile_application(record.application_key, registry, store)

    assert second is not None
    assert (second.waiting_since, second.revision, second.updated_at) == first_state


def test_reconcile_all_pauses_tasks_for_archived_applications(tmp_path) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    registry = ApplicationRegistry(tmp_path / "applications")
    store = MarkdownTaskStore(tmp_path / "tasks")
    archived = application(now)
    archived.status = "archived"
    registry.save(archived)
    linked = task(now)
    store.save(linked)

    assert reconcile_all_applications(registry, store) == 1

    saved_task = store.load(linked.id)
    saved_application = registry.load(archived.application_key)
    assert saved_task is not None
    assert saved_task.application_paused is True
    assert saved_application is not None
    assert saved_application.status == "archived"


@pytest.mark.parametrize(
    ("stage", "expected_depth"),
    (
        ("一面", 4),
        ("第一轮面试", 4),
        ("二面", 5),
        ("第2轮", 5),
        ("三面", 6),
        ("第三轮技术面", 6),
        ("四面", 7),
        ("第4轮", 7),
        ("五面", 8),
        ("第五轮面试", 8),
        ("一至五面", 8),
        ("技术面", 4),
        ("HRBP", 10),
        ("终面", 9),
        ("Offer", 11),
    ),
)
def test_stage_depth_covers_interview_rounds(
    stage: str,
    expected_depth: int,
) -> None:
    assert stage_depth(stage) == expected_depth


def test_same_stage_pending_does_not_regress_completed_status() -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    record = application(now)
    record.manual_stage = "第三轮面试"
    record.manual_stage_status = "completed"

    update_application_from_user_payload(
        record,
        {
            "manual_stage": "三面",
            "manual_stage_status": "pending",
        },
    )

    assert record.manual_stage == "三面"
    assert record.manual_stage_status == "completed"
