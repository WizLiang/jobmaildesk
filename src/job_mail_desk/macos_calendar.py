from __future__ import annotations

import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from .models import JobTask
from .task_service import critical_time


@dataclass(frozen=True)
class CalendarSyncResult:
    status: str
    synced: int = 0
    removed: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CalendarEventPlan:
    """Platform-neutral description of what a task means for a calendar."""

    marker: str
    active: bool
    start: datetime | None
    end: datetime | None
    title: str
    notes: str


def plan_event(task: JobTask) -> CalendarEventPlan:
    """Decide activity, window, title and notes for ``task``.

    Shared by the AppleScript generator and the Windows ICS exporter so both
    platforms apply the identical rules: only actionable, unpaused, confirmed
    or planned tasks with a critical time are active; an end-only task gets a
    30-minute window ending at its deadline instead of a zero-length event.
    """
    marker = f"jobmaildesk:{task.id}"
    target = critical_time(task)
    active = (
        not task.deleted_at
        and not task.tombstoned
        and not task.application_paused
        and task.is_actionable
        and task.status in {"confirmed", "planned"}
        and target is not None
    )
    title = f"{task.company} · {task.stage}"
    notes = f"{marker}\n{task.role or '岗位待确认'}\n{task.action_summary}"
    if not active or target is None:
        return CalendarEventPlan(marker, False, None, None, title, notes)
    event_start = target
    end_at = task.end_at or target + timedelta(minutes=30)
    if (
        task.start_at is None
        and task.deadline_at is None
        and task.end_at == target
    ):
        event_start = target - timedelta(minutes=30)
        end_at = target
    return CalendarEventPlan(marker, True, event_start, end_at, title, notes)


def _apple_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _date_script(prefix: str, value) -> str:
    local = value.astimezone()
    return (
        f"set {prefix} to current date\n"
        f"set year of {prefix} to {local.year}\n"
        f"set month of {prefix} to {local.month}\n"
        f"set day of {prefix} to {local.day}\n"
        f"set hours of {prefix} to {local.hour}\n"
        f"set minutes of {prefix} to {local.minute}\n"
        f"set seconds of {prefix} to {local.second}\n"
    )


def _run(script: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (completed.stderr or completed.stdout).strip()
    return completed.returncode == 0, detail


def _event_script(task: JobTask, calendar_name: str) -> tuple[str, bool]:
    plan = plan_event(task)
    marker = plan.marker
    active = plan.active
    base = (
        'tell application "Calendar"\n'
        f"set matchingCalendars to every calendar whose name is {_apple_string(calendar_name)}\n"
        "if (count of matchingCalendars) is 0 then\n"
        f"set targetCalendar to make new calendar with properties {{name:{_apple_string(calendar_name)}}}\n"
        "else if (count of matchingCalendars) is 1 then\n"
        "set targetCalendar to item 1 of matchingCalendars\n"
        "else\n"
        'error "存在多个同名日历，请在设置中使用唯一名称。"\n'
        "end if\n"
        f"set matchedEvents to every event of targetCalendar whose description contains {_apple_string(marker)}\n"
    )
    if not active:
        return (
            base
            + "repeat with existingEvent in matchedEvents\n"
            + "delete existingEvent\nend repeat\nend tell",
            False,
        )
    event_start, end_at = plan.start, plan.end
    title, notes = plan.title, plan.notes
    body = _date_script("eventStart", event_start) + _date_script("eventEnd", end_at)
    body += (
        "if (count of matchedEvents) > 0 then\n"
        "set managedEvent to item 1 of matchedEvents\n"
        f"set summary of managedEvent to {_apple_string(title)}\n"
        "set start date of managedEvent to eventStart\n"
        "set end date of managedEvent to eventEnd\n"
        f"set description of managedEvent to {_apple_string(notes)}\n"
        "else\n"
        f"make new event at end of events of targetCalendar with properties "
        f"{{summary:{_apple_string(title)}, start date:eventStart, "
        f"end date:eventEnd, description:{_apple_string(notes)}}}\n"
        "end if\n"
    )
    return base + body + "end tell", True


def sync_macos_calendar(
    tasks: list[JobTask],
    *,
    calendar_name: str = "JobMailDesk",
) -> CalendarSyncResult:
    if sys.platform == "win32":
        # Windows has no scriptable system calendar; the ledger is exported as
        # an ICS file the user opens or subscribes to in Outlook / Calendar.
        from .ics_calendar import sync_ics_calendar

        return sync_ics_calendar(tasks, calendar_name=calendar_name)
    if sys.platform != "darwin":
        return CalendarSyncResult("unsupported", detail="仅 macOS 支持系统日历同步。")
    synced = removed = 0
    for task in tasks:
        script, active = _event_script(task, calendar_name)
        ok, detail = _run(script)
        if not ok:
            status = (
                "permission_denied"
                if "-1743" in detail or "not authorized" in detail.lower()
                else "error"
            )
            return CalendarSyncResult(status, synced, removed, detail)
        if active:
            synced += 1
        else:
            removed += 1
    return CalendarSyncResult("ok", synced, removed)
