from job_mail_desk.derived_outbox import DerivedOutbox


def test_outbox_is_idempotent_retryable_and_privacy_safe(tmp_path) -> None:
    outbox = DerivedOutbox(tmp_path / "derived-outbox.json")

    first = outbox.enqueue(
        operation_id="op1_test",
        entity_id="application_test",
        kinds=("export", "calendar", "activity"),
    )
    duplicate = outbox.enqueue(
        operation_id="op1_test",
        entity_id="application_test",
        kinds=("export", "calendar", "activity"),
    )
    assert duplicate.id == first.id
    assert len(outbox.pending()) == 1

    outbox.mark_attempt(first.id, RuntimeError("private body must not persist"))
    pending = outbox.pending()[0]
    assert pending.attempts == 1
    assert pending.last_error_type == "RuntimeError"
    assert "private body" not in outbox.path.read_text(encoding="utf-8")

    outbox.complete(first.id)
    assert outbox.pending() == []


def test_outbox_process_retries_failed_derived_work(tmp_path) -> None:
    outbox = DerivedOutbox(tmp_path / "derived-outbox.json")
    item = outbox.enqueue(
        operation_id="op1_retry",
        entity_id="application_retry",
        kinds=("export", "calendar"),
    )
    calls: list[str] = []

    def fail_once(entry) -> None:
        calls.append(entry.id)
        if len(calls) == 1:
            raise OSError("temporary")

    assert outbox.process({"export": fail_once, "calendar": fail_once}) == (0, 1)
    assert outbox.pending()[0].attempts == 1
    assert outbox.process({"export": fail_once, "calendar": fail_once}) == (1, 0)
    assert outbox.pending() == []
    assert calls[-2:] == [item.id, item.id]
