from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re

from .application_registry import ApplicationRegistry
from .config import (
    APPLICATIONS_DIR,
    DASHBOARD_CACHE,
    RESEARCH_QUEUE,
    STATE_DB,
    TASKS_DIR,
    UNRESOLVED_DIR,
)
from .markdown_store import MarkdownTaskStore, _atomic_write
from .models import JobTask
from .normalization import normalize_company_project
from .parser import SHANGHAI
from .progress import progress_payload
from .research import request_states
from .state import StateStore
from .task_service import critical_time
from .unresolved_store import UnresolvedStore
from .review_explanation import explain_review


DASHBOARD_CACHE_SCHEMA = 14


def _company_key(company: str) -> str:
    normalized, _ = normalize_company_project(company)
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", normalized).casefold()


def _view(task: JobTask, now: datetime) -> str:
    if task.application_paused:
        return "progress"
    if not task.is_actionable:
        return "progress"
    if task.status in {"done", "expired", "cancelled"}:
        return "progress"
    if (
        task.status == "confirmed"
        and task.event_type == "application"
        and not critical_time(task)
    ):
        return "progress"
    if task.snoozed_until and task.snoozed_until > now:
        return "snoozed"
    target = critical_time(task)
    if not target:
        return "review"
    if target.date() == now.date() or target <= now + timedelta(hours=24):
        return "today"
    return "week"


def _task_payload(
    task: JobTask,
    now: datetime,
    research_state: dict[str, object] | None = None,
) -> dict[str, object]:
    target = critical_time(task)
    remaining = None
    if target:
        seconds = int((target - now).total_seconds())
        if seconds < 0:
            remaining = "已过时间"
        elif seconds < 3600:
            remaining = f"{max(1, seconds // 60)} 分钟"
        elif seconds < 86400:
            remaining = f"{seconds // 3600} 小时"
        else:
            remaining = f"{seconds // 86400} 天"
    queue_status = str((research_state or {}).get("status") or "")
    research_status = {
        "pending": "queued",
        "running": "running",
        "completed": "completed",
        "blocked": "blocked",
        "closed": "closed",
    }.get(queue_status, task.research_status)
    result_path = str((research_state or {}).get("result_path") or "")
    todo_visible = not task.application_paused and task.is_actionable and (
        task.status == "done"
        or (
            task.status in {"confirmed", "planned"}
            and (bool(target) or task.event_type == "manual")
        )
        or (task.event_type == "manual" and task.status == "needs_review")
    )
    return {
        "id": task.id,
        "application_id": task.application_id,
        "application_key": task.application_key,
        "company": task.company,
        "company_key": _company_key(task.company),
        "role": task.role or "岗位待确认",
        "location": task.location or "",
        "event_type": task.event_type,
        "received_at": task.received_at.isoformat(),
        "project": task.recruiting_project or "",
        "stage": task.stage,
        "round": task.round or "",
        "time": target.isoformat() if target else None,
        "start_at": task.start_at.isoformat() if task.start_at else None,
        "end_at": task.end_at.isoformat() if task.end_at else None,
        "deadline_at": task.deadline_at.isoformat() if task.deadline_at else None,
        "duration_minutes": task.duration_minutes,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "completed_at_inferred": task.completed_at_inferred,
        "snoozed_until": (
            task.snoozed_until.isoformat() if task.snoozed_until else None
        ),
        "time_label": target.astimezone(SHANGHAI).strftime("%m-%d %H:%M")
        if target
        else "时间待确认",
        "remaining": remaining,
        "action": task.action_summary,
        "manual_notes": task.manual_notes,
        "status": task.status,
        "priority": task.priority,
        "research_status": research_status,
        "research_result_path": result_path,
        "has_source": bool(task.source_url),
        "has_mail_locator": bool(task.mail_locator),
        "actionable": todo_visible,
        "view": _view(task, now),
        "deleted_at": task.deleted_at.isoformat() if task.deleted_at else None,
        "application_paused": task.application_paused,
    }


def _company_timelines(
    applications: list[dict[str, object]],
) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for application in applications:
        grouped.setdefault(str(application.get("company") or "公司待确认"), []).append(
            application
        )
    result: list[dict[str, object]] = []
    for company, items in grouped.items():
        items.sort(
            key=lambda item: (
                item.get("active") is not True,
                str(item.get("role") or ""),
            )
        )
        times = [str(item["next_time"]) for item in items if item.get("next_time")]
        result.append(
            {
                "company": company,
                "active_count": sum(item.get("active") is True for item in items),
                "application_count": len(items),
                "next_time": min(times) if times else None,
                "applications": items,
            }
        )
    return sorted(
        result,
        key=lambda item: (
            int(item["active_count"]) == 0,
            str(item["next_time"] or "9999"),
            str(item["company"]),
        ),
    )


