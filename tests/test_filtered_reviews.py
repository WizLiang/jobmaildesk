from dataclasses import replace
from datetime import datetime, timezone

import pytest

from job_mail_desk.models import MailRecord
from job_mail_desk.unresolved_store import UnresolvedStore, filtered_from_mail


@pytest.fixture
def marketing():
    return MailRecord(
        uid="73",
        subject="尊敬的周同学 校园宣讲邀请 learner@example.invalid 13800138000 "
        "HTTPS://invite.example.invalid/path?ticket=REDACTED 授权码=synthetic-secret",
        sender="私人发件人 <sender@example.invalid>",
        message_id="<private-message@example.invalid>",
        received_at=datetime(2026, 9, 20, 8, tzinfo=timezone.utc),
        body="PRIVATE-BODY-MUST-NOT-BE-PERSISTED 线下地址与活动详情。",
        links=("https://invite.example.invalid/body-link?token=hidden",),
        mailbox="INBOX", uidvalidity="8", account_fingerprint="a" * 24,
    )


def test_first_filtered_record_stores_only_safe_header_and_locator(tmp_path, marketing):
    store = UnresolvedStore(tmp_path / "unresolved")
    record = store.put_filtered(filtered_from_mail("source-hash", marketing, parser_version="rules-1"))
    saved = store.path_for(record.id).read_text(encoding="utf-8")
    for private in (
        "周同学", "learner@example.invalid", "13800138000", "HTTPS://", "https://",
        "REDACTED", "synthetic-secret", marketing.body, marketing.sender,
        marketing.message_id, "body-link",
    ):
        assert private not in saved
    assert "校园宣讲邀请" in record.title
    assert record.status == "filtered" and record.reason == "recruiting-marketing"
    assert record.mail_locator == marketing.locator()
    assert record.received_at == marketing.received_at
    assert record.requirements == () and record.private_link_ref is None
    assert not record.has_action_link and not record.recommend_task
    assert record.company is None and record.role is None
    assert record.start_at is None and record.end_at is None and record.deadline_at is None
    assert store.load(record.id) == record


def test_filter_builder_does_not_access_body_sender_or_links(marketing):
    class HeaderOnly:
        subject = marketing.subject
        received_at = marketing.received_at

        def locator(self):
            return marketing.locator()

        def __getattr__(self, name):
            raise AssertionError(f"Filtered review read forbidden mail field: {name}")

    assert filtered_from_mail("source-hash", HeaderOnly(), parser_version="rules-1").status == "filtered"


@pytest.mark.parametrize("link", [
    "https://example.invalid/x?code=secret",
    "HTTPS://example.invalid/x?code=secret",
    "www.example.invalid/x?code=secret",
    "mailto:personal@example.invalid",
])
def test_filtered_title_hides_links_before_truncating(marketing, link):
    record = filtered_from_mail(
        "source-hash", replace(marketing, subject=f"校园宣讲 {link} " + "介绍" * 200),
        parser_version="rules-1",
    )
    assert "example.invalid" not in record.title
    assert "secret" not in record.title
    assert len(record.title) <= 240


@pytest.mark.parametrize("private_text,secret", [
    ("收件人learner@example.invalid的推荐", "learner@example.invalid"),
    ("邮箱：learner@example.invalid", "learner@example.invalid"),
    ("学号AB12345678的推荐", "AB12345678"),
    ("学号：12345678", "12345678"),
    ("考生号98765432", "98765432"),
])
def test_filtered_title_redacts_chinese_adjacent_identifiers(marketing, private_text, secret):
    record = filtered_from_mail(
        "source-hash", replace(marketing, subject=f"2027 届校园招聘 {private_text}"),
        parser_version="rules-1",
    )
    assert secret not in record.title
    assert "2027 届校园招聘" in record.title


def test_repeated_filter_keeps_one_record_and_stable_revision(tmp_path, marketing):
    store = UnresolvedStore(tmp_path / "unresolved")
    record = filtered_from_mail("source-hash", marketing, parser_version="rules-1")
    initial = store.put_filtered(record)
    repeated = store.put_filtered(record)
    upgraded = store.put_filtered(replace(record, parser_version="rules-2"))
    assert initial == repeated
    assert upgraded.id == initial.id and upgraded.revision == initial.revision
    assert upgraded.parser_version == "rules-2"
    assert len(store.all()) == 1


