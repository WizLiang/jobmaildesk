from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from job_mail_desk import scanner
from job_mail_desk.config import Settings
from job_mail_desk.credentials import MailCredential
from job_mail_desk.mail_reader import ImapReader, MailFetchBatch, account_fingerprint
from job_mail_desk.models import MailRecord
from job_mail_desk.runtime_control import RuntimeControl, RuntimeStopping
from job_mail_desk.state import StateStore
from job_mail_desk.unresolved_store import UnresolvedStore


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 9, 20, 12, tzinfo=scanner.SHANGHAI)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    control = {"email": "synthetic@example.invalid", "error": None,
               "fetch_failed": (), "parse_failed": (), "windows": [], "records": ()}

    class Reader:
        def __init__(self, settings, credential, **kwargs):
            self.settings, self.credential = settings, credential

        def fetch_batch_since(self, days):
            control["windows"].append((self.credential.email, self.settings.mail_folder, days))
            if control["error"]:
                raise control["error"]
            failed = control["fetch_failed"] + control["parse_failed"]
            records = tuple(record for record in control["records"]
                            if record.internal_date >= self.scan_started_at - timedelta(days=days))
            return MailFetchBatch(
                records=records, searched_uids=failed + tuple(r.uid for r in records),
                fetch_failed_uids=control["fetch_failed"],
                parse_failed_uids=control["parse_failed"],
                account_fingerprint=account_fingerprint(self.settings.mail_host, self.credential.email),
                mailbox=self.settings.mail_folder, uidvalidity="1", uidnext="10",
            )

    for name, relative in {"TASKS_DIR": "tasks", "STATE_DB": "state.db",
                           "DASHBOARD_FILE": "dashboard.md", "DICTIONARIES_DIR": "dict"}.items():
        monkeypatch.setattr(scanner, name, tmp_path / relative)
    monkeypatch.setattr(scanner, "datetime", Clock)
    monkeypatch.setattr(scanner, "ImapReader", Reader)
    monkeypatch.setattr(scanner, "ensure_directories", lambda: None)
    monkeypatch.setattr(scanner, "load_credential", lambda: MailCredential(control["email"], "synthetic-code"))
    monkeypatch.setattr(scanner, "_learn_from_confirmed_records", lambda *a, **kw: {})
    settings = Settings(progress_enabled=False, lookback_days=3)
    return control, Clock, settings, StateStore(tmp_path / "state.db")


def test_scope_first_scan_ignores_legacy_global_success_and_switches_accounts(mailbox):
    control, clock, settings, state = mailbox
    run = state.begin_scan()
    state.finish_scan(run, fetched=0, candidates=0,
                      parser_version=scanner.parser_version_for_settings(settings))
    assert scanner.scan_once(settings).lookback_days == 30
    clock.current += timedelta(hours=1)
    assert scanner.scan_once(settings).lookback_days == 3
    control["email"] = "another@example.invalid"
    assert scanner.scan_once(settings).lookback_days == 30
    assert scanner.scan_once(replace(settings, mail_folder="Recruitment")).lookback_days == 30
    control["email"] = "synthetic@example.invalid"
    assert scanner.scan_once(settings).lookback_days == 3


@pytest.mark.parametrize("changes", [
    {"mail_host": "other.example.invalid"}, {"mail_port": 1993},
    {"mail_ssl": False}, {"mail_folder": "Recruitment"},
])
def test_scan_scope_distinguishes_server_and_folder(changes):
    settings = Settings()
    original = scanner._scan_scope_key(settings, "fictional@example.invalid")
    assert scanner._scan_scope_key(replace(settings, **changes), "fictional@example.invalid") != original
    assert "fictional" not in original


def test_scan_scope_normalizes_inbox_and_email_case():
    assert scanner._scan_scope_key(Settings(), "Fictional@example.invalid") == scanner._scan_scope_key(
        Settings(mail_folder="inbox"), "fictional@example.invalid")


@pytest.mark.parametrize("idle_days", [7, 400])
def test_scan_catches_up_complete_offline_gap_without_year_cap(mailbox, idle_days):
    control, clock, settings, state = mailbox
    scanner.scan_once(settings)
    clock.current += timedelta(days=idle_days)
    assert scanner.scan_once(settings).lookback_days == idle_days + 1
    key = scanner._scan_scope_key(settings, control["email"])
    assert state.scan_coverage(key).covered_until == clock.current


