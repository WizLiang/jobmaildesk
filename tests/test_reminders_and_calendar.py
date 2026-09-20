from datetime import datetime, timedelta

import job_mail_desk.macos_calendar as macos_calendar
import job_mail_desk.notifier as notifier
import job_mail_desk.scheduler as scheduler
from job_mail_desk.config import Settings
from job_mail_desk.models import JobTask
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.state import StateStore


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
        action_summary="参加面试",
    )


def test_due_reminder_is_sent_once(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    sent: list[int] = []
    monkeypatch.setattr(
        notifier,
        "notify_task",
        lambda _task, offset: sent.append(offset) or True,
    )
    state = StateStore(tmp_path / "state.db")
    settings = Settings(reminder_offsets_minutes=(1440, 120, 30))
    task = scheduled_task(now)

    assert notifier.send_due_reminders([task], settings, state, now=now) == 1
    assert notifier.send_due_reminders([task], settings, state, now=now) == 0
    assert sent == [120]


def test_done_task_does_not_send_reminder(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    monkeypatch.setattr(notifier, "notify_task", lambda *_args: True)
    assert (
        notifier.send_due_reminders(
            [scheduled_task(now, status="done")],
            Settings(),
            StateStore(tmp_path / "state.db"),
            now=now,
        )
        == 0
    )


def test_timeline_only_task_has_no_calendar_or_notification(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    task = scheduled_task(now)
    task.is_actionable = False
    sent: list[int] = []
    monkeypatch.setattr(
        notifier,
        "notify_task",
        lambda _task, offset: sent.append(offset) or True,
    )

    assert notifier.send_due_reminders(
        [task],
        Settings(),
        StateStore(tmp_path / "state.db"),
        now=now,
    ) == 0
    _script, active = macos_calendar._event_script(task, "JobMailDesk")
    assert active is False
    assert sent == []


def test_ended_application_task_has_no_calendar_or_notification(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    task = scheduled_task(now)
    task.application_paused = True
    monkeypatch.setattr(notifier, "notify_task", lambda *_args: True)

    assert notifier.send_due_reminders(
        [task],
        Settings(),
        StateStore(tmp_path / "state.db"),
        now=now,
    ) == 0
    _script, active = macos_calendar._event_script(task, "JobMailDesk")
    assert active is False


def test_calendar_script_uses_stable_marker() -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    script, active = macos_calendar._event_script(
        scheduled_task(now),
        "JobMailDesk",
    )
    assert active is True
    assert "jobmaildesk:" + "a" * 24 in script
    assert "样例公司 · 一面" in script


def test_end_only_calendar_task_has_positive_duration() -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    task = scheduled_task(now)
    task.start_at = None
    task.end_at = now + timedelta(hours=2)

    script, active = macos_calendar._event_script(task, "JobMailDesk")

    # AppleScript dates are built in the host's local time zone, so derive the
    # expected wall-clock values the same way instead of assuming UTC+8.
    expected_start = (task.end_at - timedelta(minutes=30)).astimezone()
    expected_end = task.end_at.astimezone()
    assert active is True
    assert f"set hours of eventStart to {expected_start.hour}" in script
    assert f"set minutes of eventStart to {expected_start.minute}" in script
    assert f"set hours of eventEnd to {expected_end.hour}" in script
    assert expected_end - expected_start == timedelta(minutes=30)


def test_calendar_permission_error_degrades(monkeypatch) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    monkeypatch.setattr(macos_calendar.sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_calendar,
        "_run",
        lambda _script: (False, "Not authorized (-1743)"),
    )
    result = macos_calendar.sync_macos_calendar([scheduled_task(now)])
    assert result.status == "permission_denied"


def test_scheduled_local_reminders_do_not_require_mail_credentials(
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 8, 10, tzinfo=SHANGHAI)
    task = scheduled_task(now)
    calls: list[list[JobTask]] = []
    monkeypatch.setattr(
        scheduler,
        "load_credential",
        lambda: (_ for _ in ()).throw(AssertionError("credentials are scan-only")),
    )
    monkeypatch.setattr(
        scheduler,
        "MarkdownTaskStore",
        lambda _path: type("Store", (), {"all": lambda self: [task]})(),
    )
    monkeypatch.setattr(scheduler, "StateStore", lambda _path: object())
    monkeypatch.setattr(
        scheduler,
        "send_due_reminders",
        lambda tasks, _settings, _state: calls.append(tasks) or 1,
    )

    scheduler.ScheduledJobs(Settings()).reminders()

    assert calls == [[task]]


def test_saving_a_task_does_not_wait_for_the_calendar(monkeypatch, tmp_path) -> None:
    """Calendar writes shell out per event, so they must not block a save."""
    import threading
    from dataclasses import replace

    from job_mail_desk.config import Settings
    from job_mail_desk.markdown_store import MarkdownTaskStore
    from job_mail_desk.ui_app import DesktopApi

    entered = threading.Event()
    release = threading.Event()

    def slow_sync(tasks, calendar_name=""):
        entered.set()
        release.wait(timeout=5)
        return None

    monkeypatch.setattr("job_mail_desk.ui_app.sync_macos_calendar", slow_sync)
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tmp_path / "tasks")

    api = DesktopApi(replace(Settings(), calendar_sync_enabled=True))

    api._schedule_calendar_sync(MarkdownTaskStore(tmp_path / "tasks"))

    # The caller returns while the shell-out is still in flight.
    assert entered.wait(timeout=2)
    release.set()
