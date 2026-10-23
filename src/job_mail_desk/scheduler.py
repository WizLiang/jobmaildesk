from __future__ import annotations

import logging
from datetime import datetime, timedelta
from threading import Event

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import DIGESTS_DIR, STATE_DB, TASKS_DIR, UNRESOLVED_DIR, Settings
from .credentials import load_credential
from .digest import generate_digest
from .macos_calendar import sync_macos_calendar
from .markdown_store import MarkdownTaskStore
from .notifier import notify_message, notify_urgent, send_due_reminders
from .scan_alerts import announce_new_reviews, new_pending_reviews, pending_snapshot
from .unresolved_store import UnresolvedStore
from .runtime_control import RuntimeControl, RuntimeStopping
from .scanner import scan_once
from .state import StateStore


LOGGER = logging.getLogger(__name__)


class ScheduledJobs:
    def __init__(
        self,
        settings: Settings,
        runtime_control: RuntimeControl | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_control = runtime_control or RuntimeControl()
        self.lock = self.runtime_control.scan_lock
        self._retired = Event()

    def retire(self) -> None:
        """Invalidate queued workers before replacing their settings snapshot."""
        self._retired.set()

    def scan(self) -> None:
        if self._retired.is_set() or self.runtime_control.stopping:
            return
        if not self.lock.acquire(blocking=False):
            LOGGER.info("上一次扫描尚未结束，本次跳过")
            return
        try:
            self.runtime_control.raise_if_stopping()
            # A worker may have paused before acquiring the lock while settings
            # were saved. shutdown(wait=False) alone does not cancel that worker.
            if self._retired.is_set():
                return
            try:
                load_credential()
            except RuntimeError:
                LOGGER.debug("未配置 IMAP 凭据，跳过邮件扫描")
                return
            unresolved_store = UnresolvedStore(UNRESOLVED_DIR)
            before = pending_snapshot(unresolved_store)
            summary = scan_once(
                self.settings,
                runtime_control=self.runtime_control,
            )
            notify_urgent(summary.urgent)
            LOGGER.info("扫描完成：%s", summary.to_dict())
            if not self.runtime_control.stopping:
                fresh = new_pending_reviews(before, unresolved_store)
                if fresh and announce_new_reviews(fresh, self.settings, notify_message):
                    LOGGER.info("已通知 %s 封新的待确认邮件", len(fresh))
        except RuntimeStopping:
            LOGGER.info("应用退出，邮件扫描已取消")
        except Exception:
            LOGGER.exception("定时扫描失败")
        finally:
            self.lock.release()

    def digest(self, period: str) -> None:
        if self._retired.is_set() or self.runtime_control.stopping:
            return
        try:
            path = generate_digest(
                period,
                MarkdownTaskStore(TASKS_DIR),
                DIGESTS_DIR,
            )
            LOGGER.info("%s 简报已生成：%s", period, path)
        except Exception:
            LOGGER.exception("%s 简报生成失败", period)

    def reminders(self) -> None:
        if self._retired.is_set() or self.runtime_control.stopping:
            return
        try:
            sent = send_due_reminders(
                MarkdownTaskStore(TASKS_DIR).all(),
                self.settings,
                StateStore(STATE_DB),
            )
            if sent:
                LOGGER.info("已发送 %s 条任务提醒", sent)
        except Exception:
            LOGGER.exception("任务提醒失败")

    def calendar_sync(self) -> None:
        if self._retired.is_set() or self.runtime_control.stopping or not self.settings.calendar_sync_enabled:
            return
        state = StateStore(STATE_DB)
        if state.metadata("calendar_sync_status") == "permission_denied":
            return
        result = sync_macos_calendar(
            MarkdownTaskStore(TASKS_DIR).all(),
            calendar_name=self.settings.calendar_name,
        )
        state.set_metadata("calendar_sync_status", result.status)
        if result.status not in {"ok", "unsupported"}:
            LOGGER.warning("系统日历同步失败：%s", result.detail)


def _add_jobs(
    scheduler: BackgroundScheduler | BlockingScheduler,
    settings: Settings,
    *,
    initial_delay_seconds: int = 0,
    runtime_control: RuntimeControl | None = None,
) -> ScheduledJobs:
    jobs = ScheduledJobs(settings, runtime_control)
    common = {
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 3600,
    }
    scheduler.add_job(
        jobs.scan,
        IntervalTrigger(
            minutes=settings.poll_minutes,
            timezone=settings.timezone,
        ),
        id="mail-poll",
        replace_existing=True,
        next_run_time=(
            datetime.now().astimezone() + timedelta(seconds=initial_delay_seconds)
        ),
        **common,
    )
    scheduler.add_job(
        jobs.reminders,
        IntervalTrigger(minutes=1, timezone=settings.timezone),
        id="task-reminders",
        replace_existing=True,
        **common,
    )
    scheduler.add_job(
        jobs.calendar_sync,
        IntervalTrigger(minutes=10, timezone=settings.timezone),
        id="macos-calendar-sync",
        replace_existing=True,
        **common,
    )
    scheduler.add_job(
        jobs.scan,
        CronTrigger(
            minute=settings.hourly_minute,
            timezone=settings.timezone,
        ),
        id="hourly-refresh",
        replace_existing=True,
        **common,
    )
    labels = ("morning", "noon", "evening")
    for label, time_value in zip(labels, settings.digest_times, strict=False):
        hour, minute = (int(part) for part in time_value.split(":", 1))
        scheduler.add_job(
            jobs.digest,
            CronTrigger(
                hour=hour,
                minute=minute,
                timezone=settings.timezone,
            ),
            args=(label,),
            id=f"digest-{label}",
            replace_existing=True,
            **common,
        )
    return jobs


def create_background_scheduler(
    settings: Settings,
    runtime_control: RuntimeControl | None = None,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    jobs = _add_jobs(
        scheduler,
        settings,
        initial_delay_seconds=15,
        runtime_control=runtime_control,
    )
    scheduler._jobmaildesk_jobs = jobs
    return scheduler


def retire_background_scheduler(scheduler: BackgroundScheduler) -> None:
    """Prevent old callbacks from running after a replacement scheduler starts."""
    jobs = getattr(scheduler, "_jobmaildesk_jobs", None)
    if jobs is not None:
        jobs.retire()


def run_forever(settings: Settings) -> None:
    scheduler = BlockingScheduler(timezone=settings.timezone)
    _add_jobs(scheduler, settings)
    scheduler.start()
