"""Training set for written-exam / interview / campus-session scheduling.

`tests/golden/schedule_vectors.json` holds redacted real recruiting mail with
hand-read expected times. Every extraction change is measured against the whole
set at once so a fix for one sender cannot silently regress another.
"""

import json
from datetime import datetime
from pathlib import Path

from job_mail_desk.identity_dictionaries import load_identity_dictionaries
from job_mail_desk.models import MailRecord
from job_mail_desk.parser import parse_record


VECTORS = Path(__file__).parent / "golden" / "schedule_vectors.json"
TIME_FIELDS = ("start_at", "end_at", "deadline_at")


def load_vectors() -> list[dict]:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def parse_vector(vector: dict, dictionaries):
    return parse_record(
        MailRecord(
            uid=str(vector["name"]),
            subject=str(vector["subject"]),
            message_id=f"<{vector['name']}@example.invalid>",
            sender=str(vector["sender"]),
            received_at=datetime.fromisoformat(str(vector["received_at"])),
            body=str(vector["body"]),
        ),
        dictionaries,
    )


def describe(vector: dict, event) -> list[str]:
    """Return one line per field the parser got wrong."""
    if vector.get("filtered"):
        return [] if event is None else [f"{vector['name']}: broadcast entered review"]
    if event is None:
        return [f"{vector['name']}: treated as non-candidate ({vector['note']})"]
    problems = []
    for field in TIME_FIELDS:
        expected = (
            datetime.fromisoformat(vector[field]) if vector[field] else None
        )
        actual = getattr(event, field)
        if actual != expected:
            problems.append(
                f"{vector['name']}.{field}: expected {expected} got {actual}"
                f"  [{vector['note']}]"
            )
    if event.duration_minutes != vector["duration_minutes"]:
        problems.append(
            f"{vector['name']}.duration_minutes: "
            f"expected {vector['duration_minutes']} got {event.duration_minutes}"
        )
    if event.company != vector["company"]:
        problems.append(
            f"{vector['name']}.company: expected {vector['company']!r} "
            f"got {event.company!r} (source {event.company_source})"
        )
    return problems


def test_schedule_training_set_is_fully_recognised() -> None:
    dictionaries = load_identity_dictionaries()
    vectors = load_vectors()
    assert vectors, "the training set must not be empty"

    problems: list[str] = []
    for vector in vectors:
        problems.extend(describe(vector, parse_vector(vector, dictionaries)))

    assert not problems, (
        f"{len(problems)} schedule mismatches across {len(vectors)} vectors:\n"
        + "\n".join(problems)
    )


def test_training_set_carries_no_personal_data() -> None:
    """The corpus is real mail, so redaction is part of the contract."""
    raw = VECTORS.read_text(encoding="utf-8")
    assert "张三" not in raw
    for vector in load_vectors():
        assert vector["note"], f"{vector['name']} must record why it is labelled"
        assert vector["received_at"], vector["name"]
