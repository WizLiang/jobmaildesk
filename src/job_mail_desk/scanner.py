from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from email.utils import parseaddr
import hashlib
import json
import logging
import math
import re

from .application_registry import ApplicationRegistry, preview_progress_applications
from .application_lifecycle import reconcile_all_applications
from .activity_store import ActivityStore
from .config import (
    APPLICATIONS_DIR,
    DASHBOARD_FILE,
    DICTIONARIES_DIR,
    STATE_DB,
    TASKS_DIR,
    UNRESOLVED_DIR,
    Settings,
    ensure_directories,
)
from .credentials import load_credential
from .data_lock import data_directory_lease
from .exporter import export_dashboard, import_checked_states
from .mail_reader import ImapReader, MailFetchBatch, account_fingerprint
from .macos_dock import set_dock_badge
from .identity_dictionaries import load_identity_dictionaries
from .identity_learning import IdentityLearningStore
from .identity_pipeline import resolve_event_batch
from .markdown_store import MarkdownTaskStore
from .models import MailRecord, ParsedEvent
from .parser import PARSER_VERSION, SHANGHAI, parse_record, parser_diagnostics
from .progress import export_progress
from .private_link_store import PrivateLinkStore
from .research import synchronize_research_state
from .runtime_control import RuntimeControl, RuntimeStopping
from .scan_progress import report_progress, track_items
from .state import MailboxScope, ScanCoverage, StateStore
from .stages import is_stage_advance
from .task_service import (
    critical_time,
    message_hash,
)
from .unresolved_store import UnresolvedStore, filtered_from_mail, unresolved_from_decision


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanSummary:
    fetched: int
    skipped: int
    candidates: int
    tasks_updated: int
    parse_failed: int
    research_queued: int
    urgent: int
    exported: int
    shadow: bool
    preview: tuple[dict[str, object], ...] = ()
    identity_mode: str = "legacy"
    identity_matched: int = 0
    identity_new_applications: int = 0
    identity_unresolved: int = 0
    identity_conflicts: int = 0
    searched: int = 0
    fetch_failed: int = 0
    uidvalidity: str | None = None
    uidnext: str | None = None
    lookback_days: int | None = None
    mailbox: str | None = None
    filtered: int = 0
    reviews_created: int = 0
    reviews_updated: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parser_version_for_settings(settings: Settings) -> str:
    return PARSER_VERSION + "+filtered-review-v1" + ("+onsite" if settings.include_onsite_sessions else "")


def parse_for_settings(record, dictionaries, settings: Settings):
    if settings.include_onsite_sessions:
        return parse_record(record, dictionaries, include_onsite_sessions=True)
    return parse_record(record, dictionaries)


INITIAL_LOOKBACK_DAYS = 30
PARSER_REPLAY_DAYS = 30
STALE_ACTION_DAYS = 7
STALE_EVENT_TYPES = {"assessment", "interview", "deadline"}
SOURCE_IDENTITY_VERSION = "imap-uid-v2"


def _scope_from_locator(
    locator: dict[str, str] | None,
) -> MailboxScope | None:
    if not locator:
        return None
    account = str(locator.get("account_fingerprint") or "")
    mailbox = str(locator.get("mailbox") or "")
    uidvalidity = str(locator.get("uidvalidity") or "")
    if (
        not re.fullmatch(r"[0-9a-f]{24}", account)
        or not mailbox
        or mailbox != mailbox.strip()
        or any(ord(character) < 32 for character in mailbox)
        or not uidvalidity.isdigit()
        or int(uidvalidity) < 1
    ):
        return None
    return MailboxScope(account, mailbox, uidvalidity)


def _source_hash_from_locator(locator: dict[str, str] | None) -> str | None:
    scope = _scope_from_locator(locator)
    if not scope or not locator:
        return None
    uid = str(locator.get("uid") or "")
    if not uid.isdigit() or int(uid) < 1:
        return None
    return message_hash(
        "imap\0"
        f"{scope.account_fingerprint}\0"
        f"{scope.mailbox}\0"
        f"{scope.uidvalidity}\0"
        f"{uid}"
    )


def _record_source_hashes(record: MailRecord) -> tuple[str, str | None]:
    locator_hash = _source_hash_from_locator(record.locator())
    legacy_hash = message_hash(record.message_id) if record.message_id else None
    if locator_hash:
        return locator_hash, legacy_hash
    if legacy_hash:
        # Compatibility for imported/synthetic records only. A UID-derived
        # identity is never created without the complete IMAP namespace.
        return legacy_hash, legacy_hash
    raise ValueError("邮件缺少完整 IMAP 来源身份。")


def _scope_from_batch(batch: MailFetchBatch) -> MailboxScope | None:
    return _scope_from_locator(
        {
            "account_fingerprint": batch.account_fingerprint,
            "mailbox": batch.mailbox,
            "uidvalidity": batch.uidvalidity,
        }
    )


def _batch_locator(batch: MailFetchBatch, uid: str) -> dict[str, str] | None:
    scope = _scope_from_batch(batch)
    if not scope or not uid.isdigit() or int(uid) < 1:
        return None
    return {
        "account_fingerprint": scope.account_fingerprint,
        "mailbox": scope.mailbox,
        "uidvalidity": scope.uidvalidity,
        "uid": uid,
    }


def _fetch_scan_batch(reader, days: int) -> MailFetchBatch:
    fetch_batch = getattr(reader, "fetch_batch_since", None)
    if callable(fetch_batch):
        return fetch_batch(days)
    # Test/plug-in compatibility: production ImapReader always provides the
    # richer batch API. Strict locators are retained when all records share a
    # namespace; otherwise only the legacy record identities are available.
    records = tuple(reader.fetch_since(days))
    record_scopes = [
        _scope_from_locator(record.locator()) for record in records
    ]
    scopes = {scope for scope in record_scopes if scope is not None}
    scope = (
        next(iter(scopes))
        if records
        and all(record_scope is not None for record_scope in record_scopes)
        and len(scopes) == 1
        else None
    )
    return MailFetchBatch(
        records=records,
        searched_uids=tuple(record.uid for record in records),
        fetch_failed_uids=(),
        parse_failed_uids=(),
        account_fingerprint=scope.account_fingerprint if scope else "",
        mailbox=scope.mailbox if scope else "",
        uidvalidity=scope.uidvalidity if scope else "",
        uidnext=None,
    )


