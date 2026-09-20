from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from job_mail_desk.application_registry import (
    ApplicationRegistry,
    application_from_user_payload,
)
from job_mail_desk.config import Settings
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.progress import build_application_timeline
from job_mail_desk.review_window import ReviewWindowController
from job_mail_desk.runtime_control import RuntimeControl
from job_mail_desk.ui_app import DesktopApi
from job_mail_desk.unresolved_store import UnresolvedRecord, UnresolvedStore


class FakeEvent:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        return [handler() for handler in tuple(self.handlers)]


class FakeWindow:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.events = SimpleNamespace(closing=FakeEvent(), closed=FakeEvent())
        self.destroyed = False
        self.confirm_close = bool(kwargs.get("confirm_close", False))

    def destroy(self) -> None:
        if self.destroyed:
            return
        self.destroyed = True
        self.events.closed.fire()

class FakeReviewBackend:
    def __init__(self) -> None:
        self.original_calls = []
        self.confirmed = []
        self.cancelled = False
        self.target = {
            "application_key": "app-ended",
            "revision": 7,
            "company": "样例科技",
            "role": "后端工程师",
            "project": "2027 校招",
            "job_code": "J10001",
            "location": "上海",
            "attempt_sequence": 1,
            "status": "ended",
            "lifecycle": "已结束",
            "current_stage": "已结束",
            "label": "完整候选标签",
        }

    def get_review_window_bootstrap(self, source_hash, preferred_key=""):
        del preferred_key
        return {
            "review": {
                "id": source_hash,
                "revision": 3,
                "company": "样例科技",
                "role": "后端工程师",
                "stage": "笔试",
            },
            "target": dict(self.target),
            "initial_targets": [dict(self.target)],
            "recommendation": {
                "review_revision": 3,
                "application_revision": 7,
                "reasons": [],
            },
        }

    def search_review_targets(self, source_hash, query=""):
        del source_hash, query
        return [dict(self.target)]

    def get_review_target(self, application_key):
        assert application_key == self.target["application_key"]
        return dict(self.target)

    def get_review_recommendation(self, source_hash, application_key=""):
        del source_hash
        return {
            "review_revision": 3,
            "application_revision": 7 if application_key else None,
            "reasons": [],
        }

    def get_pending_original_mail(
        self,
        source_hash,
        expected_review_revision,
        load_remote_images=False,
        *,
        runtime_control,
    ):
        self.original_calls.append(
            (
                source_hash,
                expected_review_revision,
                load_remote_images,
                runtime_control,
            )
        )
        runtime_control.register_interrupt(
            lambda: setattr(self, "cancelled", True)
        )
        return {"subject": "原邮件", "text": "仅内存正文"}

    def resolve_unresolved_workflow(self, source_hash, payload):
        self.confirmed.append((source_hash, payload))
        return {"unresolved": []}


def fake_controller():
    backend = FakeReviewBackend()
    windows = []

    def create_window(_title, _url, **kwargs):
        window = FakeWindow(**kwargs)
        windows.append(window)
        return window

    controller = ReviewWindowController(
        backend,
        RuntimeControl(),
        "file:///review.html",
        create_window,
    )
    return controller, backend, windows


def test_controller_opens_multiple_bound_resizable_native_windows() -> None:
    controller, backend, windows = fake_controller()

    first = controller.open_window("a" * 32)
    second = controller.open_window("a" * 32)

    assert controller.window_count == 2
    assert first["window_id"] != second["window_id"]
    assert first["request_id"] != second["request_id"]
    assert first["review_revision"] == 3
    assert first["target_key"] == "app-ended"
    assert first["application_revision"] == 7
    assert windows[0].kwargs["width"] == 1080
    assert windows[0].kwargs["height"] == 760
    assert windows[0].kwargs["min_size"] == (900, 600)
    assert windows[0].kwargs["resizable"] is True
    bootstrap = windows[0].kwargs["js_api"].get_bootstrap()
    assert bootstrap["default_mode"] == "new_attempt"

    mail = windows[0].kwargs["js_api"].load_original_mail()
    assert mail["text"] == "仅内存正文"
    assert backend.original_calls[0][:3] == ("a" * 32, 3, False)
    windows[0].kwargs["js_api"].close_window(True)
    assert backend.cancelled is True
    assert controller.window_count == 1


def test_controller_rejects_tampered_session_and_prompts_dirty_close() -> None:
    controller, backend, windows = fake_controller()
    session = controller.open_window("b" * 32)
    bridge = windows[0].kwargs["js_api"]
    bridge.set_dirty(True)

    assert bridge.close_window(False) == {
        "closed": False,
        "requires_confirmation": True,
    }
    with pytest.raises(ValueError, match="会话无效"):
        bridge.confirm(
            {
                **session,
                "window_id": "review-tampered",
                "expected_review_revision": 3,
                "expected_application_revision": 7,
                "application_key": "app-ended",
                "mode": "new_attempt",
            }
        )
    assert windows[0].events.closing.fire() == [None]
    assert windows[0].confirm_close is True
    assert controller.window_count == 1
    assert backend.confirmed == []


