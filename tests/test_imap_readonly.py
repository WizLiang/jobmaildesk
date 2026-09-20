from contextlib import nullcontext
from datetime import datetime, timedelta
from email.message import EmailMessage

import pytest

from job_mail_desk.config import Settings
from job_mail_desk.credentials import MailCredential
from job_mail_desk.mail_reader import MAX_MESSAGE_BYTES, ImapReader
from job_mail_desk.parser import SHANGHAI

# The reader drops anything older than the lookback window measured from
# ``datetime.now()``, so the fake server must always report a *recent*
# INTERNALDATE, computed at fetch time.  A hardcoded calendar date silently
# expires after 30 days.
_IMAP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def imap_internal_date(value: datetime) -> str:
    """Render ``value`` as an RFC 3501 INTERNALDATE (locale-independent)."""
    return (
        f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year} "
        f"{value.strftime('%H:%M:%S %z')}"
    )


def recent_internal_date() -> datetime:
    """A wall-clock-relative INTERNALDATE safely inside every lookback window."""
    return (datetime.now(SHANGHAI) - timedelta(days=1)).replace(microsecond=0)


class FakeImap:
    instance = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.readonly = None
        self.fetch_query = None
        self.id_args = None
        self.internal_date: datetime | None = None
        FakeImap.instance = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, email, code):
        return "OK", []

    def select(self, folder, readonly=False):
        self.readonly = readonly
        return "OK", [b"1"]

    def _simple_command(self, command, *args):
        assert command == "ID"
        self.id_args = args
        return "OK", [b"ID completed"]

    def response(self, name):
        if name == "UIDVALIDITY":
            return "OK", [b"777"]
        if name == "UIDNEXT":
            return "OK", [b"43"]
        return "OK", []

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"42"]
        if command == "fetch":
            self.fetch_query = args[-1]
            message = EmailMessage()
            message["Subject"] = "【样例】笔试通知"
            message["From"] = "noreply@example.invalid"
            message["Date"] = datetime.now(SHANGHAI)
            message.set_content("笔试时间待确认")
            body = message.as_bytes()
            self.internal_date = recent_internal_date()
            internal_date = imap_internal_date(self.internal_date)
            return "OK", [
                (
                    (
                        f'42 (UID 42 INTERNALDATE "{internal_date}" '
                        f"RFC822.SIZE {len(body)} BODY[]<0> {{{len(body)}}}"
                    ).encode("ascii"),
                    body,
                )
            ]
        raise AssertionError(command)


def test_imap_uses_readonly_and_body_peek(monkeypatch) -> None:
    monkeypatch.setattr("job_mail_desk.mail_reader.imaplib.IMAP4_SSL", FakeImap)
    records = ImapReader(
        Settings(),
        MailCredential("private@example.invalid", "authorization-code"),
    ).fetch_since(30)
    assert len(records) == 1
    assert FakeImap.instance.readonly is True
    assert "BODY.PEEK" in FakeImap.instance.fetch_query
    assert "INTERNALDATE" in FakeImap.instance.fetch_query
    assert "RFC822.SIZE" in FakeImap.instance.fetch_query
    assert "UID" in FakeImap.instance.fetch_query
    assert f"<0.{MAX_MESSAGE_BYTES + 1}>" in FakeImap.instance.fetch_query
    assert FakeImap.instance.timeout is not None
    assert records[0].internal_date == FakeImap.instance.internal_date
    assert records[0].locator()["uidvalidity"] == "777"


def test_imap_can_use_plain_connection_when_ssl_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr("job_mail_desk.mail_reader.imaplib.IMAP4", FakeImap)
    records = ImapReader(
        Settings(mail_ssl=False),
        MailCredential("private@example.invalid", "authorization-code"),
    ).fetch_since(30)
    assert len(records) == 1
    assert FakeImap.instance.readonly is True
    assert "BODY.PEEK" in FakeImap.instance.fetch_query


class PartialFailureImap(FakeImap):
    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"42 43"]
        if command == "fetch" and args[0] == b"43":
            return "NO", [b"temporary failure"]
        return super().uid(command, *args)


def test_fetch_batch_reports_only_safe_mailbox_telemetry(monkeypatch) -> None:
    monkeypatch.setattr(
        "job_mail_desk.mail_reader.imaplib.IMAP4_SSL",
        PartialFailureImap,
    )
    batch = ImapReader(
        Settings(),
        MailCredential("private@example.invalid", "authorization-code"),
    ).fetch_batch_since(30)

    assert batch.searched_uids == ("42", "43")
    assert batch.fetch_failed_uids == ("43",)
    assert batch.telemetry() == {
        "searched_uids": 2,
        "fetched": 1,
        "fetch_failures": 1,
        "uidvalidity": "777",
        "uidnext": "43",
    }
    serialized = repr(batch.telemetry())
    assert "private@example.invalid" not in serialized
    assert "笔试时间待确认" not in serialized
    assert "noreply" not in serialized


class MismatchedFetchUidImap(FakeImap):
    def uid(self, command, *args):
        status, data = super().uid(command, *args)
        if command != "fetch":
            return status, data
        metadata, body = data[0]
        return status, [(metadata.replace(b"UID 42", b"UID 41"), body)]