def _review_outcome(status: str) -> str:
    if status == "filtered":
        return "filtered"
    if status == "resolved":
        return "resolved"
    if status == "ignored":
        return "ignored"
    return "pending"


_FACT_PRIORITY = {
    "pending": 1,
    "resolved": 2,
    "ignored": 3,
    "tombstoned": 4,
}


def _known_source_facts(
    store: MarkdownTaskStore,
    unresolved_store: UnresolvedStore,
) -> dict[tuple[str, str], tuple[str, str | None]]:
    facts: dict[tuple[str, str], tuple[str, str | None]] = {}

    def remember(
        source_hash: str | None,
        outcome: str,
        task_id: str | None = None,
        *,
        canonical_source_hash: str | None = None,
    ) -> None:
        if not source_hash:
            return
        fact_key = (source_hash, canonical_source_hash or source_hash)
        previous = facts.get(fact_key)
        if (
            previous
            and _FACT_PRIORITY.get(previous[0], 0)
            >= _FACT_PRIORITY.get(outcome, 0)
        ):
            return
        facts[fact_key] = (outcome, task_id)

    for task in store.all():
        outcome = (
            "tombstoned"
            if task.tombstoned or task.deleted_at
            else "resolved"
        )
        canonical_source_hash = _source_hash_from_locator(task.mail_locator)
        remember(
            canonical_source_hash,
            outcome,
            task.id,
            canonical_source_hash=canonical_source_hash,
        )
        if task.source_message_hash and task.source_message_hash != "manual":
            remember(
                task.source_message_hash,
                outcome,
                task.id,
                canonical_source_hash=canonical_source_hash,
            )
    for review in unresolved_store.all():
        outcome = _review_outcome(review.status)
        canonical_source_hash = _source_hash_from_locator(review.mail_locator)
        remember(
            canonical_source_hash,
            outcome,
            canonical_source_hash=canonical_source_hash,
        )
        remember(
            review.id,
            outcome,
            canonical_source_hash=canonical_source_hash,
        )
    return facts


def _known_source_fact(
    facts: dict[tuple[str, str], tuple[str, str | None]],
    source_hash: str,
    legacy_source_hash: str | None,
) -> tuple[str, str | None] | None:
    candidates = [facts.get((source_hash, source_hash))]
    if legacy_source_hash:
        candidates.append(facts.get((legacy_source_hash, source_hash)))
    present = [candidate for candidate in candidates if candidate is not None]
    if not present:
        return None
    return max(present, key=lambda item: _FACT_PRIORITY.get(item[0], 0))


def _legacy_source_matches(
    state: StateStore,
    facts: dict[tuple[str, str], tuple[str, str | None]],
    legacy_source_hash: str,
    source_hash: str,
) -> bool:
    if (legacy_source_hash, source_hash) in facts:
        return True
    legacy_outcome = state.outcome(legacy_source_hash)
    if legacy_outcome is None:
        return False
    return (
        _source_hash_from_locator(
            {
                "account_fingerprint": (
                    legacy_outcome.account_fingerprint or ""
                ),
                "mailbox": legacy_outcome.mailbox or "",
                "uidvalidity": legacy_outcome.uidvalidity or "",
                "uid": legacy_outcome.uid or "",
            }
        )
        == source_hash
    )


def _prepare_uid_source_migration(
    state: StateStore,
    records: list[MailRecord],
    store: MarkdownTaskStore,
    unresolved_store: UnresolvedStore,
    *,
    scopes: tuple[MailboxScope, ...] = (),
    parser_version: str = PARSER_VERSION,
) -> tuple[MailboxScope, ...]:
    discovered_scopes = {
        scope
        for record in records
        if (scope := _scope_from_locator(record.locator())) is not None
    }
    discovered_scopes.update(scopes)
    pending_scopes = tuple(
        sorted(
            (
                scope
                for scope in discovered_scopes
                if state.source_migration_needed(
                    scope,
                    SOURCE_IDENTITY_VERSION,
                )
            ),
            key=lambda scope: (
                scope.account_fingerprint,
                scope.mailbox,
                scope.uidvalidity,
            ),
        )
    )
    if not pending_scopes:
        return ()
    pending_scope_set = set(pending_scopes)
    known_facts = _known_source_facts(store, unresolved_store)
    recent_cutoff = datetime.now(SHANGHAI) - timedelta(hours=48)
    for record in records:
        locator = record.locator()
        scope = _scope_from_locator(locator)
        source_hash = _source_hash_from_locator(locator)
        if not scope or scope not in pending_scope_set or not source_hash:
            continue
        fact = _known_source_fact(known_facts, source_hash, None)
        if fact:
            outcome, task_id = fact
            state.record_outcome(
                source_hash,
                outcome,  # type: ignore[arg-type]
                parser_version=parser_version,
                task_id=task_id,
                locator=locator,
                internal_date=record.internal_date,
            )
            continue
        if record.internal_date is None:
            continue
        internal_date = record.internal_date
        if internal_date.tzinfo is None:
            internal_date = internal_date.replace(tzinfo=SHANGHAI)
        # Only baseline mail actually seen by the pre-UID reader. A new
        # installation has no legacy outcome; applying the old 48-hour
        # migration shortcut there silently skips most of its first 30 days.
        legacy_outcome = (
            state.outcome(message_hash(record.message_id))
            if record.message_id else None
        )
        if (
            internal_date < recent_cutoff
            and legacy_outcome is not None
            and not legacy_outcome.account_fingerprint
        ):
            state.record_outcome(
                source_hash,
                "noncandidate",
                parser_version=parser_version,
                locator=locator,
                internal_date=internal_date,
            )
    # Completion is committed atomically with a successful scan. A crash
    # leaves these scopes pending and the idempotent migration runs again.
    return pending_scopes


def _scan_scope_key(settings: Settings, email: str) -> str:
    # Preserve existing message IDs; only scan coverage distinguishes endpoints.
    folder = settings.mail_folder
    if folder.casefold() == "inbox":
        folder = "INBOX"
    identity = [account_fingerprint(settings.mail_host, email), settings.mail_port,
                settings.mail_ssl, folder]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()


