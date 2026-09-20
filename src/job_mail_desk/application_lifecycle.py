from __future__ import annotations

from datetime import datetime

from .application_registry import ApplicationRegistry
from .markdown_store import MarkdownTaskStore
from .models import ApplicationRecord, JobTask
from .parser import SHANGHAI
from .stages import is_same_stage, is_stage_advance, is_terminal_stage


ACTIONABLE_STATUSES = {"new", "needs_review", "confirmed", "planned"}
OPEN_STATUSES = {"new", "needs_review", "confirmed", "planned"}


def node_linked_to_task(node: dict[str, object], task: JobTask) -> bool:
    """True when a mail-review progress node describes ``task``."""
    if str(node.get("source_task_id") or "") == task.id:
        return True
    hashes = {str(value) for value in (node.get("source_hashes") or ()) if value}
    if node.get("source_hash"):
        hashes.add(str(node["source_hash"]))
    return task.source_message_hash in hashes


def sync_application_progress_from_task(
    record: ApplicationRecord,
    task: JobTask,
    *,
    now: datetime | None = None,
) -> bool:
    """Mirror a human task edit into the application's own progress fields.

    Task cards and the application chain used to drift apart: completing or
    rescheduling a task changed ``tasks/*.md`` while ``progress_nodes`` and
    ``manual_stage_status`` kept the state copied at confirmation time. The
    caller is a user action (desktop card, CLI ``task-update``), so the task is
    authoritative here: linked mail-review nodes follow the task's stage,
    round and done/pending state, and the application's current stage follows
    when the task is at that stage (or deeper, which advances it). Ended and
    archived chains are never touched, so finishing an old task cannot revive
    a closed application (invariants 8 and 16). Returns ``True`` on change.
    """
    if task.deleted_at or task.tombstoned or task.status == "irrelevant":
        return False
    if record.deleted_at or record.merged_into or record.status != "active":
        return False
    node_status = (
        "completed"
        if task.status == "done"
        else "pending" if task.status in OPEN_STATUSES else None
    )
    changed = False
    for node in record.progress_nodes:
        if not node_linked_to_task(node, task):
            continue
        updates: dict[str, object] = {}
        if node_status and node.get("status") != node_status:
            updates["status"] = node_status
        if task.stage and node.get("stage") != task.stage:
            updates["stage"] = task.stage
        if (node.get("round") or None) != (task.round or None):
            updates["round"] = task.round
        if not node.get("source_task_id"):
            updates["source_task_id"] = task.id
        if updates:
            node.update(updates)
            changed = True
    if (
        node_status
        and record.status == "active"
        and record.manual_stage
        and not is_terminal_stage(task.stage)
    ):
        if is_same_stage(record.manual_stage, task.stage):
            if record.manual_stage_status != node_status:
                record.manual_stage_status = node_status
                changed = True
        elif is_stage_advance(record.manual_stage, task.stage):
            record.manual_stage = task.stage
            record.manual_stage_status = node_status
            if record.next_stage and is_same_stage(record.next_stage, task.stage):
                record.next_stage = None
                record.next_action = ""
            changed = True
    if changed:
        record.revision += 1
        record.updated_at = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    return changed


def _task_fact_time(task: JobTask) -> datetime:
    """Return a source event time that scanner replay cannot move forward."""
    return (
        task.received_at
        or task.completed_at
        or task.updated_at
        or datetime.min.replace(tzinfo=SHANGHAI)
    )


def _task_order(task: JobTask) -> tuple[datetime, datetime, str]:
    minimum = datetime.min.replace(tzinfo=SHANGHAI)
    return (
        _task_fact_time(task),
        task.completed_at or task.received_at or minimum,
        task.id,
    )


def _is_terminal(task: JobTask) -> bool:
    if task.status == "irrelevant":
        return False
    return task.event_type == "rejection" or is_terminal_stage(task.stage)


def _latest_manual_lifecycle(
    record: ApplicationRecord,
) -> tuple[datetime, str] | None:
    events: list[tuple[datetime, str]] = []
    for event in record.manual_progress_history:
        try:
            event_at = datetime.fromisoformat(str(event.get("event_at") or ""))
        except ValueError:
            continue
        if event_at.tzinfo is None:
            event_at = event_at.replace(tzinfo=SHANGHAI)
        lifecycle_status = event.get("lifecycle_status")
        if lifecycle_status not in {"active", "ended", "archived"}:
            lifecycle_status = (
                "ended"
                if is_terminal_stage(str(event.get("stage") or ""))
                else "active"
            )
        events.append((event_at.astimezone(SHANGHAI), str(lifecycle_status)))
    if not events:
        return None
    return max(events, key=lambda item: item[0])


