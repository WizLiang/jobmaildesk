import base64
from datetime import datetime
from email.message import EmailMessage

import pytest

from job_mail_desk.config import Settings
from job_mail_desk.credentials import MailCredential
from job_mail_desk.mail_reader import (
    ImapReader,
    account_fingerprint,
    parse_message,
    parse_original_message,
    sanitize_html,
)
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.models import JobTask, ParsedEvent
from job_mail_desk.parser import SHANGHAI, parse_record
from job_mail_desk.task_service import task_from_event
from job_mail_desk.ui_app import DesktopApi


class OriginalMailImap:
    instance = None

    def __init__(self, host, port, timeout=None):
        self.readonly = None
        self.folder = None
        self.fetch_query = None
        self.timeout = timeout
        OriginalMailImap.instance = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, email, code):
        return "OK", []

    def select(self, folder, readonly=False):
        self.folder = folder
        self.readonly = readonly
        return "OK", [b"1"]

    def response(self, name):
        return "OK", [b"777"] if name == "UIDVALIDITY" else [b"43"]

    def uid(self, command, *args):
        assert command == "fetch"
        self.fetch_query = args[-1]
        message = EmailMessage()
        message["Subject"] = "面试通知"
        message["From"] = "招聘团队 <jobs@example.test>"
        message["Date"] = datetime(2026, 8, 9, 12, 0, tzinfo=SHANGHAI)
        message.set_content("请准时参加面试")
        message.add_alternative(
            '<p onclick="steal()">面试详情</p>'
            '<script>alert(1)</script>'
            '<img src="https://tracker.example/pixel.png">'
            '<a href="javascript:alert(2)">危险链接</a>',
            subtype="html",
        )
        body = message.as_bytes()
        metadata = (
            f"1 (UID 42 RFC822.SIZE {len(body)} "
            f"BODY[]<0> {{{len(body)}}}"
        ).encode("ascii")
        return "OK", [(metadata, body)]


def locator(email="private@example.test"):
    return {
        "mailbox": "INBOX",
        "uid": "42",
        "uidvalidity": "777",
        "account_fingerprint": account_fingerprint("imap.qq.com", email),
    }


def test_fetch_original_is_readonly_body_peek_and_validates_locator(monkeypatch):
    monkeypatch.setattr(
        "job_mail_desk.mail_reader.imaplib.IMAP4_SSL",
        OriginalMailImap,
    )
    reader = ImapReader(
        Settings(),
        MailCredential("private@example.test", "authorization-code"),
    )

    result = reader.fetch_original(locator())

    assert result["subject"] == "面试通知"
    assert OriginalMailImap.instance.readonly is True
    assert OriginalMailImap.instance.folder == "INBOX"
    assert "BODY.PEEK" in OriginalMailImap.instance.fetch_query
    assert "RFC822.SIZE" in OriginalMailImap.instance.fetch_query
    assert "UID" in OriginalMailImap.instance.fetch_query
    assert result["remote_images_blocked"] is True
    with pytest.raises(ValueError, match="其他邮箱账号"):
        reader.fetch_original(locator("other@example.test"))
    with pytest.raises(ValueError, match="定位信息无效"):
        reader.fetch_original({**locator(), "uid": "1 OR 2"})
    with pytest.raises(ValueError, match="定位信息无效"):
        reader.fetch_original({**locator(), "mailbox": "INBOX\r\nBAD"})
    without_uidvalidity = locator()
    without_uidvalidity.pop("uidvalidity")
    with pytest.raises(ValueError, match="定位信息无效"):
        reader.fetch_original(without_uidvalidity)


def test_uidvalidity_mismatch_is_explicit(monkeypatch):
    monkeypatch.setattr(
        "job_mail_desk.mail_reader.imaplib.IMAP4_SSL",
        OriginalMailImap,
    )
    reader = ImapReader(
        Settings(),
        MailCredential("private@example.test", "authorization-code"),
    )
    with pytest.raises(RuntimeError, match="文件夹已重建"):
        reader.fetch_original({**locator(), "uidvalidity": "999"})


def test_fetch_original_rejects_mismatched_fetch_uid(monkeypatch):
    class MismatchedUidImap(OriginalMailImap):
        def uid(self, command, *args):
            status, fetched = super().uid(command, *args)
            metadata, body = fetched[0]
            return status, [(metadata.replace(b"UID 42", b"UID 41"), body)]

    monkeypatch.setattr(
        "job_mail_desk.mail_reader.imaplib.IMAP4_SSL",
        MismatchedUidImap,
    )
    reader = ImapReader(
        Settings(),
        MailCredential("private@example.test", "authorization-code"),
    )

    with pytest.raises(RuntimeError, match="读取响应无效"):
        reader.fetch_original(locator())