def _effective_lookback_days(
    settings: Settings,
    state: StateStore,
    requested_days: int | None,
    *,
    parser_changed: bool = False,
    replay_debt: bool = False,
    coverage: ScanCoverage | None = None,
    scoped: bool = False,
    now: datetime | None = None,
) -> int:
    if requested_days is not None:
        return requested_days
    if scoped:
        now = now or datetime.now(SHANGHAI)
        baseline = max(INITIAL_LOOKBACK_DAYS, settings.lookback_days)
        if coverage is None:
            return baseline
        boundary = coverage.covered_until or coverage.required_since
        if coverage.pending_since is not None:
            boundary = min(boundary, coverage.pending_since)
        # An extra overlap day for completed coverage avoids clock/SEARCH timing
        # edges. Failed initial attempts retain their original required_since.
        gap = math.ceil(max(0, (now - boundary).total_seconds()) / 86400)
        if coverage.covered_until is not None and coverage.pending_since is None:
            gap += 1
        return max(settings.lookback_days, gap,
                   baseline if parser_changed or replay_debt or coverage.covered_until is None else 1)
    if parser_changed or replay_debt or not state.has_successful_scan():
        return max(INITIAL_LOOKBACK_DAYS, settings.lookback_days)
    return settings.lookback_days


def _pending_review_replay_debt(
    state: StateStore,
    unresolved_store: UnresolvedStore,
    parser_version: str,
    replay_cutoff: datetime,
) -> bool:
    for review in unresolved_store.all():
        if review.status not in {"pending", "filtered"} or review.manual_restore:
            continue
        canonical_source_hash = _source_hash_from_locator(review.mail_locator)
        current = (
            state.outcome(canonical_source_hash)
            if canonical_source_hash
            else None
        )
        if current is None:
            current = state.outcome(review.id)
        if current and current.outcome in {"resolved", "ignored", "tombstoned"}:
            continue
        if StateStore.parser_replay_due(
            review.parser_version,
            parser_version,
            internal_date=(
                current.internal_date
                if current and current.internal_date
                else review.received_at
            ),
            replay_cutoff=replay_cutoff,
        ):
            return True
    return False


def _is_stale_attention(task, now: datetime) -> bool:
    return (
        task.status == "needs_review"
        and task.event_type in STALE_EVENT_TYPES
        and critical_time(task) is None
        and task.received_at < now - timedelta(days=STALE_ACTION_DAYS)
    )


def _resolution_applications(settings: Settings):
    records = {
        record.application_key: record
        for record in ApplicationRegistry(APPLICATIONS_DIR).active()
    }
    for record in preview_progress_applications(settings.progress_source):
        records.setdefault(record.application_key, record)
    return list(records.values())


def _backfill_task_application_keys(
    registry: ApplicationRegistry,
    store: MarkdownTaskStore,
) -> int:
    legacy_candidates: dict[str, set[str]] = {}
    for record in registry.active(ignore_invalid=True):
        for legacy_id in record.legacy_application_ids:
            legacy_candidates.setdefault(legacy_id, set()).add(record.application_key)
    legacy_map = {
        legacy_id: next(iter(keys))
        for legacy_id, keys in legacy_candidates.items()
        if len(keys) == 1
    }
    updated = 0
    for task in store.all():
        if task.application_key:
            continue
        application_key = legacy_map.get(task.application_id)
        if not application_key:
            continue
        task.application_key = application_key
        store.save(task)
        updated += 1
    return updated


def _scan_identity_preview(
    settings: Settings,
    *,
    days: int | None = None,
) -> ScanSummary:
    """Replay a bounded mailbox window without changing tasks or scan state."""
    ensure_directories()
    effective_days = days if days is not None else settings.lookback_days
    batch = ImapReader(settings, load_credential()).fetch_batch_since(
        effective_days
    )
    records = list(batch.records)
    dictionaries = load_identity_dictionaries(DICTIONARIES_DIR)
    learning = IdentityLearningStore(DICTIONARIES_DIR / "manual")
    parsed_records: list[tuple[MailRecord, ParsedEvent | None, str]] = []
    events = []
    parse_failed = 0
    for record in records:
        try:
            event = parse_for_settings(record, dictionaries, settings)
            if event:
                event = learning.enrich(
                    event,
                    sender=record.sender,
                    subject=record.subject,
                )
        except Exception as exc:
            parse_failed += 1
            parsed_records.append(
                (record, None, f"parse_failed:{type(exc).__name__}")
            )
            LOGGER.warning(
                "身份预览解析失败，已隔离 uid=%s error=%s",
                record.uid,
                type(exc).__name__,
            )
            continue
        if event:
            events.append(event)
            parsed_records.append((record, event, "candidate"))
        else:
            parsed_records.append(
                (
                    record,
                    None,
                    (
                        "truncated-noncandidate"
                        if record.content_truncated
                        else "noncandidate"
                    ),
                )
            )
    decisions = resolve_event_batch(
        events,
        _resolution_applications(settings),
        dictionaries,
    )
    decision_iterator = iter(decisions)
    preview_items: list[dict[str, object]] = []
    seen_semantic_events: dict[tuple[object, ...], int] = {}
    for record, event, parser_status in parsed_records:
        diagnostics = parser_diagnostics(record)
        if event is not None:
            decision = next(decision_iterator)
            item = dict(decision.to_preview())
            schedule_identity = (
                event.start_at,
                event.end_at,
                event.deadline_at,
            )
            temporal_identity: object = (
                schedule_identity
                if any(schedule_identity)
                else event.source_received_at.replace(second=0, microsecond=0)
            )
            semantic_key = (
                event.company,
                event.role_canonical or event.role,
                event.recruiting_project,
                event.event_type,
                event.stage,
                event.round,
                temporal_identity,
                event.duration_minutes,
            )
            duplicate_of = seen_semantic_events.get(semantic_key)
            if duplicate_of is None:
                seen_semantic_events[semantic_key] = len(preview_items) + 1
            else:
                item["semantic_duplicate_of"] = duplicate_of
            item.update(
                {
                    "company_confidence": event.company_confidence,
                    "company_source": event.company_source,
                    "role_confidence": event.role_confidence,
                    "role_source": event.role_source,
                    "location": event.location,
                    "event_type": event.event_type,
                    "start_at": (
                        event.start_at.isoformat() if event.start_at else None
                    ),
                    "end_at": (
                        event.end_at.isoformat() if event.end_at else None
                    ),
                    "deadline_at": (
                        event.deadline_at.isoformat()
                        if event.deadline_at
                        else None
                    ),
                    "duration_minutes": event.duration_minutes,
                    "has_action_link": bool(event.source_url or record.links),
                }
            )
        else:
            item = {
                "company": None,
                "role": None,
                "project": None,
                "stage": None,
                "identity_action": parser_status.split(":", 1)[0],
                "resolution_reason": parser_status,
                "application_key": None,
                "company_confidence": 0.0,
                "company_source": None,
                "role_confidence": 0.0,
                "role_source": None,
                "location": None,
                "event_type": None,
                "start_at": None,
                "end_at": None,
                "deadline_at": None,
                "duration_minutes": None,
                "has_action_link": bool(record.links),
            }
        item.update(
            {
                "uid": record.uid,
                "received_at": record.received_at.isoformat(),
                "subject": record.subject,
                "sender_display_name": parseaddr(record.sender)[0],
                "parser_status": parser_status,
                "diagnostics": diagnostics,
            }
        )
        preview_items.append(item)
    preview = tuple(preview_items)
    return ScanSummary(
        fetched=len(records),
        skipped=0,
        candidates=len(events),
        tasks_updated=0,
        parse_failed=parse_failed,
        research_queued=0,
        urgent=0,
        exported=0,
        shadow=True,
        preview=preview,
        identity_mode="preview",
        identity_matched=sum(
            decision.action in {"matched", "batch_context_match"}
            for decision in decisions
        ),
        identity_new_applications=sum(
            decision.action == "new_application" for decision in decisions
        ),
        identity_unresolved=sum(
            decision.action == "unresolved" for decision in decisions
        ),
        identity_conflicts=sum(
            decision.action == "conflict" for decision in decisions
        ),
        searched=batch.searched,
        fetch_failed=batch.fetch_failures,
        uidvalidity=batch.uidvalidity,
        uidnext=batch.uidnext,
    )


