from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import pytest

from job_mail_desk import scanner
from job_mail_desk.activity_store import ActivityStore
from job_mail_desk.application_registry import ApplicationRegistry
from job_mail_desk.config import Settings
from job_mail_desk.identity_dictionaries import load_identity_dictionaries
from job_mail_desk.identity_pipeline import resolve_event_batch
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.models import MailRecord
from job_mail_desk.parser import SHANGHAI, parse_record
from job_mail_desk.state import StateStore
from job_mail_desk.unresolved_store import UnresolvedStore, unresolved_from_decision


def message(subject, body, uid="1"):
    received = datetime.now(SHANGHAI) - timedelta(days=7)
    return MailRecord(
        uid=uid, subject=subject, body=body,
        message_id=f"<{uid}@example.invalid>",
        sender="招聘服务 <noreply@example.invalid>",
        received_at=received, internal_date=received,
        mailbox="INBOX", uidvalidity="1", account_fingerprint="f" * 24,
    )


@pytest.mark.parametrize("subject,body", [
    ("【示例科技】在线笔试通知", "应聘岗位：工程师。考试时间：9月22日14:00。页脚：校招宣讲会启动，现场直通offer。"),
    ("【示例科技】面试安排", "感谢参加宣讲会。您的技术面试时间：9月22日14:00。"),
    ("感谢您投递示例科技的软件工程师岗位", "校园招聘火热报名，欢迎参加宣讲会。"),
    ("【示例科技】宣讲会暨笔试邀请", "欢迎您应聘本公司的软件工程师岗位。诚邀您前来参加本公司的现场宣讲会暨笔试。时间：9月22日14:00。"),
    ("【示例科技】现场技术面试邀请", "感谢您参加校园招聘。您的预计面试开始时间：9月22日14:00。本宣讲邀约由招聘平台代发。"),
    ("应聘结果反馈", "遗憾地通知您，您的申请未通过。页脚：欢迎关注校招宣讲会。"),
])
def test_personal_hiring_steps_survive_marketing_footer(subject, body):
    assert parse_record(message(subject, body)) is not None


@pytest.mark.parametrize("status,truncated,shadow,filtered", [
    ("pending", False, False, True),
    ("pending", True, False, False),
    ("resolved", False, False, False),
    ("ignored", False, False, False),
    ("pending", False, True, False),
])
def test_upgrade_withdraws_only_complete_pending_broadcasts(
    tmp_path, monkeypatch, status, truncated, shadow, filtered,
):
    record = replace(message(
        "【示例科技】宣讲会邀请", "9月22日14:00，地点：示例大学教学楼301。现场面试直通offer。",
    ), content_truncated=truncated)
    # Model the old parser's false positive without copying a real mailbox.
    old_event = parse_record(message("【示例科技】招聘通知", "应聘岗位：软件工程师。"))
    assert old_event
    old_event = replace(old_event, title=record.subject)
    decision = resolve_event_batch([old_event], [], load_identity_dictionaries())[0]
    source_hash = scanner._record_source_hashes(record)[0]
    reviews = UnresolvedStore(tmp_path / "unresolved")
    old = replace(unresolved_from_decision(
        source_hash, decision, record.locator(), parser_version="2026.09.12.1",
    ), status=status)
    reviews.save(old)
    activities = ActivityStore(tmp_path / "activity.json")
    activities.record_event(dedup_key="old-review", kind="review.created",
                            entity_id=f"review:{source_hash}", tabs=("today", "review"))
    activities.record_event(dedup_key="unrelated", kind="review.created",
                            entity_id="review:other", tabs=("today", "review"))

    class Reader:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_since(self, days):
            assert days == 30
            return [record]

    for name, path in {"TASKS_DIR": "tasks", "STATE_DB": "state.db",
                       "DASHBOARD_FILE": "dashboard.md", "DICTIONARIES_DIR": "dict"}.items():
        monkeypatch.setattr(scanner, name, tmp_path / path)
    monkeypatch.setattr(scanner, "ImapReader", Reader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "ActivityStore", lambda path: activities)
    monkeypatch.setattr(scanner, "_learn_from_confirmed_records", lambda *a, **kw: {})
    scanner.scan_once(Settings(), shadow=shadow)
    actual = reviews.load(source_hash)
    if filtered:
        assert actual.status == "filtered"
        assert actual.reason == "recruiting-marketing"
        assert actual.revision == old.revision + 1
        assert actual.parser_version == scanner.PARSER_VERSION
        assert StateStore(tmp_path / "state.db").outcome(source_hash).outcome == "filtered"
        assert activities.unique_unread_count() == 1
        # A subsequent replay cannot revive it or generate new cards.
        scanner.scan_once(Settings(), days=30)
        assert reviews.load(source_hash) == actual
    else:
        assert actual == old
        assert activities.unique_unread_count() == 2
    assert MarkdownTaskStore(tmp_path / "tasks").all() == []
    assert ApplicationRegistry(tmp_path / "applications").all() == []