def dashboard_payload(
    research_queue: Path = RESEARCH_QUEUE,
    progress_source: Path | None = None,
    *,
    tasks_dir: Path | None = None,
    state_db: Path | None = None,
    unresolved_dir: Path | None = None,
    applications_dir: Path | None = None,
) -> dict[str, object]:
    tasks_dir = tasks_dir or TASKS_DIR
    state_db = state_db or STATE_DB
    unresolved_dir = unresolved_dir or UNRESOLVED_DIR
    applications_dir = applications_dir or (
        APPLICATIONS_DIR
        if APPLICATIONS_DIR.parent == tasks_dir.parent
        else tasks_dir.parent / "applications"
    )
    now = datetime.now(SHANGHAI)
    registry = ApplicationRegistry(applications_dir)
    registry_records = registry.all(
        ignore_invalid=True,
        include_merged=True,
        include_deleted=True,
    )
    hidden_records = [
        record
        for record in registry_records
        if record.merged_into or record.deleted_at
    ]
    hidden_keys = {
        key
        for record in hidden_records
        for key in (record.application_key, *record.aliases)
    }
    hidden_legacy_ids = {
        legacy_id
        for record in hidden_records
        for legacy_id in record.legacy_application_ids
    }
    stored_tasks = MarkdownTaskStore(tasks_dir).all()
    all_tasks = [
        task
        for task in stored_tasks
        if task.application_key not in hidden_keys
        and task.application_id not in hidden_legacy_ids
        and not task.deleted_at
        and not task.tombstoned
    ]
    tasks = [
        task
        for task in all_tasks
        if task.status not in {"cancelled", "expired", "irrelevant"}
    ]
    states = request_states(research_queue)
    payload = [_task_payload(task, now, states.get(task.id)) for task in tasks]
    progress = progress_payload(
        all_tasks,
        progress_source,
        application_records=registry_records,
    )
    progress = [
        application
        for application in progress
        if str(application.get("application_key") or "") not in hidden_keys
        and str(application.get("application_id") or "") not in hidden_keys
        and str(application.get("legacy_application_id") or "")
        not in hidden_legacy_ids
    ]
    application_views: dict[str, dict[str, object]] = {}
    for application in progress:
        for key in (
            application.get("application_id"),
            application.get("application_key"),
            application.get("legacy_application_id"),
        ):
            if key:
                application_views[str(key)] = application
    for item in payload:
        application = application_views.get(str(item.get("application_key") or ""))
        if not application:
            application = application_views.get(str(item.get("application_id") or ""))
        if not application:
            continue
        item["company"] = application.get("company") or item["company"]
        item["role"] = application.get("role") or item["role"]
        item["location"] = application.get("location") or item["location"]
        if application.get("active") is False:
            item["view"] = "progress"
            item["actionable"] = False
    for application in progress:
        key = str(application.get("application_key") or "")
        record = registry.load(key) if key else None
        if not record:
            continue
        if record.identity_locked:
            application["company"] = record.company
            application["role"] = record.role or "岗位待确认"
            application["project"] = record.recruiting_project or ""
            application["location"] = record.location or ""
        application["company_key"] = record.company_key
        application["workflow_status"] = record.workflow_status
        application["next_action"] = record.next_action
        application["manual_notes"] = record.manual_notes
        application["manual_stage"] = record.manual_stage
        application["manual_stage_status"] = record.manual_stage_status
        application["next_stage"] = record.next_stage
        if record.manual_stage:
            application["current_stage"] = record.manual_stage
        application["stage_status_label"] = (
            "已完成"
            if record.manual_stage_status == "completed"
            else "未完成"
        )
        if record.next_stage:
            application["current_action"] = record.next_stage
        if record.workflow_status == "waiting_next_step":
            application["active"] = True
            application["current_status"] = "waiting_next_step"
            application["status_label"] = "等待下一步"
            application["current_action"] = record.next_action or "等待下一步通知"
            application["next_time"] = None
        elif record.workflow_status in {"ended", "archived"}:
            application["active"] = False
    unresolved: list[dict[str, object]] = []
    for record in UnresolvedStore(unresolved_dir).all():
        if record.status != "pending":
            continue
        candidates = []
        candidate_keys = tuple(
            dict.fromkeys(
                (
                    *record.candidate_application_keys,
                    *(
                        (record.recommended_application_key,)
                        if record.recommended_application_key
                        else ()
                    ),
                )
            )
        )
        for key in candidate_keys:
            application = registry.load(key)
            if application and not application.deleted_at and not application.merged_into:
                lifecycle = {
                    "active": "进行中",
                    "ended": "已结束",
                    "archived": "已归档",
                }.get(application.status, application.status)
                role = application.role or "岗位待确认"
                project = application.recruiting_project or ""
                job_code = application.job_code or ""
                location = application.location or ""
                attempt = max(1, int(application.attempt_sequence or 1))
                current_stage = application.manual_stage or "待确认"
                candidates.append(
                    {
                        "application_key": application.application_key,
                        "company": application.company,
                        "role": role,
                        "project": project,
                        "job_code": job_code,
                        "location": location,
                        "attempt_sequence": attempt,
                        "status": application.status,
                        "lifecycle": lifecycle,
                        "current_stage": current_stage,
                        "label": (
                            f"{application.company}｜{role}"
                            f"｜项目：{project or '—'}"
                            f"｜职位编号：{job_code or '—'}"
                            f"｜地点：{location or '—'}"
                            f"｜第 {attempt} 次"
                            f"｜{lifecycle}"
                            f"｜当前：{current_stage}"
                        ),
                    }
                )
        target = record.deadline_at or record.end_at or record.start_at
        unresolved.append(
            {
                **record.to_dict(),
                **explain_review(record),
                "attention_type": "unresolved_identity",
                "candidates": candidates,
                "time": target.isoformat() if target else None,
                "time_label": target.astimezone(SHANGHAI).strftime("%m-%d %H:%M")
                if target
                else "时间待确认",
            }
        )
    unresolved.sort(
        key=lambda item: str(item.get("received_at") or ""),
        reverse=True,
    )
    payload.sort(
        key=lambda item: (
            item["status"] == "done",
            item["time"] is None,
            item["time"] or "9999",
        )
    )
    active_records = [
        record
        for record in registry_records
        if not record.merged_into
        and not record.deleted_at
        and record.status != "archived"
    ]
    fingerprints: dict[str, list[object]] = {}
    for record in active_records:
        if record.identity_fingerprint:
            fingerprints.setdefault(record.identity_fingerprint, []).append(record)
    duplicate_candidates = [
        {
            "identity_fingerprint": fingerprint,
            "applications": [
                {
                    "application_key": record.application_key,
                    "company": record.company,
                    "role": record.role or "岗位待确认",
                    "location": record.location or "",
                    "project": record.recruiting_project,
                    "attempt_sequence": record.attempt_sequence,
                }
                for record in records
            ],
        }
        for fingerprint, records in sorted(fingerprints.items())
        if len(records) > 1
    ]
    trash = [
        {
            **record.to_dict(),
            "task_count": sum(
                task.application_key
                in {record.application_key, *record.aliases}
                for task in MarkdownTaskStore(tasks_dir).all()
            ),
        }
        for record in registry_records
        if record.deleted_at and not record.merged_into
    ]
    task_trash = [
        _task_payload(task, now, states.get(task.id))
        for task in stored_tasks
        if task.deleted_at and not task.tombstoned
    ]
    task_trash.sort(key=lambda item: str(item.get("deleted_at") or ""), reverse=True)
    future_tasks = [
        item
        for item in payload
        if item.get("actionable")
        and item.get("status") != "done"
        and item.get("time")
        and not (
            item.get("snoozed_until")
            and datetime.fromisoformat(str(item["snoozed_until"])) > now
        )
        and datetime.fromisoformat(str(item["time"])) >= now
    ]
    urgent_cutoff = now + timedelta(hours=72)
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    urgent_overview = [
        item
        for item in future_tasks
        if datetime.fromisoformat(str(item["time"])) <= urgent_cutoff
    ]
    urgent_ids = {str(item["id"]) for item in urgent_overview}
    today_overview = [
        item
        for item in future_tasks
        if str(item["id"]) not in urgent_ids
        and datetime.fromisoformat(str(item["time"])).date() == now.date()
    ]
    overview_ids = urgent_ids | {str(item["id"]) for item in today_overview}
    week_overview = [
        item
        for item in future_tasks
        if str(item["id"]) not in overview_ids
        and week_start <= datetime.fromisoformat(str(item["time"])) < week_end
    ]
    overview_count = len(urgent_overview) + len(today_overview) + len(week_overview)
    return {
        "generated_at": now.isoformat(),
        "tasks": payload,
        "unresolved": unresolved,
        "progress": progress,
        "company_timelines": _company_timelines(progress),
        "trash": trash,
        "task_trash": task_trash,
        "overview": {
            "urgent": urgent_overview,
            "today": today_overview,
            "week": week_overview,
            "latest_unresolved": unresolved[:3],
        },
        "duplicate_candidates": duplicate_candidates,
        "counts": {
            "today": overview_count,
            "week": sum(
                item["view"] == "week" and item["status"] != "done"
                for item in payload
            ),
            "review": sum(
                item["view"] == "review" and item["status"] != "done"
                for item in payload
            ) + len(unresolved),
            "list": sum(
                item["status"] != "done" and bool(item["actionable"])
                for item in payload
            ),
            "progress": len(progress),
            "research": sum(
                item["research_status"] in {"queued", "running", "blocked"}
                or bool(item["research_result_path"])
                for item in payload
            ),
        },
        "health": StateStore(state_db).health(),
    }