def test_mime_parsing_and_html_sanitizing_blocks_active_content_and_images():
    message = EmailMessage()
    message["Subject"] = "=?utf-8?b?5rWL6K+V6YKu5Lu2?="
    message["From"] = "sender@example.test"
    message.set_content("纯文本正文")
    message.add_alternative(
        '<style>body{display:none}</style><form><input value="x"></form>'
        '<iframe src="https://evil.example"></iframe>'
        '<object data="https://evil.example"></object><embed src="https://evil.example">'
        '<meta http-equiv="refresh" content="0;url=https://evil.example">'
        '<p onmouseover="x()">安全正文</p>'
        '<img src="https://images.example/a.png">'
        '<img src="data:image/png;base64,abc">'
        '<a href="https://example.test">通知链接</a>',
        subtype="html",
    )

    blocked = parse_original_message(message.as_bytes())
    assert blocked["text"] == "纯文本正文"
    assert blocked["remote_images_blocked"] is True
    html = str(blocked["html"])
    assert "安全正文" in html
    assert "<script" not in html
    assert "<style" not in html
    assert "<form" not in html
    assert "<iframe" not in html
    assert "<object" not in html
    assert "<embed" not in html
    assert "<meta" not in html
    assert "onmouseover" not in html
    assert "href=" not in html
    assert "src=" not in html
    assert "remote-image-blocked" in html

    loaded = parse_original_message(
        message.as_bytes(),
        load_remote_images=True,
    )
    assert 'src="https://images.example/a.png"' in str(loaded["html"])
    assert "data:image" not in str(loaded["html"])
    assert loaded["remote_images_blocked"] is True


def test_scanner_extracts_safe_html_hrefs_without_persisting_html() -> None:
    message = EmailMessage()
    message["Subject"] = "【样例科技】在线笔试"
    message["From"] = "样例科技 <noreply@example.invalid>"
    message["Date"] = datetime.now(SHANGHAI)
    message.set_content("请参加在线笔试")
    message.add_alternative(
        '<p>请<a href="https://exam.example.invalid/private">进入考试</a></p>'
        '<a href="https://example.invalid/unsubscribe">退订</a>',
        subtype="html",
    )

    record = parse_message("1", message.as_bytes())

    assert "https://exam.example.invalid/private" in record.links
    assert "<a" not in record.body


def test_sanitize_html_rejects_obfuscated_javascript_and_data_urls():
    html, blocked = sanitize_html(
        '<a href=" java\nscript:alert(1)">x</a>'
        '<img src=" data:image/png;base64,abc">'
    )
    assert "javascript" not in html
    assert "data:image" not in html
    assert "href=" not in html
    assert "src=" not in html
    assert blocked is True


def test_raw_mime_html_fallback_preserves_chinese_and_drops_active_content():
    subject = base64.b64encode("【样例科技】在线笔试通知".encode()).decode()
    html = (
        "<html><head>"
        "<style>.hidden{display:none}样式泄漏词</style>"
        "<script>脚本泄漏词</script>"
        "</head><body><p><span>请参加</span><span>在线</span><span>笔试</span></p>"
        '<a href="https://portal.example.invalid/start?id=redacted">'
        "<span>进入</span><span>考试</span></a></body></html>"
    )
    encoded_html = base64.b64encode(html.encode("gb18030")).decode()
    raw = (
        f"Subject: =?x-unknown-mailer?B?{subject}?=\r\n"
        "From: jobs@example.invalid\r\n"
        "Date: Sat, 15 Aug 2026 10:00:00 +0800\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/alternative; boundary="real-mail"\r\n'
        "\r\n"
        "--real-mail\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        " \t \r\n"
        "--real-mail\r\n"
        "Content-Type: text/html; charset=x-unknown-mailer\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{encoded_html}\r\n"
        "--real-mail--\r\n"
    ).encode("ascii")

    record = parse_message("71", raw)
    original = parse_original_message(raw)

    assert "请参加在线笔试" in record.body
    assert "请参加在线笔试" in original["text"]
    assert record.subject == "【样例科技】在线笔试通知"
    assert "脚本泄漏词" not in record.body
    assert "样式泄漏词" not in record.body
    assert "脚本泄漏词" not in original["text"]
    assert "样式泄漏词" not in original["text"]
    assert record.links == (
        "https://portal.example.invalid/start?id=redacted",
    )


def test_raw_mime_message_attachment_descendants_never_enter_body():
    outer_text = base64.b64encode("外层正文保留".encode()).decode()
    nested_text = base64.b64encode("附件中的面试内容不得进入正文".encode()).decode()
    nested_html = base64.b64encode(
        (
            '<p>附件泄漏</p><a href="https://nested.example.invalid/private">'
            "进入附件考试</a>"
        ).encode()
    ).decode()
    raw = (
        "Subject: 外层通知\r\n"
        "From: jobs@example.invalid\r\n"
        "Date: Sat, 15 Aug 2026 10:00:00 +0800\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="outer"\r\n'
        "\r\n"
        "--outer\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{outer_text}\r\n"
        "--outer\r\n"
        'Content-Type: message/rfc822; name="forwarded.eml"\r\n'
        'Content-Disposition: attachment; filename="forwarded.eml"\r\n'
        "\r\n"
        "Subject: 嵌套通知\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/alternative; boundary="inner"\r\n'
        "\r\n"
        "--inner\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{nested_text}\r\n"
        "--inner\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{nested_html}\r\n"
        "--inner--\r\n"
        "--outer--\r\n"
    ).encode("utf-8")

    record = parse_message("72", raw)
    original = parse_original_message(raw)

    assert record.body.strip() == "外层正文保留"
    assert record.links == ()
    assert original["text"] == "外层正文保留"
    assert "附件泄漏" not in str(original["html"])


