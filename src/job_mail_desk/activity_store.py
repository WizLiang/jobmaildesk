from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .fs_utils import fchmod_private, fsync_directory

ACTIVITY_SCHEMA = 1
ACTIVITY_TABS = ("today", "progress", "week", "month", "review", "list")
NORMAL_ACTIVITY_TABS = tuple(tab for tab in ACTIVITY_TABS if tab != "progress")

_OPAQUE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_EVENT_FIELDS = {
    "event_id",
    "dedup_key",
    "sequence",
    "kind",
    "company",
    "entity_id",
    "tabs",
}
_STATE_FIELDS = {
    "schema",
    "sequence",
    "events",
    "tab_acks",
    "progress_acks",
}
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class ActivityStoreError(RuntimeError):
    """Raised when persisted activity state is invalid or unreadable."""


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """Minimal, privacy-safe metadata for one logical activity."""

    event_id: str
    dedup_key: str
    sequence: int
    kind: str
    company: str | None
    entity_id: str | None
    tabs: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "dedup_key": self.dedup_key,
            "sequence": self.sequence,
            "kind": self.kind,
            "company": self.company,
            "entity_id": self.entity_id,
            "tabs": list(self.tabs),
        }


@dataclass(slots=True)
class _ActivityState:
    sequence: int
    events: list[ActivityEvent]
    tab_acks: dict[str, int]
    progress_acks: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": ACTIVITY_SCHEMA,
            "sequence": self.sequence,
            "events": [event.to_dict() for event in self.events],
            "tab_acks": self.tab_acks,
            "progress_acks": self.progress_acks,
        }


