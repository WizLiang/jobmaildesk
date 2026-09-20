from datetime import datetime, timedelta

import job_mail_desk.dashboard as dashboard
from job_mail_desk.activity_store import ActivityStore
from job_mail_desk.application_registry import (
    ApplicationRegistry,
    application_from_user_payload,
)
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.models import JobTask
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.task_service import create_manual_task
from job_mail_desk.unresolved_store import UnresolvedRecord, UnresolvedStore


def test_completed_task_stays_in_dashboard_and_active_count_excludes_it(
    tmp_path,
    monkeypatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    store = MarkdownTaskStore(tasks_dir)
    task = JobTask(
        id="1" * 24,
        application_id="2" * 20,
        company="京东",
        role="TET 综合方向",
        recruiting_project=None,
        event_type="manual",
        stage="群面",
        round="群面",
        received_at=datetime(2026, 7, 31, 10, 0, tzinfo=SHANGHAI),
        start_at=datetime(2026, 8, 6, 14, 0, tzinfo=SHANGHAI),
        end_at=datetime(2026, 8, 6, 17, 0, tzinfo=SHANGHAI),
        deadline_at=None,
        priority="high",
        status="done",
        change_type="new",
        source_message_hash="manual",
        research_status="closed",
        confidence=1.0,
        title="参加京东群面",
        action_summary="参加京东群面",
    )
    store.save(task)
    monkeypatch.setattr(dashboard, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(dashboard, "STATE_DB", tmp_path / "state.db")
    payload = dashboard.dashboard_payload(tmp_path / "research.jsonl")
    assert [item["id"] for item in payload["tasks"]] == [task.id]
    assert payload["tasks"][0]["status"] == "done"
    assert payload["tasks"][0]["view"] == "progress"
    assert payload["counts"]["list"] == 0
    assert payload["counts"]["today"] == 0
    assert payload["counts"]["progress"] == 1
    assert payload["progress"][0]["current_stage"] == "群面"


def test_snoozed_task_leaves_attention_views_but_keeps_event_time() -> None:
    now = datetime(2026, 8, 1, 10, 0, tzinfo=SHANGHAI)
    task = JobTask(
        id="3" * 24,
        application_id="4" * 20,
        company="京东",
        role="TET 综合方向",
        recruiting_project=None,
        event_type="manual",
        stage="群面",
        round="群面",
        received_at=now,
        start_at=now + timedelta(hours=4),
        end_at=None,
        deadline_at=None,
        priority="high",
        status="planned",
        change_type="new",
        source_message_hash="manual",
        research_status="closed",
        confidence=1.0,
        title="参加京东群面",
        action_summary="参加京东群面",
        source_url="https://example.com/notice",
        snoozed_until=now + timedelta(hours=2),
    )
    item = dashboard._task_payload(task, now)
    assert item["view"] == "snoozed"
    assert item["time"] == task.start_at.isoformat()
    assert item["snoozed_until"] == task.snoozed_until.isoformat()
    assert item["has_source"] is True


def test_needs_review_without_time_stays_out_of_todo_list() -> None:
    now = datetime(2026, 8, 1, 10, 0, tzinfo=SHANGHAI)
    task = JobTask(
        id="8" * 24,
        application_id="9" * 20,
        company="样例公司",
        role="管培生",
        recruiting_project=None,
        event_type="application",
        stage="简历筛选",
        round=None,
        received_at=now,
        start_at=None,
        end_at=None,
        deadline_at=None,
        priority="normal",
        status="needs_review",
        change_type="new",
        source_message_hash="a" * 32,
        research_status="closed",
        confidence=1.0,
        title="简历筛选中",
        action_summary="等待后续通知",
    )

    item = dashboard._task_payload(task, now)
    assert item["view"] == "review"
    assert item["actionable"] is False


def test_manual_review_and_end_only_tasks_stay_in_todo_list(tmp_path) -> None:
    now = datetime.now(SHANGHAI)
    tasks_dir = tmp_path / "tasks"
    store = MarkdownTaskStore(tasks_dir)
    no_time = create_manual_task(
        {
            "company": "个人计划",
            "stage": "复盘",
            "action_summary": "整理项目复盘",
        },
        store,
        now=now,
    )
    end_only = create_manual_task(
        {
            "company": "个人计划",
            "stage": "材料提交",
            "end_at": (now + timedelta(hours=2)).isoformat(),
            "action_summary": "提交申请材料",
        },
        store,
        now=now,
    )

    payload = dashboard.dashboard_payload(
        tmp_path / "research.jsonl",
        tasks_dir=tasks_dir,
        state_db=tmp_path / "state.db",
        unresolved_dir=tmp_path / "unresolved",
        applications_dir=tmp_path / "applications",
    )
    tasks = {item["id"]: item for item in payload["tasks"]}

    assert tasks[no_time.id]["status"] == "needs_review"
    assert tasks[no_time.id]["view"] == "review"
    assert tasks[no_time.id]["actionable"] is True
    assert tasks[end_only.id]["status"] == "planned"
    assert tasks[end_only.id]["time"] == end_only.end_at.isoformat()
    assert tasks[end_only.id]["actionable"] is True
    assert payload["counts"]["review"] == 1
    assert payload["counts"]["list"] == 2


def test_dashboard_cache_reuses_unchanged_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(dashboard, "STATE_DB", tmp_path / "state.db")
    cache = tmp_path / "dashboard-cache.json"
    first = dashboard.cached_dashboard_payload(
        tmp_path / "research.jsonl",
        cache_path=cache,
    )

    def fail_if_recomputed(*_args, **_kwargs):
        raise AssertionError("unchanged dashboard should use cache")

    monkeypatch.setattr(dashboard, "dashboard_payload", fail_if_recomputed)
    second = dashboard.cached_dashboard_payload(
        tmp_path / "research.jsonl",
        cache_path=cache,
    )
    assert second == first


def test_dashboard_cache_retries_when_facts_change_during_render(
    tmp_path,
    monkeypatch,
) -> None:
    signatures = iter(
        (
            [["state", 1]],
            [["state", 1]],
            [["state", 2]],
            [["state", 2]],
            [["state", 2]],
        )
    )
    renders: list[int] = []
    monkeypatch.setattr(dashboard, "_source_signature", lambda *_a, **_k: next(signatures))

    def render_payload(*_args, **_kwargs):
        renders.append(len(renders) + 1)
        return {"render": renders[-1]}

    monkeypatch.setattr(dashboard, "dashboard_payload", render_payload)

    payload = dashboard.cached_dashboard_payload(
        tmp_path / "research.jsonl",
        cache_path=tmp_path / "cache.json",
    )

    assert len(renders) == 2
    assert payload["render"] == 2


def test_expired_task_is_hidden_from_action_views_but_kept_in_progress(
    tmp_path,
    monkeypatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    store = MarkdownTaskStore(tasks_dir)
    task = JobTask(
        id="5" * 24,
        application_id="6" * 20,
        company="样例公司",
        role="产品经理",
        recruiting_project=None,
        event_type="assessment",
        stage="在线笔试",
        round=None,
        received_at=datetime(2026, 7, 20, 10, 0, tzinfo=SHANGHAI),
        start_at=datetime(2026, 7, 21, 10, 0, tzinfo=SHANGHAI),
        end_at=None,
        deadline_at=None,
        priority="normal",
        status="expired",
        change_type="new",
        source_message_hash="7" * 32,
        research_status="closed",
        confidence=1.0,
        title="已过期笔试",
        action_summary="参加在线笔试",
    )
    store.save(task)
    monkeypatch.setattr(dashboard, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(dashboard, "STATE_DB", tmp_path / "state.db")

    payload = dashboard.dashboard_payload(tmp_path / "research.jsonl")
    assert payload["tasks"] == []
    assert payload["counts"]["list"] == 0
    assert payload["progress"][0]["current_stage"] == "在线笔试"


def test_unresolved_items_share_the_review_count(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(dashboard, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(dashboard, "UNRESOLVED_DIR", tmp_path / "unresolved")
    monkeypatch.setattr(dashboard, "APPLICATIONS_DIR", tmp_path / "applications")
    record = UnresolvedRecord(
        id="a" * 32,
        status="pending",
        resolution_status="unresolved",
        reason="multiple-candidates",
        company="样例公司",
        role=None,
        recruiting_project=None,
        event_type="assessment",
        stage="在线笔试",
        round=None,
        received_at=datetime(2026, 8, 8, 8, 0, tzinfo=SHANGHAI),
        start_at=None,
        end_at=None,
        deadline_at=None,
        action_summary="确认申请归属",
        title="笔试通知",
        requirements=(),
        confidence=0.8,
        change_type="new",
        candidate_application_keys=(),
        resolved_application_key=None,
        resolved_task_id=None,
        rule_version="identity-registry-v1",
    )
    UnresolvedStore(tmp_path / "unresolved").save(record)

    payload = dashboard.dashboard_payload(tmp_path / "research.jsonl")
    assert payload["counts"]["review"] == 1
    assert payload["unresolved"][0]["attention_type"] == "unresolved_identity"


def test_terminal_ledger_status_removes_task_from_action_views(
    tmp_path,
    monkeypatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    store = MarkdownTaskStore(tasks_dir)
    task = JobTask(
        id="c" * 24,
        application_id="d" * 20,
        application_key="app-1234567890abcdef1234",
        company="旧企业",
        role="旧岗位",
        recruiting_project=None,
        event_type="interview",
        stage="面试",
        round=None,
        received_at=datetime(2026, 8, 8, 8, 0, tzinfo=SHANGHAI),
        start_at=datetime(2026, 8, 9, 14, 0, tzinfo=SHANGHAI),
        end_at=None,
        deadline_at=None,
        priority="high",
        status="planned",
        change_type="new",
        source_message_hash="e" * 32,
        research_status="closed",
        confidence=1.0,
        title="面试通知",
        action_summary="参加面试",
    )
    store.save(task)
    ledger = tmp_path / "台账.md"
    ledger.write_text(
        """### 已投递或已进入流程
- [x] 新企业｜新岗位｜**未通过**｜停止跟进 <!-- jobmaildesk:application:app-1234567890abcdef1234 -->
### 当前优先待投
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(dashboard, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(dashboard, "UNRESOLVED_DIR", tmp_path / "unresolved")
    monkeypatch.setattr(dashboard, "APPLICATIONS_DIR", tmp_path / "applications")

    payload = dashboard.dashboard_payload(tmp_path / "research.jsonl", ledger)
    assert payload["tasks"][0]["company"] == "新企业"
    assert payload["tasks"][0]["role"] == "新岗位"
    assert payload["tasks"][0]["view"] == "progress"
    assert payload["tasks"][0]["actionable"] is False
    assert payload["counts"]["today"] == 0


def test_overview_uses_72_hour_window_hides_overdue_and_sorts_unresolved(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime.now(SHANGHAI)
    tasks_dir = tmp_path / "tasks"
    store = MarkdownTaskStore(tasks_dir)
    base = dict(
        application_id="f" * 20,
        company="速览公司",
        role="产品经理",
        recruiting_project=None,
        event_type="manual",
        stage="面试",
        round=None,
        received_at=now,
        end_at=None,
        priority="high",
        status="planned",
        change_type="new",
        source_message_hash="manual",
        research_status="closed",
        confidence=1.0,
        title="参加面试",
        action_summary="参加面试",
    )
    urgent = JobTask(
        id="1" * 24,
        start_at=now + timedelta(hours=71),
        deadline_at=None,
        **base,
    )
    overdue = JobTask(
        id="2" * 24,
        start_at=now - timedelta(minutes=1),
        deadline_at=None,
        **base,
    )
    outside_urgent = JobTask(
        id="3" * 24,
        start_at=now + timedelta(hours=73),
        deadline_at=None,
        **base,
    )
    snoozed = JobTask(
        id="4" * 24,
        start_at=now + timedelta(hours=1),
        deadline_at=None,
        snoozed_until=now + timedelta(hours=4),
        **base,
    )
    store.save(urgent)
    store.save(overdue)
    store.save(outside_urgent)
    store.save(snoozed)
    unresolved_dir = tmp_path / "unresolved"
    unresolved_store = UnresolvedStore(unresolved_dir)
    for index in range(4):
        unresolved_store.save(
            UnresolvedRecord(
                id=str(index) * 32,
                status="pending",
                resolution_status="unresolved",
                reason="identity",
                company=f"公司{index}",
                role="岗位",
                recruiting_project=None,
                event_type="assessment",
                stage="笔试",
                round=None,
                received_at=now - timedelta(hours=index),
                start_at=None,
                end_at=None,
                deadline_at=None,
                action_summary="确认归属",
                title="笔试通知",
                requirements=(),
                confidence=0.7,
                change_type="new",
                candidate_application_keys=(),
                resolved_application_key=None,
                resolved_task_id=None,
                rule_version="test",
            )
        )
    monkeypatch.setattr(dashboard, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(dashboard, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(dashboard, "UNRESOLVED_DIR", unresolved_dir)
    monkeypatch.setattr(dashboard, "APPLICATIONS_DIR", tmp_path / "applications")

    payload = dashboard.dashboard_payload(tmp_path / "research.jsonl")

    assert [item["id"] for item in payload["overview"]["urgent"]] == [urgent.id]
    overview_ids = {
        item["id"]
        for section in ("urgent", "today", "week")
        for item in payload["overview"][section]
    }
    assert overdue.id not in overview_ids
    assert outside_urgent.id not in {
        item["id"] for item in payload["overview"]["urgent"]
    }
    assert snoozed.id not in overview_ids
    assert [item["company"] for item in payload["overview"]["latest_unresolved"]] == [
        "公司0",
        "公司1",
        "公司2",
    ]


def test_progress_only_application_reaches_cache_timeline_and_unread_ack(
    tmp_path,
) -> None:
    applications_dir = tmp_path / "applications"
    current = datetime(2026, 8, 15, 11, 27, tzinfo=SHANGHAI)
    record = application_from_user_payload(
        {
            "company": "江波龙",
            "role": "IC实现工程师（上海）(J11374)",
            "job_code": "J11374",
            "location": "上海",
        },
        now=current,
    )
    record.progress_nodes = [
        {
            "id": "progress-" + "4" * 24,
            "source_hash": "5" * 32,
            "event_at": current.isoformat(),
            "stage": "网申",
            "status": "completed",
            "next_stage": "简历筛选",
        }
    ]
    second = application_from_user_payload(
        {
            "company": "江波龙",
            "role": "数字验证工程师",
            "manual_stage": "网申",
        },
        now=current,
    )
    registry = ApplicationRegistry(applications_dir)
    registry.save(record)
    registry.save(second)
    activity = ActivityStore(tmp_path / "activity-state.json")
    activity.record_event(
        dedup_key="application:jiangbolong:progress",
        kind="application.updated",
        tabs=("progress",),
        company=record.company,
        entity_id=f"application:{record.application_key}",
    )

    payload = dashboard.cached_dashboard_payload(
        tmp_path / "research.jsonl",
        cache_path=tmp_path / "dashboard-cache.json",
        tasks_dir=tmp_path / "tasks",
        state_db=tmp_path / "state.db",
        unresolved_dir=tmp_path / "unresolved",
        applications_dir=applications_dir,
    )

    assert payload["tasks"] == []
    assert payload["counts"]["progress"] == 2
    assert {item["application_id"] for item in payload["progress"]} == {
        record.application_key,
        second.application_key,
    }
    assert payload["company_timelines"][0]["company"] == "江波龙"
    assert payload["company_timelines"][0]["application_count"] == 2
    unread = activity.unread_payload()
    assert unread["progress_unread_companies"] == ["江波龙"]
    assert any(
        item["company"] in unread["progress_unread_companies"]
        for item in payload["company_timelines"]
    )
    acknowledged = activity.acknowledge_progress_company(
        "江波龙",
        through_sequence=unread["snapshot_sequence"],
    )
    assert acknowledged["counts"]["progress"] == 0

    cached = dashboard.cached_dashboard_payload(
        tmp_path / "research.jsonl",
        cache_path=tmp_path / "dashboard-cache.json",
        tasks_dir=tmp_path / "tasks",
        state_db=tmp_path / "state.db",
        unresolved_dir=tmp_path / "unresolved",
        applications_dir=applications_dir,
    )
    assert cached["progress"] == payload["progress"]


def test_removing_last_task_keeps_registry_progress_card(tmp_path) -> None:
    tasks_dir = tmp_path / "tasks"
    applications_dir = tmp_path / "applications"
    store = MarkdownTaskStore(tasks_dir)
    current = datetime(2026, 8, 15, 12, 0, tzinfo=SHANGHAI)
    record = application_from_user_payload(
        {
            "company": "江波龙",
            "role": "IC实现工程师（上海）(J11374)",
        },
        now=current,
    )
    source = create_manual_task(
        {
            "company": record.company,
            "role": record.role,
            "stage": "笔试",
            "action_summary": "参加笔试",
        },
        store,
        now=current,
    )
    source.application_key = record.application_key
    store.save(source)
    record.progress_nodes = [
        {
            "id": "progress-" + "6" * 24,
            "source_hash": "7" * 32,
            "source_task_id": source.id,
            "event_at": current.isoformat(),
            "stage": "笔试",
            "status": "pending",
            "next_stage": None,
        }
    ]
    ApplicationRegistry(applications_dir).save(record)

    before = dashboard.dashboard_payload(
        tmp_path / "research.jsonl",
        tasks_dir=tasks_dir,
        state_db=tmp_path / "state.db",
        unresolved_dir=tmp_path / "unresolved",
        applications_dir=applications_dir,
    )
    assert len(before["progress"]) == 1
    assert before["progress"][0]["history"][0]["task_ids"] == [source.id]

    store.trash(source.id, now=current + timedelta(minutes=1))
    store.permanently_delete(source.id, now=current + timedelta(minutes=2))
    after = dashboard.dashboard_payload(
        tmp_path / "research.jsonl",
        tasks_dir=tasks_dir,
        state_db=tmp_path / "state.db",
        unresolved_dir=tmp_path / "unresolved",
        applications_dir=applications_dir,
    )

    assert after["tasks"] == []
    assert after["counts"]["progress"] == 1
    assert after["progress"][0]["application_id"] == record.application_key
    assert after["progress"][0]["history"][0]["task_id"] is None
