from dataclasses import replace
from datetime import datetime

import pytest

from job_mail_desk import ui_app
from job_mail_desk.config import Settings
from job_mail_desk.models import MailRecord
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.scanner import _source_hash_from_locator
from job_mail_desk.state import StateStore
from job_mail_desk.unresolved_store import UnresolvedStore, filtered_from_mail


@pytest.fixture
def filtered_desk(tmp_path, monkeypatch):
    for name, relative in {
        "LOCAL_ROOT": ".", "TASKS_DIR": "tasks", "STATE_DB": "state.db",
        "APPLICATIONS_DIR": "applications", "UNRESOLVED_DIR": "unresolved",
    }.items():
        monkeypatch.setattr(ui_app, name, tmp_path / relative)
    mail = MailRecord(
        uid="9", subject="校园宣讲会邀请", sender="services@example.invalid",
        message_id="<filtered@example.invalid>", body="Never persist this raw body",
        received_at=datetime.now(SHANGHAI), mailbox="INBOX", uidvalidity="1",
        account_fingerprint="f" * 24,
    )
    record = filtered_from_mail("legacy-filtered", mail, parser_version="fixture")
    store = UnresolvedStore(tmp_path / "unresolved")
    store.save(record)
    state = StateStore(tmp_path / "state.db")
    keys = {record.id, _source_hash_from_locator(record.mail_locator)}
    for key in keys:
        state.record_outcome(key, "filtered", locator=mail.locator())
    api = ui_app.DesktopApi(Settings(progress_enabled=False))
    return api, store, state, record, keys


def test_filtered_list_and_restore_use_minimal_payload(filtered_desk):
    api, store, state, record, keys = filtered_desk
    assert api.list_ignored_reviews() == []
    payload = api.list_filtered_reviews()
    assert len(payload) == 1
    assert set(payload[0]) == {"id", "revision", "title", "received_at", "reason_label"}
    assert api.restore_filtered_review(record.id, record.revision) == {"status": "ok"}
    restored = store.load(record.id)
    assert restored.status == "pending" and restored.manual_restore
    assert restored.revision == record.revision + 1
    assert api.list_filtered_reviews() == []
    assert all(state.outcome(key).outcome == "pending" for key in keys)


@pytest.mark.parametrize("outcome", ["resolved", "tombstoned", "ignored"])
def test_filtered_restore_rejects_protected_alias(filtered_desk, outcome):
    api, store, state, record, keys = filtered_desk
    canonical = _source_hash_from_locator(record.mail_locator)
    state.record_outcome(canonical, outcome)
    with pytest.raises(ValueError):
        api.restore_filtered_review(record.id, record.revision)
    assert store.load(record.id) == record
    assert state.outcome(record.id).outcome == "filtered"
    assert state.outcome(canonical).outcome == outcome


@pytest.mark.parametrize("status", ["ignored", "pending", "resolved"])
def test_filtered_restore_rejects_changed_status(filtered_desk, status):
    api, store, _, record, _ = filtered_desk
    store.save(replace(record, status=status))
    with pytest.raises(ValueError):
        api.restore_filtered_review(record.id, record.revision)


def test_filtered_restore_rejects_stale_revision(filtered_desk):
    api, store, _, record, _ = filtered_desk
    with pytest.raises(ValueError):
        api.restore_filtered_review(record.id, record.revision - 1)
    assert store.load(record.id) == record


@pytest.mark.parametrize("revision", [True, 1.0, "1"])
def test_filtered_restore_rejects_invalid_revision_before_index_changes(filtered_desk, revision):
    api, store, state, record, keys = filtered_desk
    with pytest.raises(ValueError):
        api.restore_filtered_review(record.id, revision)
    assert store.load(record.id) == record
    assert all(state.outcome(key).outcome == "filtered" for key in keys)
