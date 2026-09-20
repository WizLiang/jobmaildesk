from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from job_mail_desk import scanner, ui_app
from job_mail_desk.config import Settings
from job_mail_desk.identity_dictionaries import load_identity_dictionaries
from job_mail_desk.identity_pipeline import resolve_event_batch
from job_mail_desk.models import MailRecord
from job_mail_desk.review_explanation import explain_review
from job_mail_desk.state import StateStore
from job_mail_desk.unresolved_store import UnresolvedStore, unresolved_from_decision


@pytest.fixture
def desk(tmp_path, monkeypatch):
    received = datetime.now(scanner.SHANGHAI) - timedelta(days=60)
    mail = MailRecord(
        uid="42", subject="甲辰微电子（深圳）股份有限公司-线上笔试通知",
        sender="services <noreply@example.invalid>", message_id="<history@example.invalid>",
        body="简历已通过我司“数字设计工程师-深圳岗”筛选。笔试时间：9月22日14:00。",
        received_at=received, internal_date=received,
        mailbox="INBOX", uidvalidity="7", account_fingerprint="a" * 24,
    )
    dictionaries = load_identity_dictionaries()
    event = scanner.parse_record(mail, dictionaries)
    key = scanner._record_source_hashes(mail)[0]
    review = unresolved_from_decision(
        key, resolve_event_batch([event], [], dictionaries)[0], mail.locator(),
        parser_version=scanner.PARSER_VERSION,
    )
    for module in (scanner, ui_app):
        for name, path in {"TASKS_DIR": "tasks", "STATE_DB": "state.db", "DASHBOARD_FILE": "dashboard.md"}.items():
            monkeypatch.setattr(module, name, tmp_path / path)
    monkeypatch.setattr(ui_app, "APPLICATIONS_DIR", tmp_path / "applications")
    monkeypatch.setattr(ui_app, "UNRESOLVED_DIR", tmp_path / "unresolved")
    monkeypatch.setattr(scanner, "DICTIONARIES_DIR", tmp_path / "dict")
    monkeypatch.setattr(scanner, "ensure_directories", lambda: None)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "_learn_from_confirmed_records", lambda *a, **kw: {})
    windows = []

    class Reader:
        def __init__(self, *args, **kwargs): pass
        def fetch_since(self, days):
            windows.append(days)
            return [mail] if days >= 60 else []

    monkeypatch.setattr(scanner, "ImapReader", Reader)
    store = UnresolvedStore(tmp_path / "unresolved")
    state = StateStore(tmp_path / "state.db")
    run = state.begin_scan()
    state.finish_scan(run, fetched=0, candidates=0, parser_version=scanner.PARSER_VERSION,
                      source_identity_version=scanner.SOURCE_IDENTITY_VERSION)
    settings = Settings(progress_enabled=False, lookback_days=3)
    api = ui_app.DesktopApi(settings)
    monkeypatch.setattr(api, "get_dashboard", lambda: {})
    monkeypatch.setattr(ui_app, "announce_new_reviews", lambda *args: False)
    return store, state, review, settings, api, windows


@pytest.mark.parametrize("days", [7, 30, 90])
def test_history_scan_adds_only_within_range_and_deduplicates(desk, days):
    store, state, review, settings, api, windows = desk
    summary = api.trigger_scan(days)["summary"]
    assert windows == [days]
    assert summary["reviews_created"] == (1 if days == 90 else 0)
    assert len(store.all()) == (1 if days == 90 else 0)
    assert api.trigger_scan(days)["summary"]["reviews_created"] == 0
    assert settings.lookback_days == 3
    api.trigger_scan()
    assert windows[-1] == 3


@pytest.mark.parametrize("status", ["pending", "ignored", "resolved", "tombstoned"])
def test_history_rechecks_current_parser_old_pending_but_preserves_manual_states(desk, status):
    store, state, review, settings, api, windows = desk
    old = replace(review, status="ignored" if status == "tombstoned" else status,
                  company=None, role=None, role_raw=None, role_canonical=None)
    store.save(old)
    state.record_outcome(review.id, status, parser_version=scanner.PARSER_VERSION,
                         locator=review.mail_locator, internal_date=review.received_at)
    summary = api.trigger_scan(90)["summary"]
    actual = store.load(review.id)
    if status == "pending":
        assert actual.id == old.id and actual.company == review.company and actual.role == review.role
        assert summary["reviews_updated"] == 1
        assert api.trigger_scan(90)["summary"]["reviews_updated"] == 0
    else:
        assert actual == old
        assert state.outcome(review.id).outcome == status


