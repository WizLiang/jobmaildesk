from job_mail_desk.config import Settings
from datetime import datetime, timedelta

from job_mail_desk.models import JobTask, MailRecord, ParsedEvent
from job_mail_desk.application_registry import (
    ApplicationRegistry,
    application_from_user_payload,
)
from job_mail_desk.activity_store import ActivityStore
from job_mail_desk.identity_dictionaries import load_identity_dictionaries
from job_mail_desk.mail_reader import MailFetchBatch
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.scanner import (
    INITIAL_LOOKBACK_DAYS,
    SOURCE_IDENTITY_VERSION,
    _effective_lookback_days,
    _is_stale_attention,
    _prepare_uid_source_migration,
    _record_source_hashes,
    _source_hash_from_locator,
)
from job_mail_desk import scanner
from job_mail_desk.state import MailboxScope, StateStore
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.unresolved_store import UnresolvedRecord, UnresolvedStore
import pytest


def _configure_scan_test(
    monkeypatch,
    tmp_path,
    *,
    reader,
    parse_record,
) -> None:
    monkeypatch.setattr(scanner, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(scanner, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(scanner, "DASHBOARD_FILE", tmp_path / "dashboard.md")
    monkeypatch.setattr(scanner, "ImapReader", reader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "parse_record", parse_record)


def _located_record(
    uid: str,
    *,
    message_id: str,
    internal_date: datetime,
    account_fingerprint: str,
    uidvalidity: str,
) -> MailRecord:
    return MailRecord(
        uid=uid,
        subject=f"招聘通知 {uid}",
        message_id=message_id,
        sender="招聘系统 <noreply@example.invalid>",
        received_at=internal_date,
        body="请查看招聘进展。",
        mailbox="INBOX",
        uidvalidity=uidvalidity,
        account_fingerprint=account_fingerprint,
        internal_date=internal_date,
    )


def _candidate_event(record: MailRecord) -> ParsedEvent:
    return ParsedEvent(
        company="样例公司",
        role="工程师",
        recruiting_project=None,
        event_type="interview",
        stage="面试",
        round=None,
        title=record.subject,
        start_at=None,
        end_at=None,
        deadline_at=None,
        source_message_id=record.message_id,
        source_received_at=record.received_at,
        source_sender=record.sender,
        source_url=None,
        action_summary="查看招聘进展",
        requirements=(),
        matched_keywords=(),
        confidence=0.9,
        change_type="new",
        company_confidence=0.97,
        company_source="test-explicit-company",
        role_confidence=0.97,
        role_source="test-explicit-role",
    )


def _commit_scan_baseline(
    state: StateStore,
    *,
    parser_version: str,
    scope: MailboxScope,
) -> None:
    run_id = state.begin_scan()
    state.finish_scan(
        run_id,
        fetched=0,
        candidates=0,
        parser_version=parser_version,
        source_migrations=(scope,),
        source_identity_version=SOURCE_IDENTITY_VERSION,
    )


def _pending_review(
    source_hash: str,
    record: MailRecord,
    *,
    parser_version: str,
) -> UnresolvedRecord:
    return UnresolvedRecord(
        id=source_hash,
        status="pending",
        resolution_status="unresolved",
        reason="test-pending",
        company="样例公司",
        role="工程师",
        recruiting_project=None,
        event_type="interview",
        stage="面试",
        round=None,
        received_at=record.received_at,
        start_at=None,
        end_at=None,
        deadline_at=None,
        action_summary="查看招聘进展",
        title=record.subject,
        requirements=(),
        confidence=0.9,
        change_type="new",
        candidate_application_keys=(),
        resolved_application_key=None,
        resolved_task_id=None,
        rule_version="identity-registry-v1",
        mail_locator=record.locator(),
        parser_version=parser_version,
        semantic_hash=f"semantic-{parser_version}",
    )


def test_first_scan_uses_30_days_then_returns_to_configured_window(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    settings = Settings(lookback_days=3)
    assert _effective_lookback_days(settings, state, None) == INITIAL_LOOKBACK_DAYS
    assert _effective_lookback_days(settings, state, 12) == 12

    run_id = state.begin_scan()
    state.finish_scan(run_id, fetched=0, candidates=0)

    assert _effective_lookback_days(settings, state, None) == 3
    assert (
        _effective_lookback_days(
            settings,
            state,
            None,
            parser_changed=True,
        )
        == INITIAL_LOOKBACK_DAYS
    )


def test_uid_identity_recovers_recent_messages_with_reused_message_id(
    tmp_path,
) -> None:
    now = datetime.now(SHANGHAI)

    def record(uid: str, received_at: datetime) -> MailRecord:
        return MailRecord(
            uid=uid,
            subject="申请确认",
            message_id="<provider-reused@example.invalid>",
            sender="招聘系统 <noreply@example.invalid>",
            received_at=received_at,
            body="感谢您投递岗位",
            mailbox="INBOX",
            uidvalidity="1",
            account_fingerprint="a" * 24,
            internal_date=received_at,
        )

    recent_one = record("101", now - timedelta(minutes=10))
    recent_two = record("102", now - timedelta(minutes=5))
    historical = record("99", now - timedelta(days=10))
    state = StateStore(tmp_path / "state.db")
    state.mark_processed(
        scanner.message_hash("<provider-reused@example.invalid>"),
        None,
    )
    store = MarkdownTaskStore(tmp_path / "tasks")
    unresolved = UnresolvedStore(tmp_path / "unresolved")

    assert _prepare_uid_source_migration(
        state,
        [historical, recent_one, recent_two],
        store,
        unresolved,
    )

    first_hash, first_legacy = _record_source_hashes(recent_one)
    second_hash, second_legacy = _record_source_hashes(recent_two)
    historical_hash, _ = _record_source_hashes(historical)
    assert first_hash != second_hash
    assert first_legacy == second_legacy
    assert not state.is_processed(first_hash)
    assert not state.is_processed(second_hash)
    assert state.is_processed(historical_hash)


def test_uid_source_hash_requires_complete_namespace() -> None:
    locator = {
        "account_fingerprint": "a" * 24,
        "mailbox": "INBOX",
        "uidvalidity": "10",
        "uid": "42",
    }

    assert _source_hash_from_locator(locator)
    for field in ("account_fingerprint", "mailbox", "uidvalidity", "uid"):
        incomplete = dict(locator)
        incomplete.pop(field)
        assert _source_hash_from_locator(incomplete) is None
    assert _source_hash_from_locator({**locator, "uid": "0"}) is None
    assert _source_hash_from_locator(
        {**locator, "uidvalidity": "not-a-number"}
    ) is None


def test_uid_migration_uses_internaldate_and_is_namespace_scoped(
    tmp_path,
) -> None:
    now = datetime.now(SHANGHAI)
    fingerprint = "a" * 24

    def record(
        uid: str,
        *,
        received_at: datetime,
        internal_date: datetime,
        account: str = fingerprint,
        mailbox: str = "INBOX",
        uidvalidity: str = "10",
    ) -> MailRecord:
        return MailRecord(
            uid=uid,
            subject="通知",
            message_id=f"<{uid}@example.invalid>",
            sender="招聘系统 <noreply@example.invalid>",
            received_at=received_at,
            body="普通通知",
            mailbox=mailbox,
            uidvalidity=uidvalidity,
            account_fingerprint=account,
            internal_date=internal_date,
        )

    old_header_recent_internal = record(
        "1",
        received_at=now - timedelta(days=30),
        internal_date=now - timedelta(hours=1),
    )
    recent_header_old_internal = record(
        "2",
        received_at=now - timedelta(minutes=5),
        internal_date=now - timedelta(days=30),
    )
    state = StateStore(tmp_path / "state.db")
    # This fixture represents a real pre-UID import, not an empty first run.
    state.mark_processed(scanner.message_hash(recent_header_old_internal.message_id), None)
    store = MarkdownTaskStore(tmp_path / "tasks")
    unresolved = UnresolvedStore(tmp_path / "unresolved")

    scopes = _prepare_uid_source_migration(
        state,
        [old_header_recent_internal, recent_header_old_internal],
        store,
        unresolved,
    )

    recent_hash, _ = _record_source_hashes(old_header_recent_internal)
    historical_hash, _ = _record_source_hashes(recent_header_old_internal)
    assert state.outcome(recent_hash) is None
    assert state.outcome(historical_hash).outcome == "noncandidate"
    assert scopes == (MailboxScope(fingerprint, "INBOX", "10"),)

    run_id = state.begin_scan()
    state.finish_scan(
        run_id,
        fetched=2,
        candidates=0,
        source_migrations=scopes,
        source_identity_version=SOURCE_IDENTITY_VERSION,
    )
    assert not state.source_migration_needed(
        MailboxScope(fingerprint, "INBOX", "10"),
        SOURCE_IDENTITY_VERSION,
    )

    changed_uidvalidity = record(
        "1",
        received_at=now,
        internal_date=now,
        uidvalidity="11",
    )
    switched_account = record(
        "1",
        received_at=now,
        internal_date=now,
        account="b" * 24,
    )
    changed_mailbox = record(
        "1",
        received_at=now,
        internal_date=now,
        mailbox="Archive",
    )
    hashes = {
        _record_source_hashes(item)[0]
        for item in (
            old_header_recent_internal,
            changed_uidvalidity,
            switched_account,
            changed_mailbox,
        )
    }
    assert len(hashes) == 4
    for item in (changed_uidvalidity, switched_account, changed_mailbox):
        assert _prepare_uid_source_migration(
            state,
            [item],
            store,
            unresolved,
        )


def test_scan_processes_reused_message_id_uids_independently(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime.now(SHANGHAI)
    records = [
        MailRecord(
            uid=uid,
            subject="申请确认",
            message_id="<provider-reused@example.invalid>",
            sender="招聘系统 <noreply@example.invalid>",
            received_at=now,
            body="普通通知",
            mailbox="INBOX",
            uidvalidity="12",
            account_fingerprint="b" * 24,
            internal_date=now,
        )
        for uid in ("101", "102")
    ]

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_since(self, days):
            return records

    parsed_uids: list[str] = []

    def fake_parse(record, dictionaries=None):
        parsed_uids.append(record.uid)
        return None

    monkeypatch.setattr(scanner, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(scanner, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(scanner, "DASHBOARD_FILE", tmp_path / "dashboard.md")
    monkeypatch.setattr(scanner, "ImapReader", FakeReader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "parse_record", fake_parse)

    first = scanner.scan_once(Settings(), days=3)
    second = scanner.scan_once(Settings(), days=3)

    assert first.skipped == 0
    assert second.skipped == 2
    assert parsed_uids == ["101", "102"]
    hashes = {_record_source_hashes(record)[0] for record in records}
    assert len(hashes) == 2
    state = StateStore(tmp_path / "state.db")
    assert {
        state.outcome(source_hash).outcome for source_hash in hashes
    } == {"noncandidate"}


def test_reused_message_id_does_not_match_legacy_review_with_other_uid(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime.now(SHANGHAI)
    fingerprint = "3" * 24
    message_id = "<legacy-review-reused@example.invalid>"
    original = _located_record(
        "101",
        message_id=message_id,
        internal_date=now - timedelta(minutes=2),
        account_fingerprint=fingerprint,
        uidvalidity="63",
    )
    reused = _located_record(
        "102",
        message_id=message_id,
        internal_date=now,
        account_fingerprint=fingerprint,
        uidvalidity="63",
    )
    legacy_hash = scanner.message_hash(message_id)
    unresolved = UnresolvedStore(tmp_path / "unresolved")
    unresolved.save(
        _pending_review(
            legacy_hash,
            original,
            parser_version=scanner.PARSER_VERSION,
        )
    )

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_since(self, days):
            return [reused]

    parsed_uids: list[str] = []

    def fake_parse(mail_record, dictionaries=None):
        parsed_uids.append(mail_record.uid)
        return _candidate_event(mail_record)

    _configure_scan_test(
        monkeypatch,
        tmp_path,
        reader=FakeReader,
        parse_record=fake_parse,
    )
    state = StateStore(tmp_path / "state.db")
    scope = MailboxScope(fingerprint, "INBOX", "63")
    _commit_scan_baseline(
        state,
        parser_version=scanner.PARSER_VERSION,
        scope=scope,
    )
    state.set_metadata(
        "identity_learning_bootstrap_version",
        scanner.PARSER_VERSION,
    )

    summary = scanner.scan_once(Settings(), days=3)

    reused_hash, _ = _record_source_hashes(reused)
    assert summary.skipped == 0
    assert summary.candidates == 1
    assert parsed_uids == [reused.uid]
    assert unresolved.load(legacy_hash) is not None
    assert unresolved.load(reused_hash) is not None
    assert state.outcome(reused_hash).outcome == "pending"


def test_reused_message_id_does_not_inherit_legacy_task_fact(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime.now(SHANGHAI)
    fingerprint = "4" * 24
    message_id = "<legacy-task-reused@example.invalid>"
    original = _located_record(
        "201",
        message_id=message_id,
        internal_date=now - timedelta(minutes=2),
        account_fingerprint=fingerprint,
        uidvalidity="64",
    )
    reused = _located_record(
        "202",
        message_id=message_id,
        internal_date=now,
        account_fingerprint=fingerprint,
        uidvalidity="64",
    )
    MarkdownTaskStore(tmp_path / "tasks").save(
        JobTask(
            id="a" * 24,
            application_id="b" * 20,
            company="样例公司",
            role="工程师",
            recruiting_project=None,
            event_type="notice",
            stage="招聘通知",
            round=None,
            received_at=original.received_at,
            start_at=None,
            end_at=None,
            deadline_at=None,
            priority="normal",
            status="needs_review",
            change_type="new",
            source_message_hash=scanner.message_hash(message_id),
            research_status="closed",
            confidence=1.0,
            title=original.subject,
            action_summary="查看招聘进展",
            mail_locator=original.locator(),
        )
    )

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_since(self, days):
            return [reused]

    parsed_uids: list[str] = []

    def fake_parse(mail_record, dictionaries=None):
        parsed_uids.append(mail_record.uid)
        return None

    _configure_scan_test(
        monkeypatch,
        tmp_path,
        reader=FakeReader,
        parse_record=fake_parse,
    )
    state = StateStore(tmp_path / "state.db")
    scope = MailboxScope(fingerprint, "INBOX", "64")
    _commit_scan_baseline(
        state,
        parser_version=scanner.PARSER_VERSION,
        scope=scope,
    )
    state.set_metadata(
        "identity_learning_bootstrap_version",
        scanner.PARSER_VERSION,
    )

    summary = scanner.scan_once(Settings(), days=3)

    reused_hash, _ = _record_source_hashes(reused)
    assert summary.skipped == 0
    assert parsed_uids == [reused.uid]
    assert state.outcome(reused_hash).outcome == "noncandidate"


def test_legacy_tombstone_requires_matching_locator(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime.now(SHANGHAI)
    fingerprint = "5" * 24
    message_id = "<legacy-tombstone-reused@example.invalid>"
    original = _located_record(
        "301",
        message_id=message_id,
        internal_date=now - timedelta(minutes=2),
        account_fingerprint=fingerprint,
        uidvalidity="65",
    )
    reused = _located_record(
        "302",
        message_id=message_id,
        internal_date=now,
        account_fingerprint=fingerprint,
        uidvalidity="65",
    )
    legacy_hash = scanner.message_hash(message_id)
    application = application_from_user_payload(
        {"company": "样例公司", "role": "工程师"},
        now=now,
    )
    application.deleted_at = now
    application.deletion_reason = "permanently-deleted"
    application.deleted_source_hashes = [legacy_hash]
    ApplicationRegistry(tmp_path / "applications").save(application)

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_since(self, days):
            return [original, reused]

    parsed_uids: list[str] = []

    def fake_parse(mail_record, dictionaries=None):
        parsed_uids.append(mail_record.uid)
        return None

    _configure_scan_test(
        monkeypatch,
        tmp_path,
        reader=FakeReader,
        parse_record=fake_parse,
    )
    state = StateStore(tmp_path / "state.db")
    scope = MailboxScope(fingerprint, "INBOX", "65")
    _commit_scan_baseline(
        state,
        parser_version=scanner.PARSER_VERSION,
        scope=scope,
    )
    state.set_metadata(
        "identity_learning_bootstrap_version",
        scanner.PARSER_VERSION,
    )
    state.record_outcome(
        legacy_hash,
        "tombstoned",
        parser_version=scanner.PARSER_VERSION,
        locator=original.locator(),
        internal_date=original.internal_date,
    )

    summary = scanner.scan_once(Settings(), days=3)

    original_hash, _ = _record_source_hashes(original)
    reused_hash, _ = _record_source_hashes(reused)
    assert summary.skipped == 1
    assert parsed_uids == [reused.uid]
    assert state.outcome(original_hash).outcome == "tombstoned"
    assert state.outcome(reused_hash).outcome == "noncandidate"


def test_old_undated_assessment_leaves_attention_views() -> None:
    now = datetime(2026, 8, 1, 12, 0, tzinfo=SHANGHAI)
    task = JobTask(
        id="1" * 24,
        application_id="2" * 20,
        company="样例公司",
        role="产品经理",
        recruiting_project=None,
        event_type="assessment",
        stage="人才测评",
        round=None,
        received_at=now - timedelta(days=8),
        start_at=None,
        end_at=None,
        deadline_at=None,
        priority="normal",
        status="needs_review",
        change_type="new",
        source_message_hash="3" * 32,
        research_status="closed",
        confidence=1.0,
        title="人才测评",
        action_summary="完成测评",
    )

    assert _is_stale_attention(task, now)
    task.status = "confirmed"
    assert not _is_stale_attention(task, now)


def test_scan_does_not_expire_application_paused_task(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime.now(SHANGHAI)
    paused = JobTask(
        id="9" * 24,
        application_id="8" * 20,
        application_key="app-" + "7" * 24,
        company="样例公司",
        role="产品经理",
        recruiting_project=None,
        event_type="assessment",
        stage="在线测评",
        round=None,
        received_at=now - timedelta(days=2),
        start_at=None,
        end_at=now - timedelta(hours=1),
        deadline_at=None,
        priority="normal",
        status="planned",
        change_type="new",
        source_message_hash="6" * 32,
        research_status="not_queued",
        confidence=1.0,
        title="在线测评",
        action_summary="完成测评",
        updated_at=now - timedelta(days=1),
        application_paused=True,
    )
    store = MarkdownTaskStore(tmp_path / "tasks")
    store.save(paused)

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_since(self, days):
            return []

    monkeypatch.setattr(scanner, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(scanner, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(scanner, "DASHBOARD_FILE", tmp_path / "dashboard.md")
    monkeypatch.setattr(scanner, "DICTIONARIES_DIR", tmp_path / "dictionaries")
    monkeypatch.setattr(scanner, "ensure_directories", lambda: None)
    monkeypatch.setattr(scanner, "ImapReader", FakeReader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "set_dock_badge", lambda _count: None)

    scanner.scan_once(
        Settings(research_queue=tmp_path / "research.jsonl"),
        days=3,
    )

    refreshed = store.load(paused.id)
    assert refreshed is not None
    assert refreshed.status == "planned"
    assert refreshed.updated_at == paused.updated_at
    assert refreshed.application_paused is True


def test_one_parser_failure_does_not_abort_the_mail_batch(tmp_path, monkeypatch) -> None:
    records = [
        MailRecord(
            uid="bad",
            subject="坏日期模板",
            message_id="<bad@example.invalid>",
            sender="样例招聘 <noreply@example.invalid>",
            received_at=datetime(2026, 8, 3, 9, 0, tzinfo=SHANGHAI),
            body="400-618-5106 服务时间 9:00-18:00",
        ),
        MailRecord(
            uid="good",
            subject="普通通知",
            message_id="<good@example.invalid>",
            sender="样例招聘 <noreply@example.invalid>",
            received_at=datetime(2026, 8, 3, 9, 1, tzinfo=SHANGHAI),
            body="普通邮件",
        ),
    ]

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_since(self, days):
            return records

    def fake_parse(record, dictionaries=None):
        if record.uid == "bad":
            raise ValueError("month must be in 1..12")
        return None

    monkeypatch.setattr(scanner, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(scanner, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(scanner, "DASHBOARD_FILE", tmp_path / "dashboard.md")
    monkeypatch.setattr(scanner, "ImapReader", FakeReader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "parse_record", fake_parse)

    summary = scanner.scan_once(Settings(), days=3)
    assert summary.fetched == 2
    assert summary.parse_failed == 1
    assert summary.candidates == 0

    repeated = scanner.scan_once(Settings(), days=3)
    assert repeated.fetched == 2
    assert repeated.parse_failed == 0
    assert repeated.skipped == 2

    forced = scanner.scan_once(Settings(), days=3, force_reprocess=True)
    assert forced.fetched == 2
    assert forced.skipped == 0
    assert forced.parse_failed == 1


def test_parser_upgrade_and_manual_retry_only_replay_no_fact_records(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime.now(SHANGHAI)
    fingerprint = "c" * 24

    def record(uid: str, internal_date: datetime) -> MailRecord:
        return MailRecord(
            uid=uid,
            subject=f"通知 {uid}",
            message_id=f"<{uid}@example.invalid>",
            sender="招聘系统 <noreply@example.invalid>",
            received_at=internal_date,
            body="普通通知",
            mailbox="INBOX",
            uidvalidity="20",
            account_fingerprint=fingerprint,
            internal_date=internal_date,
        )

    records = [
        record("1", now - timedelta(hours=1)),
        record("2", now - timedelta(hours=2)),
        record("3", now - timedelta(days=31)),
        record("4", now - timedelta(hours=3)),
    ]

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_since(self, days):
            return records

    monkeypatch.setattr(scanner, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(scanner, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(scanner, "DASHBOARD_FILE", tmp_path / "dashboard.md")
    monkeypatch.setattr(scanner, "ImapReader", FakeReader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "PARSER_VERSION", "v2")

    state = StateStore(tmp_path / "state.db")
    scope = MailboxScope(fingerprint, "INBOX", "20")
    baseline = state.begin_scan()
    state.finish_scan(
        baseline,
        fetched=0,
        candidates=0,
        parser_version="v1",
        source_migrations=(scope,),
        source_identity_version=SOURCE_IDENTITY_VERSION,
    )
    state.set_metadata("identity_learning_bootstrap_version", "v2")
    hashes = [_record_source_hashes(item)[0] for item in records]
    state.record_outcome(
        hashes[0],
        "noncandidate",
        parser_version="v1",
        internal_date=records[0].internal_date,
    )
    state.record_outcome(
        hashes[1],
        "parse_failed",
        parser_version="v1",
        internal_date=records[1].internal_date,
    )
    state.record_outcome(
        hashes[2],
        "noncandidate",
        parser_version="v1",
        internal_date=records[2].internal_date,
    )
    state.record_outcome(
        hashes[3],
        "ignored",
        parser_version="v1",
        internal_date=records[3].internal_date,
    )
    parsed_uids: list[str] = []

    def fake_parse(record, dictionaries=None):
        parsed_uids.append(record.uid)
        return None

    monkeypatch.setattr(scanner, "parse_record", fake_parse)

    upgraded = scanner.scan_once(Settings(), days=40)
    assert parsed_uids == ["1", "2"]
    assert upgraded.skipped == 2
    assert state.metadata("parser_version") == "v2"

    manually_retried = scanner.scan_once(
        Settings(),
        days=40,
        force_reprocess=True,
    )
    assert parsed_uids == ["1", "2", "1", "2", "3"]
    assert manually_retried.skipped == 1
    assert state.outcome(hashes[3]).outcome == "ignored"


def test_parser_upgrade_fetch_failure_keeps_uid_replay_debt(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime.now(SHANGHAI)
    fingerprint = "1" * 24
    record = _located_record(
        "41",
        message_id="<upgrade-debt@example.invalid>",
        internal_date=now - timedelta(days=10),
        account_fingerprint=fingerprint,
        uidvalidity="61",
    )
    batches = [
        MailFetchBatch(
            records=(),
            searched_uids=(record.uid,),
            fetch_failed_uids=(record.uid,),
            parse_failed_uids=(),
            account_fingerprint=fingerprint,
            mailbox="INBOX",
            uidvalidity="61",
            uidnext="42",
        ),
        MailFetchBatch(
            records=(record,),
            searched_uids=(record.uid,),
            fetch_failed_uids=(),
            parse_failed_uids=(),
            account_fingerprint=fingerprint,
            mailbox="INBOX",
            uidvalidity="61",
            uidnext="42",
        ),
    ]
    requested_days: list[int] = []

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_batch_since(self, days):
            requested_days.append(days)
            return batches.pop(0)

    parsed_uids: list[str] = []

    def fake_parse(mail_record, dictionaries=None):
        parsed_uids.append(mail_record.uid)
        return None

    monkeypatch.setattr(scanner, "PARSER_VERSION", "v2")
    _configure_scan_test(
        monkeypatch,
        tmp_path,
        reader=FakeReader,
        parse_record=fake_parse,
    )
    state = StateStore(tmp_path / "state.db")
    scope = MailboxScope(fingerprint, "INBOX", "61")
    _commit_scan_baseline(state, parser_version="v1", scope=scope)
    state.set_metadata("identity_learning_bootstrap_version", "v2")
    source_hash, _ = _record_source_hashes(record)
    state.record_outcome(
        source_hash,
        "noncandidate",
        parser_version="v1",
        locator=record.locator(),
        internal_date=record.internal_date,
    )

    failed_fetch = scanner.scan_once(Settings(lookback_days=3))

    debt = state.outcome(source_hash)
    assert failed_fetch.fetch_failed == 1
    assert state.metadata("parser_version") == "v2"
    assert debt.outcome == "noncandidate"
    assert debt.parser_version == "v1"
    assert state.has_parser_replay_debt(
        "v2",
        replay_cutoff=now - timedelta(days=30),
    )

    scanner.scan_once(Settings(lookback_days=3))

    replayed = state.outcome(source_hash)
    assert requested_days == [INITIAL_LOOKBACK_DAYS, INITIAL_LOOKBACK_DAYS]
    assert parsed_uids == [record.uid]
    assert replayed.outcome == "noncandidate"
    assert replayed.parser_version == "v2"
    assert not state.has_parser_replay_debt(
        "v2",
        replay_cutoff=now - timedelta(days=30),
    )


def test_parser_upgrade_mime_failure_keeps_uid_replay_debt(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime.now(SHANGHAI)
    fingerprint = "6" * 24
    record = _located_record(
        "42",
        message_id="<upgrade-mime-debt@example.invalid>",
        internal_date=now - timedelta(days=10),
        account_fingerprint=fingerprint,
        uidvalidity="66",
    )
    batches = [
        MailFetchBatch(
            records=(),
            searched_uids=(record.uid,),
            fetch_failed_uids=(),
            parse_failed_uids=(record.uid,),
            account_fingerprint=fingerprint,
            mailbox="INBOX",
            uidvalidity="66",
            uidnext="43",
        ),
        MailFetchBatch(
            records=(record,),
            searched_uids=(record.uid,),
            fetch_failed_uids=(),
            parse_failed_uids=(),
            account_fingerprint=fingerprint,
            mailbox="INBOX",
            uidvalidity="66",
            uidnext="43",
        ),
    ]
    requested_days: list[int] = []

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_batch_since(self, days):
            requested_days.append(days)
            return batches.pop(0)

    parsed_uids: list[str] = []

    def fake_parse(mail_record, dictionaries=None):
        parsed_uids.append(mail_record.uid)
        return None

    monkeypatch.setattr(scanner, "PARSER_VERSION", "v2")
    _configure_scan_test(
        monkeypatch,
        tmp_path,
        reader=FakeReader,
        parse_record=fake_parse,
    )
    state = StateStore(tmp_path / "state.db")
    scope = MailboxScope(fingerprint, "INBOX", "66")
    _commit_scan_baseline(state, parser_version="v1", scope=scope)
    state.set_metadata("identity_learning_bootstrap_version", "v2")
    source_hash, _ = _record_source_hashes(record)
    state.record_outcome(
        source_hash,
        "noncandidate",
        parser_version="v1",
        locator=record.locator(),
        internal_date=record.internal_date,
    )

    scanner.scan_once(Settings(lookback_days=3))

    failed = state.outcome(source_hash)
    assert state.metadata("parser_version") == "v2"
    assert failed.outcome == "parse_failed"
    assert failed.parser_version is None

    scanner.scan_once(Settings(lookback_days=3))

    replayed = state.outcome(source_hash)
    assert requested_days == [INITIAL_LOOKBACK_DAYS, INITIAL_LOOKBACK_DAYS]
    assert parsed_uids == [record.uid]
    assert replayed.outcome == "noncandidate"
    assert replayed.parser_version == "v2"


def test_pending_review_replays_after_failed_upgrade_fetch(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime.now(SHANGHAI)
    fingerprint = "2" * 24
    record = _located_record(
        "51",
        message_id="<pending-upgrade-debt@example.invalid>",
        internal_date=now - timedelta(days=10),
        account_fingerprint=fingerprint,
        uidvalidity="62",
    )
    batches = [
        MailFetchBatch(
            records=(),
            searched_uids=(record.uid,),
            fetch_failed_uids=(record.uid,),
            parse_failed_uids=(),
            account_fingerprint=fingerprint,
            mailbox="INBOX",
            uidvalidity="62",
            uidnext="52",
        ),
        MailFetchBatch(
            records=(record,),
            searched_uids=(record.uid,),
            fetch_failed_uids=(),
            parse_failed_uids=(),
            account_fingerprint=fingerprint,
            mailbox="INBOX",
            uidvalidity="62",
            uidnext="52",
        ),
    ]
    requested_days: list[int] = []

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_batch_since(self, days):
            requested_days.append(days)
            return batches.pop(0)

    parse_calls = 0

    def fake_parse(mail_record, dictionaries=None):
        nonlocal parse_calls
        parse_calls += 1
        return _candidate_event(mail_record)

    monkeypatch.setattr(scanner, "PARSER_VERSION", "v2")
    _configure_scan_test(
        monkeypatch,
        tmp_path,
        reader=FakeReader,
        parse_record=fake_parse,
    )
    state = StateStore(tmp_path / "state.db")
    scope = MailboxScope(fingerprint, "INBOX", "62")
    _commit_scan_baseline(state, parser_version="v1", scope=scope)
    state.set_metadata("identity_learning_bootstrap_version", "v2")
    source_hash, _ = _record_source_hashes(record)
    unresolved = UnresolvedStore(tmp_path / "unresolved")
    unresolved.save(
        _pending_review(source_hash, record, parser_version="v1")
    )
    state.record_outcome(
        source_hash,
        "pending",
        parser_version="v1",
        locator=record.locator(),
        internal_date=record.internal_date,
    )

    scanner.scan_once(Settings(lookback_days=3))

    assert state.metadata("parser_version") == "v2"
    assert state.outcome(source_hash).parser_version == "v1"
    assert unresolved.load(source_hash).parser_version == "v1"

    scanner.scan_once(Settings(lookback_days=3))

    refreshed = unresolved.load(source_hash)
    assert requested_days == [INITIAL_LOOKBACK_DAYS, INITIAL_LOOKBACK_DAYS]
    assert parse_calls == 1
    assert refreshed is not None
    assert refreshed.revision == 2
    assert refreshed.parser_version == "v2"
    assert state.outcome(source_hash).outcome == "pending"
    assert state.outcome(source_hash).parser_version == "v2"


def test_asr_noncandidate_from_previous_parser_replays_into_review(
    tmp_path,
    monkeypatch,
) -> None:
    # Must stay inside PARSER_REPLAY_DAYS (30) but outside the 48h
    # "recent unknown UID" window; a fixed calendar date expires silently.
    received_at = datetime.now(SHANGHAI).replace(microsecond=0) - timedelta(
        days=7
    )
    records = []
    for offset, (uid, role) in enumerate(
        (
            ("1709047101", "数字中端实现工程师"),
            ("1709047102", "数字后端工程师"),
        )
    ):
        subject = f"张三，感谢你投递翱捷科技股份有限公司公司的{role}职位"
        event_at = received_at + timedelta(seconds=offset * 20)
        records.append(
            MailRecord(
                uid=uid,
                subject=subject,
                message_id=f"<asr-replay-{uid}@example.invalid>",
                sender="翱捷科技招聘 <noreply@example.invalid>",
                received_at=event_at,
                body="请登录系统查看详情。",
                mailbox="INBOX",
                uidvalidity="1",
                account_fingerprint="f" * 24,
                internal_date=event_at,
            )
        )

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_since(self, days):
            return records

    monkeypatch.setattr(scanner, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(scanner, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(scanner, "DASHBOARD_FILE", tmp_path / "dashboard.md")
    monkeypatch.setattr(scanner, "ImapReader", FakeReader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())

    state = StateStore(tmp_path / "state.db")
    scope = MailboxScope("f" * 24, "INBOX", "1")
    run_id = state.begin_scan()
    state.finish_scan(
        run_id,
        fetched=0,
        candidates=0,
        parser_version="2026.08.15.4",
        source_migrations=(scope,),
        source_identity_version=SOURCE_IDENTITY_VERSION,
    )
    state.set_metadata(
        "identity_learning_bootstrap_version",
        scanner.PARSER_VERSION,
    )
    source_hashes = []
    for record in records:
        source_hash = _record_source_hashes(record)[0]
        source_hashes.append(source_hash)
        state.record_outcome(
            source_hash,
            "noncandidate",
            parser_version="2026.08.15.4",
            locator=record.locator(),
            internal_date=record.internal_date,
        )

    summary = scanner.scan_once(Settings(), days=3)

    reviews = [
        UnresolvedStore(tmp_path / "unresolved").load(source_hash)
        for source_hash in source_hashes
    ]
    assert scanner.PARSER_VERSION == "2026.09.20.1"
    assert summary.skipped == 0
    assert all(review is not None for review in reviews)
    assert {review.company for review in reviews if review} == {
        "翱捷科技股份有限公司"
    }
    assert {review.role for review in reviews if review} == {
        "数字后端工程师",
        "数字中端实现工程师",
    }
    assert {review.status for review in reviews if review} == {"pending"}


def test_failed_scan_retries_crash_state_without_replaying_completed_parse(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime.now(SHANGHAI)
    fingerprint = "d" * 24
    record = MailRecord(
        uid="7",
        subject="普通通知",
        message_id="<crash@example.invalid>",
        sender="招聘系统 <noreply@example.invalid>",
        received_at=now,
        body="普通通知",
        mailbox="INBOX",
        uidvalidity="30",
        account_fingerprint=fingerprint,
        internal_date=now,
    )

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_since(self, days):
            return [record]

    monkeypatch.setattr(scanner, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(scanner, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(scanner, "DASHBOARD_FILE", tmp_path / "dashboard.md")
    monkeypatch.setattr(scanner, "ImapReader", FakeReader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "PARSER_VERSION", "v2")

    state = StateStore(tmp_path / "state.db")
    baseline = state.begin_scan()
    state.finish_scan(
        baseline,
        fetched=0,
        candidates=0,
        parser_version="v1",
    )
    state.set_metadata("identity_learning_bootstrap_version", "v2")
    parse_calls = 0

    def fake_parse(mail_record, dictionaries=None):
        nonlocal parse_calls
        parse_calls += 1
        return None

    export_calls = 0

    def flaky_export(*args, **kwargs):
        nonlocal export_calls
        export_calls += 1
        if export_calls == 1:
            raise RuntimeError("simulated crash")
        return 0

    monkeypatch.setattr(scanner, "parse_record", fake_parse)
    monkeypatch.setattr(scanner, "export_dashboard", flaky_export)

    with pytest.raises(RuntimeError, match="simulated crash"):
        scanner.scan_once(Settings(), days=3)

    scope = MailboxScope(fingerprint, "INBOX", "30")
    assert state.metadata("parser_version") == "v1"
    assert state.source_migration_needed(scope, SOURCE_IDENTITY_VERSION)
    source_hash, _ = _record_source_hashes(record)
    assert state.outcome(source_hash).outcome == "noncandidate"

    scanner.scan_once(Settings(), days=3)

    assert parse_calls == 1
    assert state.metadata("parser_version") == "v2"
    assert not state.source_migration_needed(scope, SOURCE_IDENTITY_VERSION)


def test_fetch_failures_are_retryable_and_report_safe_counts(
    tmp_path,
    monkeypatch,
) -> None:
    fingerprint = "e" * 24
    batch = MailFetchBatch(
        records=(),
        searched_uids=("9",),
        fetch_failed_uids=("9",),
        parse_failed_uids=(),
        account_fingerprint=fingerprint,
        mailbox="INBOX",
        uidvalidity="40",
        uidnext="10",
    )

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_batch_since(self, days):
            return batch

    monkeypatch.setattr(scanner, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(scanner, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(scanner, "DASHBOARD_FILE", tmp_path / "dashboard.md")
    monkeypatch.setattr(scanner, "ImapReader", FakeReader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())

    summary = scanner.scan_once(Settings(), days=3)

    locator = {
        "account_fingerprint": fingerprint,
        "mailbox": "INBOX",
        "uidvalidity": "40",
        "uid": "9",
    }
    source_hash = _source_hash_from_locator(locator)
    state = StateStore(tmp_path / "state.db")
    outcome = state.outcome(source_hash)
    assert outcome.outcome == "fetch_failed"
    assert outcome.retry_eligible
    assert summary.searched == 1
    assert summary.fetch_failed == 1
    assert summary.uidvalidity == "40"
    assert summary.uidnext == "10"
    assert state.health()["fetch_failures"] == 1


def test_old_pending_hash_is_replayed_in_place_on_parser_upgrade(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime.now(SHANGHAI)
    fingerprint = "f" * 24
    record = MailRecord(
        uid="12",
        subject="面试通知",
        message_id="<legacy-pending@example.invalid>",
        sender="招聘系统 <noreply@example.invalid>",
        received_at=now,
        body="请参加面试",
        mailbox="INBOX",
        uidvalidity="50",
        account_fingerprint=fingerprint,
        internal_date=now,
    )
    old_hash = "1" * 32
    unresolved = UnresolvedStore(tmp_path / "unresolved")
    unresolved.save(
        UnresolvedRecord(
            id=old_hash,
            status="pending",
            resolution_status="unresolved",
            reason="legacy-source-hash",
            company="样例公司",
            role="工程师",
            recruiting_project=None,
            event_type="interview",
            stage="面试",
            round=None,
            received_at=now,
            start_at=None,
            end_at=None,
            deadline_at=None,
            action_summary="参加面试",
            title="面试通知",
            requirements=(),
            confidence=0.8,
            change_type="new",
            candidate_application_keys=(),
            resolved_application_key=None,
            resolved_task_id=None,
            rule_version="identity-registry-v1",
            mail_locator=record.locator(),
            parser_version="v1",
            semantic_hash="legacy-semantic",
        )
    )

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_since(self, days):
            return [record]

    parse_calls = 0

    def fake_parse(mail_record, dictionaries=None):
        nonlocal parse_calls
        parse_calls += 1
        return ParsedEvent(
            company="样例公司",
            role="工程师",
            recruiting_project=None,
            event_type="interview",
            stage="面试",
            round=None,
            title=mail_record.subject,
            start_at=now + timedelta(days=1),
            end_at=None,
            deadline_at=None,
            source_message_id=mail_record.message_id,
            source_received_at=mail_record.received_at,
            source_sender=mail_record.sender,
            source_url=None,
            action_summary="参加面试",
            requirements=(),
            matched_keywords=(),
            confidence=0.9,
            change_type="new",
            company_confidence=0.97,
            company_source="test-explicit-company",
            role_confidence=0.97,
            role_source="test-explicit-role",
        )

    monkeypatch.setattr(scanner, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(scanner, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(scanner, "DASHBOARD_FILE", tmp_path / "dashboard.md")
    monkeypatch.setattr(scanner, "ImapReader", FakeReader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "parse_record", fake_parse)

    summary = scanner.scan_once(Settings(), days=3)

    canonical_hash, _ = _record_source_hashes(record)
    refreshed = unresolved.load(old_hash)
    assert summary.candidates == 1
    assert parse_calls == 1
    assert refreshed is not None
    assert refreshed.revision == 2
    assert refreshed.parser_version == scanner.PARSER_VERSION
    assert unresolved.load(canonical_hash) is None
    assert StateStore(tmp_path / "state.db").outcome(
        canonical_hash
    ).outcome == "pending"


def test_force_reprocess_refreshes_review_without_materializing_facts(tmp_path) -> None:
    received = datetime(2026, 7, 27, 20, 0, tzinfo=SHANGHAI)
    unresolved = UnresolvedStore(tmp_path / "unresolved")
    common = {
        "status": "pending",
        "resolution_status": "unresolved",
        "reason": "insufficient-identity-evidence",
        "company": "联发科技",
        "role": None,
        "recruiting_project": None,
        "round": None,
        "start_at": None,
        "end_at": None,
        "deadline_at": None,
        "requirements": (),
        "confidence": 0.8,
        "change_type": "new",
        "candidate_application_keys": (),
        "resolved_application_key": None,
        "resolved_task_id": None,
        "rule_version": "identity-registry-v1",
    }
    unresolved.save(
        UnresolvedRecord(
            id="1" * 32,
            event_type="application",
            stage="网申",
            received_at=received,
            title="【联发科技】感谢您投递本公司职位",
            action_summary=(
                "您已成功投递 数字IC工程师(设计/验证/整合方向) 职位。"
            ),
            **common,
        )
    )
    unresolved.save(
        UnresolvedRecord(
            id="2" * 32,
            event_type="assessment",
            stage="在线笔试",
            received_at=received + timedelta(days=3),
            title="【联发科技】邀你参加数字IC工程师(设计/验证/整合方向)岗位的考试",
            action_summary="感谢关注，现邀请你参加该职位的考试。",
            **common,
        )
    )
    registry = ApplicationRegistry(tmp_path / "applications")
    store = MarkdownTaskStore(tmp_path / "tasks")

    refreshed = scanner._reprocess_pending_unresolved(
        registry,
        store,
        unresolved,
        load_identity_dictionaries(),
    )

    assert refreshed == 2
    assert registry.all() == []
    assert store.all() == []
    assert all(item.status == "pending" for item in unresolved.all())
    assert all(item.revision == 2 for item in unresolved.all())


def test_identity_preview_batches_resolution_without_writing_state(
    tmp_path,
    monkeypatch,
) -> None:
    received = datetime(2026, 8, 4, 9, 0, tzinfo=SHANGHAI)
    records = [
        MailRecord(
            uid="receipt",
            subject="已收到申请",
            message_id="<receipt@example.invalid>",
            sender="campus@example.invalid",
            received_at=received,
            body="已收到申请",
        ),
        MailRecord(
            uid="jds",
            subject="JDS在线测评",
            message_id="<jds@example.invalid>",
            sender="campus@example.invalid",
            received_at=received + timedelta(minutes=8),
            body="JDS在线测评",
        ),
        MailRecord(
            uid="late-receipt",
            subject="已收到申请",
            message_id="<late@example.invalid>",
            sender="campus@example.invalid",
            received_at=received + timedelta(hours=6),
            body="已收到申请",
        ),
        MailRecord(
            uid="newsletter",
            subject="普通产品周报",
            message_id="<newsletter@example.invalid>",
            sender="news@example.invalid",
            received_at=received + timedelta(hours=7),
            body="本周产品更新。",
        ),
    ]

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_batch_since(self, days):
            return MailFetchBatch(
                records=tuple(records),
                searched_uids=tuple(record.uid for record in records),
                fetch_failed_uids=(),
                parse_failed_uids=(),
                account_fingerprint="f" * 24,
                mailbox="INBOX",
                uidvalidity="1",
                uidnext="5",
            )

    def fake_parse(record, dictionaries=None):
        if record.uid == "newsletter":
            return None
        explicit = record.uid == "jds"
        return ParsedEvent(
            company="京东",
            role="产品经理" if explicit else None,
            recruiting_project="JDS" if explicit else None,
            event_type="assessment" if explicit else "application",
            stage="在线测评" if explicit else "网申",
            round=None,
            title=record.subject,
            start_at=None,
            end_at=None,
            deadline_at=None,
            source_message_id=record.message_id,
            source_received_at=record.received_at,
            source_sender=record.sender,
            source_url=None,
            action_summary="等待后续",
            requirements=(),
            matched_keywords=(),
            confidence=0.9,
            change_type="new",
            company_confidence=0.97,
            company_source="test-explicit-company",
            role_confidence=0.97 if explicit else 0.0,
            role_source="test-explicit-role" if explicit else None,
        )

    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        """# 台账

### 已投递或已进入流程
- [x] 京东｜2027 JDS 产品经理｜已投递｜等待后续
- [x] 京东｜2027 TET 综合方向｜已投递｜等待后续
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(scanner, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(scanner, "APPLICATIONS_DIR", tmp_path / "applications")
    monkeypatch.setattr(scanner, "DICTIONARIES_DIR", tmp_path / "dictionaries")
    monkeypatch.setattr(scanner, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(scanner, "ImapReader", FakeReader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "parse_record", fake_parse)

    summary = scanner.scan_once(
        Settings(
            progress_source=ledger,
            obsidian_enabled=True,
            obsidian_output=tmp_path / "obsidian.md",
        ),
        days=3,
        identity_preview=True,
    )
    assert summary.identity_mode == "preview"
    assert summary.identity_matched == 0
    assert summary.identity_new_applications == 1
    assert summary.identity_unresolved == 2
    assert summary.tasks_updated == 0
    assert summary.preview[0]["identity_action"] == "unresolved"
    assert summary.preview[1]["identity_action"] == "new_application"
    assert summary.preview[2]["identity_action"] == "unresolved"
    assert summary.preview[3]["identity_action"] == "noncandidate"
    assert summary.preview[3]["subject"] == "普通产品周报"
    assert summary.preview[3]["parser_status"] == "noncandidate"
    assert not (tmp_path / "state.db").exists()
    assert not (tmp_path / "tasks").exists()


def test_registry_scan_groups_batch_receipt_and_persists_unresolved(
    tmp_path,
    monkeypatch,
) -> None:
    received = datetime(2026, 8, 4, 9, 0, tzinfo=SHANGHAI)
    records = [
        MailRecord(
            uid="receipt",
            subject="【京东校招】我们已收到你的申请，请及时关注后续进展",
            message_id="<receipt@example.invalid>",
            sender="campus@example.invalid",
            received_at=received,
            body="感谢你对京东校招的关注，我们已收到申请",
        ),
        MailRecord(
            uid="jds",
            subject="JDS 在线测评",
            message_id="<jds@example.invalid>",
            sender="campus@example.invalid",
            received_at=received + timedelta(minutes=8),
            body="产品经理 JDS 在线测评",
        ),
        MailRecord(
            uid="unknown",
            subject="网申成功提交",
            message_id="<unknown@example.invalid>",
            sender="campus@example.invalid",
            received_at=received + timedelta(hours=6),
            body="网申成功提交",
        ),
    ]

    class FakeReader:
        def __init__(self, settings, credential, runtime_control=None):
            pass

        def fetch_since(self, days):
            return records

    def fake_parse(record, dictionaries=None):
        if record.uid == "jds":
            role = "产品经理"
            project = "JDS"
            event_type = "assessment"
            stage = "在线测评"
        elif record.uid == "receipt":
            role = project = None
            event_type = "application"
            stage = "网申"
        else:
            role = project = None
            event_type = "application"
            stage = "网申"
        return ParsedEvent(
            company="京东" if record.uid != "unknown" else "样例公司",
            role=role,
            recruiting_project=project,
            event_type=event_type,
            stage=stage,
            round=None,
            title=record.subject,
            start_at=None,
            end_at=None,
            deadline_at=None,
            source_message_id=record.message_id,
            source_received_at=record.received_at,
            source_sender=record.sender,
            source_url=None,
            action_summary="等待后续",
            requirements=(),
            matched_keywords=(),
            confidence=0.9,
            change_type="new",
            company_confidence=0.97,
            company_source="test-explicit-company",
            role_confidence=0.97 if role else 0.0,
            role_source="test-explicit-role" if role else None,
        )

    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        """# 台账

### 已投递或已进入流程
- [x] 京东｜2027 JDS 产品经理｜已投递｜等待后续
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(scanner, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(scanner, "APPLICATIONS_DIR", tmp_path / "applications")
    monkeypatch.setattr(scanner, "DICTIONARIES_DIR", tmp_path / "dictionaries")
    monkeypatch.setattr(scanner, "UNRESOLVED_DIR", tmp_path / "unresolved")
    monkeypatch.setattr(scanner, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(scanner, "DASHBOARD_FILE", tmp_path / "dashboard.md")
    monkeypatch.setattr(scanner, "ImapReader", FakeReader)
    monkeypatch.setattr(scanner, "load_credential", lambda: object())
    monkeypatch.setattr(scanner, "parse_record", fake_parse)

    summary = scanner.scan_once(Settings(progress_source=ledger), days=3)
    tasks = MarkdownTaskStore(tmp_path / "tasks").all()
    unresolved = UnresolvedStore(tmp_path / "unresolved").all()

    assert summary.identity_mode == "registry"
    assert summary.identity_matched == 0
    assert summary.identity_new_applications == 1
    assert summary.identity_unresolved == 2
    assert tasks == []
    assert len(unresolved) == 3
    matched = [record for record in unresolved if record.company == "京东"]
    assert len(matched) == 2
    assert all(record.recommended_application_key is None for record in matched)
    assert any(record.company == "样例公司" for record in unresolved)
    unread = ActivityStore(tmp_path / "activity-state.json").unread_payload()
    assert unread["counts"]["review"] == 3
    assert unread["counts"]["today"] == 3
    assert unread["unique_unread_count"] == 3

    repeated = scanner.scan_once(Settings(progress_source=ledger), days=3)
    assert repeated.skipped == 3
    assert MarkdownTaskStore(tmp_path / "tasks").all() == []
