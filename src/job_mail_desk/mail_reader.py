from __future__ import annotations

import imaplib
import hashlib
import hmac
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email import policy
from email.header import decode_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from . import __version__
from .config import Settings
from .credentials import MailCredential
from .models import MailRecord
from .runtime_control import RuntimeControl


SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_MESSAGE_BYTES = 1_000_000
MAX_ORIGINAL_MESSAGE_BYTES = 5_000_000
NETEASE_IMAP_HOSTS = {"imap.163.com", "imap.126.com", "imap.yeah.net"}
IMAP_ERROR = imaplib.IMAP4.error
IMAP_TIMEOUT_SECONDS = 15
MESSAGE_URL = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
# A shut-down socket surfaces as a protocol abort, a dead descriptor, or a NULL
# buffer view depending on where the blocked call was standing.
_TORN_SESSION_ERRORS = (IMAP_ERROR, OSError, ValueError)
LOGGER = logging.getLogger(__name__)
DROP_CONTENT_TAGS = {"script", "style", "iframe", "object", "svg", "math"}
# Keep adjacent anchors outside the parser's context window so footer and
# action-link labels cannot influence one another.
LINK_CONTEXT_BOUNDARY = "─" * 32

# NetEase requires RFC 2971 ID after LOGIN and before selecting a mailbox.
# Python's imaplib does not register this extension command by default.
imaplib.Commands.setdefault("ID", ("AUTH",))


@dataclass(frozen=True)
class MailFetchBatch:
    """Mailbox fetch result with privacy-safe counters and internal UID facts."""

    records: tuple[MailRecord, ...] = field(repr=False)
    searched_uids: tuple[str, ...] = field(repr=False)
    fetch_failed_uids: tuple[str, ...] = field(repr=False)
    parse_failed_uids: tuple[str, ...] = field(repr=False)
    account_fingerprint: str
    mailbox: str
    uidvalidity: str
    uidnext: str | None

    @property
    def searched(self) -> int:
        return len(self.searched_uids)

    @property
    def fetch_failures(self) -> int:
        return len(self.fetch_failed_uids)

    def telemetry(self) -> dict[str, str | int | None]:
        return {
            "searched_uids": self.searched,
            "fetched": len(self.records),
            "fetch_failures": self.fetch_failures,
            "uidvalidity": self.uidvalidity,
            "uidnext": self.uidnext,
        }