def _source_signature(
    research_queue: Path,
    progress_source: Path | None,
    *,
    tasks_dir: Path | None = None,
    state_db: Path | None = None,
    unresolved_dir: Path | None = None,
    applications_dir: Path | None = None,
) -> list[list[object]]:
    tasks_dir = tasks_dir or TASKS_DIR
    state_db = state_db or STATE_DB
    unresolved_dir = unresolved_dir or UNRESOLVED_DIR
    applications_dir = applications_dir or (
        APPLICATIONS_DIR
        if APPLICATIONS_DIR.parent == tasks_dir.parent
        else tasks_dir.parent / "applications"
    )
    paths = list(sorted(tasks_dir.glob("*.md")))
    paths.extend(sorted(unresolved_dir.glob("*.md")))
    paths.extend(sorted(applications_dir.glob("app-*.md")))
    paths.extend([research_queue, state_db])
    if progress_source:
        paths.append(progress_source)
    signature: list[list[object]] = [
        ["schema", DASHBOARD_CACHE_SCHEMA],
        ["minute", datetime.now(SHANGHAI).strftime("%Y-%m-%dT%H:%M")]
    ]
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            signature.append([str(path), 0, 0])
        else:
            signature.append([str(path), stat.st_mtime_ns, stat.st_size])
    return signature