def test_existing_pending_review_keeps_structured_identity_when_filtered(tmp_path, marketing):
    store = UnresolvedStore(tmp_path / "unresolved")
    record = filtered_from_mail("source-hash", marketing, parser_version="rules-1")
    pending = replace(record, status="pending", company="虚构科技", role="设计工程师",
                      title="此前已识别的标题", reason="missing-role", revision=4,
                      recommend_task=True, recommendation_reasons=("explicit_start",))
    store.save(pending)
    actual = store.put_filtered(replace(record, parser_version="rules-2"))
    assert actual.company == pending.company and actual.role == pending.role
    assert actual.title == pending.title and actual.id == pending.id
    assert actual.revision == 5 and actual.status == "filtered"
    assert not actual.recommend_task and actual.recommendation_reasons == ()


@pytest.mark.parametrize("status,manual_restore", [
    ("resolved", False), ("ignored", False), ("tombstoned", False), ("pending", True),
])
def test_filter_never_overrides_manual_choices(tmp_path, marketing, status, manual_restore):
    store = UnresolvedStore(tmp_path / "unresolved")
    record = filtered_from_mail("source-hash", marketing, parser_version="rules-1")
    protected = replace(record, status=status, manual_restore=manual_restore, revision=7)
    store.save(protected)
    assert store.put_filtered(replace(record, parser_version="rules-2")) == protected
    assert store.load(record.id) == protected


def test_restore_filtered_is_revision_guarded_and_survives_replay(tmp_path, marketing):
    store = UnresolvedStore(tmp_path / "unresolved")
    filtered = store.put_filtered(filtered_from_mail("source-hash", marketing, parser_version="rules-1"))
    with pytest.raises(ValueError, match="过滤"):
        store.resolve(filtered.id, application_key="new-application", task_id=None)
    restored = store.restore_filtered(filtered.id, filtered.revision)
    assert restored.status == "pending" and restored.manual_restore
    assert restored.revision == filtered.revision + 1
    assert restored.resolved_task_id is None and restored.resolved_application_key is None
    assert store.put_filtered(replace(filtered, parser_version="rules-2")) == restored
    assert store.put_pending(replace(filtered, status="pending", parser_version="rules-2")) == restored
    with pytest.raises(ValueError, match="记录已变化"):
        store.restore_filtered(filtered.id, filtered.revision)
    ignored = store.ignore(filtered.id)
    assert ignored.status == "ignored" and not ignored.manual_restore


@pytest.mark.parametrize("revision", [0, 2, True, "1", 1.0])
def test_restore_rejects_stale_or_non_integer_revision(tmp_path, marketing, revision):
    store = UnresolvedStore(tmp_path / "unresolved")
    filtered = store.put_filtered(filtered_from_mail("source-hash", marketing, parser_version="rules-1"))
    with pytest.raises(ValueError, match="记录已变化"):
        store.restore_filtered(filtered.id, revision)
    assert store.load(filtered.id) == filtered


@pytest.mark.parametrize("field,value", [
    ("resolved_application_key", "application"), ("resolved_task_id", "task"),
    ("confirmation_operation_id", "operation"), ("progress_node_id", "node"),
    ("resolved_at", datetime(2026, 9, 20, tzinfo=timezone.utc)),
])
def test_filter_and_restore_reject_records_with_resolution_markers(tmp_path, marketing, field, value):
    store = UnresolvedStore(tmp_path / "unresolved")
    record = filtered_from_mail("source-hash", marketing, parser_version="rules-1")
    protected = replace(record, **{field: value})
    store.save(protected)
    with pytest.raises(ValueError, match="已有归属"):
        store.restore_filtered(record.id, record.revision)
    assert store.put_filtered(replace(record, parser_version="rules-2")) == protected
    assert store.load(record.id) == protected


def test_failed_restore_keeps_filtered_markdown_authoritative(tmp_path, marketing, monkeypatch):
    store = UnresolvedStore(tmp_path / "unresolved")
    filtered = store.put_filtered(filtered_from_mail("source-hash", marketing, parser_version="rules-1"))

    def fail_save(record):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(store, "save", fail_save)
    with pytest.raises(OSError):
        store.restore_filtered(filtered.id, filtered.revision)
    assert store.load(filtered.id) == filtered