@pytest.mark.parametrize("failure", ["fetch", "parse", "network", "cancel"])
def test_failed_initial_attempt_keeps_original_start_after_restart(mailbox, failure):
    control, clock, settings, state = mailbox
    start = clock.current
    if failure == "fetch":
        control["fetch_failed"] = ("1",)
    elif failure == "parse":
        control["parse_failed"] = ("1",)
    else:
        control["error"] = RuntimeStopping() if failure == "cancel" else OSError("synthetic failure")
    if control["error"]:
        with pytest.raises(type(control["error"])):
            scanner.scan_once(settings)
    else:
        scanner.scan_once(settings)
    key = scanner._scan_scope_key(settings, control["email"])
    restarted = StateStore(state.path)
    before = restarted.scan_coverage(key)
    assert before.covered_until is None
    assert before.required_since == start - timedelta(days=30)
    control.update(error=None, fetch_failed=(), parse_failed=())
    clock.current += timedelta(days=5)
    assert scanner.scan_once(settings).lookback_days == 35
    assert restarted.scan_coverage(key).covered_until == clock.current


@pytest.mark.parametrize("failure", ["fetch", "parse", "network", "cancel"])
def test_incomplete_scan_does_not_advance_existing_coverage(mailbox, failure):
    control, clock, settings, state = mailbox
    scanner.scan_once(settings)
    key = scanner._scan_scope_key(settings, control["email"])
    completed = state.scan_coverage(key).covered_until
    clock.current += timedelta(days=5)
    if failure == "fetch":
        control["fetch_failed"] = ("1",)
    elif failure == "parse":
        control["parse_failed"] = ("1",)
    else:
        control["error"] = RuntimeStopping() if failure == "cancel" else OSError("synthetic failure")
    if control["error"]:
        with pytest.raises(type(control["error"])):
            scanner.scan_once(settings)
    else:
        scanner.scan_once(settings)
    assert state.scan_coverage(key).covered_until == completed
    control.update(error=None, fetch_failed=(), parse_failed=())
    clock.current += timedelta(days=2)
    assert scanner.scan_once(settings).lookback_days == 8


@pytest.mark.parametrize("initial", [True, False])
def test_narrow_history_scan_cannot_cover_an_older_gap(mailbox, initial):
    control, clock, settings, state = mailbox
    if not initial:
        scanner.scan_once(settings)
        clock.current += timedelta(days=10)
    key = scanner._scan_scope_key(settings, control["email"])
    before = state.scan_coverage(key)
    scanner.scan_once(settings, days=7, force_reprocess=True, recheck_pending=True)
    assert state.scan_coverage(key).covered_until == (before.covered_until if before else None)
    # A pending attempt uses the exact anchored gap; the normal extra overlap
    # day must not keep expanding the retry window after every narrow rescan.
    assert scanner.scan_once(settings).lookback_days == (30 if initial else 10)
    assert state.scan_coverage(key).pending_since is None


def test_wide_history_scan_can_cover_existing_gap(mailbox):
    control, clock, settings, state = mailbox
    scanner.scan_once(settings)
    clock.current += timedelta(days=10)
    scanner.scan_once(settings, days=30, force_reprocess=True, recheck_pending=True)
    key = scanner._scan_scope_key(settings, control["email"])
    assert state.scan_coverage(key).covered_until == clock.current
    assert scanner.scan_once(settings).lookback_days == 3


def test_shadow_scan_does_not_create_or_advance_coverage(mailbox):
    control, clock, settings, state = mailbox
    scanner.scan_once(settings, shadow=True)
    key = scanner._scan_scope_key(settings, control["email"])
    assert state.scan_coverage(key) is None


def test_cancellation_during_export_does_not_advance_coverage(mailbox, monkeypatch):
    control, clock, settings, state = mailbox
    runtime = RuntimeControl()

    def cancel_export(*args, **kwargs):
        runtime.begin_stop()
        return 0

    monkeypatch.setattr(scanner, "export_dashboard", cancel_export)
    with pytest.raises(RuntimeStopping):
        scanner.scan_once(settings, runtime_control=runtime)
    assert state.scan_coverage(scanner._scan_scope_key(settings, control["email"])).covered_until is None


