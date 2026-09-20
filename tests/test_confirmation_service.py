from dataclasses import replace
from datetime import datetime

import pytest

from job_mail_desk.application_registry import ApplicationRegistry
from job_mail_desk.config import Settings
from job_mail_desk.confirmation_service import append_progress_node
from job_mail_desk.derived_outbox import DerivedOutbox
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.models import ApplicationRecord
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.ui_app import DesktopApi
from job_mail_desk.unresolved_store import UnresolvedRecord, UnresolvedStore


def pending_record(source_hash: str) -> UnresolvedRecord:
    return UnresolvedRecord(
        id=source_hash,
        status="pending",
        resolution_status="matched",
        reason="review-first",
        company="样例科技",
        role="后端工程师",
        recruiting_project=None,
        event_type="assessment",
        stage="笔试",
        round=None,
        received_at=datetime(2026, 8, 14, 9, 0, tzinfo=SHANGHAI),
        start_at=datetime(2026, 8, 15, 19, 0, tzinfo=SHANGHAI),
        end_at=None,
        deadline_at=datetime(2026, 8, 15, 21, 0, tzinfo=SHANGHAI),
        action_summary="参加笔试",
        title="笔试邀请",
        requirements=(),
        confidence=0.99,
        change_type="new",
        candidate_application_keys=(),
        resolved_application_key=None,
        resolved_task_id=None,
        rule_version="test",
        revision=3,
        duration_minutes=90,
    )


def configure_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(
        "job_mail_desk.ui_app.APPLICATIONS_DIR",
        tmp_path / "applications",
    )
    monkeypatch.setattr(
        "job_mail_desk.ui_app.UNRESOLVED_DIR",
        tmp_path / "unresolved",
    )
    monkeypatch.setattr("job_mail_desk.ui_app.STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(
        "job_mail_desk.ui_app.DASHBOARD_FILE",
        tmp_path / "dashboard.md",
    )


def test_confirmation_atomically_creates_progress_and_optional_task(
    tmp_path,
    monkeypatch,
) -> None:
    configure_paths(monkeypatch, tmp_path)
    record = pending_record("a" * 32)
    UnresolvedStore(tmp_path / "unresolved").save(record)
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)
    request_id = "op1_" + "b" * 32

    api.resolve_unresolved_workflow(
        record.id,
        {
            "request_id": request_id,
            "expected_review_revision": 3,
            "mode": "new",
            "company": "样例科技",
            "role": "后端工程师",
            "stage": "笔试",
            "manual_stage_status": "pending",
            "create_task": True,
        },
    )

    application = ApplicationRegistry(tmp_path / "applications").all()[0]
    task = MarkdownTaskStore(tmp_path / "tasks").all()[0]
    resolved = UnresolvedStore(tmp_path / "unresolved").load(record.id)
    assert task.application_key == application.application_key
    assert task.source_message_hash == record.id
    assert task.duration_minutes == 90
    assert task.status == "planned"
    assert application.progress_nodes[0]["source_task_id"] == task.id
    assert application.manual_progress_history == []
    assert resolved and resolved.confirmation_operation_id == request_id
    assert resolved.resolved_task_id == task.id


def test_stale_review_revision_writes_nothing(tmp_path, monkeypatch) -> None:
    configure_paths(monkeypatch, tmp_path)
    record = pending_record("c" * 32)
    UnresolvedStore(tmp_path / "unresolved").save(record)
    api = DesktopApi(Settings())

    with pytest.raises(ValueError, match="已更新"):
        api.resolve_unresolved_workflow(
            record.id,
            {
                "request_id": "op1_" + "d" * 32,
                "expected_review_revision": 2,
                "mode": "new",
                "company": "样例科技",
                "role": "后端工程师",
                "stage": "笔试",
            },
        )

    assert ApplicationRegistry(tmp_path / "applications").all() == []
    assert UnresolvedStore(tmp_path / "unresolved").load(record.id).status == "pending"


def test_task_default_requires_time_deadline_or_link_not_stage_alone(
    tmp_path,
    monkeypatch,
) -> None:
    configure_paths(monkeypatch, tmp_path)
    application = ApplicationRecord(
        application_key="app-existing",
        company_key="sample",
        company="样例科技",
        recruiting_project=None,
        recruiting_year=2027,
        business_unit=None,
        role="后端工程师",
        role_aliases=[],
        job_code=None,
        submitted_at=None,
        status="active",
        source="desktop-ui",
        confirmed_by_user=True,
        identity_locked=True,
        manual_stage="网申",
    )
    ApplicationRegistry(tmp_path / "applications").save(application)
    record = pending_record("f" * 32)
    record = record.__class__(
        **{
            **record.__dict__,
            "start_at": None,
            "deadline_at": None,
            "recommendation_reasons": ("stage_advanced",),
            "recommended_application_key": application.application_key,
        }
    )
    UnresolvedStore(tmp_path / "unresolved").save(record)
    api = DesktopApi(Settings())

    recommendation = api.get_review_recommendation(
        record.id,
        application.application_key,
    )

    assert recommendation["stage_advanced"] is True
    assert recommendation["recommend_task"] is True
    assert recommendation["default_create_task"] is False