def test_activity_retraction_preserves_other_unreads_sequences_and_dedup(tmp_path):
    path = tmp_path / "activity.json"
    activities = ActivityStore(path)
    original = activities.record_event(dedup_key="promo", kind="review.created",
                                       entity_id="review:promo", tabs=("today", "review"))
    activities.record_event(dedup_key="real", kind="review.created",
                            entity_id="review:real", tabs=("today", "review"))
    activities.dismiss_filtered_review("promo")
    activities.dismiss_filtered_review("promo")
    reopened = ActivityStore(path)
    assert reopened.unique_unread_count() == 1
    assert reopened.unread_payload()["snapshot_sequence"] == 2
    replay = reopened.record_event(dedup_key="promo", kind="review.created",
                                   entity_id="review:promo", tabs=("today", "review"))
    assert replay.sequence == original.sequence
    assert reopened.unique_unread_count() == 1


def test_onsite_preference_golden_vectors():
    vectors = json.loads((Path(__file__).parent / "golden/mail_filter_policy.json").read_text(encoding="utf-8"))
    for vector in vectors:
        record = message(vector["subject"], vector["body"])
        assert parse_record(record) is None, vector["name"]
        event = parse_record(record, include_onsite_sessions=True)
        assert (event is not None) == vector["keep_onsite"], vector["name"]
        if event:
            assert event.stage == "招聘通知"
            assert event.event_type == "notice"
            assert event.start_at is not None


def test_scan_policy_toggle_replays_filtered_records_but_never_manual_ignores(tmp_path, monkeypatch):
    records = [
        message("示例科技宣讲会", "宣讲时间：2026年9月22日14:00。地点：示例大学教学楼301。", "11"),
        message("示例科技宣讲会", "宣讲时间：2026年9月23日14:00。地点：示例大学教学楼302。", "12"),
    ]
    ranges = []

    class Reader:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_since(self, days):
            ranges.append(days)
            return records

    for name, path in {"TASKS_DIR": "tasks", "STATE_DB": "state.db",
                       "DASHBOARD_FILE": "dashboard.md", "DICTIONARIES_DIR": "dict"}.items():
        monkeypatch.setattr(scanner, name, tmp_path / path)
    monkeypatch.setattr(scanner, "ImapReader", Reader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "_learn_from_confirmed_records", lambda *a, **kw: {})
    off, on = Settings(), Settings(include_onsite_sessions=True)
    summary = scanner.scan_once(off)
    assert summary.fetched == 2 and summary.filtered == 2 and summary.candidates == 0
    assert summary.lookback_days == 30
    state = StateStore(tmp_path / "state.db")
    details = state.health()["scan_details"]
    assert details["fetched"] == 2 and details["filtered"] == 2 and details["lookback_days"] == 30
    summary = scanner.scan_once(on)
    assert summary.candidates == 2 and summary.filtered == 0
    reviews = UnresolvedStore(tmp_path / "unresolved")
    keys = [scanner._record_source_hashes(r)[0] for r in records]
    assert all(reviews.load(key).status == "pending" for key in keys)
    initial_revision = reviews.load(keys[0]).revision
    reviews.ignore(keys[1])
    scanner.scan_once(off)
    assert reviews.load(keys[0]).status == "filtered"
    assert reviews.load(keys[1]).status == "ignored"
    summary = scanner.scan_once(on)
    assert summary.candidates == 1
    assert reviews.load(keys[0]).status == "pending"
    assert reviews.load(keys[0]).revision > initial_revision
    assert reviews.load(keys[1]).status == "ignored"
    scanner.scan_once(on)
    assert ranges == [30, 30, 30, 30, 3]
    assert state.health()["first_scan"]["fetched"] == 2
    assert MarkdownTaskStore(tmp_path / "tasks").all() == []
    assert ApplicationRegistry(tmp_path / "applications").all() == []


