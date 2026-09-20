from __future__ import annotations

from pathlib import Path

import pytest

from job_mail_desk import ui_app
from job_mail_desk.application_registry import ApplicationRegistry
from job_mail_desk.config import Settings
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.ui_app import DesktopApi


def _desktop_api(tmp_path, monkeypatch) -> DesktopApi:
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
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)
    monkeypatch.setattr(api, "_refresh_task_runtime", lambda *_args, **_kwargs: None)
    return api


def test_application_and_task_edits_reject_stale_revisions(
    tmp_path,
    monkeypatch,
) -> None:
    api = _desktop_api(tmp_path, monkeypatch)
    created = api.create_application(
        {
            "company": "样例公司",
            "role": "产品经理",
            "manual_stage": "网申",
        }
    )
    application = created["progress"][0]
    application_key = application["application_key"]
    application_revision = application["revision"]
    linked_task = created["tasks"][0]

    api.edit_application(
        application_key,
        {
            "role": "高级产品经理",
            "expected_revision": application_revision,
        },
    )
    with pytest.raises(ValueError, match="已更新"):
        api.edit_application(
            application_key,
            {
                "role": "会覆盖新值的旧草稿",
                "expected_revision": application_revision,
            },
        )
    assert (
        ApplicationRegistry(tmp_path / "applications").load(application_key).role
        == "高级产品经理"
    )
    with pytest.raises(ValueError, match="已更新"):
        api.edit_task(
            linked_task["id"],
            {
                "application_key": application_key,
                "action_summary": "基于旧申请详情的待办草稿",
                "expected_revision": linked_task["revision"],
            },
        )

    dashboard = api.create_task(
        {
            "company": "独立事项",
            "role": "准备材料",
            "stage": "自定义待办",
            "action_summary": "第一版",
        }
    )
    task = next(item for item in dashboard["tasks"] if item["company"] == "独立事项")
    api.edit_task(
        task["id"],
        {
            "company": "独立事项",
            "action_summary": "第二版",
            "expected_revision": task["revision"],
        },
    )
    with pytest.raises(ValueError, match="已更新"):
        api.edit_task(
            task["id"],
            {
                "company": "过期草稿",
                "action_summary": "不应写入",
                "expected_revision": task["revision"],
            },
        )
    with pytest.raises(ValueError, match="已更新"):
        api.update_status(task["id"], "done", task["revision"])
    stored = MarkdownTaskStore(tmp_path / "tasks").load(task["id"])
    assert stored is not None
    assert stored.company == "独立事项"
    assert stored.action_summary == "第二版"


def test_merge_preview_token_binds_both_records_and_revisions(
    tmp_path,
    monkeypatch,
) -> None:
    api = _desktop_api(tmp_path, monkeypatch)
    for role in ("后端工程师", "测试工程师", "产品经理"):
        api.create_application({"company": "样例公司", "role": role})
    source, target, other = ApplicationRegistry(tmp_path / "applications").active()
    preview = api.get_application_merge_preview(
        source.application_key,
        target.application_key,
    )

    with pytest.raises(ValueError, match="预览已失效"):
        api.merge_applications(
            source.application_key,
            other.application_key,
            preview_token=preview["preview_token"],
        )

    api.edit_application(
        target.application_key,
        {
            "manual_stage": "测评",
            "expected_revision": target.revision,
        },
    )
    with pytest.raises(ValueError, match="预览已失效"):
        api.merge_applications(
            source.application_key,
            target.application_key,
            preview_token=preview["preview_token"],
        )
    assert (
        ApplicationRegistry(tmp_path / "applications")
        .raw_load(source.application_key)
        .merged_into
        is None
    )