@pytest.fixture
def filtered_integration(tmp_path, monkeypatch, marketing):
    from job_mail_desk import scanner, ui_app
    from job_mail_desk.config import Settings
    from job_mail_desk.credentials import MailCredential
    from job_mail_desk.state import StateStore

    mail = replace(marketing, received_at=datetime.now(scanner.SHANGHAI),
                   internal_date=datetime.now(scanner.SHANGHAI))
    for module in (scanner, ui_app):
        for name, relative in {"TASKS_DIR": "tasks", "STATE_DB": "state.db", "DASHBOARD_FILE": "dashboard.md"}.items():
            monkeypatch.setattr(module, name, tmp_path / relative)
    for name, relative in {"LOCAL_ROOT": ".", "APPLICATIONS_DIR": "applications",
                           "UNRESOLVED_DIR": "unresolved", "DICTIONARIES_DIR": "dictionaries"}.items():
        monkeypatch.setattr(ui_app, name, tmp_path / relative)
    monkeypatch.setattr(scanner, "DICTIONARIES_DIR", tmp_path / "dictionaries")
    monkeypatch.setattr(scanner, "ensure_directories", lambda: None)
    monkeypatch.setattr(scanner, "_learn_from_confirmed_records", lambda *args, **kwargs: {})
    monkeypatch.setattr(scanner, "load_credential", lambda: MailCredential("fixture@example.invalid", "synthetic"))

    def forbidden_credentials(*args, **kwargs):
        raise AssertionError("Integration test must not access stored credentials")

    monkeypatch.setattr(ui_app, "load_credential", forbidden_credentials)

    class Reader:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_since(self, days):
            return [mail]

    monkeypatch.setattr(scanner, "ImapReader", Reader)
    settings = Settings(progress_enabled=False, obsidian_enabled=False,
                        research_queue=tmp_path / "research.jsonl")
    record = filtered_from_mail("legacy-filtered", mail, parser_version=scanner.parser_version_for_settings(settings))
    store = UnresolvedStore(tmp_path / "unresolved")
    store.save(record)
    state = StateStore(tmp_path / "state.db")
    keys = {record.id, scanner._source_hash_from_locator(record.mail_locator)}
    for key in keys:
        state.record_outcome(key, "filtered", locator=mail.locator(), internal_date=mail.internal_date)
    api = ui_app.DesktopApi(settings)
    monkeypatch.setattr(api, "_export", lambda _store: None)
    monkeypatch.setattr(api, "_refresh_task_runtime", lambda: None)
    monkeypatch.setattr(ui_app, "announce_new_reviews", lambda *args: False)
    return api, store, state, record, keys, tmp_path


@pytest.mark.parametrize("create_task", [False, True])
def test_restored_filtered_header_can_be_confirmed_into_real_application(filtered_integration, create_task):
    from job_mail_desk.application_registry import ApplicationRegistry
    from job_mail_desk.markdown_store import MarkdownTaskStore

    api, store, state, record, keys, root = filtered_integration
    assert api.get_dashboard()["unresolved"] == []
    api.restore_filtered_review(record.id, record.revision)
    restored = store.load(record.id)
    bootstrap = api.get_review_window_bootstrap(record.id)
    assert bootstrap["review"]["revision"] == restored.revision
    assert bootstrap["recommendation"]["default_create_task"] is False
    assert "mail_locator" not in bootstrap["review"]
    request = {"request_id": "op1_" + "b" * 32,
               "expected_review_revision": restored.revision, "mode": "new_identity",
               "company": "虚构科技", "role": "设计工程师", "stage": "招聘通知",
               "manual_stage_status": "pending", "create_task": create_task}
    api.resolve_unresolved_workflow(record.id, request)
    api.resolve_unresolved_workflow(record.id, request)  # Safe duplicate submission.
    applications = ApplicationRegistry(root / "applications").all()
    tasks = MarkdownTaskStore(root / "tasks").all()
    assert len(applications) == 1 and len(tasks) == int(create_task)
    assert store.load(record.id).status == "resolved"
    assert store.load(record.id).resolved_application_key == applications[0].application_key
    assert len(applications[0].progress_nodes) == 1
    assert api.list_filtered_reviews() == []
    if create_task:
        assert tasks[0].source_message_hash == record.id
        assert tasks[0].application_key == applications[0].application_key


def test_failed_filtered_restore_reconciles_indexes_on_next_scan(filtered_integration, monkeypatch):
    api, store, state, record, keys, _root = filtered_integration
    original_save = UnresolvedStore.save

    def fail_pending(self, updated):
        if updated.manual_restore:
            raise OSError("synthetic fact write failure")
        return original_save(self, updated)

    with monkeypatch.context() as failing:
        failing.setattr(UnresolvedStore, "save", fail_pending)
        with pytest.raises(OSError):
            api.restore_filtered_review(record.id, record.revision)
    assert store.load(record.id) == record
    assert all(state.outcome(key).outcome == "pending" for key in keys)
    api.trigger_scan()
    assert store.load(record.id).status == "filtered"
    canonical = record.mail_locator
    from job_mail_desk.scanner import _source_hash_from_locator
    assert state.outcome(_source_hash_from_locator(canonical)).outcome == "filtered"
    assert api.get_dashboard()["unresolved"] == []
    # A retry succeeds even if the unvisited legacy alias is still pending.
    api.restore_filtered_review(record.id, store.load(record.id).revision)
    assert store.load(record.id).manual_restore
    assert all(state.outcome(key).outcome == "pending" for key in keys)
