from __future__ import annotations

from pathlib import Path
from datetime import datetime

from .activity_store import ActivityStore
from .application_lifecycle import (
    reconcile_application,
    sync_application_progress_from_task,
)
from .application_registry import ApplicationRegistry
from .config import DASHBOARD_FILE, TASKS_DIR, Settings
from .data_lock import data_directory_lease
from .exporter import export_dashboard, import_checked_states
from .markdown_store import MarkdownTaskStore
from .macos_dock import set_dock_badge
from .progress import export_progress, sync_task_to_ledger
from .research import close_requests_for_task
from .task_service import critical_time, edit_task_fields


ALLOWED_STATUSES = {
    "needs_review",
    "confirmed",
    "planned",
    "done",
    "cancelled",
    "irrelevant",
}


def _summary(task) -> dict[str, object]:
    target = critical_time(task)
    return {
        "id": task.id,
        "company": task.company,
        "role": task.role,
        "stage": task.stage,
        "round": task.round,
        "status": task.status,
        "start_at": task.start_at.isoformat() if task.start_at else None,
        "end_at": task.end_at.isoformat() if task.end_at else None,
        "deadline_at": task.deadline_at.isoformat() if task.deadline_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "completed_at_inferred": task.completed_at_inferred,
        "critical_time": target.isoformat() if target else None,
        "action_summary": task.action_summary,
    }


def list_tasks(
    *,
    company: str = "",
    role: str = "",
    stage: str = "",
    include_irrelevant: bool = False,
    store: MarkdownTaskStore | None = None,
) -> list[dict[str, object]]:
    source = store or MarkdownTaskStore(TASKS_DIR)

    def matches(value: str | None, query: str) -> bool:
        return not query or query.casefold() in (value or "").casefold()

    tasks = [
        task
        for task in source.all()
        if (include_irrelevant or task.status != "irrelevant")
        and matches(task.company, company)
        and matches(task.role, role)
        and matches(task.stage, stage)
    ]
    tasks.sort(key=lambda item: (critical_time(item) is None, critical_time(item) or item.received_at))
    return [_summary(task) for task in tasks]


def sync_outputs(
    settings: Settings,
    store: MarkdownTaskStore,
    *,
    local_dashboard: Path = DASHBOARD_FILE,
) -> dict[str, object]:
    tasks = store.all()
    export_dashboard(tasks, local_dashboard, settings)
    outputs = {"local_dashboard": str(local_dashboard)}
    if settings.obsidian_enabled:
        export_dashboard(tasks, settings.obsidian_output, settings)
        outputs["obsidian"] = str(settings.obsidian_output)
    if settings.progress_enabled:
        application_records = ApplicationRegistry(
            store.tasks_dir.parent / "applications"
        ).all(
            ignore_invalid=True,
            include_merged=False,
            include_deleted=False,
        )
        export_progress(
            tasks,
            settings.progress_output,
            source_path=settings.progress_source,
            application_records=application_records,
        )
        outputs["progress"] = str(settings.progress_output)
    return {"task_count": len(tasks), "outputs": outputs}


def _apply_task_update_unlocked(
    settings: Settings,
    task_id: str,
    changes: dict[str, object],
    *,
    store: MarkdownTaskStore | None = None,
    local_dashboard: Path = DASHBOARD_FILE,
    record_activity: bool = True,
) -> dict[str, object]:
    changes = dict(changes)
    target_store = store or MarkdownTaskStore(TASKS_DIR)
    if settings.obsidian_enabled:
        import_checked_states(settings.obsidian_output, target_store)
    task = target_store.load(task_id)
    if not task:
        raise KeyError(f"未找到任务：{task_id}")
    status_value = str(changes.pop("status", "") or "").strip()
    editable = {
        key: value
        for key, value in changes.items()
        if key
        in {
            "company",
            "role",
            "recruiting_project",
            "stage",
            "round",
            "start_at",
            "end_at",
            "deadline_at",
            "action_summary",
            "manual_notes",
            "application_key",
        }
    }
    if editable:
        task = edit_task_fields(task_id, editable, target_store)
    if status_value:
        if status_value not in ALLOWED_STATUSES:
            raise ValueError(f"不支持的状态：{status_value}")
        task = target_store.update_status(task_id, status_value)
        if settings.progress_enabled:
            sync_task_to_ledger(task, settings.progress_source)
        if status_value in {"done", "cancelled", "irrelevant"}:
            close_requests_for_task(
                settings.research_queue,
                task_id,
                reason=f"task_status:{status_value}",
            )
            task.research_status = "closed"
            target_store.save(task)
    if task.application_key:
        # Keep the application chain in step with the edited card before the
        # exports and dashboard read it (progress nodes, current stage,
        # workflow status). Human edits outrank the state copied at review.
        registry = ApplicationRegistry(target_store.tasks_dir.parent / "applications")
        record = registry.load(task.application_key)
        if record and sync_application_progress_from_task(record, task):
            registry.save(record)
        reconcile_application(task.application_key, registry, target_store)
    synced = sync_outputs(settings, target_store, local_dashboard=local_dashboard)
    if record_activity:
        version = int(
            (task.updated_at or datetime.now().astimezone()).timestamp() * 1_000_000
        )
        tabs = ["list"]
        if task.application_key:
            tabs.append("progress")
        if task.status == "needs_review":
            tabs.append("review")
        if task.start_at or task.end_at or task.deadline_at:
            tabs.extend(("today", "week", "month"))
        activity = ActivityStore(target_store.tasks_dir.parent / "activity-state.json")
        activity.record_event(
            dedup_key=f"task:{task.id}:agent:{version}",
            kind="task.agent-update",
            tabs=tuple(dict.fromkeys(tabs)),
            company=task.company if task.application_key else None,
            entity_id=f"task:{task.id}",
        )
        set_dock_badge(activity.unique_unread_count())
    return {"task": _summary(task), **synced}


def apply_task_update(
    settings: Settings,
    task_id: str,
    changes: dict[str, object],
    *,
    store: MarkdownTaskStore | None = None,
    local_dashboard: Path = DASHBOARD_FILE,
    record_activity: bool = True,
) -> dict[str, object]:
    target_store = store or MarkdownTaskStore(TASKS_DIR)
    with data_directory_lease(target_store.tasks_dir.parent / ".data.lock"):
        return _apply_task_update_unlocked(
            settings,
            task_id,
            changes,
            store=target_store,
            local_dashboard=local_dashboard,
            record_activity=record_activity,
        )