def reconcile_application(
    application_key: str,
    registry: ApplicationRegistry,
    task_store: MarkdownTaskStore,
) -> ApplicationRecord | None:
    record = registry.load(application_key)
    if not record or record.deleted_at or record.merged_into:
        return None
    previous_state = (
        record.status,
        record.workflow_status,
        record.current_task_id,
        record.waiting_since,
        record.workflow_reason,
        record.next_action,
    )
    previous_schema_version = record.schema_version
    tasks = [
        task
        for task in task_store.all()
        if task.application_key == application_key
        and not task.deleted_at
        and not task.tombstoned
    ]
    tasks.sort(key=_task_order, reverse=True)
    terminal_tasks = [task for task in tasks if _is_terminal(task)]
    latest_terminal_at = (
        max(_task_fact_time(task) for task in terminal_tasks)
        if terminal_tasks
        else None
    )
    manual_lifecycle = _latest_manual_lifecycle(record)
    manual_lifecycle_at = manual_lifecycle[0] if manual_lifecycle else None
    manual_lifecycle_status = manual_lifecycle[1] if manual_lifecycle else None
    terminal_wins = bool(
        latest_terminal_at
        and (
            manual_lifecycle_status != "active"
            or not manual_lifecycle_at
            or latest_terminal_at >= manual_lifecycle_at
        )
    )
    if manual_lifecycle_status == "archived" or (
        record.status == "archived" and not manual_lifecycle
    ):
        record.status = "archived"
        record.workflow_status = "archived"
        record.workflow_reason = "application-archived"
        record.current_task_id = None
    elif manual_lifecycle_status == "ended" or terminal_wins or (
        record.status == "ended"
        and not manual_lifecycle
    ):
        record.status = "ended"
        record.workflow_status = "ended"
        record.workflow_reason = "terminal-result"
        record.current_task_id = None
        record.waiting_since = None
    else:
        if manual_lifecycle_status == "active":
            record.status = "active"
        actionable = next(
            (
                task
                for task in tasks
                if task.is_actionable and task.status in ACTIONABLE_STATUSES
            ),
            None,
        )
        latest = tasks[0] if tasks else None
        if actionable and (not latest or _task_order(actionable) >= _task_order(latest)):
            record.workflow_status = "action_required"
            record.current_task_id = actionable.id
            record.waiting_since = None
            record.workflow_reason = "actionable-task"
        elif latest and latest.status == "done":
            record.workflow_status = "waiting_next_step"
            record.current_task_id = latest.id
            record.waiting_since = (
                latest.completed_at or latest.updated_at or latest.received_at
            )
            record.workflow_reason = "latest-task-completed"
            record.next_action = record.next_action or "等待下一步通知"
        elif actionable:
            record.workflow_status = "action_required"
            record.current_task_id = actionable.id
            record.waiting_since = None
            record.workflow_reason = "actionable-task"
        else:
            record.workflow_status = "waiting_next_step"
            record.current_task_id = latest.id if latest else None
            if latest:
                record.waiting_since = (
                    latest.completed_at or latest.updated_at or latest.received_at
                )
            else:
                record.waiting_since = (
                    record.waiting_since
                    or record.updated_at
                    or record.created_at
                    or record.submitted_at
                )
            record.workflow_reason = "no-actionable-task"
    record.schema_version = 6
    current_state = (
        record.status,
        record.workflow_status,
        record.current_task_id,
        record.waiting_since,
        record.workflow_reason,
        record.next_action,
    )
    record_changed = current_state != previous_state
    if record_changed:
        record.revision += 1
    paused = record.status in {"ended", "archived"}
    for task in tasks:
        if task.application_paused == paused:
            continue
        task.application_paused = paused
        task_store.save(task)
    if record_changed or previous_schema_version != record.schema_version:
        registry.save(record)
    return record


def reconcile_all_applications(
    registry: ApplicationRegistry,
    task_store: MarkdownTaskStore,
) -> int:
    count = 0
    for record in registry.all(
        ignore_invalid=True,
        include_merged=False,
        include_deleted=False,
    ):
        if reconcile_application(record.application_key, registry, task_store):
            count += 1
    return count
