"""Windows notification, tray badge and ICS calendar contracts, run on any host."""
from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from job_mail_desk import ics_calendar, macos_calendar, macos_dock, notifier, tray_badge, windows_notify
from job_mail_desk.mail_reader import imap_date
from job_mail_desk.models import JobTask
from job_mail_desk.parser import SHANGHAI


def scheduled_task(now: datetime, *, status: str = "planned") -> JobTask:
    return JobTask(
        id="a" * 24,
        application_id="b" * 20,
        company="样例公司",
        role="产品经理",
        recruiting_project=None,
        event_type="interview",
        stage="一面",
        round="一面",
        received_at=now,
        start_at=now + timedelta(minutes=90),
        end_at=now + timedelta(minutes=120),
        deadline_at=None,
        priority="high",
        status=status,  # type: ignore[arg-type]
        change_type="new",
        source_message_hash="c" * 32,
        research_status="not_queued",
        confidence=1,
        title="面试",
        action_summary="参加面试; 请提前 10 分钟, 带好身份证\n备注行",
    )


# --------------------------------------------------------------------------- notifications


@pytest.fixture
def win32(monkeypatch):
    monkeypatch.setattr(notifier.sys, "platform", "win32")
    monkeypatch.setattr(windows_notify.sys, "platform", "win32")
    monkeypatch.setattr(macos_dock.sys, "platform", "win32")
    monkeypatch.setattr(macos_calendar.sys, "platform", "win32")
    yield
    windows_notify.register_notification_sink(None)
    macos_dock.register_badge_renderer(None)


def test_windows_reminder_goes_through_the_tray_sink(win32) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    shown: list[tuple[str, str]] = []
    windows_notify.register_notification_sink(lambda title, message: shown.append((title, message)))

    assert notifier.notify_task(scheduled_task(now), 120) is True
    assert shown == [("样例公司 · 一面", "产品经理将在2小时后到期")]


def test_windows_reminder_falls_back_to_toast_when_the_sink_fails(win32, monkeypatch) -> None:
    # The developer may have run the desktop app, which registers its AppID.
    # This case explicitly covers the unregistered fallback, independent of HKCU.
    monkeypatch.setattr(windows_notify, "toast_registration_present", lambda: False)
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    calls: list[dict] = []

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, "", "")

    def broken_sink(_title, _message):
        raise RuntimeError("tray gone")

    windows_notify.register_notification_sink(broken_sink)
    monkeypatch.setattr(windows_notify.subprocess, "run", fake_run)

    assert notifier.notify_task(scheduled_task(now), 30) is True
    assert len(calls) == 1
    command = calls[0]["command"]
    assert command[0] == "powershell.exe"
    assert "-EncodedCommand" in command
    assert calls[0]["timeout"] == windows_notify.TOAST_TIMEOUT_SECONDS
    env = calls[0]["env"]
    # Reminder text travels via the environment, never spliced into the script.
    assert env["JOBMAILDESK_TOAST_TITLE"] == "样例公司 · 一面"
    assert env["JOBMAILDESK_TOAST_MESSAGE"] == "产品经理将在30分钟后到期"
    assert env["JOBMAILDESK_TOAST_APP_ID"] == windows_notify.POWERSHELL_APP_USER_MODEL_ID
    assert "样例公司" not in " ".join(command)


def test_windows_reminder_reports_failure_so_the_threshold_is_retried(win32, monkeypatch) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    windows_notify.register_notification_sink(None)

    monkeypatch.setattr(
        windows_notify.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "boom"),
    )
    assert notifier.notify_task(scheduled_task(now), 30) is False

    def raising_run(*_a, **_k):
        raise OSError("powershell missing")

    monkeypatch.setattr(windows_notify.subprocess, "run", raising_run)
    assert notifier.notify_task(scheduled_task(now), 30) is False


def test_encoded_toast_command_is_utf16_base64_powershell() -> None:
    import base64

    decoded = base64.b64decode(windows_notify.encoded_toast_command()).decode("utf-16-le")
    assert "ToastNotificationManager" in decoded
    assert "JOBMAILDESK_TOAST_TITLE" in decoded
    assert "SecurityElement]::Escape" in decoded


def test_toast_registration_is_a_noop_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(windows_notify.sys, "platform", "linux")
    assert windows_notify.ensure_toast_registration(Path("x.ico")) is False
    assert windows_notify.toast_registration_present() is False
    assert windows_notify.send_toast("t", "m") is False