def _reprocess_pending_unresolved(
    registry: ApplicationRegistry,
    store: MarkdownTaskStore,
    unresolved_store: UnresolvedStore,
    dictionaries,
    *,
    _pass: int = 0,
) -> int:
    """Refresh review suggestions without materializing applications or tasks."""
    del store, _pass
    prepared: list[tuple[object, object]] = []
    for record in sorted(
        (
            item
            for item in unresolved_store.all()
            if item.status == "pending"
        ),
        key=lambda item: (item.received_at, item.id),
    ):
        parsed = parse_record(
            MailRecord(
                uid=record.id,
                subject=record.title,
                message_id=f"unresolved:{record.id}",
                sender=record.company or "",
                received_at=record.received_at,
                body=" ".join((record.action_summary, *record.requirements)),
            ),
            dictionaries,
        )
        if not parsed:
            continue
        parsed = replace(
            parsed,
            company=parsed.company or record.company,
            role=parsed.role or record.role,
            role_raw=parsed.role_raw or record.role_raw,
            role_canonical=parsed.role_canonical or record.role_canonical,
            job_code=parsed.job_code or record.job_code,
            recruiting_project=(
                parsed.recruiting_project or record.recruiting_project
            ),
            location=parsed.location or record.location,
            round=parsed.round or record.round,
            start_at=parsed.start_at or record.start_at,
            end_at=parsed.end_at or record.end_at,
            deadline_at=parsed.deadline_at or record.deadline_at,
            duration_minutes=(
                parsed.duration_minutes or record.duration_minutes
            ),
            source_message_id=f"unresolved:{record.id}",
            source_received_at=record.received_at,
        )
        prepared.append((record, parsed))
    if not prepared:
        return 0
    decisions = resolve_event_batch(
        [event for _record, event in prepared],
        registry.active(ignore_invalid=True),
        dictionaries,
    )
    refreshed_count = 0
    for (record, event), decision in zip(prepared, decisions, strict=True):
        preserve_action_recommendation = bool(
            "action_link" in record.recommendation_reasons
            and event.event_type != "rejection"
            and event.change_type != "cancel"
        )
        refreshed = unresolved_from_decision(
            record.id,
            decision,
            record.mail_locator,
            parser_version=PARSER_VERSION,
            private_link_ref=record.private_link_ref,
        )
        refreshed = replace(
            refreshed,
            role_raw=refreshed.role_raw or record.role_raw,
            role_canonical=(
                refreshed.role_canonical or record.role_canonical
            ),
            job_code=refreshed.job_code or record.job_code,
            recruiting_year=(
                refreshed.recruiting_year or record.recruiting_year
            ),
            business_unit=refreshed.business_unit or record.business_unit,
            round=refreshed.round or record.round,
            duration_minutes=(
                refreshed.duration_minutes or record.duration_minutes
            ),
            has_action_link=(
                refreshed.has_action_link or record.has_action_link
            ),
            private_link_ref=(
                refreshed.private_link_ref or record.private_link_ref
            ),
            recommend_task=(
                refreshed.recommend_task or preserve_action_recommendation
            ),
            recommendation_reasons=(
                refreshed.recommendation_reasons
                or (
                    ("action_link",)
                    if preserve_action_recommendation
                    else ()
                )
            ),
            sender_scope_hash=record.sender_scope_hash,
            subject_shape_hash=record.subject_shape_hash,
        )
        saved = unresolved_store.put_pending(refreshed)
        if saved.revision > record.revision:
            refreshed_count += 1
    return refreshed_count