@pytest.mark.parametrize("automatic", [True, False])
def test_prior_rc_auto_filter_migrates_without_reviving_manual_ignore(tmp_path, monkeypatch, automatic):
    record = message("示例科技宣讲会", "宣讲时间：2026年9月22日14:00。地点：示例大学教学楼301。")
    event = parse_record(record, include_onsite_sessions=True)
    decision = resolve_event_batch([event], [], load_identity_dictionaries())[0]
    key = scanner._record_source_hashes(record)[0]
    reviews = UnresolvedStore(tmp_path / "unresolved")
    review = replace(unresolved_from_decision(key, decision, record.locator(), parser_version="2026.09.19.1"),
                     status="ignored", reason="recruiting-marketing" if automatic else "insufficient-identity-evidence")
    reviews.save(review)
    state = StateStore(tmp_path / "state.db")
    state.record_outcome(key, "ignored", parser_version="2026.09.19.1",
                         locator=record.locator(), internal_date=record.internal_date)

    class Reader:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_since(self, days):
            return [record]

    for name, path in {"TASKS_DIR": "tasks", "STATE_DB": "state.db",
                       "DASHBOARD_FILE": "dashboard.md", "DICTIONARIES_DIR": "dict"}.items():
        monkeypatch.setattr(scanner, name, tmp_path / path)
    monkeypatch.setattr(scanner, "ImapReader", Reader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "_learn_from_confirmed_records", lambda *a, **kw: {})
    scanner.scan_once(Settings(include_onsite_sessions=True))
    expected = "pending" if automatic else "ignored"
    assert reviews.load(key).status == expected
    assert state.outcome(key).outcome == expected


def test_scan_history_upgrade_preserves_unknown_range_and_first_counts(tmp_path):
    import sqlite3

    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE scan_runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, fetched INTEGER, candidates INTEGER, error TEXT)")
        db.execute("INSERT INTO scan_runs VALUES (1, '2026-09-19', '2026-09-19', 125, 8, NULL)")
    state = StateStore(path)
    before = state.health()
    assert before["first_scan"]["fetched"] == 125
    assert before["first_scan"]["candidates"] == 8
    assert before["scan_details"]["lookback_days"] is None
    scan = state.begin_scan()
    state.finish_scan(scan, fetched=11, candidates=0, searched=15, lookback_days=3, mailbox="INBOX", skipped=11)
    after = state.health()
    assert after["first_scan"] == before["first_scan"]
    assert after["scan_details"]["lookback_days"] == 3
    assert after["scan_details"]["skipped"] == 11


@pytest.mark.parametrize("previously_baselined", [False, True])
def test_first_scan_and_upgrade_recognise_personal_mail_older_than_48_hours(tmp_path, monkeypatch, previously_baselined):
    received = datetime.now(SHANGHAI) - timedelta(days=20)
    record = replace(message("【示例科技】面试通知", "应聘岗位：软件工程师。面试时间：9月22日14:00。"),
                     received_at=received, internal_date=received)
    key = scanner._record_source_hashes(record)[0]
    state = StateStore(tmp_path / "state.db")
    if previously_baselined:
        state.record_outcome(key, "noncandidate", parser_version="2026.09.12.1",
                             locator=record.locator(), internal_date=received)
        run = state.begin_scan()
        state.finish_scan(run, fetched=1, candidates=0, parser_version="2026.09.12.1")

    class Reader:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_since(self, days):
            assert days == 30
            return [record]

    for name, path in {"TASKS_DIR": "tasks", "STATE_DB": "state.db",
                       "DASHBOARD_FILE": "dashboard.md", "DICTIONARIES_DIR": "dict"}.items():
        monkeypatch.setattr(scanner, name, tmp_path / path)
    monkeypatch.setattr(scanner, "ImapReader", Reader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "_learn_from_confirmed_records", lambda *a, **kw: {})
    summary = scanner.scan_once(Settings())
    assert summary.fetched == 1 and summary.candidates == 1 and summary.skipped == 0
    assert UnresolvedStore(tmp_path / "unresolved").load(key).status == "pending"
    assert state.outcome(key).outcome == "pending"
    assert MarkdownTaskStore(tmp_path / "tasks").all() == []