def test_lower_stage_review_adds_history_without_regressing_application(
    tmp_path,
    monkeypatch,
) -> None:
    configure_paths(monkeypatch, tmp_path)
    application = ApplicationRecord(
        application_key="app-existing",
        company_key="sample",
        company="样例科技",
        recruiting_project=None,
        recruiting_year=2027,
        business_unit=None,
        role="后端工程师",
        role_aliases=[],
        job_code=None,
        submitted_at=None,
        status="active",
        source="desktop-ui",
        confirmed_by_user=True,
        identity_locked=True,
        manual_stage="笔试",
        manual_stage_status="completed",
    )
    ApplicationRegistry(tmp_path / "applications").save(application)
    record = pending_record("1" * 32)
    record = record.__class__(
        **{
            **record.__dict__,
            "stage": "网申",
            "event_type": "application",
            "start_at": None,
            "deadline_at": None,
        }
    )
    UnresolvedStore(tmp_path / "unresolved").save(record)
    api = DesktopApi(Settings())
    monkeypatch.setattr(api, "_export", lambda _store: None)

    api.resolve_unresolved_workflow(
        record.id,
        {
            "request_id": "op1_" + "2" * 32,
            "expected_review_revision": record.revision,
            "expected_application_revision": application.revision,
            "mode": "existing",
            "application_key": application.application_key,
            "company": application.company,
            "role": application.role,
            "stage": "网申",
            "manual_stage_status": "pending",
            "create_task": False,
        },
    )

    saved = ApplicationRegistry(tmp_path / "applications").load(
        application.application_key
    )
    assert saved and saved.manual_stage == "笔试"
    assert saved.manual_stage_status == "completed"
    assert saved.progress_nodes[0]["stage"] == "网申"


def test_committed_confirmation_retries_failed_derived_work(
    tmp_path,
    monkeypatch,
) -> None:
    configure_paths(monkeypatch, tmp_path)
    record = pending_record("3" * 32)
    UnresolvedStore(tmp_path / "unresolved").save(record)
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(
        api,
        "_export",
        lambda _store: (_ for _ in ()).throw(OSError("locked")),
    )

    api.resolve_unresolved_workflow(
        record.id,
        {
            "request_id": "op1_" + "4" * 32,
            "expected_review_revision": record.revision,
            "mode": "new_identity",
            "company": "样例科技",
            "role": "后端工程师",
            "stage": "笔试",
            "manual_stage_status": "pending",
            "create_task": False,
        },
    )

    assert UnresolvedStore(tmp_path / "unresolved").load(record.id).status == "resolved"
    outbox = DerivedOutbox(tmp_path / "derived-outbox.json")
    assert outbox.pending()
    monkeypatch.setattr(api, "_export", lambda _store: None)
    api.get_dashboard()
    assert outbox.pending() == []


def test_semantically_duplicate_mail_sources_share_one_progress_node() -> None:
    application = ApplicationRecord(
        application_key="app-existing",
        company_key="sample",
        company="样例科技",
        recruiting_project="2027校园招聘",
        recruiting_year=2027,
        business_unit=None,
        role="后端工程师",
        role_aliases=[],
        job_code=None,
        submitted_at=None,
        status="active",
        source="test",
        confirmed_by_user=True,
        identity_locked=True,
    )
    first = pending_record("a" * 32)
    second = replace(
        first,
        id="b" * 32,
        received_at=first.received_at.replace(minute=1),
    )
    first_id = append_progress_node(
        application,
        first,
        operation_id="op1_" + "1" * 32,
        stage="笔试",
        status="pending",
        next_stage=None,
        task_id=None,
    )
    second_id = append_progress_node(
        application,
        second,
        operation_id="op1_" + "2" * 32,
        stage="笔试",
        status="pending",
        next_stage=None,
        task_id=None,
    )
    assert first_id == second_id
    assert len(application.progress_nodes) == 1
    assert application.progress_nodes[0]["source_hashes"] == [
        "a" * 32,
        "b" * 32,
    ]