def _learn_from_confirmed_records(
    records: list[MailRecord],
    *,
    dictionaries,
    learning: IdentityLearningStore,
    registry: ApplicationRegistry,
    store: MarkdownTaskStore,
) -> dict[str, int]:
    tasks_by_source = {
        task.source_message_hash: task
        for task in store.all()
        if task.source_message_hash
        and task.source_message_hash != "manual"
        and not task.deleted_at
        and not task.tombstoned
        and task.application_key
    }
    learned = conflicts = eligible = 0
    for mail_record in records:
        source_hash, legacy_hash = _record_source_hashes(mail_record)
        task = tasks_by_source.get(source_hash)
        if not task:
            legacy_task = (
                tasks_by_source.get(legacy_hash) if legacy_hash else None
            )
            if (
                legacy_task
                and _source_hash_from_locator(legacy_task.mail_locator) == source_hash
            ):
                task = legacy_task
        if not task or not task.application_key:
            continue
        application = registry.raw_load(task.application_key)
        if (
            not application
            or not application.confirmed_by_user
            or not application.identity_locked
            or application.deleted_at
            or application.merged_into
        ):
            continue
        try:
            parsed = parse_record(mail_record, dictionaries)
        except Exception as exc:
            LOGGER.warning(
                "学习样本解析失败，已跳过 uid=%s error=%s",
                mail_record.uid,
                type(exc).__name__,
            )
            continue
        if not parsed:
            continue
        eligible += 1
        sender_hash, shape_hash = learning.scope(
            sender=mail_record.sender,
            subject=mail_record.subject,
            company=parsed.company,
            role=parsed.role,
        )
        rule = learning.learn(
            sender_scope_hash=sender_hash,
            subject_shape_hash=shape_hash,
            original_company=parsed.company,
            original_role=parsed.role,
            corrected_company=application.company,
            corrected_role=application.role,
        )
        if rule:
            if rule.conflict:
                conflicts += 1
            else:
                learned += 1
    return {"eligible": eligible, "learned": learned, "conflicts": conflicts}


def bootstrap_identity_learning(
    settings: Settings,
    *,
    days: int = 30,
    rebuild: bool = False,
    runtime_control: RuntimeControl | None = None,
) -> dict[str, int]:
    """Learn only from recent mail already linked to user-confirmed applications."""
    ensure_directories()
    records = ImapReader(
        settings,
        load_credential(),
        runtime_control=runtime_control,
    ).fetch_since(max(1, min(90, days)))
    dictionaries = load_identity_dictionaries(DICTIONARIES_DIR)
    learning = IdentityLearningStore(DICTIONARIES_DIR / "manual")
    if rebuild:
        learning.clear()
    return _learn_from_confirmed_records(
        records,
        dictionaries=dictionaries,
        learning=learning,
        registry=ApplicationRegistry(APPLICATIONS_DIR),
        store=MarkdownTaskStore(TASKS_DIR),
    )


def scan_once(
    settings: Settings,
    *,
    days: int | None = None,
    shadow: bool = False,
    identity_preview: bool = False,
    force_reprocess: bool = False,
    recheck_pending: bool = False,
    runtime_control: RuntimeControl | None = None,
) -> ScanSummary:
    report_progress(runtime_control, "preparing")
    try:
        summary = _scan_once_impl(
            settings, days=days, shadow=shadow, identity_preview=identity_preview,
            force_reprocess=force_reprocess, recheck_pending=recheck_pending,
            runtime_control=runtime_control,
        )
    except RuntimeStopping:
        report_progress(runtime_control, "cancelled")
        raise
    except Exception:
        # Never expose exception text, message fields or credentials in progress.
        report_progress(runtime_control, "error")
        raise
    report_progress(runtime_control, "partial" if summary.fetch_failed or summary.parse_failed else "done")
    return summary