@pytest.mark.parametrize("legacy_version,manual_days", [
    ("2026.09.20.1", None), ("current", 30),
])
def test_legacy_filtered_index_without_card_is_rebuilt_within_replay(mailbox, legacy_version, manual_days):
    control, clock, settings, state = mailbox
    received = clock.current - timedelta(days=7)
    record = MailRecord(
        uid="9", subject="示例科技校园招聘宣讲会", sender="招聘服务 <test@example.invalid>",
        message_id="<filtered@example.invalid>", received_at=received, internal_date=received,
        body="欢迎投递简历，宣讲会时间9月22日14:00，地点示例大学教学楼301。",
        mailbox="INBOX", uidvalidity="1",
        account_fingerprint=account_fingerprint(settings.mail_host, control["email"]),
    )
    control["records"] = (record,)
    source_hash = scanner._record_source_hashes(record)[0]
    version = scanner.parser_version_for_settings(settings) if legacy_version == "current" else legacy_version
    state.record_outcome(source_hash, "filtered", parser_version=version,
                         locator=record.locator(), internal_date=received)
    run = state.begin_scan()
    state.finish_scan(run, fetched=1, candidates=0, parser_version=version)
    kwargs = {"days": manual_days, "force_reprocess": True, "recheck_pending": True} if manual_days else {}
    assert scanner.scan_once(settings, **kwargs).filtered == 1
    review = UnresolvedStore(state.path.parent / "unresolved").load(source_hash)
    assert review and review.status == "filtered"
    assert review.parser_version == scanner.parser_version_for_settings(settings)
    assert not list((state.path.parent / "applications").glob("*.md"))


def test_latest_failures_survive_restart(mailbox):
    control, clock, settings, state = mailbox
    control.update(fetch_failed=("1", "2"), parse_failed=("3",))
    scanner.scan_once(settings)
    health = StateStore(state.path).health()
    assert health["scan_details"]["fetch_failed"] == health["fetch_failures"] == 2
    assert health["scan_details"]["parse_failed"] == health["parse_failures"] == 1
    control.update(fetch_failed=(), parse_failed=())
    scanner.scan_once(settings)
    assert StateStore(state.path).health()["parse_failures"] == 0


def test_invalid_coverage_does_not_silently_reset(tmp_path):
    state = StateStore(tmp_path / "state.db")
    key = "a" * 64
    with closing(state._connect()) as connection, connection:
        connection.execute("INSERT INTO scan_coverage (scope_key, required_since) VALUES (?, ?)", (key, "invalid"))
    with pytest.raises(ValueError, match="覆盖记录无效"):
        state.prepare_scan_coverage(key, datetime.now(scanner.SHANGHAI))


@pytest.mark.parametrize("terminal", ["ignored", "resolved", "tombstoned"])
def test_filtered_restore_rejects_any_manual_terminal_without_partial_update(tmp_path, terminal):
    state = StateStore(tmp_path / "state.db")
    state.record_outcome("filtered", "filtered")
    state.record_outcome("terminal", terminal)
    with pytest.raises(ValueError):
        state.restore_filtered_review({"filtered", "terminal"})
    assert state.outcome("filtered").outcome == "filtered"
    assert state.outcome("terminal").outcome == terminal


def test_filtered_restore_is_retryable_and_never_changes_ignored(tmp_path):
    state = StateStore(tmp_path / "state.db")
    state.record_outcome("filtered", "filtered")
    state.restore_filtered_review({"filtered"})
    state.restore_filtered_review({"filtered"})
    assert state.outcome("filtered").outcome == "pending"


def test_reader_allows_internal_catchup_over_a_year_without_connecting(monkeypatch):
    reader = ImapReader(Settings(), MailCredential("fictional@example.invalid", "synthetic-code"))

    class ReachedConnection(Exception):
        pass

    def stop_before_connect():
        raise ReachedConnection

    monkeypatch.setattr(reader, "_connected_client", stop_before_connect)
    with pytest.raises(ReachedConnection):
        reader.fetch_batch_since(400)
    for invalid in (0, -1, True, 10**10):
        with pytest.raises(ValueError):
            reader.fetch_batch_since(invalid)


def test_reader_search_uses_the_persisted_scan_anchor(monkeypatch):
    reader = ImapReader(Settings(), MailCredential("fictional@example.invalid", "synthetic-code"))
    reader.scan_started_at = datetime(2026, 9, 20, 23, 59, tzinfo=scanner.SHANGHAI)
    searches = []

    class Client:
        def login(self, *args): return "OK", []
        def select(self, *args, **kwargs): return "OK", []
        def response(self, name): return name, [b"1"]
        def uid(self, *args):
            searches.append(args)
            return "OK", [b""]

    @contextmanager
    def connected():
        yield Client()

    monkeypatch.setattr(reader, "_connected_client", connected)
    reader.fetch_batch_since(30)
    assert searches == [("search", None, "SINCE", "21-Aug-2026")]