def test_new_attempt_uses_pending_identity_metadata_and_mail_received_time(
    tmp_path,
    monkeypatch,
) -> None:
    configure_paths(monkeypatch, tmp_path)
    target = ApplicationRecord(
        application_key="app-existing",
        company_key="sample",
        company="样例科技",
        recruiting_project=None,
        recruiting_year=None,
        business_unit=None,
        role="后端工程师",
        role_aliases=[],
        job_code=None,
        submitted_at=datetime(2025, 8, 1, tzinfo=SHANGHAI),
        status="ended",
        source="desktop-ui",
        confirmed_by_user=True,
        identity_locked=True,
    )
    registry = ApplicationRegistry(tmp_path / "applications")
    registry.save(target)
    record = replace(
        pending_record("5" * 32),
        role_raw="后端工程师（上海）(J10001)",
        role_canonical="后端工程师",
        recruiting_project="2027 校招",
        recruiting_year=2027,
        business_unit="基础架构事业群",
        job_code="J10001",
        location="上海",
        location_confidence=0.96,
        location_source="explicit-job-location",
    )
    UnresolvedStore(tmp_path / "unresolved").save(record)
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)

    api.resolve_unresolved_workflow(
        record.id,
        {
            "request_id": "op1_" + "6" * 32,
            "expected_review_revision": record.revision,
            "expected_application_revision": target.revision,
            "mode": "new_attempt",
            "application_key": target.application_key,
            "stage": record.stage,
            "manual_stage_status": "pending",
            "create_task": False,
        },
    )

    created = next(
        item
        for item in registry.all()
        if item.application_key != target.application_key
    )
    assert created.attempt_sequence == 2
    assert created.submitted_at == record.received_at
    assert created.recruiting_project == "2027 校招"
    assert created.recruiting_year == 2027
    assert created.business_unit == "基础架构事业群"
    assert created.job_code == "J10001"
    assert created.location == "上海"
    assert record.role_raw in created.role_aliases
    assert "mail-review-confirmed" in created.identity_evidence
    assert "location-source:explicit-job-location" in created.identity_evidence


def test_incoming_identity_fills_only_blank_existing_target_metadata(
    tmp_path,
    monkeypatch,
) -> None:
    configure_paths(monkeypatch, tmp_path)
    target = ApplicationRecord(
        application_key="app-blank-target",
        company_key="sample",
        company="样例科技",
        recruiting_project=None,
        recruiting_year=None,
        business_unit=None,
        role=None,
        role_aliases=[],
        job_code=None,
        submitted_at=None,
        status="active",
        source="desktop-ui",
        confirmed_by_user=True,
        identity_locked=True,
        location=None,
    )
    registry = ApplicationRegistry(tmp_path / "applications")
    registry.save(target)
    record = replace(
        pending_record("9" * 32),
        role_raw="后端工程师（上海）(J10001)",
        role_canonical="后端工程师",
        recruiting_project="基础架构校招",
        recruiting_year=2027,
        business_unit="基础架构事业群",
        job_code="J10001",
        location="上海",
        location_confidence=0.96,
        location_source="explicit-job-location",
    )
    UnresolvedStore(tmp_path / "unresolved").save(record)
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)

    api.resolve_unresolved_workflow(
        record.id,
        {
            "request_id": "op1_" + "9" * 32,
            "expected_review_revision": record.revision,
            "expected_application_revision": target.revision,
            "mode": "update_active",
            "application_key": target.application_key,
            "stage": record.stage,
            "manual_stage_status": "pending",
            "create_task": False,
        },
    )

    updated = registry.load(target.application_key)
    assert updated
    assert updated.role == "后端工程师"
    assert updated.recruiting_project == "基础架构校招"
    assert updated.recruiting_year == 2027
    assert updated.business_unit == "基础架构事业群"
    assert updated.job_code == "J10001"
    assert updated.location == "上海"
    assert record.role_raw in updated.role_aliases
    assert "location-source:explicit-job-location" in updated.identity_evidence


def test_confirmation_distinguishes_cleared_dates_from_omitted_dates(
    tmp_path,
    monkeypatch,
) -> None:
    configure_paths(monkeypatch, tmp_path)
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)
    cleared = replace(
        pending_record("7" * 32),
        end_at=datetime(2026, 8, 15, 20, 30, tzinfo=SHANGHAI),
    )
    preserved = replace(cleared, id="8" * 32)
    unresolved = UnresolvedStore(tmp_path / "unresolved")
    unresolved.save(cleared)
    unresolved.save(preserved)
    base_payload = {
        "expected_review_revision": cleared.revision,
        "mode": "new_identity",
        "company": cleared.company,
        "role": cleared.role,
        "stage": cleared.stage,
        "manual_stage_status": "pending",
        "create_task": True,
    }

    api.resolve_unresolved_workflow(
        cleared.id,
        {
            **base_payload,
            "request_id": "op1_" + "7" * 32,
            "start_at": None,
            "end_at": "",
            "deadline_at": None,
        },
    )
    api.resolve_unresolved_workflow(
        preserved.id,
        {
            **base_payload,
            "request_id": "op1_" + "8" * 32,
        },
    )

    tasks = {
        task.source_message_hash: task
        for task in MarkdownTaskStore(tmp_path / "tasks").all()
    }
    assert tasks[cleared.id].start_at is None
    assert tasks[cleared.id].end_at is None
    assert tasks[cleared.id].deadline_at is None
    assert tasks[preserved.id].start_at == preserved.start_at
    assert tasks[preserved.id].end_at == preserved.end_at
    assert tasks[preserved.id].deadline_at == preserved.deadline_at