def _scan_once_impl(
    settings: Settings,
    *,
    days: int | None = None,
    shadow: bool = False,
    identity_preview: bool = False,
    force_reprocess: bool = False,
    recheck_pending: bool = False,
    runtime_control: RuntimeControl | None = None,
) -> ScanSummary:
    if recheck_pending and (type(days) is not int or not 1 <= days <= 365):
        raise ValueError("重新识别必须指定 1–365 天的范围。")
    if identity_preview:
        return _scan_identity_preview(settings, days=days)
    ensure_directories()
    store = MarkdownTaskStore(TASKS_DIR)
    local_root = TASKS_DIR.parent
    applications_dir = local_root / "applications"
    unresolved_dir = local_root / "unresolved"
    store.backfill_completed_times()
    state = StateStore(STATE_DB)
    unresolved_store = UnresolvedStore(unresolved_dir)
    scan_parser_version = parser_version_for_settings(settings)
    parser_changed = state.parser_version_changed(scan_parser_version)
    parser_replay_cutoff = datetime.now(SHANGHAI) - timedelta(
        days=days if recheck_pending else PARSER_REPLAY_DAYS
    )
    replay_debt = state.has_parser_replay_debt(
        scan_parser_version,
        replay_cutoff=parser_replay_cutoff,
    ) or _pending_review_replay_debt(
        state,
        unresolved_store,
        scan_parser_version,
        parser_replay_cutoff,
    )
    run_id = state.begin_scan()
    fetched = skipped = candidates = updated = parse_failed = queued = urgent = exported = 0
    searched = fetch_failed = filtered_count = 0
    reviews_created = reviews_updated = 0
    effective_days = None
    uidvalidity: str | None = None
    uidnext: str | None = None
    migration_scopes: tuple[MailboxScope, ...] = ()
    fact_lease = None
    preview: list[dict[str, object]] = []
    coverage_key = None
    coverage_start = None
    coverage_until = None
    retry_incomplete = False
    try:
        # One immutable settings/credential snapshot is used for scope and login.
        credential = load_credential()
        coverage_key = _scan_scope_key(settings, credential.email)
        coverage_until = datetime.now(SHANGHAI)
        baseline_since = coverage_until - timedelta(days=max(INITIAL_LOOKBACK_DAYS, settings.lookback_days))
        coverage = (state.scan_coverage(coverage_key) if shadow else
                    state.prepare_scan_coverage(coverage_key, baseline_since))
        retry_incomplete = bool(coverage and coverage.pending_since is not None)
        effective_days = _effective_lookback_days(
            settings,
            state,
            days,
            parser_changed=parser_changed,
            replay_debt=replay_debt,
            coverage=coverage,
            scoped=True,
            now=coverage_until,
        )
        coverage_start = coverage_until - timedelta(days=effective_days)
        if retry_incomplete:
            parser_replay_cutoff = coverage_start
        if not shadow:
            attempt_start = coverage_start
            if coverage and coverage.pending_since is not None:
                # Keep the original retry anchor. Rounding an automatic retry
                # to whole days must not expand the debt by a day every poll.
                requested_window = days if days is not None else max(
                    settings.lookback_days,
                    INITIAL_LOOKBACK_DAYS if parser_changed or replay_debt else 1,
                )
                attempt_start = min(coverage.pending_since,
                                    coverage_until - timedelta(days=requested_window))
            state.begin_coverage_attempt(coverage_key, attempt_start)
        report_progress(runtime_control, "connecting", lookback_days=effective_days)
        reader = ImapReader(
            settings,
            credential,
            runtime_control=runtime_control,
        )
        # Match the persisted boundary exactly, including the first 30-day scan.
        reader.scan_started_at = coverage_until
        batch = _fetch_scan_batch(reader, effective_days)
        records = list(batch.records)
        report_progress(runtime_control, "preparing_records")
        searched = batch.searched
        fetch_failed = batch.fetch_failures
        uidvalidity = batch.uidvalidity or None
        uidnext = batch.uidnext
        records_by_uid = {record.uid: record for record in records}
        if not shadow:
            for uid in batch.searched_uids:
                locator = _batch_locator(batch, uid)
                source_hash = _source_hash_from_locator(locator)
                if not locator or not source_hash:
                    continue
                record = records_by_uid.get(uid)
                state.record_seen(
                    source_hash,
                    locator=locator,
                    internal_date=record.internal_date if record else None,
                )
            for uid in batch.fetch_failed_uids:
                locator = _batch_locator(batch, uid)
                source_hash = _source_hash_from_locator(locator)
                if locator and source_hash:
                    state.record_outcome(
                        source_hash,
                        "fetch_failed",
                        locator=locator,
                    )
            for uid in batch.parse_failed_uids:
                locator = _batch_locator(batch, uid)
                source_hash = _source_hash_from_locator(locator)
                if locator and source_hash:
                    state.record_outcome(
                        source_hash,
                        "parse_failed",
                        locator=locator,
                    )
        parse_failed += len(batch.parse_failed_uids)
        if not shadow:
            fact_lease = data_directory_lease(local_root / ".data.lock")
            fact_lease.__enter__()
            if settings.obsidian_enabled:
                import_checked_states(settings.obsidian_output, store)
        registry = ApplicationRegistry(applications_dir)
        deleted_source_hashes = {
            source_hash
            for application in registry.all(
                ignore_invalid=True,
                include_merged=True,
                include_deleted=True,
            )
            if application.deleted_at
            for source_hash in application.deleted_source_hashes
        }
        dictionaries = load_identity_dictionaries(DICTIONARIES_DIR)
        learning = IdentityLearningStore(DICTIONARIES_DIR / "manual")
        if not shadow:
            for review in unresolved_store.all():
                if (review.status == "ignored" and review.reason == "recruiting-marketing"
                        and review.parser_version == "2026.09.19.1"
                        and not review.resolved_task_id and not review.resolved_application_key):
                    for key in {review.id, _source_hash_from_locator(review.mail_locator)} - {None}:
                        state.migrate_automatic_filter(key)
                    unresolved_store.save(replace(review, status="filtered", revision=review.revision + 1))
        review_records = {record.id: record for record in unresolved_store.all()}
        reviews_by_locator = {
            locator_hash: record
            for record in review_records.values()
            if (locator_hash := _source_hash_from_locator(record.mail_locator))
        }
        batch_scope = _scope_from_batch(batch)
        if not shadow:
            migration_scopes = _prepare_uid_source_migration(
                state,
                records,
                store,
                unresolved_store,
                scopes=(batch_scope,) if batch_scope else (),
                parser_version=scan_parser_version,
            )
        known_source_facts = _known_source_facts(store, unresolved_store)
        if state.metadata("identity_learning_bootstrap_version") != scan_parser_version:
            learning_summary = _learn_from_confirmed_records(
                records,
                dictionaries=dictionaries,
                learning=learning,
                registry=registry,
                store=store,
            )
            state.set_metadata("identity_learning_bootstrap_version", scan_parser_version)
            LOGGER.info("近期已确认申请学习完成：%s", learning_summary)
        pending_events: list[tuple[object, str, str, object]] = []
        for record in track_items(runtime_control, "parsing", records, total=len(records)):
            if runtime_control:
                runtime_control.raise_if_stopping()
            fetched += 1
            source_hash, legacy_source_hash = _record_source_hashes(record)
            record_internal_date = record.internal_date
            if record_internal_date and record_internal_date.tzinfo is None:
                record_internal_date = record_internal_date.replace(
                    tzinfo=SHANGHAI
                )
            legacy_tombstone_matches = bool(
                legacy_source_hash
                and legacy_source_hash in deleted_source_hashes
                and _legacy_source_matches(
                    state,
                    known_source_facts,
                    legacy_source_hash,
                    source_hash,
                )
            )
            if source_hash in deleted_source_hashes or legacy_tombstone_matches:
                skipped += 1
                if not shadow:
                    state.record_outcome(
                        source_hash,
                        "tombstoned",
                        parser_version=scan_parser_version,
                        locator=record.locator(),
                        internal_date=record_internal_date,
                    )
                continue
            existing_review = review_records.get(
                source_hash
            ) or reviews_by_locator.get(source_hash)
            if existing_review is None and legacy_source_hash:
                legacy_review = review_records.get(legacy_source_hash)
                if (
                    legacy_review is not None
                    and _source_hash_from_locator(legacy_review.mail_locator)
                    == source_hash
                ):
                    existing_review = legacy_review
            review_hash = existing_review.id if existing_review else source_hash
            known_fact = _known_source_fact(
                known_source_facts,
                source_hash,
                legacy_source_hash,
            )
            review_fact_outcome = (
                _review_outcome(existing_review.status)
                if existing_review
                else None
            )
            if known_fact and (
                review_fact_outcome is None
                or _FACT_PRIORITY.get(known_fact[0], 0)
                > _FACT_PRIORITY.get(review_fact_outcome, 0)
            ):
                skipped += 1
                if not shadow:
                    fact_outcome, task_id = known_fact
                    state.record_outcome(
                        source_hash,
                        fact_outcome,  # type: ignore[arg-type]
                        parser_version=scan_parser_version,
                        task_id=task_id,
                        locator=record.locator(),
                        internal_date=record_internal_date,
                    )
                continue
            current_outcome = state.outcome(source_hash)
            replay_pending_review = bool(
                existing_review
                and existing_review.status in {"pending", "filtered"}
                and not existing_review.manual_restore
                and not (
                    current_outcome
                    and current_outcome.outcome
                    in {"resolved", "ignored", "tombstoned"}
                )
                and (((recheck_pending or retry_incomplete) and record_internal_date is not None
                      and record_internal_date >= parser_replay_cutoff) or state.parser_replay_due(
                    existing_review.parser_version,
                    scan_parser_version,
                    internal_date=record_internal_date,
                    replay_cutoff=parser_replay_cutoff,
                ))
            )
            if existing_review and not replay_pending_review:
                skipped += 1
                if not shadow:
                    state.record_outcome(
                        source_hash,
                        _review_outcome(existing_review.status),  # type: ignore[arg-type]
                        parser_version=(
                            existing_review.parser_version or scan_parser_version
                        ),
                        task_id=existing_review.resolved_task_id,
                        locator=record.locator(),
                        internal_date=record_internal_date,
                    )
                continue
            if not replay_pending_review and not state.should_process(
                source_hash,
                parser_version=scan_parser_version,
                parser_changed=parser_changed,
                manual_retry=force_reprocess,
                internal_date=record_internal_date,
                replay_cutoff=parser_replay_cutoff,
            ):
                skipped += 1
                continue
            try:
                event = parse_for_settings(record, dictionaries, settings)
                if event:
                    event = learning.enrich(
                        event,
                        sender=record.sender,
                        subject=record.subject,
                    )
            except Exception as exc:
                # A malformed template must not block every later message.
                # Preserve a retryable no-fact outcome for a later parser.
                parse_failed += 1
                LOGGER.warning(
                    "邮件解析失败，已隔离 uid=%s hash=%s error=%s",
                    record.uid,
                    source_hash[:12],
                    type(exc).__name__,
                )
                if not shadow:
                    state.record_outcome(
                        source_hash,
                        "parse_failed",
                        parser_version=scan_parser_version,
                        locator=record.locator(),
                        internal_date=record_internal_date,
                    )
                continue
            if event:
                candidates += 1
                pending_events.append((event, review_hash, source_hash, record))
            elif not shadow:
                diagnostics = parser_diagnostics(record)
                LOGGER.info(
                    "邮件未识别 uid=%s hash=%s diagnostics=%s",
                    record.uid,
                    source_hash[:12],
                    diagnostics,
                )
                outcome = (
                    "fetch_failed"
                    if record.content_truncated
                    else "noncandidate"
                )
                if record.content_truncated:
                    fetch_failed += 1
                elif diagnostics["recruiting_marketing"]:
                    filtered_count += 1
                    outcome = "filtered"
                    if existing_review is None:
                        filtered = unresolved_store.put_filtered(filtered_from_mail(
                            source_hash, record, parser_version=scan_parser_version,
                        ))
                        review_records[filtered.id] = filtered
                        reviews_by_locator[source_hash] = filtered
                if (
                    existing_review
                    and existing_review.status in {"pending", "filtered"}
                    and not record.content_truncated
                    and diagnostics["recruiting_marketing"]
                ):
                    filtered = unresolved_store.filter_marketing(
                        existing_review.id, parser_version=scan_parser_version,
                    )
                    review_records[filtered.id] = filtered
                    reviews_by_locator[source_hash] = filtered
                    outcome = "filtered"
                state.record_outcome(
                    source_hash,
                    outcome,
                    parser_version=scan_parser_version,
                    locator=record.locator(),
                    internal_date=record_internal_date,
                )

        # One-time compatibility bootstrap: an existing ledger may seed an
        # empty registry, but established application state is never replaced
        # by subsequent Markdown edits.
        report_progress(runtime_control, "saving")
        if (
            not shadow
            and settings.progress_source
            and not registry.all(ignore_invalid=True)
        ):
            registry.import_progress(settings.progress_source)
        if not shadow:
            _backfill_task_application_keys(registry, store)
        private_links = PrivateLinkStore(local_root / "private-links.json")
        try:
            activity_store: ActivityStore | None = ActivityStore(
                local_root / "activity-state.json"
            )
        except (OSError, RuntimeError) as exc:
            activity_store = None
            LOGGER.warning("未读状态暂时不可用，邮件扫描继续：%s", exc)
        applications = (
            _resolution_applications(settings)
            if shadow
            else registry.active(ignore_invalid=True)
        )
        decisions = resolve_event_batch(
            [item[0] for item in pending_events],
            applications,
            dictionaries,
        )
        identity_matched = identity_new = identity_unresolved = identity_conflicts = 0
        for (event, source_hash, canonical_source_hash, mail_record), decision in zip(
            pending_events,
            decisions,
            strict=True,
        ):
            if shadow:
                preview.append(decision.to_preview())
                continue
            recommended_application = (
                registry.load(decision.application_key)
                if decision.action in {"matched", "batch_context_match"}
                and decision.application_key
                else None
            )
            previous_stage = (
                recommended_application.manual_stage
                if recommended_application
                else None
            )
            stage_advanced = bool(
                recommended_application
                and is_stage_advance(previous_stage, event.stage)
            )
            try:
                private_link_ref = private_links.put(
                    source_hash,
                    event.source_url,
                )
            except (OSError, RuntimeError) as exc:
                private_link_ref = None
                LOGGER.warning("邮件链接安全保存失败，仍保留待处理记录：%s", exc)
            pending = unresolved_from_decision(
                source_hash,
                decision,
                mail_record.locator(),
                parser_version=scan_parser_version,
                private_link_ref=private_link_ref,
            )
            reasons = list(pending.recommendation_reasons)
            if stage_advanced:
                reasons.append("stage_advanced")
            pending = replace(
                pending,
                previous_stage=previous_stage,
                stage_advanced=stage_advanced,
                recommend_task=bool(reasons),
                recommendation_reasons=tuple(dict.fromkeys(reasons)),
            )
            existing_pending = unresolved_store.load(source_hash)
            saved_pending = unresolved_store.put_pending(pending)
            if existing_pending is None:
                reviews_created += 1
            elif saved_pending.revision > existing_pending.revision:
                reviews_updated += 1
            review_records[source_hash] = saved_pending
            reviews_by_locator[canonical_source_hash] = saved_pending
            if activity_store is not None and (
                existing_pending is None
                or saved_pending.revision > existing_pending.revision
            ):
                try:
                    activity_store.record_event(
                        dedup_key=f"review:{source_hash}:r{saved_pending.revision}",
                        kind=(
                            "review.created"
                            if existing_pending is None
                            else "review.changed"
                        ),
                        tabs=("today", "review"),
                        company=saved_pending.company,
                        entity_id=f"review:{source_hash}",
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    LOGGER.warning("待处理已保存，但未读活动记录失败：%s", exc)
            if decision.action in {"matched", "batch_context_match"}:
                identity_matched += 1
            elif decision.action == "new_application":
                identity_new += 1
            elif decision.action == "conflict":
                identity_conflicts += 1
            else:
                identity_unresolved += 1
            state.record_outcome(
                canonical_source_hash,
                "pending",
                parser_version=scan_parser_version,
                locator=mail_record.locator(),
                internal_date=mail_record.internal_date,
            )
        if not shadow:
            # Retry derived badge cleanup even if a previous scan stopped after
            # saving the filtered review. Do not acknowledge unrelated mail.
            if activity_store is not None:
                for review in unresolved_store.all():
                    if review.status == "filtered" and review.reason == "recruiting-marketing":
                        try:
                            activity_store.dismiss_filtered_review(review.id)
                        except (OSError, ValueError, RuntimeError):
                            LOGGER.warning("已过滤宣传邮件，未读角标将在下次扫描重试刷新")
            tasks = store.all()
            synchronize_research_state(
                tasks,
                settings.research_queue,
                store,
            )
            tasks = store.all()
            now = datetime.now().astimezone()
            for task in tasks:
                if task.application_paused:
                    continue
                expires_at = task.end_at or task.deadline_at or task.start_at
                expired_now = False
                if (
                    expires_at
                    and expires_at < now
                    and task.status
                    not in {"done", "cancelled", "irrelevant", "expired"}
                ):
                    task.status = "expired"
                    task.updated_at = now
                    store.save(task)
                    expired_now = True
                elif _is_stale_attention(task, now):
                    task.status = "expired"
                    task.updated_at = now
                    store.save(task)
                    expired_now = True
                if expired_now and activity_store is not None:
                    tabs = ["today", "week", "month", "list"]
                    if task.application_key:
                        tabs.append("progress")
                    try:
                        activity_store.record_event(
                            dedup_key=(
                                f"task:{task.id}:expired:"
                                f"{int(now.timestamp() * 1_000_000)}"
                            ),
                            kind="task.expired",
                            tabs=tuple(tabs),
                            company=task.company if task.application_key else None,
                            entity_id=f"task:{task.id}",
                        )
                    except (OSError, ValueError, RuntimeError) as exc:
                        LOGGER.warning("任务状态已更新，但未读活动记录失败：%s", exc)
            tasks = store.all()
            reconcile_all_applications(registry, store)
            tasks = store.all()
            report_progress(runtime_control, "exporting")
            export_dashboard(tasks, DASHBOARD_FILE, settings)
            if settings.obsidian_enabled:
                exported = export_dashboard(
                    tasks,
                    settings.obsidian_output,
                    settings,
                )
            if settings.progress_enabled:
                export_progress(
                    tasks,
                    settings.progress_output,
                    source_path=settings.progress_source,
                )
            if activity_store is not None:
                try:
                    set_dock_badge(activity_store.unique_unread_count())
                except (OSError, RuntimeError):
                    LOGGER.debug("暂时无法刷新 Dock 未读数字", exc_info=True)
        summary = ScanSummary(
            fetched=fetched,
            skipped=skipped,
            candidates=candidates,
            tasks_updated=updated,
            parse_failed=parse_failed,
            research_queued=queued,
            urgent=urgent,
            exported=exported,
            shadow=shadow,
            preview=tuple(preview),
            identity_mode="registry",
            identity_matched=(
                sum(
                    decision.action in {"matched", "batch_context_match"}
                    for decision in decisions
                )
                if shadow
                else identity_matched
            ),
            identity_new_applications=(
                sum(decision.action == "new_application" for decision in decisions)
                if shadow
                else identity_new
            ),
            identity_unresolved=(
                sum(decision.action == "unresolved" for decision in decisions)
                if shadow
                else identity_unresolved
            ),
            identity_conflicts=(
                sum(decision.action == "conflict" for decision in decisions)
                if shadow
                else identity_conflicts
            ),
            searched=searched,
            fetch_failed=fetch_failed,
            uidvalidity=uidvalidity,
            uidnext=uidnext,
            lookback_days=effective_days,
            mailbox=settings.mail_folder,
            filtered=filtered_count,
            reviews_created=reviews_created,
            reviews_updated=reviews_updated,
        )
        if fact_lease is not None:
            fact_lease.__exit__(None, None, None)
            fact_lease = None
        if runtime_control:
            runtime_control.raise_if_stopping()
        state.finish_scan(
            run_id,
            skipped=skipped,
            parse_failed=parse_failed,
            fetched=fetched,
            candidates=candidates,
            searched=searched,
            fetch_failed=fetch_failed,
            uidvalidity=uidvalidity,
            uidnext=uidnext,
            lookback_days=effective_days,
            mailbox=settings.mail_folder,
            filtered=filtered_count,
            parser_version=scan_parser_version if not shadow else None,
            source_migrations=migration_scopes if not shadow else (),
            source_identity_version=(
                SOURCE_IDENTITY_VERSION if not shadow else None
            ),
            coverage_key=coverage_key if not shadow else None,
            coverage_start=coverage_start,
            coverage_until=coverage_until,
        )
        return summary
    except Exception as exc:
        if fact_lease is not None:
            fact_lease.__exit__(type(exc), exc, exc.__traceback__)
        state.finish_scan(
            run_id,
            skipped=skipped,
            parse_failed=parse_failed,
            fetched=fetched,
            candidates=candidates,
            searched=searched,
            fetch_failed=fetch_failed,
            uidvalidity=uidvalidity,
            uidnext=uidnext,
            lookback_days=effective_days,
            mailbox=settings.mail_folder,
            filtered=filtered_count,
            error=type(exc).__name__,
        )
        raise
