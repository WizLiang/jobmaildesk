from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal


MailOutcomeName = Literal[
    "seen",
    "fetch_failed",
    "parse_failed",
    "noncandidate",
    "filtered",
    "pending",
    "resolved",
    "ignored",
    "tombstoned",
]
MAIL_OUTCOMES = {
    "seen",
    "fetch_failed",
    "parse_failed",
    "noncandidate",
    "filtered",
    "pending",
    "resolved",
    "ignored",
    "tombstoned",
}
NO_FACT_OUTCOMES = {"seen", "fetch_failed", "parse_failed", "noncandidate", "filtered"}
TERMINAL_FACT_OUTCOMES = {"resolved", "ignored", "tombstoned"}


@dataclass(frozen=True)
class MailboxScope:
    account_fingerprint: str
    mailbox: str
    uidvalidity: str


@dataclass(frozen=True)
class MessageOutcome:
    source_hash: str
    outcome: MailOutcomeName
    parser_version: str | None
    retry_eligible: bool
    task_id: str | None
    account_fingerprint: str | None
    mailbox: str | None
    uidvalidity: str | None
    uid: str | None
    internal_date: datetime | None
    first_seen_at: datetime
    updated_at: datetime


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_hash TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL,
                    task_id TEXT
                );
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    fetched INTEGER NOT NULL DEFAULT 0,
                    candidates INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS message_outcomes (
                    source_hash TEXT PRIMARY KEY,
                    outcome TEXT NOT NULL,
                    parser_version TEXT,
                    retry_eligible INTEGER NOT NULL,
                    task_id TEXT,
                    account_fingerprint TEXT,
                    mailbox TEXT,
                    uidvalidity TEXT,
                    uid TEXT,
                    internal_date TEXT,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_migrations (
                    account_fingerprint TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    uidvalidity TEXT NOT NULL,
                    version TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (
                        account_fingerprint,
                        mailbox,
                        uidvalidity,
                        version
                    )
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS sent_reminders (
                    task_id TEXT NOT NULL,
                    target_at TEXT NOT NULL,
                    offset_minutes INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, target_at, offset_minutes)
                );
                """
            )
            self._ensure_column(
                connection,
                "scan_runs",
                "searched",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "scan_runs",
                "fetch_failed",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "scan_runs", "uidvalidity", "TEXT")
            self._ensure_column(connection, "scan_runs", "uidnext", "TEXT")
            for column, definition in (
                ("lookback_days", "INTEGER"), ("mailbox", "TEXT"),
                ("skipped", "INTEGER"), ("filtered", "INTEGER"),
                ("parse_failed", "INTEGER"),
            ):
                self._ensure_column(connection, "scan_runs", column, definition)
            parser_row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'parser_version'"
            ).fetchone()
            legacy_parser_version = (
                str(parser_row["value"]) if parser_row and parser_row["value"] else "legacy"
            )
            now = datetime.now().astimezone().isoformat()
            # Keep the legacy table as an immutable compatibility fact. Rows
            # without task IDs cannot be classified more precisely, so they
            # become retryable non-candidates and are reconciled with task and
            # review facts by the scanner before any replay.
            connection.execute(
                """
                INSERT OR IGNORE INTO message_outcomes (
                    source_hash,
                    outcome,
                    parser_version,
                    retry_eligible,
                    task_id,
                    first_seen_at,
                    updated_at
                )
                SELECT
                    message_hash,
                    CASE WHEN task_id IS NULL THEN 'noncandidate' ELSE 'resolved' END,
                    ?,
                    CASE WHEN task_id IS NULL THEN 1 ELSE 0 END,
                    task_id,
                    processed_at,
                    ?
                FROM processed_messages
                """,
                (legacy_parser_version, now),
            )
            connection.commit()

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    @staticmethod
    def _select_outcome(
        existing: MailOutcomeName | None,
        incoming: MailOutcomeName,
    ) -> MailOutcomeName:
        if existing is None:
            return incoming
        if existing == "tombstoned" or incoming == "tombstoned":
            return "tombstoned"
        if existing in {"resolved", "ignored"}:
            return existing
        if existing == "pending" and incoming in NO_FACT_OUTCOMES - {"filtered"}:
            return existing
        if incoming == "seen":
            return existing
        if (
            incoming == "fetch_failed"
            and existing in {"parse_failed", "noncandidate"}
        ):
            return existing
        return incoming

    @staticmethod
    def _locator_values(
        locator: dict[str, str] | None,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        if not locator:
            return None, None, None, None
        return (
            str(locator.get("account_fingerprint") or "") or None,
            str(locator.get("mailbox") or "") or None,
            str(locator.get("uidvalidity") or "") or None,
            str(locator.get("uid") or "") or None,
        )

    @staticmethod
    def _row_to_outcome(row: sqlite3.Row) -> MessageOutcome:
        internal_date = (
            datetime.fromisoformat(str(row["internal_date"]))
            if row["internal_date"]
            else None
        )
        return MessageOutcome(
            source_hash=str(row["source_hash"]),
            outcome=str(row["outcome"]),  # type: ignore[arg-type]
            parser_version=(
                str(row["parser_version"]) if row["parser_version"] else None
            ),
            retry_eligible=bool(row["retry_eligible"]),
            task_id=str(row["task_id"]) if row["task_id"] else None,
            account_fingerprint=(
                str(row["account_fingerprint"])
                if row["account_fingerprint"]
                else None
            ),
            mailbox=str(row["mailbox"]) if row["mailbox"] else None,
            uidvalidity=(
                str(row["uidvalidity"]) if row["uidvalidity"] else None
            ),
            uid=str(row["uid"]) if row["uid"] else None,
            internal_date=internal_date,
            first_seen_at=datetime.fromisoformat(str(row["first_seen_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def outcome(self, source_hash: str) -> MessageOutcome | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM message_outcomes WHERE source_hash = ?",
                (source_hash,),
            ).fetchone()
        return self._row_to_outcome(row) if row else None

    def restore_ignored_review(self, source_hashes: set[str]) -> None:
        """Explicit user restoration only; ordinary replay keeps terminal precedence."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for source_hash in source_hashes:
                row = connection.execute(
                    "SELECT outcome FROM message_outcomes WHERE source_hash = ?", (source_hash,),
                ).fetchone()
                if row and row["outcome"] in {"resolved", "tombstoned"}:
                    raise ValueError("邮件已确认或已删除，不能从已忽略列表恢复。")
            for source_hash in source_hashes:
                connection.execute(
                    "UPDATE message_outcomes SET outcome = 'pending', retry_eligible = 0, "
                    "updated_at = ? WHERE source_hash = ? AND outcome = 'ignored'",
                    (datetime.now().astimezone().isoformat(), source_hash),
                )

    def record_outcome(
        self,
        source_hash: str,
        outcome: MailOutcomeName,
        *,
        parser_version: str | None = None,
        retry_eligible: bool | None = None,
        task_id: str | None = None,
        locator: dict[str, str] | None = None,
        internal_date: datetime | None = None,
    ) -> MessageOutcome:
        if not source_hash:
            raise ValueError("source_hash must not be empty")
        if outcome not in MAIL_OUTCOMES:
            raise ValueError(f"unsupported mail outcome: {outcome}")
        account, mailbox, uidvalidity, uid = self._locator_values(locator)
        now = datetime.now().astimezone()
        with closing(self._connect()) as connection:
            existing_row = connection.execute(
                "SELECT * FROM message_outcomes WHERE source_hash = ?",
                (source_hash,),
            ).fetchone()
            existing = (
                self._row_to_outcome(existing_row) if existing_row else None
            )
            selected = self._select_outcome(
                existing.outcome if existing else None,
                outcome,
            )
            preserve_existing = bool(existing and selected != outcome)
            selected_parser_version = (
                existing.parser_version
                if preserve_existing
                else parser_version
            )
            selected_retry_eligible = (
                existing.retry_eligible
                if preserve_existing
                else (
                    retry_eligible
                    if retry_eligible is not None
                    else selected in NO_FACT_OUTCOMES
                )
            )
            first_seen_at = existing.first_seen_at if existing else now
            selected_task_id = (
                task_id
                if task_id is not None
                else (existing.task_id if existing else None)
            )
            connection.execute(
                """
                INSERT INTO message_outcomes (
                    source_hash,
                    outcome,
                    parser_version,
                    retry_eligible,
                    task_id,
                    account_fingerprint,
                    mailbox,
                    uidvalidity,
                    uid,
                    internal_date,
                    first_seen_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_hash) DO UPDATE SET
                    outcome = excluded.outcome,
                    parser_version = excluded.parser_version,
                    retry_eligible = excluded.retry_eligible,
                    task_id = excluded.task_id,
                    account_fingerprint = COALESCE(
                        excluded.account_fingerprint,
                        message_outcomes.account_fingerprint
                    ),
                    mailbox = COALESCE(
                        excluded.mailbox,
                        message_outcomes.mailbox
                    ),
                    uidvalidity = COALESCE(
                        excluded.uidvalidity,
                        message_outcomes.uidvalidity
                    ),
                    uid = COALESCE(excluded.uid, message_outcomes.uid),
                    internal_date = COALESCE(
                        excluded.internal_date,
                        message_outcomes.internal_date
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    source_hash,
                    selected,
                    selected_parser_version,
                    int(selected_retry_eligible),
                    selected_task_id,
                    account,
                    mailbox,
                    uidvalidity,
                    uid,
                    internal_date.isoformat() if internal_date else None,
                    first_seen_at.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM message_outcomes WHERE source_hash = ?",
                (source_hash,),
            ).fetchone()
        if row is None:
            raise RuntimeError("mail outcome write did not persist")
        return self._row_to_outcome(row)

    def record_seen(
        self,
        source_hash: str,
        *,
        locator: dict[str, str] | None = None,
        internal_date: datetime | None = None,
    ) -> MessageOutcome:
        return self.record_outcome(
            source_hash,
            "seen",
            retry_eligible=True,
            locator=locator,
            internal_date=internal_date,
        )

    def migrate_automatic_filter(self, source_hash: str) -> None:
        """Only for a matching generated marketing review from the prior RC.

        The caller verifies that review's reason/version and holds the data
        lease. Never downgrade manual ignores, confirmed facts or tombstones.
        """
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE message_outcomes SET outcome = 'filtered', retry_eligible = 1
                WHERE source_hash = ? AND outcome = 'ignored'
                  AND parser_version = '2026.09.19.1' AND task_id IS NULL""",
                (source_hash,),
            )
            connection.commit()

    @staticmethod
    def parser_replay_due(
        recorded_version: str | None,
        parser_version: str,
        *,
        internal_date: datetime | None,
        replay_cutoff: datetime | None,
    ) -> bool:
        if (
            recorded_version == parser_version
            or internal_date is None
            or replay_cutoff is None
        ):
            return False
        replay_date = internal_date
        cutoff = replay_cutoff
        if replay_date.tzinfo is None and cutoff.tzinfo is not None:
            replay_date = replay_date.replace(tzinfo=cutoff.tzinfo)
        elif replay_date.tzinfo is not None and cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=replay_date.tzinfo)
        return replay_date >= cutoff

    def has_parser_replay_debt(
        self,
        parser_version: str,
        *,
        replay_cutoff: datetime,
    ) -> bool:
        """Return whether a bounded mailbox replay still has row-local debt."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM message_outcomes
                WHERE outcome IN (
                    'seen',
                    'fetch_failed',
                    'parse_failed',
                    'noncandidate',
                    'filtered',
                    'pending'
                )
                """
            ).fetchall()
        for row in rows:
            current = self._row_to_outcome(row)
            if (
                current.outcome in NO_FACT_OUTCOMES
                and not current.retry_eligible
            ):
                continue
            if self.parser_replay_due(
                current.parser_version,
                parser_version,
                internal_date=current.internal_date,
                replay_cutoff=replay_cutoff,
            ):
                return True
        return False

    def should_process(
        self,
        source_hash: str,
        *,
        parser_version: str,
        parser_changed: bool,
        manual_retry: bool = False,
        internal_date: datetime | None = None,
        replay_cutoff: datetime | None = None,
    ) -> bool:
        current = self.outcome(source_hash)
        if current is None:
            return True
        if current.outcome not in NO_FACT_OUTCOMES:
            return False
        if manual_retry:
            return True
        if not current.retry_eligible:
            return False
        if current.outcome in {"seen", "fetch_failed"}:
            return True
        if current.outcome == "parse_failed" and current.parser_version is None:
            return True
        # ``parser_changed`` is retained for call-site compatibility, but the
        # committed global version cannot settle debt for rows that were not
        # successfully fetched and parsed during that upgrade scan.
        del parser_changed
        return self.parser_replay_due(
            current.parser_version,
            parser_version,
            internal_date=internal_date or current.internal_date,
            replay_cutoff=replay_cutoff,
        )

    def is_processed(self, message_hash: str) -> bool:
        current = self.outcome(message_hash)
        return bool(
            current and current.outcome not in {"seen", "fetch_failed"}
        )

    def mark_processed(self, message_hash: str, task_id: str | None) -> None:
        processed_at = datetime.now().astimezone().isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO processed_messages
                    (message_hash, processed_at, task_id)
                VALUES (?, ?, ?)
                """,
                (message_hash, processed_at, task_id),
            )
            connection.commit()
        self.record_outcome(
            message_hash,
            "resolved" if task_id else "noncandidate",
            parser_version=self.metadata("parser_version") or "legacy",
            task_id=task_id,
        )

    def has_successful_scan(self) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM scan_runs
                WHERE finished_at IS NOT NULL AND error IS NULL
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def parser_version_changed(self, version: str) -> bool:
        """Compare the committed parser version without mutating scan state."""
        return self.metadata("parser_version") != version

    def prepare_parser_version(self, version: str) -> bool:
        """Backward-compatible, read-only parser version check."""
        return self.parser_version_changed(version)

    def source_migration_needed(
        self,
        scope: MailboxScope,
        version: str,
    ) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM source_migrations
                WHERE account_fingerprint = ?
                  AND mailbox = ?
                  AND uidvalidity = ?
                  AND version = ?
                """,
                (
                    scope.account_fingerprint,
                    scope.mailbox,
                    scope.uidvalidity,
                    version,
                ),
            ).fetchone()
        return row is None

    def begin_scan(self) -> int:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO scan_runs (started_at) VALUES (?)",
                (datetime.now().astimezone().isoformat(),),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def finish_scan(
        self,
        run_id: int,
        *,
        fetched: int,
        candidates: int,
        searched: int = 0,
        fetch_failed: int = 0,
        uidvalidity: str | None = None,
        uidnext: str | None = None,
        parser_version: str | None = None,
        source_migrations: Iterable[MailboxScope] = (),
        source_identity_version: str | None = None,
        lookback_days: int | None = None,
        mailbox: str | None = None,
        skipped: int = 0,
        filtered: int = 0,
        parse_failed: int = 0,
        error: str | None = None,
    ) -> None:
        safe_error = (
            error
            if error and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,79}", error)
            else ("ScanError" if error else None)
        )
        safe_uidvalidity = (
            uidvalidity if uidvalidity and uidvalidity.isdigit() else None
        )
        safe_uidnext = uidnext if uidnext and uidnext.isdigit() else None
        finished_at = datetime.now().astimezone().isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE scan_runs
                SET finished_at = ?,
                    fetched = ?,
                    candidates = ?,
                    searched = ?,
                    fetch_failed = ?,
                    uidvalidity = ?,
                    uidnext = ?,
                    lookback_days = ?, mailbox = ?, skipped = ?, filtered = ?, parse_failed = ?,
                    error = ?
                WHERE id = ?
                """,
                (
                    finished_at,
                    fetched,
                    candidates,
                    max(0, searched),
                    max(0, fetch_failed),
                    safe_uidvalidity,
                    safe_uidnext,
                    lookback_days, mailbox, skipped, filtered, parse_failed,
                    safe_error,
                    run_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO metadata (key, value) VALUES ('last_scan_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (finished_at,),
            )
            if safe_error:
                connection.execute(
                    """
                    INSERT INTO metadata (key, value) VALUES ('last_error', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (safe_error,),
                )
            else:
                connection.execute("DELETE FROM metadata WHERE key = 'last_error'")
                if parser_version:
                    connection.execute(
                        """
                        INSERT INTO metadata (key, value)
                        VALUES ('parser_version', ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (parser_version,),
                    )
                if source_identity_version:
                    for scope in source_migrations:
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO source_migrations (
                                account_fingerprint,
                                mailbox,
                                uidvalidity,
                                version,
                                completed_at
                            )
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                scope.account_fingerprint,
                                scope.mailbox,
                                scope.uidvalidity,
                                source_identity_version,
                                finished_at,
                            ),
                        )
            connection.commit()

    def health(self) -> dict[str, object]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT key, value FROM metadata").fetchall()
            latest = connection.execute(
                """
                SELECT searched, fetch_failed, uidvalidity, uidnext,
                       fetched, candidates, lookback_days, mailbox, skipped, filtered, parse_failed
                FROM scan_runs
                WHERE finished_at IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            first = connection.execute(
                """SELECT fetched, candidates, lookback_days FROM scan_runs
                WHERE finished_at IS NOT NULL AND error IS NULL ORDER BY id LIMIT 1"""
            ).fetchone()
        metadata = {str(row["key"]): str(row["value"]) for row in rows}
        return {
            "scan_details": dict(latest) if latest else None,
            "first_scan": dict(first) if first else None,
            "last_scan_at": metadata.get("last_scan_at"),
            "last_error": metadata.get("last_error"),
            "searched_uids": int(latest["searched"]) if latest else 0,
            "fetch_failures": int(latest["fetch_failed"]) if latest else 0,
            "uidvalidity": (
                str(latest["uidvalidity"])
                if latest and latest["uidvalidity"]
                else None
            ),
            "uidnext": (
                str(latest["uidnext"])
                if latest and latest["uidnext"]
                else None
            ),
        }

    def reminder_sent(
        self,
        task_id: str,
        target_at: datetime,
        offset_minutes: int,
    ) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM sent_reminders
                WHERE task_id = ? AND target_at = ? AND offset_minutes = ?
                """,
                (task_id, target_at.isoformat(), offset_minutes),
            ).fetchone()
        return row is not None

    def mark_reminder_sent(
        self,
        task_id: str,
        target_at: datetime,
        offset_minutes: int,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO sent_reminders
                    (task_id, target_at, offset_minutes, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    task_id,
                    target_at.isoformat(),
                    offset_minutes,
                    datetime.now().astimezone().isoformat(),
                ),
            )
            connection.commit()

    def clear_task_reminders(self, task_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM sent_reminders WHERE task_id = ?",
                (task_id,),
            )
            connection.commit()

    def metadata(self, key: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else None

    def set_metadata(self, key: str, value: str | None) -> None:
        with closing(self._connect()) as connection:
            if value is None:
                connection.execute("DELETE FROM metadata WHERE key = ?", (key,))
            else:
                connection.execute(
                    """
                    INSERT INTO metadata (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )
            connection.commit()