def test_notify_task_off_supported_platforms_still_returns_false(monkeypatch) -> None:
    monkeypatch.setattr(notifier.sys, "platform", "linux")
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    assert notifier.notify_task(scheduled_task(now), 30) is False


# --------------------------------------------------------------------------- badge


def test_windows_badge_uses_the_registered_renderer(win32) -> None:
    seen: list[int] = []
    macos_dock.register_badge_renderer(seen.append)

    assert macos_dock.set_dock_badge(3) is True
    assert macos_dock.set_dock_badge(0) is True
    assert seen == [3, 0]


def test_windows_badge_without_renderer_is_a_noop(win32) -> None:
    macos_dock.register_badge_renderer(None)
    assert macos_dock.set_dock_badge(5) is False


def test_windows_badge_validates_counts_and_swallows_renderer_errors(win32) -> None:
    macos_dock.register_badge_renderer(lambda _count: None)
    with pytest.raises(ValueError, match="non-negative"):
        macos_dock.set_dock_badge(-1)
    with pytest.raises(ValueError):
        macos_dock.set_dock_badge(True)

    def failing(_count: int) -> None:
        raise RuntimeError("tray gone")

    macos_dock.register_badge_renderer(failing)
    assert macos_dock.set_dock_badge(2) is False


def test_render_badge_draws_only_for_positive_counts() -> None:
    base = Image.new("RGBA", (64, 64), "#f6f0e6")
    plain = tray_badge.render_badge(base, 0)
    badged = tray_badge.render_badge(base, 7)
    # The badge circle occupies the bottom-right 32x32 square; probe a point
    # on its centre row just inside the white outline, away from the glyph.
    probe = (37, 48)
    assert plain.getpixel(probe) == base.getpixel(probe)
    red, green, blue, alpha = badged.getpixel(probe)
    assert alpha == 255 and red > 180 and green < 140 and blue < 120
    assert badged.getpixel((4, 4)) == base.getpixel((4, 4))  # icon body untouched
    glyph_box = badged.crop((40, 40, 60, 60))
    assert any(pixel[:3] == (255, 255, 255) for pixel in glyph_box.getdata())
    assert tray_badge.render_badge(base, 250).size == (64, 64)


def test_badge_title_and_label_contract() -> None:
    assert tray_badge.badge_title(0) == "JobMailDesk"
    assert tray_badge.badge_title(3) == "JobMailDesk · 3 条未读"
    assert tray_badge.badge_label(99) == "99"
    assert tray_badge.badge_label(100) == "99+"
    assert len(tray_badge.badge_title(10**9)) <= tray_badge.MAX_TITLE_LENGTH


def test_tray_badge_updates_icon_image_and_title() -> None:
    class FakeIcon:
        icon = None
        title = "JobMailDesk"

    icon = FakeIcon()
    badge = tray_badge.TrayBadge(icon, Image.new("RGBA", (64, 64), "#f6f0e6"))
    badge.apply(4)
    assert isinstance(icon.icon, Image.Image)
    assert icon.title == "JobMailDesk · 4 条未读"
    badge.apply(0)
    assert icon.title == "JobMailDesk"


# --------------------------------------------------------------------------- ICS calendar


def test_ics_event_uses_stable_uid_utc_times_and_escaping() -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    task = scheduled_task(now)
    task.updated_at = datetime(2026, 8, 8, 12, tzinfo=SHANGHAI)
    content, synced, removed = ics_calendar.render_ics(
        [task], calendar_name="求职日程", now=datetime(2026, 8, 8, 4, tzinfo=timezone.utc)
    )
    assert content.endswith("\r\n") and "\n" not in content.replace("\r\n", "")
    # Unfold continuation lines before inspecting properties.
    lines = content.replace("\r\n ", "").split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-2] == "END:VCALENDAR"
    assert "X-WR-CALNAME:求职日程" in lines
    assert f"UID:jobmaildesk:{'a' * 24}" in lines
    assert "DTSTART:20260808T033000Z" in lines  # 11:30 Asia/Shanghai
    assert "DTEND:20260808T040000Z" in lines
    assert "DTSTAMP:20260808T040000Z" in lines
    assert "SUMMARY:样例公司 · 一面" in lines
    description = next(line for line in lines if line.startswith("DESCRIPTION:"))
    assert description.startswith(f"DESCRIPTION:jobmaildesk:{'a' * 24}\\n产品经理\\n")
    assert chr(92) + ";" in description and chr(92) + "," in description
    assert "LAST-MODIFIED:20260808T040000Z" in lines
    assert any(line.startswith("SEQUENCE:") for line in lines)
    assert (synced, removed) == (1, 0)