class _TextExtractor(HTMLParser):
    def __init__(self, *, include_link_context: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.include_link_context = include_link_context
        self.parts: list[str] = []
        self.links: list[str] = []
        self.link_contexts: list[str] = []
        self.anchor_stack: list[tuple[str | None, list[str]]] = []
        self.drop_depth = 0

    def _break(self) -> None:
        if not self.parts or self.parts[-1] != "\ue000":
            self.parts.append("\ue000")

    def _finish_anchor(
        self,
        href: str | None,
        anchor_parts: list[str],
    ) -> None:
        if not href:
            return
        anchor_text = _normalize_inline_text("".join(anchor_parts))
        context = (
            f"{anchor_text[:200]} {href} {LINK_CONTEXT_BOUNDARY}"
        ).strip()
        if context not in self.link_contexts:
            self.link_contexts.append(context)
        if self.include_link_context:
            self.parts.extend((" ", href, " ", LINK_CONTEXT_BOUNDARY, " "))

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.casefold()
        if self.drop_depth:
            if tag in DROP_CONTENT_TAGS:
                self.drop_depth += 1
            return
        if tag in DROP_CONTENT_TAGS:
            self.drop_depth = 1
            return
        if tag in TEXT_BREAK_TAGS:
            self._break()
        if tag != "a":
            return
        href = next(
            (value for name, value in attrs if name.casefold() == "href"),
            None,
        )
        safe = _safe_message_link(href) if href else None
        if safe and safe not in self.links:
            self.links.append(safe)
        self.anchor_stack.append((safe, []))

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.drop_depth:
            if tag in DROP_CONTENT_TAGS:
                self.drop_depth -= 1
            return
        if tag == "a" and self.anchor_stack:
            self._finish_anchor(*self.anchor_stack.pop())
        if tag in TEXT_BREAK_TAGS:
            self._break()

    def handle_data(self, data: str) -> None:
        if self.drop_depth:
            return
        self.parts.append(data)
        if self.anchor_stack:
            self.anchor_stack[-1][1].append(data)

    def close(self) -> None:
        super().close()
        while self.anchor_stack:
            self._finish_anchor(*self.anchor_stack.pop())


TEXT_BREAK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
CJK_RANGE = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"


def _normalize_inline_text(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    return re.sub(
        rf"(?<=[{CJK_RANGE}]) +(?=[{CJK_RANGE}])",
        "",
        normalized,
    )


def _extract_html(
    value: str,
    *,
    include_link_context: bool = False,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    extractor = _TextExtractor(include_link_context=include_link_context)
    extractor.feed(value)
    extractor.close()
    text = re.sub(r"\s+", " ", "".join(extractor.parts))
    text = re.sub(r" *\ue000 *", "\n", text)
    text = re.sub(r"\n+", "\n", text)
    text = re.sub(
        rf"(?<=[{CJK_RANGE}]) +(?=[{CJK_RANGE}])",
        "",
        text,
    ).strip()
    return text, tuple(extractor.links), tuple(extractor.link_contexts)


def _html_to_text(value: str) -> str:
    return _extract_html(value)[0]


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for content, encoding in decode_header(value):
        if isinstance(content, bytes):
            parts.append(_decode_bytes(content, encoding))
        else:
            parts.append(content)
    return "".join(parts)


def _safe_message_link(value: str) -> str | None:
    compact = re.sub(r"[\x00-\x20]+", "", value).rstrip(".,;，。；)>】")
    parsed = urlsplit(compact)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return None
    return compact


def _decode_bytes(value: bytes, declared_charset: str | None) -> str:
    charsets = [
        charset
        for charset in (
            declared_charset,
            "utf-8",
            "gb18030",
            "windows-1252",
            "latin-1",
        )
        if charset
    ]
    attempted: set[str] = set()
    for charset in charsets:
        normalized = charset.casefold()
        if normalized in attempted:
            continue
        attempted.add(normalized)
        try:
            return value.decode(charset, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")


def _decode_text_part(part: Message) -> str:
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        content = None
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        return _decode_bytes(content, part.get_content_charset())
    raw = part.get_payload(decode=True)
    if not isinstance(raw, bytes):
        payload = part.get_payload()
        if isinstance(payload, str):
            raw = payload.encode("ascii", errors="replace")
        else:
            raw = b""
    return _decode_bytes(raw, part.get_content_charset())


def _iter_body_text_parts(message: Message):
    if (
        message.get_content_disposition() == "attachment"
        or message.get_content_type() == "message/rfc822"
    ):
        return
    if message.is_multipart():
        payload = message.get_payload()
        if isinstance(payload, list):
            for child in payload:
                if isinstance(child, Message):
                    yield from _iter_body_text_parts(child)
        return
    if message.get_content_type() in {"text/plain", "text/html"}:
        yield message


def _message_text_parts(message: Message) -> tuple[list[str], list[str]]:
    plain: list[str] = []
    html: list[str] = []
    for part in _iter_body_text_parts(message):
        content_type = part.get_content_type()
        (plain if content_type == "text/plain" else html).append(
            _decode_text_part(part)
        )
    return plain, html


def _informative_length(text: str) -> int:
    """Size of the text that is neither whitespace nor a bare URL."""
    return len("".join(MESSAGE_URL.sub(" ", text or "").split()))


def _plain_part_is_authoritative(plain_text: str, html_body: str) -> bool:
    """Decide which alternative actually carries the message.

    The two halves of a multipart/alternative are supposed to say the same
    thing, but several applicant-tracking vendors ship a placeholder plain
    part such as "新面试" beside a full HTML schedule table. Trust the plain
    part only when it carries a comparable amount of text, otherwise the
    interview time would be discarded before the parser ever sees it.
    """
    if not plain_text.strip():
        return False
    html_size = _informative_length(html_body)
    if not html_size:
        return True
    return _informative_length(plain_text) * 2 >= html_size


def _message_contents(message: EmailMessage) -> tuple[str, tuple[str, ...]]:
    plain, html = _message_text_parts(message)
    plain_text = "\n".join(plain)
    html_text = "\n".join(html)
    html_body, html_links, html_link_contexts = _extract_html(
        html_text,
        include_link_context=True,
    )
    if _plain_part_is_authoritative(plain_text, html_body):
        body = plain_text
        if html_link_contexts:
            body = "\n".join((body, *html_link_contexts))
    else:
        body = html_body
    candidates = MESSAGE_URL.findall(plain_text)
    candidates.extend(html_links)
    links: list[str] = []
    for candidate in candidates:
        safe = _safe_message_link(candidate)
        if safe and safe not in links:
            links.append(safe)
    return body, tuple(links)


def account_fingerprint(host: str, email: str) -> str:
    """Return a stable account binding without persisting the address."""
    identity = f"{host.strip().casefold()}|{email.strip().casefold()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _response_value(client, name: str) -> str | None:
    try:
        _, values = client.response(name)
    except (AttributeError, IMAP_ERROR):
        return None
    if not values:
        return None
    value = values[-1]
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="replace")
    match = re.search(r"\d+", str(value))
    return match.group(0) if match else None


_IMAP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def imap_date(value: date) -> str:
    """RFC 3501 date (``DD-Mon-YYYY``) independent of the process locale.

    ``strftime("%b")`` follows ``LC_TIME``; a Chinese Windows host that has
    set the C locale would produce a month name the IMAP server rejects.
    """
    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year}"


def _internal_date_from_fetch(fetched: object) -> datetime | None:
    for item in fetched or []:
        if not isinstance(item, tuple) or not item:
            continue
        metadata = item[0]
        if isinstance(metadata, str):
            metadata = metadata.encode("ascii", errors="replace")
        if not isinstance(metadata, bytes):
            continue
        match = re.search(
            rb'INTERNALDATE\s+"([^"]+)"',
            metadata,
            re.IGNORECASE,
        )
        if not match:
            continue
        try:
            parsed = parsedate_to_datetime(
                match.group(1).decode("ascii", errors="strict")
            )
        except (TypeError, ValueError, UnicodeDecodeError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SHANGHAI)
        return parsed.astimezone(SHANGHAI)
    return None


@dataclass(frozen=True)
class _FetchedMessage:
    body: bytes = field(repr=False)
    size: int
    internal_date: datetime | None
    truncated: bool = False


class _FetchResponseError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _validated_fetched_message(
    fetched: object,
    *,
    expected_uid: str,
    max_bytes: int,
    require_internal_date: bool = False,
) -> _FetchedMessage:
    items = fetched if isinstance(fetched, (list, tuple)) else ()
    candidates = [
        item
        for item in items
        if isinstance(item, tuple)
        and len(item) > 1
        and isinstance(item[1], bytes)
    ]
    if len(candidates) != 1:
        raise _FetchResponseError("missing-or-ambiguous-body")
    candidate = candidates[0]
    metadata = candidate[0]
    if isinstance(metadata, str):
        metadata = metadata.encode("ascii", errors="replace")
    if not isinstance(metadata, bytes):
        raise _FetchResponseError("missing-metadata")

    uid_matches = re.findall(
        rb"(?:^|[\s(])UID\s+(\d+)(?=$|[\s)])",
        metadata,
        re.IGNORECASE,
    )
    if len(uid_matches) != 1:
        raise _FetchResponseError("missing-or-ambiguous-uid")
    returned_uid = uid_matches[0].decode("ascii", errors="strict")
    if int(returned_uid) != int(expected_uid):
        raise _FetchResponseError("uid-mismatch")

    size_matches = re.findall(
        rb"(?:^|[\s(])RFC822\.SIZE\s+(\d+)(?=$|[\s)])",
        metadata,
        re.IGNORECASE,
    )
    if len(size_matches) != 1:
        raise _FetchResponseError("missing-or-ambiguous-size")
    declared_size = int(size_matches[0])
    body = candidate[1]
    if not body:
        raise _FetchResponseError("empty-body")
    literal_sizes = re.findall(rb"\{(\d+)\}", metadata)
    if literal_sizes and int(literal_sizes[-1]) != len(body):
        raise _FetchResponseError("literal-size-mismatch")

    offsets = re.findall(
        rb"BODY(?:\.PEEK)?\[\]<(\d+)(?:\.\d+)?>",
        metadata,
        re.IGNORECASE,
    )
    if any(int(offset) != 0 for offset in offsets):
        raise _FetchResponseError("unexpected-partial-offset")
    truncated = declared_size > max_bytes or len(body) > max_bytes
    if len(body) > max_bytes:
        body = body[:max_bytes]
    # Some NetEase servers report RFC822.SIZE using canonical CRLF octets
    # while returning a normalized literal. The literal's own declared length
    # remains authoritative; tolerate only a small RFC822.SIZE delta.
    size_delta = abs(len(body) - declared_size)
    allowed_delta = min(4096, max(64, int(declared_size * 0.02)))
    if not truncated and size_delta > allowed_delta:
        raise _FetchResponseError("partial-or-size-mismatch")

    internal_date = _internal_date_from_fetch([candidate])
    if require_internal_date and internal_date is None:
        raise _FetchResponseError("missing-internal-date")
    return _FetchedMessage(
        body=body,
        size=declared_size,
        internal_date=internal_date,
        truncated=truncated,
    )


def parse_message(
    uid: str,
    raw: bytes,
    *,
    mailbox: str | None = None,
    uidvalidity: str | None = None,
    account_fingerprint_value: str | None = None,
    internal_date: datetime | None = None,
    content_truncated: bool = False,
) -> MailRecord:
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    try:
        received = (
            parsedate_to_datetime(parsed.get("Date"))
            if parsed.get("Date")
            else None
        )
    except (TypeError, ValueError):
        received = None
    if received is None:
        received = internal_date or datetime.now(SHANGHAI)
    elif received.tzinfo is None:
        received = received.replace(tzinfo=SHANGHAI)
    body, links = _message_contents(parsed)
    return MailRecord(
        uid=uid,
        subject=_decode_header(parsed.get("Subject")),
        message_id=str(parsed.get("Message-ID") or "").strip(),
        sender=_decode_header(parsed.get("From")),
        received_at=received.astimezone(SHANGHAI),
        body=body,
        mailbox=mailbox,
        uidvalidity=uidvalidity,
        account_fingerprint=account_fingerprint_value,
        links=links,
        internal_date=internal_date,
        content_truncated=content_truncated,
    )


SAFE_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "div", "em", "h1", "h2",
    "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre",
    "small", "span", "strong", "sub", "sup", "table", "tbody", "td", "th",
    "thead", "tr", "u", "ul",
}
VOID_TAGS = {"br", "hr", "img"}


class _SafeHtmlSanitizer(HTMLParser):
    def __init__(self, *, load_remote_images: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.load_remote_images = load_remote_images
        self.output: list[str] = []
        self.drop_depth = 0
        self.remote_images_blocked = False

    @staticmethod
    def _escape(value: str, *, attribute: bool = False) -> str:
        escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return escaped.replace('"', "&quot;") if attribute else escaped

    @staticmethod
    def _safe_link(value: str) -> str | None:
        compact = re.sub(r"[\x00-\x20]+", "", value)
        parsed = urlsplit(compact)
        if parsed.scheme.casefold() in {"http", "https", "mailto"}:
            return compact
        return None

    @staticmethod
    def _safe_image(value: str) -> str | None:
        compact = re.sub(r"[\x00-\x20]+", "", value)
        parsed = urlsplit(compact)
        return compact if parsed.scheme.casefold() == "https" and parsed.netloc else None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if self.drop_depth:
            if tag in DROP_CONTENT_TAGS:
                self.drop_depth += 1
            return
        if tag in DROP_CONTENT_TAGS:
            self.drop_depth = 1
            return
        if tag not in SAFE_TAGS:
            return
        attributes: list[str] = []
        values = {name.casefold(): value or "" for name, value in attrs}
        if tag == "img":
            source = self._safe_image(values.get("src", ""))
            if source and self.load_remote_images:
                attributes.append(f'src="{self._escape(source, attribute=True)}"')
            else:
                self.remote_images_blocked = True
                self.output.append(
                    '<span class="remote-image-blocked" data-remote-image-blocked="true">'
                    "[远程图片已阻止]</span>"
                )
                return
            alt = values.get("alt", "")
            if alt:
                attributes.append(f'alt="{self._escape(alt[:500], attribute=True)}"')
        elif tag == "a":
            # Mail-body links remain inert. The verified notification URL is
            # exposed separately through DesktopApi.open_source(task_id).
            pass
        elif tag in {"td", "th"}:
            for name in ("colspan", "rowspan"):
                value = values.get(name, "")
                if value.isdigit() and 1 <= int(value) <= 100:
                    attributes.append(f'{name}="{value}"')
        suffix = f" {' '.join(attributes)}" if attributes else ""
        self.output.append(f"<{tag}{suffix}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.drop_depth:
            if tag in DROP_CONTENT_TAGS:
                self.drop_depth -= 1
            return
        if tag in SAFE_TAGS and tag not in VOID_TAGS:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.drop_depth:
            self.output.append(self._escape(data))


def sanitize_html(
    value: str,
    *,
    load_remote_images: bool = False,
) -> tuple[str, bool]:
    sanitizer = _SafeHtmlSanitizer(load_remote_images=load_remote_images)
    sanitizer.feed(value)
    sanitizer.close()
    return "".join(sanitizer.output), sanitizer.remote_images_blocked


def parse_original_message(
    raw: bytes,
    *,
    load_remote_images: bool = False,
) -> dict[str, object]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    plain, html = _message_text_parts(message)
    raw_html = "\n".join(html)
    safe_html, blocked = sanitize_html(
        raw_html,
        load_remote_images=load_remote_images,
    )
    text = "\n".join(plain).strip()
    if not text and raw_html:
        text = _html_to_text(raw_html).strip()
    received_header = message.get("Date")
    try:
        received_value = parsedate_to_datetime(received_header) if received_header else None
    except (TypeError, ValueError):
        received_value = None
    if received_value and received_value.tzinfo is None:
        received_value = received_value.replace(tzinfo=SHANGHAI)
    received = (
        received_value.astimezone(SHANGHAI).isoformat()
        if received_value
        else _decode_header(received_header)
    )
    return {
        "subject": _decode_header(message.get("Subject")),
        "sender": _decode_header(message.get("From")),
        "received_at": received,
        "html": safe_html,
        "text": text,
        "remote_images_blocked": blocked,
    }


class ImapReader:
    def __init__(
        self,
        settings: Settings,
        credential: MailCredential,
        runtime_control: RuntimeControl | None = None,
    ) -> None:
        self.settings = settings
        self.credential = credential
        self.runtime_control = runtime_control
        self.scan_started_at: datetime | None = None

    def _client(self):
        client_type = (
            imaplib.IMAP4_SSL if self.settings.mail_ssl else imaplib.IMAP4
        )
        return client_type(
            self.settings.mail_host,
            self.settings.mail_port,
            timeout=IMAP_TIMEOUT_SECONDS,
        )

    def _check_running(self) -> None:
        if self.runtime_control:
            self.runtime_control.raise_if_stopping()

    @contextmanager
    def _connected_client(self):
        self._check_running()
        client = self._client()
        unregister = (
            self.runtime_control.register_interrupt(client.shutdown)
            if self.runtime_control
            else lambda: None
        )
        try:
            try:
                self._check_running()
                yield client
            except _TORN_SESSION_ERRORS:
                # Interrupting a blocked read closes the socket underneath the
                # standard library, which then reports a torn descriptor rather
                # than a cancellation. Restore the cancellation the caller
                # expects and let anything else surface unchanged.
                self._check_running()
                raise
            finally:
                self._close_session(client)
        finally:
            unregister()

    @staticmethod
    def _close_session(client) -> None:
        """End the session without speaking on a socket that may be gone.

        ``imaplib`` logs out by sending a command and only skips the socket
        teardown when that command fails, so an interrupted session would both
        raise from the context manager and leak its descriptor.
        """
        try:
            client.logout()
        except Exception:
            # This runs from a finally block, so a failed logout must never
            # replace whatever the caller was already reporting.
            LOGGER.debug("IMAP 会话已断开，跳过 logout", exc_info=True)
            try:
                client.shutdown()
            except Exception:
                LOGGER.debug("IMAP socket 已释放", exc_info=True)

    def _identify_netease_client(self, client) -> None:
        if self.settings.mail_host.strip().casefold() not in NETEASE_IMAP_HOSTS:
            return
        status, detail = client._simple_command(
            "ID",
            (
                '("name" "JobMailDesk" '
                f'"version" "{__version__}" '
                '"vendor" "JobMailDesk")'
            ),
        )
        if status != "OK":
            message = detail[-1] if detail else b""
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            raise RuntimeError(f"网易邮箱 IMAP 客户端身份验证失败：{message}")

    def fetch_batch_since(self, days: int | None = None) -> MailFetchBatch:
        from .scan_progress import report_progress, track_items
        lookback = days if days is not None else self.settings.lookback_days
        scan_now = self.scan_started_at or datetime.now(SHANGHAI)
        # Interactive requests remain bounded by their callers. Automatic
        # catch-up may exceed a year and must not silently discard that gap.
        if type(lookback) is not int or not 1 <= lookback < scan_now.toordinal():
            raise ValueError("邮箱扫描范围必须为有效的正整数天数。")
        cutoff = scan_now - timedelta(days=lookback)
        since_date: date = cutoff.date()
        records: list[MailRecord] = []
        searched_uids: tuple[str, ...] = ()
        fetch_failed_uids: list[str] = []
        parse_failed_uids: list[str] = []
        with self._connected_client() as client:
            client.login(
                self.credential.email,
                self.credential.authorization_code,
            )
            self._check_running()
            self._identify_netease_client(client)
            status, _ = client.select(self.settings.mail_folder, readonly=True)
            if status != "OK":
                raise RuntimeError("无法以只读方式打开邮箱文件夹。")
            uidvalidity = _response_value(client, "UIDVALIDITY")
            if (
                not uidvalidity
                or not uidvalidity.isdigit()
                or int(uidvalidity) < 1
            ):
                raise RuntimeError(
                    "邮箱未提供有效 UIDVALIDITY，无法安全识别邮件来源。"
                )
            uidnext = _response_value(client, "UIDNEXT")
            fingerprint = account_fingerprint(
                self.settings.mail_host,
                self.credential.email,
            )
            report_progress(self.runtime_control, "searching", lookback_days=lookback)
            status, data = client.uid(
                "search",
                None,
                "SINCE",
                imap_date(since_date),
            )
            if status != "OK":
                raise RuntimeError("IMAP 搜索失败。")
            raw_uids = data[0].split() if data and data[0] else []
            decoded_uids: list[str] = []
            for raw_uid in raw_uids:
                uid = (
                    raw_uid.decode("ascii", errors="strict")
                    if isinstance(raw_uid, bytes)
                    else str(raw_uid)
                )
                if not uid.isdigit() or int(uid) < 1:
                    raise RuntimeError("邮箱返回了无效 UID，扫描已安全停止。")
                decoded_uids.append(uid)
            searched_uids = tuple(decoded_uids)
            for raw_uid, uid in track_items(
                self.runtime_control, "reading", zip(raw_uids, searched_uids, strict=True),
                total=len(searched_uids),
            ):
                self._check_running()
                status, fetched = client.uid(
                    "fetch",
                    raw_uid,
                    (
                        # One extra octet plus RFC822.SIZE distinguishes a
                        # complete limit-sized message from a partial fetch.
                        "(UID INTERNALDATE RFC822.SIZE "
                        f"BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>)"
                    ),
                )
                if status != "OK":
                    fetch_failed_uids.append(uid)
                    continue
                try:
                    fetched_message = _validated_fetched_message(
                        fetched,
                        expected_uid=uid,
                        max_bytes=MAX_MESSAGE_BYTES,
                        require_internal_date=True,
                    )
                except _FetchResponseError as exc:
                    fetch_failed_uids.append(uid)
                    LOGGER.warning(
                        "IMAP FETCH 响应无效，已安排安全重试 uid=%s reason=%s",
                        uid,
                        exc.reason,
                    )
                    continue
                if (
                    fetched_message.internal_date is not None
                    and fetched_message.internal_date < cutoff
                ):
                    continue
                try:
                    records.append(
                        parse_message(
                            uid,
                            fetched_message.body,
                            mailbox=self.settings.mail_folder,
                            uidvalidity=uidvalidity,
                            account_fingerprint_value=fingerprint,
                            internal_date=fetched_message.internal_date,
                            content_truncated=fetched_message.truncated,
                        )
                    )
                except Exception as exc:
                    parse_failed_uids.append(uid)
                    LOGGER.warning(
                        "邮件 MIME 解析失败，已隔离 uid=%s error=%s",
                        uid,
                        type(exc).__name__,
                    )
        return MailFetchBatch(
            records=tuple(records),
            searched_uids=searched_uids,
            fetch_failed_uids=tuple(fetch_failed_uids),
            parse_failed_uids=tuple(parse_failed_uids),
            account_fingerprint=fingerprint,
            mailbox=self.settings.mail_folder,
            uidvalidity=uidvalidity,
            uidnext=uidnext,
        )

    def fetch_since(self, days: int | None = None) -> list[MailRecord]:
        """Compatibility API returning only successfully decoded records."""
        return list(self.fetch_batch_since(days).records)

    def fetch_original(
        self,
        locator: dict[str, str],
        *,
        load_remote_images: bool = False,
    ) -> dict[str, object]:
        mailbox = str(locator.get("mailbox") or "")
        uid = str(locator.get("uid") or "")
        expected_uidvalidity = str(locator.get("uidvalidity") or "")
        expected_fingerprint = str(locator.get("account_fingerprint") or "")
        if (
            not mailbox
            or mailbox != mailbox.strip()
            or any(ord(character) < 32 for character in mailbox)
            or not uid.isdigit()
            or int(uid) < 1
            or not expected_uidvalidity.isdigit()
            or int(expected_uidvalidity) < 1
            or not re.fullmatch(r"[0-9a-f]{24}", expected_fingerprint)
        ):
            raise ValueError("原邮件定位信息无效；请重新扫描近期邮件。")
        current_fingerprint = account_fingerprint(
            self.settings.mail_host,
            self.credential.email,
        )
        if not hmac.compare_digest(expected_fingerprint, current_fingerprint):
            raise ValueError("原邮件属于其他邮箱账号；请切换账号或重新扫描。")
        with self._connected_client() as client:
            client.login(
                self.credential.email,
                self.credential.authorization_code,
            )
            self._check_running()
            self._identify_netease_client(client)
            status, _ = client.select(mailbox, readonly=True)
            if status != "OK":
                raise RuntimeError("原邮件所在文件夹已不可用；请重新扫描近期邮件。")
            current_uidvalidity = _response_value(client, "UIDVALIDITY")
            if expected_uidvalidity and current_uidvalidity != expected_uidvalidity:
                raise RuntimeError("邮箱文件夹已重建，原邮件定位已失效；请重新扫描近期邮件。")
            status, fetched = client.uid(
                "fetch",
                uid,
                (
                    "(UID RFC822.SIZE "
                    f"BODY.PEEK[]<0.{MAX_ORIGINAL_MESSAGE_BYTES + 1}>)"
                ),
            )
            if status != "OK":
                raise RuntimeError("原邮件读取失败；邮件可能已被移动或删除。")
            try:
                fetched_message = _validated_fetched_message(
                    fetched,
                    expected_uid=uid,
                    max_bytes=MAX_ORIGINAL_MESSAGE_BYTES,
                )
            except _FetchResponseError as exc:
                if exc.reason == "message-oversize":
                    raise RuntimeError("原邮件过大，无法在应用内安全显示。") from exc
                raise RuntimeError(
                    "原邮件读取响应无效；请重新扫描近期邮件。"
                ) from exc
            result = parse_original_message(
                fetched_message.body,
                load_remote_images=load_remote_images,
            )
            result["content_truncated"] = fetched_message.truncated
            return result

    def mailbox_snapshot(self) -> dict[str, str | int | None]:
        """Read mailbox invariants without changing flags or UID state."""
        with self._connected_client() as client:
            client.login(
                self.credential.email,
                self.credential.authorization_code,
            )
            self._check_running()
            self._identify_netease_client(client)
            status, _ = client.select(self.settings.mail_folder, readonly=True)
            if status != "OK":
                raise RuntimeError("无法以只读方式打开邮箱文件夹。")
            unseen_status, unseen_data = client.uid("search", None, "UNSEEN")
            unseen = (
                len(unseen_data[0].split())
                if unseen_status == "OK" and unseen_data and unseen_data[0]
                else 0
            )

            return {
                "unseen": unseen,
                "uidvalidity": _response_value(client, "UIDVALIDITY"),
                "uidnext": _response_value(client, "UIDNEXT"),
            }
