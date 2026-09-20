from datetime import datetime
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from job_mail_desk.application_registry import (
    ApplicationRegistry,
    application_from_user_payload,
)
from job_mail_desk.config import Settings
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.models import JobTask
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.progress import progress_payload
from job_mail_desk.task_service import legacy_application_id
from job_mail_desk.ui_app import (
    DesktopApi,
    _claim_single_instance,
    _close_instance_handle,
    _open_obsidian_uri,
    show_existing_window,
)
from job_mail_desk.unresolved_store import UnresolvedRecord, UnresolvedStore


def test_obsidian_button_uses_obsidian_uri(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(
        "job_mail_desk.ui_app.webbrowser.open",
        lambda value: opened.append(value),
    )
    _open_obsidian_uri(Path("D:/Vault/Mobile/求职硬截止待办集.md"))
    assert opened
    assert opened[0].startswith("obsidian://open?")
    assert "vault=Mobile" in opened[0]
    assert ".md" not in opened[0]


def test_application_detail_reuses_dashboard_timeline_builder(
    tmp_path,
    monkeypatch,
) -> None:
    applications_dir = tmp_path / "applications"
    tasks_dir = tmp_path / "tasks"
    monkeypatch.setattr("job_mail_desk.ui_app.APPLICATIONS_DIR", applications_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tasks_dir)
    record = application_from_user_payload(
        {"company": "样例公司", "role": "产品经理"},
        now=datetime(2026, 8, 9, 8, 0, tzinfo=SHANGHAI),
    )
    record.manual_progress_history = [
        {
            "event_at": datetime(2026, 8, 9, 18, 0, tzinfo=SHANGHAI).isoformat(),
            "stage": "二面",
            "status": "completed",
            "next_stage": "三面",
        }
    ]
    ApplicationRegistry(applications_dir).save(record)
    source = JobTask(
        id="d" * 24,
        application_id="e" * 20,
        application_key=record.application_key,
        company=record.company,
        role=record.role,
        recruiting_project=None,
        event_type="interview",
        stage="一面",
        round="一面",
        received_at=datetime(2026, 8, 8, 10, 0, tzinfo=SHANGHAI),
        start_at=datetime(2026, 8, 9, 14, 0, tzinfo=SHANGHAI),
        end_at=None,
        deadline_at=None,
        priority="high",
        status="confirmed",
        change_type="new",
        source_message_hash="f" * 32,
        research_status="closed",
        confidence=1.0,
        title="一面通知",
        action_summary="参加一面",
    )
    MarkdownTaskStore(tasks_dir).save(source)

    detail = DesktopApi(Settings()).get_application_detail(record.application_key)
    dashboard_history = progress_payload(
        [source],
        application_records=[record],
    )[0]["history"]

    assert detail["timeline"] == dashboard_history
    assert detail["timeline"][0]["source_type"] == "manual"


def test_application_choices_recommend_only_unique_company_and_role(
    tmp_path,
    monkeypatch,
) -> None:
    applications_dir = tmp_path / "applications"
    tasks_dir = tmp_path / "tasks"
    monkeypatch.setattr("job_mail_desk.ui_app.APPLICATIONS_DIR", applications_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tasks_dir)
    registry = ApplicationRegistry(applications_dir)
    registry.save(
        application_from_user_payload(
            {"company": "蔚来", "role": "芯片设计工程师"}
        )
    )
    registry.save(
        application_from_user_payload(
            {"company": "联发科技", "role": "数字IC工程师"}
        )
    )
    api = DesktopApi(Settings())

    choices = api.list_application_choices("NIO蔚来", "芯片设计工程师")

    recommended = [item for item in choices if item["recommended"]]
    assert len(recommended) == 1
    assert recommended[0]["company"] == "蔚来"
    assert recommended[0]["same_role"] is True
    assert not any(
        item["recommended"]
        for item in api.list_application_choices("NIO蔚来", "新岗位")
    )
    registry.save(
        application_from_user_payload(
            {"company": "蔚来", "role": "芯片设计工程师"}
        )
    )
    assert not any(
        item["recommended"]
        for item in api.list_application_choices("蔚来", "芯片设计工程师")
    )
    for record in registry.all():
        record.status = "ended"
        registry.save(record)
    assert not any(
        item["recommended"]
        for item in api.list_application_choices("蔚来", "芯片设计工程师")
    )


def test_unresolved_workflow_creates_timeline_only_application_atomically(
    tmp_path,
    monkeypatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    applications_dir = tmp_path / "applications"
    unresolved_dir = tmp_path / "unresolved"
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.APPLICATIONS_DIR", applications_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.UNRESOLVED_DIR", unresolved_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr("job_mail_desk.ui_app.DASHBOARD_FILE", tmp_path / "dashboard.md")
    source_hash = "9" * 32
    UnresolvedStore(unresolved_dir).save(
        UnresolvedRecord(
            id=source_hash,
            status="pending",
            resolution_status="unresolved",
            reason="new-company",
            company="新公司",
            role="后端工程师",
            recruiting_project=None,
            event_type="assessment",
            stage="笔试",
            round=None,
            received_at=datetime(2026, 8, 10, 9, 0, tzinfo=SHANGHAI),
            start_at=datetime(2026, 8, 11, 19, 0, tzinfo=SHANGHAI),
            end_at=None,
            deadline_at=None,
            action_summary="参加笔试",
            title="笔试邀请",
            requirements=(),
            confidence=0.9,
            change_type="new",
            candidate_application_keys=(),
            resolved_application_key=None,
            resolved_task_id=None,
            rule_version="test",
            mail_locator={"mailbox": "INBOX", "uid": "8", "account_fingerprint": "x"},
        )
    )
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)

    payload = {
        "mode": "new",
        "company": "新公司",
        "role": "后端工程师",
        "stage": "笔试",
        "manual_stage_status": "pending",
        "next_stage": "一面",
        "start_at": "2026-08-11T19:00:00+08:00",
        "action_summary": "参加笔试",
        "create_task": False,
    }
    api.resolve_unresolved_workflow(source_hash, payload)
    api.resolve_unresolved_workflow(source_hash, payload)

    application = ApplicationRegistry(applications_dir).all()[0]
    resolved = UnresolvedStore(unresolved_dir).load(source_hash)
    assert application.company == "新公司"
    assert application.manual_stage == "笔试"
    assert len(application.progress_nodes) == 1
    assert application.progress_nodes[0]["source_hash"] == source_hash
    assert resolved and resolved.status == "resolved"
    assert resolved.resolved_task_id is None
    assert resolved.progress_node_id == application.progress_nodes[0]["id"]
    assert len(ApplicationRegistry(applications_dir).all()) == 1
    assert MarkdownTaskStore(tasks_dir).all() == []


def test_unresolved_workflow_failure_does_not_leave_new_application(
    tmp_path,
    monkeypatch,
) -> None:
    applications_dir = tmp_path / "applications"
    unresolved_dir = tmp_path / "unresolved"
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr("job_mail_desk.ui_app.APPLICATIONS_DIR", applications_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.UNRESOLVED_DIR", unresolved_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(
        "job_mail_desk.ui_app.DASHBOARD_FILE",
        tmp_path / "dashboard.md",
    )
    record = UnresolvedRecord(
        id="8" * 32,
        status="pending",
        resolution_status="unresolved",
        reason="new-company",
        company="新公司",
        role="产品经理",
        recruiting_project=None,
        event_type="interview",
        stage="一面",
        round=None,
        received_at=datetime(2026, 8, 10, 9, 0, tzinfo=SHANGHAI),
        start_at=None,
        end_at=None,
        deadline_at=None,
        action_summary="参加面试",
        title="面试邀请",
        requirements=(),
        confidence=0.8,
        change_type="new",
        candidate_application_keys=(),
        resolved_application_key=None,
        resolved_task_id=None,
        rule_version="test",
    )
    UnresolvedStore(unresolved_dir).save(record)
    api = DesktopApi(Settings())

    with pytest.raises(ValueError):
        api.resolve_unresolved_workflow(
            record.id,
            {
                "mode": "new",
                "company": "新公司",
                "role": "产品经理",
                "stage": "一面",
                "start_at": "not-a-time",
            },
        )

    assert ApplicationRegistry(applications_dir).all() == []
    assert UnresolvedStore(unresolved_dir).load(record.id).status == "pending"


def test_ledger_only_application_materializes_idempotently(
    tmp_path,
    monkeypatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    applications_dir = tmp_path / "applications"
    ledger = tmp_path / "岗位投递决策台账.md"
    legacy_id = "c" * 20
    ledger.write_text(
        "### 已投递或已进入流程\n"
        f"- [x] 沐曦｜芯片工程师｜上海｜**流程结束**｜保留复盘 "
        f"<!-- jobmaildesk:application:{legacy_id} -->\n"
        "### 当前优先待投\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.APPLICATIONS_DIR", applications_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.DASHBOARD_FILE", tmp_path / "dashboard.md")
    api = DesktopApi(
        Settings(
            progress_source=ledger,
            research_queue=tmp_path / "research.jsonl",
        )
    )
    monkeypatch.setattr(api, "_export", lambda _store: None)
    payload = {
        "legacy_application_id": legacy_id,
        "company": "沐曦",
        "role": "芯片工程师",
        "location": "上海",
        "status": "ended",
        "manual_stage": "流程结束",
        "manual_stage_status": "completed",
    }

    first = api.materialize_progress_application(payload)
    second = api.materialize_progress_application(
        {
            **payload,
            "role": "验证工程师",
            "location": "北京",
            "manual_stage": "二面",
            "manual_stage_status": "pending",
            "status": "active",
        }
    )

    key = first["materialized_application_key"]
    assert second["materialized_application_key"] == key
    assert key.startswith("app-")
    assert len(ApplicationRegistry(applications_dir).all()) == 1
    assert len(MarkdownTaskStore(tasks_dir).all()) == 1
    assert len(first["progress"]) == len(second["progress"]) == 1
    detail = api.get_application_detail(key)
    assert detail["role"] == "验证工程师"
    assert detail["location"] == "北京"
    assert detail["manual_stage"] == "二面"
    assert detail["manual_stage_status"] == "pending"
    assert detail["legacy_application_ids"] == [legacy_id]
    task = MarkdownTaskStore(tasks_dir).all()[0]
    assert task.role == "验证工程师"
    assert task.location == "北京"


def test_quick_edit_syncs_role_and_location_across_application_chain(
    tmp_path,
    monkeypatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    applications_dir = tmp_path / "applications"
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.APPLICATIONS_DIR", applications_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.DASHBOARD_FILE", tmp_path / "dashboard.md")
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)
    api.create_application(
        {
            "company": "样例公司",
            "role": "产品经理",
            "location": "上海",
            "manual_stage": "网申",
            "manual_stage_status": "pending",
        }
    )
    record = ApplicationRegistry(applications_dir).all()[0]
    store = MarkdownTaskStore(tasks_dir)
    first_task = store.all()[0]
    second_task = JobTask.from_dict(
        {
            **first_task.to_dict(),
            "id": "f" * 24,
            "source_message_hash": "e" * 32,
        }
    )
    store.save(second_task)

    api.edit_application(
        record.application_key,
        {
            "role": "高级产品经理",
            "location": "北京",
            "manual_stage": "二面",
            "manual_stage_status": "completed",
        },
    )

    updated = ApplicationRegistry(applications_dir).load(record.application_key)
    assert updated is not None
    assert updated.role == "高级产品经理"
    assert updated.location == "北京"
    assert updated.manual_stage == "二面"
    assert updated.manual_stage_status == "completed"
    assert {(task.role, task.location) for task in store.all()} == {
        ("高级产品经理", "北京")
    }


def test_detail_edit_explicit_end_survives_task_reconciliation(
    tmp_path,
    monkeypatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    applications_dir = tmp_path / "applications"
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.APPLICATIONS_DIR", applications_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.DASHBOARD_FILE", tmp_path / "dashboard.md")
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)
    api.create_application(
        {
            "company": "样例公司",
            "role": "产品经理",
            "manual_stage": "网申",
            "manual_stage_status": "pending",
        }
    )
    record = ApplicationRegistry(applications_dir).all()[0]

    api.edit_application(
        record.application_key,
        {
            "status": "ended",
            "manual_stage": "网申",
            "manual_stage_status": "pending",
        },
    )
    ended = ApplicationRegistry(applications_dir).load(record.application_key)
    assert ended is not None
    assert ended.status == "ended"
    assert ended.workflow_status == "ended"

    api.edit_application(
        record.application_key,
        {
            "status": "active",
            "manual_stage": "网申",
            "manual_stage_status": "pending",
        },
    )
    reopened = ApplicationRegistry(applications_dir).load(record.application_key)
    assert reopened is not None
    assert reopened.status == "active"


def test_task_dialog_does_not_resize_or_move_native_window() -> None:
    api = DesktopApi(Settings())
    assert not hasattr(api, "window")
    assert not hasattr(api, "set_editor_mode")
    javascript = (
        Path(__file__).parents[1] / "src/job_mail_desk/ui/app.js"
    ).read_text(encoding="utf-8")
    assert "set_editor_mode" not in javascript


def test_status_action_updates_obsidian_checkbox_immediately(tmp_path, monkeypatch) -> None:
    task = JobTask(
        id="a" * 24,
        application_id="b" * 20,
        company="京东",
        role="TET 综合方向",
        recruiting_project=None,
        event_type="manual",
        stage="群面",
        round="群面",
        received_at=datetime(2026, 8, 1, 10, 0, tzinfo=SHANGHAI),
        start_at=datetime(2026, 8, 6, 14, 0, tzinfo=SHANGHAI),
        end_at=None,
        deadline_at=None,
        priority="high",
        status="planned",
        change_type="new",
        source_message_hash="manual",
        research_status="not_queued",
        confidence=1.0,
        title="京东群面",
        action_summary="参加京东群面",
    )
    tasks_dir = tmp_path / "tasks"
    store = MarkdownTaskStore(tasks_dir)
    store.save(task)
    obsidian = tmp_path / "求职待办.md"
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.DASHBOARD_FILE", tmp_path / "local.md")
    api = DesktopApi(Settings(obsidian_enabled=True, obsidian_output=obsidian))

    api.update_status(task.id, "done")
    assert f"- [x] **2026-08-06 14:00**" in obsidian.read_text(encoding="utf-8")
    api.update_status(task.id, "planned")
    assert f"- [ ] **2026-08-06 14:00**" in obsidian.read_text(encoding="utf-8")


def test_desktop_bridge_has_no_public_native_window() -> None:
    api = DesktopApi(Settings())
    assert not hasattr(api, "window")
    assert not hasattr(api, "settings")
    assert not hasattr(api, "download_update")
    assert not hasattr(api, "install_update")


def test_mail_connection_test_uses_unsaved_imap_form_values(monkeypatch) -> None:
    captured = {}

    class Reader:
        def __init__(self, settings, credential):
            captured["settings"] = settings
            captured["credential"] = credential

        def mailbox_snapshot(self):
            return {"unseen": 0, "uidvalidity": "1", "uidnext": "2"}

    monkeypatch.setattr("job_mail_desk.ui_app.ImapReader", Reader)
    monkeypatch.setattr(
        "job_mail_desk.ui_app.load_credential",
        lambda: (_ for _ in ()).throw(RuntimeError("missing")),
    )
    result = DesktopApi(Settings()).test_mail_settings(
        {
            "email": "user@example.test",
            "authorization_code": "one-time-code",
            "mail_provider": "custom",
            "mail_host": "mx.example.test",
            "mail_port": 587,
            "mail_ssl": False,
            "poll_minutes": 10,
            "lookback_days": 3,
            "update_channel": "preview",
        }
    )

    assert result["ok"] is True
    assert captured["settings"].mail_host == "mx.example.test"
    assert captured["settings"].mail_port == 587
    assert captured["settings"].mail_ssl is False
    assert captured["credential"].authorization_code == "one-time-code"


def test_card_actions_use_guarded_clicks_and_no_confirm_state() -> None:
    project_root = Path(__file__).resolve().parents[1]
    html = (project_root / "src/job_mail_desk/ui/index.html").read_text(
        encoding="utf-8"
    )
    javascript = (project_root / "src/job_mail_desk/ui/app.js").read_text(
        encoding="utf-8"
    )
    stylesheet = (project_root / "src/job_mail_desk/ui/style.css").read_text(
        encoding="utf-8"
    )
    assert 'data-action="confirm"' not in html
    assert 'data-action="edit_time"' in html
    assert '>查看原邮件<' in html
    assert '>打开通知链接<' in html
    assert 'id="originalMailDialog"' in html
    assert 'id="settingsDialog"' in html
    assert 'name="mail_provider"' in html
    assert 'value="custom"' in html
    assert 'name="mail_host"' in html
    assert 'name="mail_port"' in html
    assert 'name="mail_ssl"' in html
    assert "邮箱账号" in html
    assert "客户端授权码" in html
    assert "QQ邮箱" not in html
    assert 'id="createProgressTemplate"' in html
    assert 'id="checkUpdates"' not in html
    assert 'id="openUpdateRelease"' not in html
    assert 'id="selectDictionaryWorkbook"' in html
    assert 'id="compileDictionaryWorkbook"' in html
    assert 'name="dictionary_sheet"' in html
    assert '>打开下载页</button>' not in html
    assert 'data-scan-progress' in html
    assert 'get_scan_progress' in javascript
    assert "font-size: 10px" in stylesheet
    assert 'id="updateBanner"' not in html
    assert 'window.openSettingsDialog = showSettingsDialog' in javascript
    assert "applyMailProviderPreset" in javascript
    assert "mail_ssl" in javascript
    assert 'window.checkForUpdates = checkForUpdates' not in javascript
    settings_function = javascript.split(
        "async function showSettingsDialog", 1
    )[1].split("window.openSettingsDialog", 1)[0]
    assert "set_editor_mode(true)" not in settings_function
    assert "get_dictionary_status" in javascript
    assert "compile_dictionary_workbook" in javascript
    assert "get_original_mail" in javascript
    assert "originalMailBody.replaceChildren()" in javascript
    assert '"permanent_delete"' in javascript
    assert '"trash"' in javascript
    assert 'button.textContent = "再点确认"' in javascript
    assert 'document.createElement("details")' in javascript
    assert (
        'toggleAll.textContent = allExpanded ? "收起所有公司" : "展开所有公司"'
        in javascript
    )
    assert "--progress-summary-columns:" in stylesheet
    assert "expandedApplications: new Set()" in javascript
    assert (
        'const companyKey = application.company_key || application.company'
        in javascript
    )
    assert 'document.createElement("details")' in javascript
    assert '"progress-application",' in javascript
    assert '"progress-card",' in javascript
    assert 'role="status" aria-live="polite"' in javascript
    assert 'saveButton.textContent = "保存中…"' in javascript
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in stylesheet
    assert ".progress-card-actions-main" in stylesheet
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in stylesheet
    assert ".progress-card-actions-manage" in stylesheet
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in stylesheet
    assert 'name="ui_font_scale"' in html
    assert "function renderOverview()" in javascript
    assert "function renderTaskTrash()" in javascript
    assert ".overview-urgent" in stylesheet
    assert 'name="target_query"' in html
    assert 'name="cross_company_confirmed"' in html
    assert "function renderMergeTargets(" in javascript
    assert "...item," in javascript
    assert ".progress-card.direct > .progress-application-body" in stylesheet
    assert "summary:focus-visible" in stylesheet


def test_progress_company_ranking_summary_and_terminal_editing_wiring() -> None:
    project_root = Path(__file__).resolve().parents[1]
    javascript = (project_root / "src/job_mail_desk/ui/app.js").read_text(
        encoding="utf-8"
    )
    stylesheet = (project_root / "src/job_mail_desk/ui/style.css").read_text(
        encoding="utf-8"
    )

    for helper in (
        "function stageDepth(",
        "function compareApplications(",
        "function sortApplications(",
        "function compareCompanyGroups(",
        "function sortCompanyGroups(",
    ):
        assert helper in javascript
    stage_helper = javascript.split("function stageDepth(", 1)[1].split(
        "function isTerminalApplication(", 1
    )[0]
    for token, depth in (
        ("网申|投递", "return 1"),
        ("测评|性格测试", "return 2"),
        ("笔试|编程测试", "return 3"),
        ("群面|AI面试|一面", "return 4"),
        ("二面|第二轮面", "return 5"),
        ("三面|终面", "return 6"),
        ("HR面|人力面", "return 7"),
        ("offer|录用", "return 8"),
    ):
        assert token in stage_helper
        assert depth in stage_helper
    application_sort = javascript.split("function compareApplications(", 1)[1].split(
        "function sortApplications(", 1
    )[0]
    assert application_sort.index("terminalOrder") < application_sort.index("stageDepth")
    terminal_sort = application_sort.split(
        "if (leftTerminal && rightTerminal)", 1
    )[1].split("return stageDepth", 1)[0]
    assert terminal_sort.index("applicationUpdatedAt") < terminal_sort.index("left.role")
    active_sort = application_sort.split("return stageDepth", 1)[1]
    assert active_sort.index("stageDepth") < active_sort.index("pending")
    assert active_sort.index("pending") < active_sort.index("applicationUpdatedAt")
    company_sort = javascript.split("function compareCompanyGroups(", 1)[1].split(
        "function sortCompanyGroups(", 1
    )[0]
    company_order = company_sort.split("return ", 1)[1]
    assert company_order.index("leftActive.length") < company_order.index("rightDepth")
    assert company_order.index("rightDepth") < company_order.index("left.company")
    assert company_order.index("left.company") < company_order.index("left.companyKey")
    assert 'new Intl.Collator("zh-CN-u-co-pinyin"' in javascript
    assert 'new Intl.Collator("zh-CN"' in javascript
    assert "sortCompanyGroups(" in javascript
    assert "items: sortApplications(items)" in javascript
    assert "activeItems.slice(0, 2)" in javascript
    assert "applicationStage(application)" in javascript
    assert "activeItems.length - 2" in javascript
    assert 'aria-label="岗位：${escapeHtml(role)}"' in javascript
    assert '"attention"' in javascript
    assert '"all-terminal"' in javascript
    assert '"terminal closed"' in javascript
    assert "流程终止" in javascript
    assert "control.disabled = disabled" not in javascript
    assert ".progress-company-pill.pending" in stylesheet
    assert ".progress-company.attention" in stylesheet
    assert ".progress-company.all-terminal" in stylesheet
    assert ".progress-card.terminal" in stylesheet
    assert "progress-company-role" in javascript
    assert "progress-company-location" in javascript
    assert "progress-company-stage" in javascript
    assert "progress-company-status" in javascript
    assert "progress-company-location-placeholder" in javascript
    assert 'aria-hidden="true"></span>' in javascript
    assert "no-location" not in javascript
    assert "@container" not in stylesheet
    assert ".progress-company-application.no-location" not in stylesheet
    assert "function progressSummaryRow(" in javascript
    assert "function buildApplicationBody(" in javascript
    assert "function buildApplicationCard(" in javascript
    assert "if (items.length === 1)" in javascript
    assert "buildApplicationCard(items[0], { direct: true })" in javascript
    application_builder = javascript.split(
        "function buildApplicationCard(", 1
    )[1].split("function applicationIdentity", 1)[0]
    assert 'document.createElement(direct ? "div" : "details")' in application_builder
    assert "progress-application-summary" in application_builder
    save_progress = javascript.split(
        "async function saveProgressCard", 1
    )[1].split("function progressSummaryRow", 1)[0]
    assert "role," in save_progress
    assert "location:" in save_progress
    assert "next_stage" not in save_progress
    assert 'data-field="next_stage"' not in application_builder
    assert "grid-template-columns: var(--progress-summary-columns)" in stylesheet
    assert "progress-company-pill.terminal" in stylesheet
    assert "showApplicationDialog(application.application_key, application)" in javascript
    assert 'state.applicationMode === "materialize"' in javascript
    assert "materialize_progress_application" in javascript
    materialize_dialog = javascript.split(
        "async function showApplicationDialog", 1
    )[1].split("async function saveApplication", 1)[0]
    assert "const creating = !applicationKey && !materializing" in materialize_dialog


def test_dictionary_status_uses_bundled_defaults(tmp_path, monkeypatch) -> None:
    imported = tmp_path / "dictionaries" / "imported"
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(
        "job_mail_desk.ui_app.DICTIONARIES_DIR",
        tmp_path / "dictionaries",
    )
    monkeypatch.setattr(
        "job_mail_desk.ui_app.IMPORTED_DICTIONARIES_DIR",
        imported,
    )
    status = DesktopApi(Settings()).get_dictionary_status()
    assert status["counts"] == {
        "companies": 533,
        "programs": 129,
            "roles": 2829,
        "mail_templates": 4,
    }
    assert status["user_dictionary_enabled"] is False


def test_show_existing_window_is_windows_only(monkeypatch) -> None:
    monkeypatch.setattr("job_mail_desk.ui_app.sys.platform", "linux")
    assert show_existing_window() is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
def test_single_instance_mutex_is_atomic() -> None:
    name = rf"Local\JobMailDesk.Test.{uuid4().hex}"
    first_handle, first_primary = _claim_single_instance(name)
    try:
        second_handle, second_primary = _claim_single_instance(name)
        assert first_primary is True
        assert first_handle
        assert second_primary is False
        assert second_handle is None
    finally:
        _close_instance_handle(first_handle)

    replacement_handle, replacement_primary = _claim_single_instance(name)
    try:
        assert replacement_primary is True
        assert replacement_handle
    finally:
        _close_instance_handle(replacement_handle)


def test_application_create_merge_and_recycle_lifecycle(
    tmp_path,
    monkeypatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    applications_dir = tmp_path / "applications"
    unresolved_dir = tmp_path / "unresolved"
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.APPLICATIONS_DIR", applications_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.UNRESOLVED_DIR", unresolved_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(
        "job_mail_desk.ui_app.DASHBOARD_FILE",
        tmp_path / "dashboard.md",
    )
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)

    api.create_application(
        {
            "company": "蔚来",
            "role": "芯片设计工程师",
            "stage": "已投递",
            "next_action": "等待筛选",
        }
    )
    dashboard = api.create_application(
        {
            "company": "NIO蔚来",
            "role": "芯片设计工程师",
            "stage": "已投递",
        }
    )
    assert len(dashboard["duplicate_candidates"]) == 1
    registry = ApplicationRegistry(applications_dir)
    records = registry.active()
    assert len(records) == 2
    assert sorted(record.attempt_sequence for record in records) == [1, 2]
    assert all(record.application_key.startswith("app-") for record in records)
    assert all(
        task.application_id
        and task.application_key in {record.application_key for record in records}
        for task in MarkdownTaskStore(tasks_dir).all()
    )

    source, target = records
    source.deleted_source_hashes = ["source-hash"]
    source.manual_progress_history = [
        {
            "event_at": "2026-08-01T10:00:00+08:00",
            "stage": "网申",
            "status": "completed",
            "next_stage": "测评",
            "lifecycle_status": "active",
        }
    ]
    source.progress_nodes = [
        {
            "id": "pgn1_source",
            "source_hash": "source-node-hash",
            "event_at": "2026-08-01T11:00:00+08:00",
            "stage": "网申",
            "status": "completed",
            "next_stage": "测评",
            "lifecycle_status": "active",
            "source_task_id": None,
            "operation_id": "op1_source",
        }
    ]
    target.manual_progress_history = [
        {
            "event_at": "2026-08-02T10:00:00+08:00",
            "stage": "测评",
            "status": "pending",
            "next_stage": "笔试",
            "lifecycle_status": "active",
        }
    ]
    registry.save(source)
    registry.save(target)
    api.edit_application(
        source.application_key,
        {"role": "芯片验证工程师"},
    )
    preview = api.get_application_merge_preview(
        source.application_key,
        target.application_key,
    )
    assert preview["task_count"] == 1
    assert preview["conflicts"] == ["role"]
    with pytest.raises(ValueError, match="冲突字段"):
        api.merge_applications(
            source.application_key,
            target.application_key,
            preview_token=preview["preview_token"],
        )
    api.merge_applications(
        source.application_key,
        target.application_key,
        {"role": target.role},
        preview_token=preview["preview_token"],
    )
    assert registry.raw_load(source.application_key).merged_into == target.application_key
    assert registry.load(source.application_key).application_key == target.application_key
    merged_target = registry.raw_load(target.application_key)
    assert legacy_application_id(source.application_key) in (
        merged_target.legacy_application_ids
    )
    assert merged_target.deleted_source_hashes == [
        "source-hash",
        "source-node-hash",
    ]
    assert [item["id"] for item in merged_target.progress_nodes] == ["pgn1_source"]
    assert [item["stage"] for item in merged_target.manual_progress_history] == [
        "测评",
        "网申",
    ]
    assert {
        task.application_key for task in MarkdownTaskStore(tasks_dir).all()
    } == {target.application_key}

    dashboard = api.trash_application(target.application_key)
    assert api.list_application_choices("蔚来") == []
    assert dashboard["trash"][0]["application_key"] == target.application_key
    assert {
        task.status for task in MarkdownTaskStore(tasks_dir).all()
    } == {"cancelled"}
    api.restore_application(target.application_key)
    assert len(api.list_application_choices("蔚来")) == 1
    assert {
        task.status for task in MarkdownTaskStore(tasks_dir).all()
    } == {"needs_review"}
    restored_task = MarkdownTaskStore(tasks_dir).all()[0]
    restored_task.source_message_hash = "mail-source-tombstone"
    MarkdownTaskStore(tasks_dir).save(restored_task)
    api.trash_application(target.application_key)
    api.permanently_delete_application(target.application_key)
    task_tombstones = MarkdownTaskStore(tasks_dir).all()
    assert task_tombstones
    assert all(task.tombstoned for task in task_tombstones)
    tombstone = registry.raw_load(target.application_key)
    assert tombstone.deletion_reason == "permanently-deleted"
    assert tombstone.deleted_source_hashes == [
        "mail-source-tombstone",
        "source-hash",
        "source-node-hash",
    ]
    assert tombstone.progress_nodes == []


def test_cross_company_merge_requires_explicit_confirmation(
    tmp_path,
    monkeypatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    applications_dir = tmp_path / "applications"
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.APPLICATIONS_DIR", applications_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.UNRESOLVED_DIR", tmp_path / "unresolved")
    monkeypatch.setattr("job_mail_desk.ui_app.STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr("job_mail_desk.ui_app.DASHBOARD_FILE", tmp_path / "dashboard.md")
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)
    api.create_application({"company": "大疆", "role": "后端", "stage": "网申"})
    api.create_application({"company": "联发科技", "role": "数字后端", "stage": "网申"})
    source, target = ApplicationRegistry(applications_dir).active()

    preview = api.get_application_merge_preview(
        source.application_key,
        target.application_key,
    )
    assert preview["cross_company"] is True
    with pytest.raises(ValueError, match="跨公司"):
        api.merge_applications(
            source.application_key,
            target.application_key,
            {"company": target.company, "role": target.role},
            preview_token=preview["preview_token"],
        )

    api.merge_applications(
        source.application_key,
        target.application_key,
        {"company": target.company, "role": target.role},
        True,
        preview["preview_token"],
    )
    assert (
        ApplicationRegistry(applications_dir).raw_load(source.application_key).merged_into
        == target.application_key
    )


def test_application_merge_write_failure_rolls_back_fact_files(
    tmp_path,
    monkeypatch,
) -> None:
    tasks_dir = tmp_path / "tasks"
    applications_dir = tmp_path / "applications"
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.APPLICATIONS_DIR", applications_dir)
    monkeypatch.setattr("job_mail_desk.ui_app.UNRESOLVED_DIR", tmp_path / "unresolved")
    monkeypatch.setattr("job_mail_desk.ui_app.STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr("job_mail_desk.ui_app.DASHBOARD_FILE", tmp_path / "dashboard.md")
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)
    api.create_application({"company": "大疆", "role": "后端", "stage": "网申"})
    api.create_application({"company": "大疆", "role": "数字后端", "stage": "网申"})
    source, target = ApplicationRegistry(applications_dir).active()
    original_save = ApplicationRegistry.save
    failed = False

    def fail_once_on_source_tombstone(self, record):
        nonlocal failed
        if record.merged_into and not failed:
            failed = True
            raise OSError("simulated write failure")
        return original_save(self, record)

    monkeypatch.setattr(ApplicationRegistry, "save", fail_once_on_source_tombstone)
    preview = api.get_application_merge_preview(
        source.application_key,
        target.application_key,
    )
    with pytest.raises(OSError, match="simulated"):
        api.merge_applications(
            source.application_key,
            target.application_key,
            {"role": target.role},
            preview_token=preview["preview_token"],
        )

    registry = ApplicationRegistry(applications_dir)
    assert registry.raw_load(source.application_key).merged_into is None
    assert registry.raw_load(target.application_key).aliases == []
    assert {
        task.application_key
        for task in MarkdownTaskStore(tasks_dir).all()
    } == {source.application_key, target.application_key}