def _empty_state() -> _ActivityState:
    return _ActivityState(
        sequence=0,
        events=[],
        tab_acks={tab: 0 for tab in NORMAL_ACTIVITY_TABS},
        progress_acks={},
    )


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.expanduser().resolve(strict=False))
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _require_sequence(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ActivityStoreError(f"{field} must be a non-negative integer")
    return value


def _require_opaque(value: object, field: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_KEY.fullmatch(value):
        raise ValueError(
            f"{field} must be a non-empty opaque identifier containing only "
            "letters, numbers, '.', '_', ':', or '-'"
        )
    return value


def _normalize_company(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("company must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized or len(normalized) > 200:
        raise ValueError("company must contain between 1 and 200 characters")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError("company must not contain control characters")
    return normalized


def _company_scope(company: str) -> str:
    return _normalize_company(company).casefold()


def _normalize_tabs(tabs: Iterable[str]) -> tuple[str, ...]:
    if isinstance(tabs, str):
        raise TypeError("tabs must be an iterable of tab names, not a string")
    try:
        requested = tuple(tabs)
    except TypeError as exc:
        raise ValueError("tabs must be an iterable of tab names") from exc
    if not requested:
        raise ValueError("at least one activity tab is required")
    if any(not isinstance(tab, str) or tab not in ACTIVITY_TABS for tab in requested):
        raise ValueError(f"tabs must be selected from {ACTIVITY_TABS}")
    if len(set(requested)) != len(requested):
        raise ValueError("tabs must not contain duplicates")
    requested_set = set(requested)
    return tuple(tab for tab in ACTIVITY_TABS if tab in requested_set)


def event_id_for_dedup_key(dedup_key: str) -> str:
    """Return the deterministic public ID for a safe, opaque deduplication key."""

    safe_key = _require_opaque(dedup_key, "dedup_key")
    return "activity1_" + sha256(safe_key.encode("utf-8")).hexdigest()


def _fsync_directory(directory: Path) -> None:
    fsync_directory(directory)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        fchmod_private(descriptor)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary_path, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (2**attempt))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _fsync_directory(path.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _event_from_dict(payload: object) -> ActivityEvent:
    if not isinstance(payload, dict) or set(payload) != _EVENT_FIELDS:
        raise ActivityStoreError("activity event contains unsupported metadata")
    try:
        dedup_key = _require_opaque(payload["dedup_key"], "dedup_key")
        event_id = _require_opaque(payload["event_id"], "event_id")
        kind = _require_opaque(payload["kind"], "kind")
        sequence = _require_sequence(payload["sequence"], "event.sequence")
        tabs_value = payload["tabs"]
        if not isinstance(tabs_value, list):
            raise TypeError("event tabs must be a list")
        tabs = _normalize_tabs(tabs_value)
        if tabs_value != list(tabs):
            raise ValueError("event tabs are not in canonical order")
        company_value = payload["company"]
        company = None if company_value is None else _normalize_company(company_value)
        if company != company_value:
            raise ValueError("event company is not normalized")
        entity_value = payload["entity_id"]
        entity_id = (
            None if entity_value is None else _require_opaque(entity_value, "entity_id")
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ActivityStoreError(f"invalid activity event: {exc}") from exc
    if sequence == 0:
        raise ActivityStoreError("event.sequence must be positive")
    if event_id != event_id_for_dedup_key(dedup_key):
        raise ActivityStoreError("activity event ID does not match its dedup key")
    if "progress" in tabs and company is None:
        raise ActivityStoreError("progress activity event is missing company scope")
    return ActivityEvent(
        event_id=event_id,
        dedup_key=dedup_key,
        sequence=sequence,
        kind=kind,
        company=company,
        entity_id=entity_id,
        tabs=tabs,
    )


def _state_from_dict(payload: object) -> _ActivityState:
    if not isinstance(payload, dict) or set(payload) != _STATE_FIELDS:
        raise ActivityStoreError("activity state contains unsupported metadata")
    if payload.get("schema") != ACTIVITY_SCHEMA:
        raise ActivityStoreError("unsupported activity state schema")
    sequence = _require_sequence(payload.get("sequence"), "sequence")

    events_value = payload.get("events")
    if not isinstance(events_value, list):
        raise ActivityStoreError("events must be a list")
    events = [_event_from_dict(item) for item in events_value]
    event_sequences = [event.sequence for event in events]
    if event_sequences != sorted(event_sequences) or len(event_sequences) != len(
        set(event_sequences)
    ):
        raise ActivityStoreError("event sequences must be unique and increasing")
    if sequence != (event_sequences[-1] if event_sequences else 0):
        raise ActivityStoreError("sequence does not match the latest activity event")
    dedup_keys = [event.dedup_key for event in events]
    if len(dedup_keys) != len(set(dedup_keys)):
        raise ActivityStoreError("activity state contains duplicate dedup keys")

    tab_acks_value = payload.get("tab_acks")
    if not isinstance(tab_acks_value, dict) or set(tab_acks_value) != set(
        NORMAL_ACTIVITY_TABS
    ):
        raise ActivityStoreError("tab_acks must contain every normal activity tab")
    tab_acks = {
        tab: _require_sequence(tab_acks_value[tab], f"tab_acks.{tab}")
        for tab in NORMAL_ACTIVITY_TABS
    }

    progress_acks_value = payload.get("progress_acks")
    if not isinstance(progress_acks_value, dict):
        raise ActivityStoreError("progress_acks must be an object")
    progress_acks: dict[str, int] = {}
    for company, acknowledged in progress_acks_value.items():
        try:
            normalized = _company_scope(company)
        except (TypeError, ValueError) as exc:
            raise ActivityStoreError(f"invalid progress company scope: {exc}") from exc
        if company != normalized:
            raise ActivityStoreError("progress company scope is not normalized")
        progress_acks[company] = _require_sequence(
            acknowledged,
            f"progress_acks.{company}",
        )

    if any(acknowledged > sequence for acknowledged in tab_acks.values()):
        raise ActivityStoreError("tab acknowledgement exceeds current sequence")
    if any(acknowledged > sequence for acknowledged in progress_acks.values()):
        raise ActivityStoreError("progress acknowledgement exceeds current sequence")
    return _ActivityState(
        sequence=sequence,
        events=events,
        tab_acks=tab_acks,
        progress_acks=progress_acks,
    )


class ActivityStore:
    """Persist and acknowledge privacy-safe unread activity deliveries."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = _path_lock(self.path)
        with self._lock:
            if self.path.exists():
                self._load()
            else:
                self._save(_empty_state())

    def _load(self) -> _ActivityState:
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ActivityStoreError(
                f"unable to read activity state from {self.path}"
            ) from exc
        return _state_from_dict(payload)

    def _save(self, state: _ActivityState) -> None:
        _atomic_write_json(self.path, state.to_dict())

    @staticmethod
    def _event_is_unread(
        event: ActivityEvent,
        tab: str,
        state: _ActivityState,
    ) -> bool:
        if event.kind == "review.filtered" or tab not in event.tabs:
            return False
        if tab == "progress":
            assert event.company is not None
            return event.sequence > state.progress_acks.get(
                _company_scope(event.company),
                0,
            )
        return event.sequence > state.tab_acks[tab]

    @classmethod
    def _unread_payload(cls, state: _ActivityState) -> dict[str, object]:
        counts = {tab: 0 for tab in ACTIVITY_TABS}
        unread_companies: dict[str, str] = {}
        unique_unread_count = 0
        for event in state.events:
            event_is_unread = False
            for tab in event.tabs:
                if not cls._event_is_unread(event, tab, state):
                    continue
                if tab != "progress":
                    counts[tab] += 1
                event_is_unread = True
                if tab == "progress":
                    assert event.company is not None
                    unread_companies[_company_scope(event.company)] = event.company
            if event_is_unread:
                unique_unread_count += 1
        counts["progress"] = len(unread_companies)
        return {
            "counts": counts,
            "progress_unread_companies": sorted(
                unread_companies.values(),
                key=str.casefold,
            ),
            "snapshot_sequence": state.sequence,
            "unique_unread_count": unique_unread_count,
        }

    def record_event(
        self,
        *,
        dedup_key: str,
        kind: str,
        tabs: Iterable[str],
        company: str | None = None,
        entity_id: str | None = None,
    ) -> ActivityEvent:
        """Record one logical event; replaying its dedup key is a no-op."""

        safe_dedup_key = _require_opaque(dedup_key, "dedup_key")
        safe_kind = _require_opaque(kind, "kind")
        safe_tabs = _normalize_tabs(tabs)
        safe_company = None if company is None else _normalize_company(company)
        safe_entity_id = (
            None if entity_id is None else _require_opaque(entity_id, "entity_id")
        )
        if "progress" in safe_tabs and safe_company is None:
            raise ValueError("company is required for progress activity")

        with self._lock:
            state = self._load()
            for existing in state.events:
                if existing.dedup_key == safe_dedup_key:
                    return existing
            sequence = state.sequence + 1
            event = ActivityEvent(
                event_id=event_id_for_dedup_key(safe_dedup_key),
                dedup_key=safe_dedup_key,
                sequence=sequence,
                kind=safe_kind,
                company=safe_company,
                entity_id=safe_entity_id,
                tabs=safe_tabs,
            )
            state.sequence = sequence
            state.events.append(event)
            self._save(state)
            return event

    def unread_payload(self) -> dict[str, object]:
        """Return one internally consistent unread snapshot for all consumers."""

        with self._lock:
            return self._unread_payload(self._load())

    def dismiss_filtered_review(self, source_hash: str) -> None:
        """Retract just this review's deliveries; keep sequence/dedup history."""
        from dataclasses import replace

        entity_id = _require_opaque(f"review:{source_hash}", "entity_id")
        with self._lock:
            state = self._load()
            changed = False
            for index, event in enumerate(state.events):
                if event.entity_id == entity_id and event.kind != "review.filtered":
                    state.events[index] = replace(event, kind="review.filtered")
                    changed = True
            if changed:
                self._save(state)

    def unique_unread_count(self) -> int:
        """Count events with at least one unread routed delivery."""

        return int(self.unread_payload()["unique_unread_count"])

    def acknowledge_tab(
        self,
        tab: str,
        *,
        through_sequence: int,
    ) -> dict[str, object]:
        """Acknowledge a normal tab only through a rendered snapshot."""

        if tab not in NORMAL_ACTIVITY_TABS:
            if tab == "progress":
                raise ValueError(
                    "progress activity must be acknowledged by company scope"
                )
            raise ValueError(f"tab must be selected from {NORMAL_ACTIVITY_TABS}")
        target = _require_sequence(through_sequence, "through_sequence")
        with self._lock:
            state = self._load()
            acknowledged = min(target, state.sequence)
            if acknowledged > state.tab_acks[tab]:
                state.tab_acks[tab] = acknowledged
                self._save(state)
            return self._unread_payload(state)

    def acknowledge_progress_company(
        self,
        company: str,
        *,
        through_sequence: int | None = None,
    ) -> dict[str, object]:
        """Acknowledge current progress deliveries for one normalized company."""

        scope = _company_scope(company)
        target = (
            None
            if through_sequence is None
            else _require_sequence(through_sequence, "through_sequence")
        )
        with self._lock:
            state = self._load()
            acknowledged = (
                state.sequence if target is None else min(target, state.sequence)
            )
            if acknowledged > state.progress_acks.get(scope, 0):
                state.progress_acks[scope] = acknowledged
                self._save(state)
            return self._unread_payload(state)