def test_fetch_batch_rejects_mismatched_returned_uid(monkeypatch) -> None:
    monkeypatch.setattr(
        "job_mail_desk.mail_reader.imaplib.IMAP4_SSL",
        MismatchedFetchUidImap,
    )

    batch = ImapReader(
        Settings(),
        MailCredential("private@example.invalid", "authorization-code"),
    ).fetch_batch_since(30)

    assert batch.records == ()
    assert batch.fetch_failed_uids == ("42",)
    assert batch.parse_failed_uids == ()


class TruncatedAndOversizeImap(FakeImap):
    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"42 43"]
        if command == "fetch" and args[0] == b"42":
            prefix = (
                b"Subject: oversized candidate\r\n"
                b"From: jobs@example.invalid\r\n"
                b"Content-Type: text/plain; charset=us-ascii\r\n\r\n"
            )
            body = prefix + b"interview invitation " + (
                b"x" * (MAX_MESSAGE_BYTES + 1)
            )
            declared_size = MAX_MESSAGE_BYTES + 100
            return "OK", [
                (
                    (
                        f"1 (UID 42 RFC822.SIZE {declared_size} "
                        f'INTERNALDATE "{imap_internal_date(recent_internal_date())}" '
                        f"BODY[]<0> {{{len(body)}}}"
                    ).encode("ascii"),
                    body,
                )
            ]
        if command == "fetch" and args[0] == b"43":
            body = b"Subject: partial\r\n\r\nincomplete"
            declared_size = len(body) + 500
            return "OK", [
                (
                    (
                        f"2 (UID 43 RFC822.SIZE {declared_size} "
                        f'INTERNALDATE "{imap_internal_date(recent_internal_date())}" '
                        f"BODY[]<0> {{{len(body)}}}"
                    ).encode("ascii"),
                    body,
                )
            ]
        raise AssertionError(command)


def test_oversize_prefix_is_bounded_and_partial_mismatch_retries(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "job_mail_desk.mail_reader.imaplib.IMAP4_SSL",
        TruncatedAndOversizeImap,
    )

    batch = ImapReader(
        Settings(),
        MailCredential("private@example.invalid", "authorization-code"),
    ).fetch_batch_since(30)

    assert batch.searched_uids == ("42", "43")
    assert len(batch.records) == 1
    assert batch.records[0].uid == "42"
    assert batch.records[0].content_truncated is True
    assert len(batch.records[0].body.encode()) <= MAX_MESSAGE_BYTES
    assert batch.fetch_failed_uids == ("43",)
    assert batch.parse_failed_uids == ()
    assert "incomplete" not in repr(batch)


class ExactLimitImap(FakeImap):
    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"42"]
        if command == "fetch":
            prefix = (
                b"Subject: exact limit\r\n"
                b"From: jobs@example.invalid\r\n"
                b"Content-Type: text/plain; charset=us-ascii\r\n"
                b"\r\n"
            )
            body = prefix + (b"x" * (MAX_MESSAGE_BYTES - len(prefix)))
            return "OK", [
                (
                    (
                        f"1 (UID 42 RFC822.SIZE {len(body)} "
                        f'INTERNALDATE "{imap_internal_date(recent_internal_date())}" '
                        f"BODY[]<0> {{{len(body)}}}"
                    ).encode("ascii"),
                    body,
                )
            ]
        raise AssertionError(command)


def test_complete_message_at_scan_limit_is_not_treated_as_partial(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "job_mail_desk.mail_reader.imaplib.IMAP4_SSL",
        ExactLimitImap,
    )

    batch = ImapReader(
        Settings(),
        MailCredential("private@example.invalid", "authorization-code"),
    ).fetch_batch_since(30)

    assert len(batch.records) == 1
    assert batch.fetch_failed_uids == ()
    assert batch.records[0].body.startswith("x")


class MissingUidvalidityImap(FakeImap):
    def response(self, name):
        return "OK", []


def test_missing_uidvalidity_stops_before_fetch(monkeypatch) -> None:
    monkeypatch.setattr(
        "job_mail_desk.mail_reader.imaplib.IMAP4_SSL",
        MissingUidvalidityImap,
    )
    reader = ImapReader(
        Settings(),
        MailCredential("private@example.invalid", "authorization-code"),
    )

    with pytest.raises(RuntimeError, match="UIDVALIDITY"):
        reader.fetch_batch_since(30)

    assert MissingUidvalidityImap.instance.fetch_query is None


def test_netease_imap_sends_client_id_before_select(monkeypatch) -> None:
    monkeypatch.setattr("job_mail_desk.mail_reader.imaplib.IMAP4_SSL", FakeImap)
    records = ImapReader(
        Settings(
            mail_provider="163",
            mail_host="imap.163.com",
        ),
        MailCredential("private@163.com", "authorization-code"),
    ).fetch_since(30)
    assert len(records) == 1
    assert FakeImap.instance.id_args is not None
    assert "JobMailDesk" in FakeImap.instance.id_args[0]
    assert FakeImap.instance.readonly is True
