"""iCalendar export used as the Windows calendar integration.

Windows has no scriptable system calendar comparable to Calendar.app, so the
active ledger is rewritten as one RFC 5545 file that Outlook or the Windows
Calendar app can open or subscribe to. Contract preserved from the macOS
implementation (see ``macos_calendar``): one event per active task keyed by
the stable ``jobmaildesk:<task.id>`` marker (here the ``UID``), inactive
tasks disappear from the file (counted as ``removed``), end-only tasks get a
positive 30-minute duration, and the result is a ``CalendarSyncResult``.
All timestamps are written in UTC so no VTIMEZONE block is required.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .macos_calendar import CalendarEventPlan, CalendarSyncResult, plan_event
from .markdown_store import _atomic_write
from .models import JobTask

PRODUCT_ID = "-//JobMailDesk//Windows//ZH"
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_MAX_LINE_OCTETS = 75


def ics_path(calendar_name: str, root: Path | None = None) -> Path:
    """``<LOCAL_ROOT>/<calendar name>.ics`` with Windows-illegal characters removed."""
    if root is None:
        from .config import LOCAL_ROOT

        root = LOCAL_ROOT
    safe = _UNSAFE_FILENAME.sub("_", calendar_name).strip(" .") or "JobMailDesk"
    return root / f"{safe}.ics"


def _escape(value: str) -> str:
    backslash = chr(92)
    return (
        value.replace(backslash, backslash * 2)
        .replace(";", backslash + ";")
        .replace(",", backslash + ",")
        .replace("\r\n", "\n")
        .replace("\n", backslash + "n")
    )


def _fold(line: str) -> str:
    """Fold a content line at 75 octets (RFC 5545 §3.1), never inside a UTF-8 sequence."""
    if len(line.encode("utf-8")) <= _MAX_LINE_OCTETS:
        return line
    parts: list[str] = []
    current = ""
    current_octets = 0
    for character in line:
        octets = len(character.encode("utf-8"))
        limit = _MAX_LINE_OCTETS if not parts else _MAX_LINE_OCTETS - 1
        if current_octets + octets > limit:
            parts.append(current)
            current, current_octets = character, octets
        else:
            current += character
            current_octets += octets
    parts.append(current)
    return "\r\n ".join(parts)


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _event_lines(plan: CalendarEventPlan, task: JobTask, stamp: str) -> list[str]:
    assert plan.start is not None and plan.end is not None
    lines = [
        "BEGIN:VEVENT",
        f"UID:{plan.marker}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_utc(plan.start)}",
        f"DTEND:{_utc(plan.end)}",
        f"SUMMARY:{_escape(plan.title)}",
        f"DESCRIPTION:{_escape(plan.notes)}",
    ]
    if task.updated_at is not None:
        lines.append(f"LAST-MODIFIED:{_utc(task.updated_at)}")
        # A monotonically growing SEQUENCE lets Outlook treat re-imports of an
        # edited task as an update of the same UID instead of a duplicate.
        lines.append(f"SEQUENCE:{int(task.updated_at.timestamp()) % (2**31)}")
    else:
        lines.append("SEQUENCE:0")
    lines.append("END:VEVENT")
    return lines


def render_ics(
    tasks: list[JobTask],
    *,
    calendar_name: str = "JobMailDesk",
    now: datetime | None = None,
) -> tuple[str, int, int]:
    """Render the calendar text; returns ``(content, synced, removed)``."""
    stamp = _utc(now or datetime.now(timezone.utc))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODUCT_ID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
    ]
    synced = removed = 0
    for task in tasks:
        plan = plan_event(task)
        if not plan.active:
            removed += 1
            continue
        lines.extend(_event_lines(plan, task, stamp))
        synced += 1
    lines.append("END:VCALENDAR")
    content = "\r\n".join(_fold(line) for line in lines) + "\r\n"
    return content, synced, removed


def sync_ics_calendar(
    tasks: list[JobTask],
    *,
    calendar_name: str = "JobMailDesk",
    path: Path | None = None,
    now: datetime | None = None,
) -> CalendarSyncResult:
    """Rewrite the ICS file atomically; ``detail`` carries the file location."""
    target = path or ics_path(calendar_name)
    try:
        content, synced, removed = render_ics(tasks, calendar_name=calendar_name, now=now)
        _atomic_write(target, content)
    except OSError as exc:
        return CalendarSyncResult("error", 0, 0, f"无法写入日历文件 {target}：{exc}")
    return CalendarSyncResult("ok", synced, removed, str(target))