def test_raw_mime_plain_body_retains_safe_html_anchor_context_for_ranking():
    subject = base64.b64encode("【样例科技】在线笔试通知".encode()).decode()
    plain = base64.b64encode("请查看本次招聘安排。".encode()).decode()
    html = base64.b64encode(
        (
            '<a href="https://portal.example.invalid/home">官网</a>'
            '<a href="https://portal.example.invalid/action?id=redacted">'
            "<span>进入</span><span>考试</span></a>"
            '<a href="https://portal.example.invalid/unsubscribe">退订</a>'
        ).encode()
    ).decode()
    raw = (
        f"Subject: =?utf-8?B?{subject}?=\r\n"
        "From: jobs@example.invalid\r\n"
        "Date: Sat, 15 Aug 2026 10:00:00 +0800\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/alternative; boundary="ranking"\r\n'
        "\r\n"
        "--ranking\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{plain}\r\n"
        "--ranking\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{html}\r\n"
        "--ranking--\r\n"
    ).encode("ascii")

    record = parse_message("73", raw)
    event = parse_record(record)

    assert "进入考试 https://portal.example.invalid/action?id=redacted" in record.body
    assert event is not None
    assert event.source_url == "https://portal.example.invalid/action?id=redacted"


def test_desktop_api_reads_on_demand_without_writing_body(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tasks_dir)
    monkeypatch.setattr(
        "job_mail_desk.ui_app.load_credential",
        lambda: MailCredential("private@example.test", "authorization-code"),
    )
    captured = {}

    class Reader:
        def __init__(self, settings, credential, runtime_control=None):
            captured["credential"] = credential
            captured["runtime_control"] = runtime_control

        def fetch_original(self, mail_locator, *, load_remote_images=False):
            captured["locator"] = mail_locator
            captured["load_remote_images"] = load_remote_images
            return {
                "subject": "原邮件",
                "sender": "sender@example.test",
                "received_at": "",
                "html": "<p>仅内存正文</p>",
                "text": "仅内存正文",
                "remote_images_blocked": False,
            }

    monkeypatch.setattr("job_mail_desk.ui_app.ImapReader", Reader)
    task = JobTask(
        id="a" * 24,
        application_id="b" * 20,
        company="样例公司",
        role=None,
        recruiting_project=None,
        event_type="interview",
        stage="面试",
        round=None,
        received_at=datetime(2026, 8, 9, tzinfo=SHANGHAI),
        start_at=None,
        end_at=None,
        deadline_at=None,
        priority="normal",
        status="needs_review",
        change_type="new",
        source_message_hash="c" * 32,
        research_status="not_queued",
        confidence=1.0,
        title="面试通知",
        action_summary="查看安排",
        mail_locator=locator(),
    )
    store = MarkdownTaskStore(tasks_dir)
    store.save(task)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = DesktopApi(Settings()).get_original_mail(task.id, True)

    after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert result["text"] == "仅内存正文"
    assert captured["load_remote_images"] is True
    assert captured["runtime_control"] is not None
    assert before == after
    assert "仅内存正文" not in store.path_for(task.id).read_text(encoding="utf-8")

    task.mail_locator = None
    store.save(task)
    with pytest.raises(ValueError, match="重新扫描近期邮件可回填"):
        DesktopApi(Settings()).get_original_mail(task.id)


def test_task_from_event_persists_only_locator_metadata(tmp_path):
    event = ParsedEvent(
        company="样例公司",
        role=None,
        recruiting_project=None,
        event_type="interview",
        stage="面试",
        round=None,
        title="面试通知",
        start_at=None,
        end_at=None,
        deadline_at=None,
        source_message_id="<message@example.test>",
        source_received_at=datetime(2026, 8, 9, tzinfo=SHANGHAI),
        source_sender="sender@example.test",
        source_url=None,
        action_summary="查看面试安排",
        requirements=(),
        matched_keywords=(),
        confidence=1.0,
        change_type="new",
    )
    store = MarkdownTaskStore(tmp_path)
    task = task_from_event(event, store, mail_locator=locator())
    store.save(task)
    persisted = store.load(task.id)
    markdown = store.path_for(task.id).read_text(encoding="utf-8")

    assert persisted.mail_locator == locator()
    assert "authorization-code" not in markdown
    assert "private@example.test" not in markdown
    assert "纯文本正文" not in markdown