def pending(source_hash: str) -> UnresolvedRecord:
    return UnresolvedRecord(
        id=source_hash,
        status="pending",
        resolution_status="matched",
        reason="review-first",
        company="样例科技",
        role="后端工程师",
        recruiting_project="2027 校招",
        event_type="assessment",
        stage="笔试",
        round=None,
        received_at=datetime(2026, 8, 15, 9, 0, tzinfo=SHANGHAI),
        start_at=None,
        end_at=None,
        deadline_at=None,
        action_summary="参加笔试",
        title="笔试通知",
        requirements=(),
        confidence=0.98,
        change_type="new",
        candidate_application_keys=(),
        resolved_application_key=None,
        resolved_task_id=None,
        rule_version="test",
        mail_locator={
            "mailbox": "INBOX",
            "uid": "42",
            "uidvalidity": "7",
            "account_fingerprint": "a" * 24,
        },
    )


def configure_paths(monkeypatch, tmp_path: Path) -> None:
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


def test_review_target_search_canonicalizes_company_alias_and_ignores_role_mismatch(
    tmp_path,
    monkeypatch,
) -> None:
    configure_paths(monkeypatch, tmp_path)
    application = application_from_user_payload(
        {
            "company": "海光信息",
            "role": "数字后端",
            "status": "active",
        }
    )
    ApplicationRegistry(tmp_path / "applications").save(application)
    review = replace(
        pending("e" * 32),
        company="海光信息招聘",
        role="物理设计与实现工程师",
        recruiting_project="2027校园招聘",
    )
    UnresolvedStore(tmp_path / "unresolved").save(review)

    targets = DesktopApi(Settings()).search_review_targets(
        review.id,
        "海光信息招聘",
    )

    assert [item["application_key"] for item in targets] == [
        application.application_key
    ]
    assert targets[0]["same_company"] is True
    assert targets[0]["same_role"] is False


def test_pending_mail_is_loaded_by_locator_through_window_scope(
    tmp_path,
    monkeypatch,
) -> None:
    configure_paths(monkeypatch, tmp_path)
    source = pending("1" * 32)
    UnresolvedStore(tmp_path / "unresolved").save(source)
    captured = {}

    class Reader:
        def __init__(self, settings, credential, runtime_control=None):
            captured["runtime_control"] = runtime_control

        def fetch_original(self, locator, *, load_remote_images=False):
            captured["locator"] = locator
            captured["load_remote_images"] = load_remote_images
            return {"subject": "原邮件", "text": "仅内存正文"}

    monkeypatch.setattr("job_mail_desk.ui_app.ImapReader", Reader)
    monkeypatch.setattr(
        "job_mail_desk.ui_app.load_credential",
        lambda: object(),
    )
    api = DesktopApi(Settings())
    scope = api._runtime_control.create_scope()

    result = api.get_pending_original_mail(
        source.id,
        source.revision,
        True,
        runtime_control=scope,
    )

    assert result["text"] == "仅内存正文"
    assert captured["locator"] == source.mail_locator
    assert captured["load_remote_images"] is True
    assert captured["runtime_control"] is scope


def ended_application():
    application = application_from_user_payload(
        {
            "company": "样例科技",
            "role": "后端工程师",
            "recruiting_project": "2027 校招",
            "recruiting_year": 2027,
            "business_unit": "芯片事业部",
            "job_code": "J10001",
            "location": "上海",
            "stage": "已结束",
        }
    )
    application.status = "ended"
    application.workflow_status = "ended"
    application.manual_stage = "已结束"
    return application


def test_ended_match_defaults_to_new_attempt_and_label_is_complete(
    tmp_path,
    monkeypatch,
) -> None:
    configure_paths(monkeypatch, tmp_path)
    source = pending("c" * 32)
    application = ended_application()
    UnresolvedStore(tmp_path / "unresolved").save(source)
    ApplicationRegistry(tmp_path / "applications").save(application)
    source = source.__class__(
        **{
            **source.__dict__,
            "recommended_application_key": application.application_key,
        }
    )
    UnresolvedStore(tmp_path / "unresolved").save(source)
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)

    bootstrap = api.get_review_window_bootstrap(source.id)
    label = bootstrap["target"]["label"]
    assert bootstrap["target"]["recruiting_year"] == 2027
    assert bootstrap["target"]["business_unit"] == "芯片事业部"
    for value in (
        "样例科技",
        "后端工程师",
        "2027 校招",
        "J10001",
        "上海",
        "第 1 次",
        "已结束",
        "当前：已结束",
    ):
        assert value in label
    dashboard_label = api.get_dashboard()["unresolved"][0]["candidates"][0]["label"]
    assert dashboard_label == label

    api.resolve_unresolved_workflow(
        source.id,
        {
            "request_id": "op1_" + "d" * 32,
            "expected_review_revision": source.revision,
            "expected_application_revision": application.revision,
            "mode": "new_attempt",
            "application_key": application.application_key,
            "company": application.company,
            "role": application.role,
            "recruiting_project": application.recruiting_project,
            "job_code": application.job_code,
            "location": application.location,
            "stage": "笔试",
            "manual_stage_status": "pending",
            "create_task": False,
        },
    )

    records = ApplicationRegistry(tmp_path / "applications").all()
    assert len(records) == 2
    assert next(item for item in records if item.application_key == application.application_key).status == "ended"
    created = next(item for item in records if item.application_key != application.application_key)
    assert created.status == "active"
    assert created.attempt_sequence == 2
    assert created.recruiting_year == 2027
    assert created.business_unit == "芯片事业部"
    assert created.submitted_at == source.received_at
    resolved = UnresolvedStore(tmp_path / "unresolved").load(source.id)
    assert resolved and resolved.resolved_application_key == created.application_key
    assert resolved.resolved_task_id is None
    assert created.progress_nodes[0]["has_mail_locator"] is True
    timeline = build_application_timeline([], created)
    assert timeline[0]["sources"][0]["source_hash"] == source.id
    assert timeline[0]["sources"][0]["has_mail_locator"] is True