def cached_dashboard_payload(
    research_queue: Path = RESEARCH_QUEUE,
    progress_source: Path | None = None,
    cache_path: Path = DASHBOARD_CACHE,
    *,
    tasks_dir: Path | None = None,
    state_db: Path | None = None,
    unresolved_dir: Path | None = None,
    applications_dir: Path | None = None,
) -> dict[str, object]:
    """Return a persisted local snapshot when its Markdown inputs are unchanged."""
    signature = _source_signature(
        research_queue,
        progress_source,
        tasks_dir=tasks_dir,
        state_db=state_db,
        unresolved_dir=unresolved_dir,
        applications_dir=applications_dir,
    )
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("signature") == signature and isinstance(cached.get("payload"), dict):
            return cached["payload"]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    payload: dict[str, object] = {}
    for _attempt in range(3):
        before = _source_signature(
            research_queue,
            progress_source,
            tasks_dir=tasks_dir,
            state_db=state_db,
            unresolved_dir=unresolved_dir,
            applications_dir=applications_dir,
        )
        payload = dashboard_payload(
            research_queue,
            progress_source,
            tasks_dir=tasks_dir,
            state_db=state_db,
            unresolved_dir=unresolved_dir,
            applications_dir=applications_dir,
        )
        after = _source_signature(
            research_queue,
            progress_source,
            tasks_dir=tasks_dir,
            state_db=state_db,
            unresolved_dir=unresolved_dir,
            applications_dir=applications_dir,
        )
        if before != after:
            continue
        _atomic_write(
            cache_path,
            json.dumps(
                {"signature": after, "payload": payload},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return payload
    # A concurrent writer remained active. Return the latest rebuilt payload
    # without binding it to a signature; the next refresh will retry.
    return payload