def test_progress_unread_scope_survives_rename_without_orphan_badge(
    tmp_path,
    monkeypatch,
) -> None:
    api = _desktop_api(tmp_path, monkeypatch)
    created = api.create_application({"company": "旧公司", "role": "工程师"})
    application = created["progress"][0]
    old_scope = application["progress_scope"]
    assert created["unread"]["progress_unread_companies"] == [old_scope]

    renamed = api.edit_application(
        application["application_key"],
        {
            "company": "新公司",
            "expected_revision": application["revision"],
        },
    )
    current = renamed["progress"][0]
    assert current["progress_scope"] != old_scope
    assert old_scope not in renamed["unread"]["progress_unread_companies"]
    assert renamed["unread"]["progress_unread_companies"] == [
        current["progress_scope"]
    ]
    assert renamed["unread"]["counts"]["progress"] == 1

    acknowledged = api.acknowledge_progress_update(
        [current["progress_scope"], current["company"]],
        renamed["unread"]["snapshot_sequence"],
    )
    assert acknowledged["counts"]["progress"] == 0


def test_capsule_generation_discards_out_of_order_geometry(
    tmp_path,
    monkeypatch,
) -> None:
    scheduled: list[tuple[object, tuple[object, ...]]] = []

    class DeferredThread:
        def __init__(self, *, target, args, daemon) -> None:
            assert daemon is True
            self.target = target
            self.args = args

        def start(self) -> None:
            scheduled.append((self.target, self.args))

    class Window:
        x = 10
        y = 20
        width = 480
        height = 720

        def __init__(self) -> None:
            self.operations: list[tuple[object, ...]] = []

        def resize(self, width, height) -> None:
            self.operations.append(("resize", width, height))

        def move(self, x, y) -> None:
            self.operations.append(("move", x, y))

    monkeypatch.setattr(ui_app.threading, "Thread", DeferredThread)
    api = _desktop_api(tmp_path, monkeypatch)
    window = Window()
    api._window = window
    api._expanded_geometry = (10, 20, 480, 720)

    assert api.set_capsule(True) is True
    assert api.set_capsule(False) is True
    assert len(scheduled) == 2

    stale_target, stale_args = scheduled[0]
    stale_target(*stale_args)
    assert window.operations == []

    current_target, current_args = scheduled[1]
    current_target(*current_args)
    assert window.operations == [
        ("resize", 480, 720),
        ("move", 10, 20),
    ]


def test_frontend_interaction_hardening_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    ui = root / "src/job_mail_desk/ui"
    javascript = (ui / "app.js").read_text(encoding="utf-8")
    html = (ui / "index.html").read_text(encoding="utf-8")
    stylesheet = (ui / "style.css").read_text(encoding="utf-8")

    assert html.count('name="expected_revision"') == 2
    assert 'id="refreshStatus"' in html
    assert 'role="tablist"' in html
    assert html.count('aria-labelledby="') >= 7
    assert "expected_revision: application.revision ?? null" in javascript
    assert "taskForm.elements.expected_revision.value = task?.revision" in javascript
    assert "task.revision || \"\"" in javascript
    assert "function refreshBlockReason()" in javascript
    assert "const responseBlock = refreshBlockReason()" in javascript
    assert "state.pendingDashboardPayload = nextPayload" in javascript
    assert "放弃修改并刷新" in javascript
    assert "function setFormWriting(" in javascript
    assert 'dialog.dataset.writing === "true"' in javascript
    assert "state.unreadAckGeneration" in javascript
    assert "navigationGeneration !== state.navigationGeneration" in javascript
    scan_function = javascript.split("async function scanMailbox", 1)[1].split(
        "async function syncLedger",
        1,
    )[0]
    assert "activateView(" not in scan_function
    assert "navigationWasChanged" in scan_function
    assert "applicationActionForm.elements.cross_company_confirmed.addEventListener" in javascript
    assert 'event.key === "PageUp"' in javascript
    assert javascript.count("calendarTasks().filter") >= 2
    assert "state.calendarAnchor = new Date(draftDate)" in javascript
    assert "state.settingsGeneration" in javascript
    assert "Promise.allSettled" in javascript
    assert "companyUnreadCount" in javascript
    assert "state.capsuleGeneration" in javascript
    assert "showManagedDialog(" in javascript
    assert "@media (max-width: 480px)" in stylesheet
    assert ".calendar-event-open" in stylesheet
    assert ".refresh-status" in stylesheet
    assert "var(--font-scale)" in stylesheet