def test_restore_ignored_updates_both_indexes_and_survives_history_scan(desk):
    store, state, review, settings, api, windows = desk
    legacy = replace(review, id="legacy-fixture", status="ignored", parser_version="old")
    store.save(legacy)
    for key in (legacy.id, review.id):
        state.record_outcome(key, "ignored", locator=review.mail_locator)
    assert [r["id"] for r in api.list_ignored_reviews()] == [legacy.id]
    assert "mail_locator" not in api.list_ignored_reviews()[0]
    api.restore_ignored_review(legacy.id, legacy.revision)
    restored = store.load(legacy.id)
    assert restored.status == "pending" and restored.manual_restore
    assert restored.revision == legacy.revision + 1
    assert store.filter_marketing(legacy.id, parser_version="new-rules") == restored
    assert api.list_ignored_reviews() == []
    for key in (legacy.id, review.id):
        assert state.outcome(key).outcome == "pending"
    api.trigger_scan(90)
    assert store.load(legacy.id) == restored
    assert len(store.all()) == 1
    api.trigger_scan()
    assert windows[-1] == 3  # restored old parser does not create perpetual replay debt
    api.ignore_unresolved(legacy.id)
    ignored = store.load(legacy.id)
    assert ignored.status == "ignored" and not ignored.manual_restore
    api.restore_ignored_review(legacy.id, ignored.revision)
    assert store.load(legacy.id).revision == restored.revision + 2


@pytest.mark.parametrize("outcome", ["resolved", "tombstoned"])
def test_restore_rejects_protected_alias_without_partial_index_change(desk, outcome):
    store, state, review, settings, api, windows = desk
    old = replace(review, id="legacy-fixture", status="ignored")
    store.save(old)
    state.record_outcome(old.id, "ignored")
    state.record_outcome(review.id, outcome)
    with pytest.raises(ValueError):
        api.restore_ignored_review(old.id, old.revision)
    assert store.load(old.id) == old
    assert state.outcome(old.id).outcome == "ignored"
    assert state.outcome(review.id).outcome == outcome


def test_restore_save_failure_keeps_ignored_fact_authoritative(desk, monkeypatch):
    store, state, review, settings, api, windows = desk
    old = replace(review, status="ignored")
    store.save(old)
    state.record_outcome(old.id, "ignored")
    save = UnresolvedStore.save
    def fail_restore(self, record):
        if record.manual_restore:
            raise OSError("injected write failure")
        return save(self, record)
    monkeypatch.setattr(UnresolvedStore, "save", fail_restore)
    with pytest.raises(OSError):
        api.restore_ignored_review(old.id, old.revision)
    assert store.load(old.id) == old
    api.trigger_scan(90)
    assert store.load(old.id) == old
    assert state.outcome(old.id).outcome == "ignored"


def test_stale_restore_and_invalid_scan_range_are_rejected(desk):
    store, state, review, settings, api, windows = desk
    store.save(replace(review, status="ignored", revision=3))
    with pytest.raises(ValueError):
        api.restore_ignored_review(review.id, 2)
    for days in (True, 0, 365, "90", 7.0):
        with pytest.raises(ValueError):
            api.trigger_scan(days)
    assert windows == []


def test_explanations_distinguish_missing_conflict_and_confirmation(desk):
    _, _, review, _, _, _ = desk
    assert "已识别" in explain_review(review)["reason_label"]
    assert "公司、岗位" in explain_review(replace(review, company=None, role=None))["reason_label"]
    conflict = replace(review, company=None, company_source="conflicting-identity-evidence")
    assert "公司信息有冲突" in explain_review(conflict)["reason_label"]
    assert "多个" in explain_review(replace(review, reason="multiple-candidates"))["reason_label"]
    sources = explain_review(replace(review, company_source="subject-legal-employer", role_source="local-confirmed-correction"))
    assert sources["identity_sources"] == "公司：邮件标题；岗位：本机纠错规则"