def test_ics_inactive_tasks_are_dropped_and_counted_as_removed() -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    paused = scheduled_task(now)
    paused.application_paused = True
    done = scheduled_task(now, status="done")
    content, synced, removed = ics_calendar.render_ics([paused, done], now=now)
    assert "BEGIN:VEVENT" not in content
    assert (synced, removed) == (0, 2)


def test_ics_end_only_task_gets_a_positive_thirty_minute_window() -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    task = scheduled_task(now)
    task.start_at = None
    task.end_at = now + timedelta(hours=2)
    content, _, _ = ics_calendar.render_ics([task], now=now)
    assert "DTSTART:20260808T033000Z" in content  # 11:30 local
    assert "DTEND:20260808T040000Z" in content  # 12:00 local


def test_ics_folds_long_lines_without_splitting_utf8() -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    task = scheduled_task(now)
    task.action_summary = "很长的说明" * 40
    content, _, _ = ics_calendar.render_ics([task], now=now)
    for line in content.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line
    unfolded = content.replace("\r\n ", "")
    assert "很长的说明" * 40 in unfolded
    unfolded.encode("utf-8")  # every physical line is valid UTF-8


def test_ics_sync_writes_atomically_and_reports_the_path(tmp_path) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    target = tmp_path / "JobMailDesk.ics"
    result = ics_calendar.sync_ics_calendar([scheduled_task(now)], path=target, now=now)
    assert result.status == "ok"
    assert (result.synced, result.removed) == (1, 0)
    assert result.detail == str(target)
    assert target.read_text(encoding="utf-8").startswith("BEGIN:VCALENDAR")
    assert not list(tmp_path.glob(".*tmp"))


def test_ics_sync_reports_write_errors_without_permission_denied(tmp_path) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    result = ics_calendar.sync_ics_calendar([scheduled_task(now)], path=blocker / "x.ics", now=now)
    assert result.status == "error"
    assert "x.ics" in result.detail


def test_ics_path_strips_windows_illegal_characters(tmp_path) -> None:
    assert ics_calendar.ics_path('求职:日程?*', root=tmp_path) == tmp_path / "求职_日程_.ics"
    assert ics_calendar.ics_path("...", root=tmp_path) == tmp_path / "JobMailDesk.ics"


def test_sync_macos_calendar_dispatches_to_ics_on_windows(win32, monkeypatch, tmp_path) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    monkeypatch.setattr(ics_calendar, "ics_path", lambda name, root=None: tmp_path / f"{name}.ics")
    result = macos_calendar.sync_macos_calendar([scheduled_task(now)], calendar_name="JobMailDesk")
    assert result.status == "ok"
    assert (tmp_path / "JobMailDesk.ics").exists()


def test_plan_event_matches_the_applescript_generator() -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    task = scheduled_task(now)
    plan = macos_calendar.plan_event(task)
    script, active = macos_calendar._event_script(task, "JobMailDesk")
    assert plan.active is active is True
    assert plan.marker in script and plan.title in script
    assert plan.start == task.start_at and plan.end == task.end_at


# --------------------------------------------------------------------------- IMAP date


def test_imap_since_date_is_locale_independent(monkeypatch) -> None:
    import locale

    assert imap_date(date(2026, 8, 5)) == "05-Aug-2026"
    assert imap_date(date(2026, 12, 31)) == "31-Dec-2026"
    for candidate in ("zh_CN.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8"):
        try:
            locale.setlocale(locale.LC_TIME, candidate)
        except locale.Error:
            continue
        try:
            assert imap_date(date(2026, 3, 1)) == "01-Mar-2026"
        finally:
            locale.setlocale(locale.LC_TIME, "C")
        break


# --------------------------------------------------------------------------- desktop wiring


