from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timedelta

from .config import Settings
from .models import JobTask
from .state import StateStore
from .task_service import critical_time

LOGGER = logging.getLogger(__name__)


def notify_urgent(count: int) -> None:
    if count <= 0 or sys.platform != "win32":
        return
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except RuntimeError:
        logging.getLogger(__name__).warning("无法播放紧急任务提示音")


def notify_message(title: str, message: str) -> bool:
    """Show a system notification; ``False`` when nothing visible was shown."""
    if sys.platform == "win32":
        from .windows_notify import send_windows_notification

        return send_windows_notification(title, message)
    if sys.platform == "darwin":
        def apple_string(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

        script = (
            "display notification "
            + apple_string(message)
            + " with title "
            + apple_string(title)
        )
        try:
            subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            LOGGER.exception("macOS 通知失败")
            return False
    return False


def notify_task(task: JobTask, offset_minutes: int) -> bool:
    title = f"{task.company} · {task.stage}"
    message = (
        f"{task.role or '岗位待确认'}将在"
        f"{offset_minutes // 60}小时后到期"
        if offset_minutes >= 60
        else f"{task.role or '岗位待确认'}将在{offset_minutes}分钟后到期"
    )
    if sys.platform == "darwin":
        def apple_string(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

        script = (
            "display notification "
            + apple_string(message)
            + " with title "
            + apple_string(title)
        )
        try:
            subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            logging.getLogger(__name__).exception("macOS 任务提醒失败")
            return False
    if sys.platform == "win32":
        # A beep alone told the user nothing and still counted as delivered,
        # so later thresholds were never re-sent. Deliver a visible toast and
        # report failure honestly so the reminder can be retried.
        from .windows_notify import send_windows_notification

        return send_windows_notification(title, message)
    return False


def send_due_reminders(
    tasks: list[JobTask],
    settings: Settings,
    state: StateStore,
    *,
    now: datetime | None = None,
) -> int:
    if not settings.reminders_enabled:
        return 0
    current = (now or datetime.now().astimezone()).astimezone()
    sent = 0
    for task in tasks:
        if (
            task.deleted_at
            or task.tombstoned
            or task.application_paused
            or not task.is_actionable
            or task.status not in {"confirmed", "planned"}
        ):
            continue
        if task.snoozed_until and task.snoozed_until > current:
            continue
        target = critical_time(task)
        if not target or target <= current:
            continue
        remaining = target - current
        eligible = [
            offset
            for offset in settings.reminder_offsets_minutes
            if remaining <= timedelta(minutes=offset)
            and not state.reminder_sent(task.id, target, offset)
        ]
        if not eligible:
            continue
        offset = min(eligible)
        if notify_task(task, offset):
            # Mark every threshold already crossed so a late app start does
            # not emit several reminders for the same event.
            for crossed_offset in eligible:
                state.mark_reminder_sent(task.id, target, crossed_offset)
            sent += 1
    return sent