def test_real_parse_failure_retries_same_rules_until_success(mailbox, monkeypatch):
    control, clock, settings, state = mailbox
    received = clock.current - timedelta(days=2)
    record = MailRecord(
        uid="12", subject="合成通知", sender="test@example.invalid", body="合成内容",
        message_id="<retry@example.invalid>", received_at=received, internal_date=received,
        mailbox="INBOX", uidvalidity="1",
        account_fingerprint=account_fingerprint(settings.mail_host, control["email"]),
    )
    control["records"] = (record,)
    calls = []

    def flaky_parse(mail, dictionaries=None):
        calls.append(mail.uid)
        if len(calls) <= 2:
            raise ValueError("synthetic parse failure")
        return None

    monkeypatch.setattr(scanner, "parse_record", flaky_parse)
    key = scanner._scan_scope_key(settings, control["email"])
    for _ in range(2):
        assert scanner.scan_once(settings).parse_failed == 1
        assert state.scan_coverage(key).covered_until is None
        assert state.health()["parse_failures"] == 1
        clock.current += timedelta(hours=1)
    assert scanner.scan_once(settings).parse_failed == 0
    assert calls == ["12", "12", "12"]
    assert state.scan_coverage(key).covered_until == clock.current
    assert state.scan_coverage(key).pending_since is None
    assert scanner.scan_once(settings).lookback_days == 3


@pytest.mark.parametrize("existing_pending", [False, True])
def test_failed_history_scan_keeps_older_work_for_automatic_retry(mailbox, monkeypatch, existing_pending):
    control, clock, settings, state = mailbox
    scanner.scan_once(settings)
    key = scanner._scan_scope_key(settings, control["email"])
    original_watermark = state.scan_coverage(key).covered_until
    received = clock.current - timedelta(days=80)
    record = MailRecord(
        uid="80", subject="示例科技招聘通知", sender="test@example.invalid",
        body="应聘岗位：软件工程师。", message_id="<old-retry@example.invalid>",
        received_at=received, internal_date=received, mailbox="INBOX", uidvalidity="1",
        account_fingerprint=account_fingerprint(settings.mail_host, control["email"]),
    )
    control["records"] = (record,)
    if existing_pending:
        from job_mail_desk.identity_dictionaries import load_identity_dictionaries
        from job_mail_desk.identity_pipeline import resolve_event_batch
        from job_mail_desk.unresolved_store import unresolved_from_decision
        event = scanner.parse_record(record)
        assert event
        decision = resolve_event_batch([event], [], load_identity_dictionaries())[0]
        source_hash = scanner._record_source_hashes(record)[0]
        UnresolvedStore(state.path.parent / "unresolved").save(unresolved_from_decision(
            source_hash, decision, record.locator(), parser_version=scanner.parser_version_for_settings(settings),
        ))
        state.record_outcome(source_hash, "pending", parser_version=scanner.parser_version_for_settings(settings),
                             locator=record.locator(), internal_date=received)
    real_parse = scanner.parse_record
    calls = []

    def flaky_parse(mail, dictionaries=None):
        calls.append(mail.uid)
        if len(calls) <= 2:
            raise ValueError("synthetic history failure")
        return real_parse(mail, dictionaries)

    monkeypatch.setattr(scanner, "parse_record", flaky_parse)
    history = scanner.scan_once(settings, days=90, force_reprocess=True, recheck_pending=True)
    assert history.parse_failed == 1
    assert state.scan_coverage(key).covered_until == original_watermark
    retry_start = state.scan_coverage(key).pending_since
    assert retry_start == clock.current - timedelta(days=90)
    clock.current += timedelta(hours=1)
    retry = scanner.scan_once(settings)
    assert retry.lookback_days == 91 and retry.parse_failed == 1
    assert state.scan_coverage(key).pending_since == retry_start
    clock.current += timedelta(hours=1)
    recovered = scanner.scan_once(settings)
    assert recovered.lookback_days == 91 and recovered.parse_failed == 0
    assert calls == ["80", "80", "80"]
    assert state.scan_coverage(key).pending_since is None
    assert state.scan_coverage(key).covered_until == clock.current
    assert scanner.scan_once(settings).lookback_days == 3


def test_crashed_wide_attempt_survives_restart_without_rewinding_forever(mailbox):
    control, clock, settings, state = mailbox
    scanner.scan_once(settings)
    key = scanner._scan_scope_key(settings, control["email"])
    state.begin_coverage_attempt(key, clock.current - timedelta(days=90))
    restarted = StateStore(state.path)
    assert restarted.scan_coverage(key).pending_since is not None
    assert scanner.scan_once(settings).lookback_days == 90
    assert restarted.scan_coverage(key).pending_since is None
    assert scanner.scan_once(settings).lookback_days == 3
