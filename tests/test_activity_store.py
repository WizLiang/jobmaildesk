from __future__ import annotations

import json

import pytest

from job_mail_desk import activity_store
from job_mail_desk.activity_store import (
    ACTIVITY_TABS,
    ActivityStore,
    event_id_for_dedup_key,
)


def test_initial_baseline_is_empty_and_persisted(tmp_path) -> None:
    path = tmp_path / "activity.json"
    store = ActivityStore(path)

    assert store.unread_payload() == {
        "counts": {tab: 0 for tab in ACTIVITY_TABS},
        "progress_unread_companies": [],
        "snapshot_sequence": 0,
        "unique_unread_count": 0,
    }
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["schema"] == 1
    assert persisted["sequence"] == 0
    assert persisted["events"] == []


def test_failed_atomic_replace_preserves_last_complete_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "activity.json"
    store = ActivityStore(path)
    baseline = path.read_bytes()
    attempts = 0

    def fail_replace(source, destination) -> None:
        nonlocal attempts
        attempts += 1
        raise PermissionError("locked")

    monkeypatch.setattr(activity_store.os, "replace", fail_replace)
    monkeypatch.setattr(activity_store.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError, match="locked"):
        store.record_event(
            dedup_key="event:atomic-failure",
            kind="task.changed",
            tabs=("today",),
        )

    assert attempts == 5
    assert path.read_bytes() == baseline
    assert list(tmp_path.glob(".activity.json.*.tmp")) == []


def test_event_deduplication_stable_id_and_monotonic_sequence(tmp_path) -> None:
    store = ActivityStore(tmp_path / "activity.json")

    first = store.record_event(
        dedup_key="task:alpha:created",
        kind="task.created",
        company="Alpha",
        entity_id="task_alpha",
        tabs=ACTIVITY_TABS,
    )
    duplicate = store.record_event(
        dedup_key="task:alpha:created",
        kind="task.changed",
        company="Different company is ignored on replay",
        entity_id="different",
        tabs=("list",),
    )
    second = store.record_event(
        dedup_key="task:beta:created",
        kind="task.created",
        company="Beta",
        tabs=("progress",),
    )

    assert duplicate == first
    assert first.event_id == event_id_for_dedup_key(first.dedup_key)
    assert first.sequence == 1
    assert second.sequence == 2
    payload = store.unread_payload()
    assert payload["snapshot_sequence"] == 2
    assert payload["counts"] == {
        "today": 1,
        "progress": 2,
        "week": 1,
        "month": 1,
        "review": 1,
        "list": 1,
    }
    assert payload["unique_unread_count"] == 2


def test_activity_and_acknowledgements_survive_restart(tmp_path) -> None:
    path = tmp_path / "activity.json"
    first_process = ActivityStore(path)
    first_process.record_event(
        dedup_key="application:1:advanced",
        kind="application.advanced",
        company="Example",
        tabs=("progress", "list"),
    )
    first_process.acknowledge_tab("list", through_sequence=1)

    restarted = ActivityStore(path)

    assert restarted.unread_payload() == {
        "counts": {
            "today": 0,
            "progress": 1,
            "week": 0,
            "month": 0,
            "review": 0,
            "list": 0,
        },
        "progress_unread_companies": ["Example"],
        "snapshot_sequence": 1,
        "unique_unread_count": 1,
    }
    assert (
        restarted.record_event(
            dedup_key="application:1:advanced",
            kind="application.advanced",
            company="Example",
            tabs=("progress", "list"),
        ).sequence
        == 1
    )


def test_tab_ack_only_clears_events_through_rendered_snapshot(tmp_path) -> None:
    store = ActivityStore(tmp_path / "activity.json")
    store.record_event(
        dedup_key="event:before-snapshot",
        kind="task.changed",
        tabs=("today",),
    )
    rendered = store.unread_payload()
    store.record_event(
        dedup_key="event:after-snapshot",
        kind="task.changed",
        tabs=("today",),
    )

    after_ack = store.acknowledge_tab(
        "today",
        through_sequence=int(rendered["snapshot_sequence"]),
    )

    assert after_ack["snapshot_sequence"] == 2
    assert after_ack["counts"]["today"] == 1
    assert after_ack["unique_unread_count"] == 1


def test_progress_ack_clears_current_events_for_only_that_company(tmp_path) -> None:
    store = ActivityStore(tmp_path / "activity.json")
    store.record_event(
        dedup_key="acme:one",
        kind="application.changed",
        company="Acme",
        tabs=("today", "progress"),
    )
    store.record_event(
        dedup_key="acme:two",
        kind="application.changed",
        company="ACME",
        tabs=("progress",),
    )
    store.record_event(
        dedup_key="beta:one",
        kind="application.changed",
        company="Beta",
        tabs=("progress",),
    )
    before_ack = store.unread_payload()
    assert before_ack["counts"]["progress"] == 2
    assert before_ack["unique_unread_count"] == 3

    after_ack = store.acknowledge_progress_company("  acme  ")

    assert after_ack["counts"]["progress"] == 1
    assert after_ack["counts"]["today"] == 1
    assert after_ack["progress_unread_companies"] == ["Beta"]
    assert after_ack["unique_unread_count"] == 2

    store.record_event(
        dedup_key="acme:after-ack",
        kind="application.changed",
        company="Acme",
        tabs=("progress",),
    )
    assert store.unread_payload()["counts"]["progress"] == 2


def test_unique_count_is_not_the_sum_of_tab_deliveries(tmp_path) -> None:
    store = ActivityStore(tmp_path / "activity.json")
    store.record_event(
        dedup_key="one-event-many-tabs",
        kind="task.changed",
        company="Example",
        tabs=("today", "progress", "week", "month", "review", "list"),
    )

    payload = store.unread_payload()

    assert sum(payload["counts"].values()) == 6
    assert store.unique_unread_count() == 1
    assert payload["unique_unread_count"] == 1


def test_serialization_contains_only_safe_metadata(tmp_path) -> None:
    path = tmp_path / "activity.json"
    store = ActivityStore(path)
    store.record_event(
        dedup_key="mailhash:7f9a",
        kind="application.changed",
        company="Safe Company",
        entity_id="application_42",
        tabs=("progress", "review"),
    )

    serialized = path.read_text(encoding="utf-8")
    event = json.loads(serialized)["events"][0]

    assert set(event) == {
        "event_id",
        "dedup_key",
        "sequence",
        "kind",
        "company",
        "entity_id",
        "tabs",
    }
    for forbidden in (
        "body",
        "sender",
        "url",
        "locator",
        "action_text",
        "person@example.com",
        "https://private.example/token",
    ):
        assert forbidden not in serialized.casefold()
    with pytest.raises(TypeError):
        store.record_event(  # type: ignore[call-arg]
            dedup_key="unsafe-extra-field",
            kind="task.changed",
            tabs=("today",),
            body="must never be persisted",
        )


def test_progress_uses_company_ack_and_normal_tabs_require_snapshot(tmp_path) -> None:
    store = ActivityStore(tmp_path / "activity.json")

    with pytest.raises(ValueError, match="company"):
        store.record_event(
            dedup_key="missing-company",
            kind="application.changed",
            tabs=("progress",),
        )
    with pytest.raises(ValueError, match="company scope"):
        store.acknowledge_tab("progress", through_sequence=0)
