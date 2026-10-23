from datetime import datetime, timedelta
import sqlite3

import pytest

from job_mail_desk.parser import SHANGHAI
from job_mail_desk.state import MailboxScope, StateStore


def test_duplicate_message_hash_is_processed_once(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    assert not state.is_processed("abc")
    state.mark_processed("abc", "task-1")
    state.mark_processed("abc", "task-2")
    assert state.is_processed("abc")


def test_parser_version_change_replays_once(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.mark_processed("abc", "task-1")
    assert state.prepare_parser_version("v1") is True
    assert state.metadata("parser_version") is None
    assert state.is_processed("abc")

    failed = state.begin_scan()
    state.finish_scan(
        failed,
        fetched=1,
        candidates=0,
        parser_version="v1",
        error="RuntimeError",
    )
    assert state.metadata("parser_version") is None
    assert state.prepare_parser_version("v1") is True

    completed = state.begin_scan()
    state.finish_scan(
        completed,
        fetched=1,
        candidates=1,
        parser_version="v1",
    )
    assert state.metadata("parser_version") == "v1"
    assert state.prepare_parser_version("v1") is False
    assert state.prepare_parser_version("v2") is True


def test_successful_scan_state_ignores_failed_runs(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    assert not state.has_successful_scan()
    failed = state.begin_scan()
    state.finish_scan(failed, fetched=1, candidates=0, error="blocked")
    assert not state.has_successful_scan()
    completed = state.begin_scan()
    state.finish_scan(completed, fetched=1, candidates=1)
    assert state.has_successful_scan()


@pytest.mark.parametrize(
    ("outcome", "retry_eligible"),
    [
        ("seen", True),
        ("fetch_failed", True),
        ("parse_failed", True),
        ("noncandidate", True),
        ("pending", False),
        ("resolved", False),
        ("ignored", False),
        ("tombstoned", False),
    ],
)
def test_outcomes_keep_parser_and_retry_policy(
    tmp_path,
    outcome,
    retry_eligible,
) -> None:
    state = StateStore(tmp_path / "state.db")
    internal_date = datetime(2026, 8, 15, 8, 0, tzinfo=SHANGHAI)

    saved = state.record_outcome(
        "source",
        outcome,
        parser_version="parser-v1",
        internal_date=internal_date,
    )

    assert saved.outcome == outcome
    assert saved.parser_version == "parser-v1"
    assert saved.retry_eligible is retry_eligible
    assert saved.internal_date == internal_date


def test_retry_policy_is_bounded_and_never_replays_facts(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    now = datetime(2026, 8, 15, 12, 0, tzinfo=SHANGHAI)
    recent = now - timedelta(hours=1)
    old = now - timedelta(days=31)

    state.record_outcome(
        "recent",
        "noncandidate",
        parser_version="v1",
        internal_date=recent,
    )
    state.record_outcome(
        "old",
        "parse_failed",
        parser_version="v1",
        internal_date=old,
    )
    state.record_outcome("pending", "pending", parser_version="v1")
    state.record_outcome("ignored", "ignored", parser_version="v1")
    state.record_outcome("mime-failure", "parse_failed")

    assert state.should_process(
        "recent",
        parser_version="v2",
        parser_changed=True,
        internal_date=recent,
        replay_cutoff=now - timedelta(days=30),
    )
    # Failed work is retryable whenever the scanner includes it in a window;
    # the rule-upgrade cutoff only limits successful non-candidate replays.
    assert state.should_process(
        "old",
        parser_version="v2",
        parser_changed=True,
        internal_date=old,
        replay_cutoff=now - timedelta(days=30),
    )
    assert state.should_process(
        "old",
        parser_version="v2",
        parser_changed=False,
        manual_retry=True,
    )
    assert state.should_process(
        "mime-failure",
        parser_version="v1",
        parser_changed=False,
    )
    for source_hash in ("pending", "ignored"):
        assert not state.should_process(
            source_hash,
            parser_version="v2",
            parser_changed=True,
            manual_retry=True,
            internal_date=recent,
            replay_cutoff=now - timedelta(days=30),
        )


def test_parser_replay_uses_per_message_version_after_global_commit(
    tmp_path,
) -> None:
    state = StateStore(tmp_path / "state.db")
    now = datetime(2026, 8, 15, 12, 0, tzinfo=SHANGHAI)
    received = now - timedelta(days=10)
    cutoff = now - timedelta(days=30)
    state.record_outcome(
        "source",
        "noncandidate",
        parser_version="v1",
        internal_date=received,
    )

    run_id = state.begin_scan()
    state.finish_scan(
        run_id,
        fetched=0,
        candidates=0,
        parser_version="v2",
    )

    assert not state.parser_version_changed("v2")
    assert state.has_parser_replay_debt("v2", replay_cutoff=cutoff)
    assert state.should_process(
        "source",
        parser_version="v2",
        parser_changed=False,
        internal_date=received,
        replay_cutoff=cutoff,
    )

    state.record_outcome(
        "source",
        "noncandidate",
        parser_version="v2",
        internal_date=received,
    )
    assert not state.has_parser_replay_debt("v2", replay_cutoff=cutoff)


def test_fact_outcomes_cannot_be_downgraded_by_replay(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_outcome("source", "pending", parser_version="v1")
    state.record_outcome("source", "parse_failed", parser_version="v2")
    assert state.outcome("source").outcome == "pending"

    state.record_outcome("source", "ignored", parser_version="v1")
    state.record_seen("source")
    state.record_outcome("source", "pending", parser_version="v2")
    assert state.outcome("source").outcome == "ignored"

    state.record_outcome("source", "tombstoned", parser_version="v2")
    state.record_outcome("source", "resolved", parser_version="v3")
    assert state.outcome("source").outcome == "tombstoned"


def test_legacy_processed_rows_migrate_without_deletion(tmp_path) -> None:
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE processed_messages (
            message_hash TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL,
            task_id TEXT
        );
        INSERT INTO processed_messages VALUES
            ('without-task', '2026-08-01T00:00:00+08:00', NULL),
            ('with-task', '2026-08-01T00:00:00+08:00', 'task-1');
        """
    )
    connection.commit()
    connection.close()

    state = StateStore(path)

    assert state.outcome("without-task").outcome == "noncandidate"
    assert state.outcome("with-task").outcome == "resolved"
    with sqlite3.connect(path) as verify:
        assert verify.execute(
            "SELECT COUNT(*) FROM processed_messages"
        ).fetchone()[0] == 2


def test_source_migration_and_telemetry_commit_only_on_success(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    scope = MailboxScope("a" * 24, "INBOX", "777")

    failed = state.begin_scan()
    state.finish_scan(
        failed,
        fetched=1,
        candidates=0,
        searched=3,
        fetch_failed=2,
        uidvalidity="777",
        uidnext="43",
        source_migrations=(scope,),
        source_identity_version="imap-uid-v2",
        error="RuntimeError",
    )
    assert state.source_migration_needed(scope, "imap-uid-v2")

    completed = state.begin_scan()
    state.finish_scan(
        completed,
        fetched=2,
        candidates=1,
        searched=3,
        fetch_failed=1,
        uidvalidity="777",
        uidnext="44",
        source_migrations=(scope,),
        source_identity_version="imap-uid-v2",
    )

    assert not state.source_migration_needed(scope, "imap-uid-v2")
    health = state.health()
    assert health["first_scan"] == {"fetched": 2, "candidates": 1, "lookback_days": None}
    assert health["scan_details"]["fetched"] == 2
    assert health["scan_details"]["candidates"] == 1
    assert health["scan_details"]["fetch_failed"] == 1
    assert health["scan_details"]["lookback_days"] is None
    assert {key: value for key, value in health.items() if key not in {"first_scan", "scan_details"}} == {
        "last_scan_at": state.metadata("last_scan_at"),
        "last_error": None,
        "searched_uids": 3,
        "fetch_failures": 1,
        "parse_failures": 0,
        "uidvalidity": "777",
        "uidnext": "44",
    }


def test_scan_error_telemetry_rejects_private_text(tmp_path) -> None:
    path = tmp_path / "state.db"
    state = StateStore(path)
    run_id = state.begin_scan()
    private_error = "failure parsing Subject: candidate@example.invalid"

    state.finish_scan(
        run_id,
        fetched=0,
        candidates=0,
        error=private_error,
    )

    assert state.health()["last_error"] == "ScanError"
    assert private_error.encode() not in path.read_bytes()