def test_explicit_reactivate_is_distinct_from_updating_ended_chain(
    tmp_path,
    monkeypatch,
) -> None:
    configure_paths(monkeypatch, tmp_path)
    application = ended_application()
    registry = ApplicationRegistry(tmp_path / "applications")
    registry.save(application)
    source = pending("e" * 32)
    UnresolvedStore(tmp_path / "unresolved").save(source)
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)
    base = {
        "request_id": "op1_" + "f" * 32,
        "expected_review_revision": source.revision,
        "expected_application_revision": application.revision,
        "application_key": application.application_key,
        "company": application.company,
        "role": application.role,
        "stage": "笔试",
        "manual_stage_status": "pending",
        "create_task": False,
    }

    with pytest.raises(ValueError, match="只能更新进行中"):
        api.resolve_unresolved_workflow(
            source.id,
            {**base, "mode": "update_active"},
        )
    api.resolve_unresolved_workflow(
        source.id,
        {**base, "mode": "reactivate"},
    )

    reopened = registry.load(application.application_key)
    assert reopened and reopened.status == "active"
    assert reopened.attempt_sequence == 1


def test_second_window_gets_stale_confirmation_conflict(
    tmp_path,
    monkeypatch,
) -> None:
    configure_paths(monkeypatch, tmp_path)
    source = pending("2" * 32)
    UnresolvedStore(tmp_path / "unresolved").save(source)
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))
    monkeypatch.setattr(api, "_export", lambda _store: None)
    payload = {
        "expected_review_revision": source.revision,
        "mode": "new_identity",
        "company": source.company,
        "role": source.role,
        "stage": source.stage,
        "manual_stage_status": "pending",
        "create_task": False,
    }

    api.resolve_unresolved_workflow(
        source.id,
        {**payload, "request_id": "op1_" + "3" * 32},
    )
    with pytest.raises(ValueError, match="其他确认操作"):
        api.resolve_unresolved_workflow(
            source.id,
            {**payload, "request_id": "op1_" + "4" * 32},
        )


def test_review_resources_encode_split_panes_and_race_guards() -> None:
    source_dir = Path(__file__).parents[1] / "src/job_mail_desk"
    ui_dir = source_dir / "ui"
    main_html = (ui_dir / "index.html").read_text(encoding="utf-8")
    main_javascript = (ui_dir / "app.js").read_text(encoding="utf-8")
    main_stylesheet = (ui_dir / "style.css").read_text(encoding="utf-8")
    html = (ui_dir / "review.html").read_text(encoding="utf-8")
    javascript = (ui_dir / "review.js").read_text(encoding="utf-8")
    stylesheet = (ui_dir / "review.css").read_text(encoding="utf-8")

    assert 'class="mail-pane"' in html
    assert 'class="form-pane"' in html
    for mode in ("new_identity", "new_attempt", "update_active", "reactivate"):
        assert f'value="{mode}"' in html
    assert "grid-template-columns" in stylesheet
    assert stylesheet.count("overflow-y: auto") >= 2
    assert ".sticky-actions" in stylesheet
    assert "lookupToken" in javascript
    assert "response.token !== token" in javascript
    assert "setIfClean" in javascript
    assert "targetValueOrIncoming" in javascript
    for value in (
        'targetValueOrIncoming(target?.project, "recruiting_project")',
        'targetValueOrIncoming(target?.job_code, "job_code")',
        'targetValueOrIncoming(target?.location, "location")',
    ):
        assert value in javascript
    assert 'setIfClean("job_code", review.job_code)' in javascript
    assert 'targetQuery.value = bootstrap.review.company || ""' in javascript
    assert "reviewState.dirtyFields" in javascript
    assert "reviewState.lookupPending" in javascript
    assert 'name="recruiting_year"' in html
    assert 'name="business_unit"' in html
    assert 'name="role_raw"' in html
    assert 'name="location_source"' in html
    controller = (source_dir / "review_window.py").read_text(encoding="utf-8")
    assert "managed.window.confirm_close = dirty" in controller
    assert "独立原生窗口" in main_html
    assert "open_review_window" in main_javascript
    assert "reviewWindowCompleted" in main_javascript
    assert "open-review-window" in main_stylesheet