def test_webview2_preflight_is_transparent_off_windows(monkeypatch) -> None:
    from job_mail_desk import ui_app

    monkeypatch.setattr(ui_app.sys, "platform", "linux")
    assert ui_app.webview2_runtime_version() is None
    assert ui_app.ensure_webview2_runtime() is True
    assert ui_app._webview_start_options() == {"debug": False, "private_mode": False}
    assert ui_app._calendar_backend() == "unsupported"


def test_webview2_preflight_blocks_and_points_to_the_download_on_windows(monkeypatch) -> None:
    from job_mail_desk import ui_app

    monkeypatch.setattr(ui_app.sys, "platform", "win32")
    monkeypatch.setattr(ui_app, "webview2_runtime_version", lambda: None)
    opened: list[str] = []
    monkeypatch.setattr(ui_app.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(ui_app.ctypes, "windll", None, raising=False)  # no MessageBox off Windows

    assert ui_app.ensure_webview2_runtime() is False
    assert opened == [ui_app.WEBVIEW2_DOWNLOAD_URL]

    monkeypatch.setattr(ui_app, "webview2_runtime_version", lambda: "129.0.2792.52")
    assert ui_app.ensure_webview2_runtime() is True
    monkeypatch.setenv("JOBMAILDESK_SKIP_WEBVIEW2_CHECK", "1")
    monkeypatch.setattr(ui_app, "webview2_runtime_version", lambda: None)
    assert ui_app.ensure_webview2_runtime() is True


def test_windows_webview_pins_edge_and_keeps_its_profile_in_the_data_dir(monkeypatch) -> None:
    from job_mail_desk import ui_app

    monkeypatch.setattr(ui_app.sys, "platform", "win32")
    options = ui_app._webview_start_options()
    assert options["gui"] == "edgechromium"
    assert options["private_mode"] is False
    assert Path(options["storage_path"]) == ui_app.LOCAL_ROOT / "webview2"
    assert ui_app._calendar_backend() == "ics_file"


def test_settings_ui_exposes_the_calendar_backend_and_export_folder() -> None:
    from job_mail_desk import ui_app
    from job_mail_desk.config import Settings

    ui_dir = Path(ui_app.__file__).parent / "ui"
    html = (ui_dir / "index.html").read_text(encoding="utf-8")
    javascript = (ui_dir / "app.js").read_text(encoding="utf-8")
    assert "macOS 日历" not in html
    for element_id in ("calendarBackendHint", "openCalendarExportFolder", "syncCalendarNow"):
        assert f'id="{element_id}"' in html and f'"#{element_id}"' in javascript
    assert "open_calendar_export_folder" in javascript
    assert "calendar_backend" in javascript and "calendar_export_path" in javascript

    api = ui_app.DesktopApi(Settings())
    payload = api.get_app_settings()
    assert payload["calendar_backend"] == ui_app._calendar_backend()
    assert "calendar_export_path" in payload
    status = api.get_calendar_status()
    assert status["backend"] == ui_app._calendar_backend()
    assert api.open_calendar_export_folder() is (ui_app.sys.platform == "win32")


def test_desktop_startup_registers_windows_sinks_before_the_tray_runs() -> None:
    from job_mail_desk import ui_app

    startup = Path(ui_app.__file__).read_text(encoding="utf-8").split("def _run_ui_primary", 1)[1]
    tray_block = startup.split('if sys.platform == "win32":', 1)[1]
    for hook in (
        "register_badge_renderer(TrayBadge(tray, tray_image).apply)",
        "register_notification_sink(lambda title, message: tray.notify(message, title))",
        "ensure_toast_registration(_tray_icon_path())",
    ):
        assert hook in tray_block
        assert tray_block.index(hook) < tray_block.index("threading.Thread(target=tray.run")
    assert "webview.start(**_webview_start_options())" in startup
    assert "if not ensure_webview2_runtime():" in startup


def test_toast_decode_errors_never_escape_into_the_reminder_path(win32, monkeypatch) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    windows_notify.register_notification_sink(None)

    def undecodable(*_a, **kwargs):
        assert kwargs.get("errors") == "replace" and kwargs.get("encoding") == "utf-8"
        raise UnicodeDecodeError("utf-8", b"\xcb\xf9", 0, 1, "invalid start byte")

    monkeypatch.setattr(windows_notify.subprocess, "run", undecodable)
    assert notifier.notify_task(scheduled_task(now), 30) is False
