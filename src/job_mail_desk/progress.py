from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
from pathlib import Path
import re

from .markdown_store import _atomic_write
from .models import ApplicationRecord, JobTask
from .normalization import (
    canonical_role,
    is_invalid_role,
    normalize_company_project,
    role_key,
)
from .parser import SHANGHAI
from .privacy import redact_text
from .stages import is_terminal_stage
from .task_service import critical_time


MANAGED_START = "<!-- jobmaildesk:progress-start -->"
MANAGED_END = "<!-- jobmaildesk:progress-end -->"
APPLICATION_MARKER = re.compile(
    r"<!--\s*jobmaildesk:application:(?P<id>(?:app-)?[0-9a-f]{20,64})\s*-->"
)
JOB_CODE = re.compile(r"\b([A-Za-z]{1,4}\d{4,})\b", re.IGNORECASE)


def _application_identity(task: JobTask) -> str:
    return task.application_key or task.application_id


def _task_marker_ids(task: JobTask) -> set[str]:
    return {item for item in (task.application_key, task.application_id) if item}


def create_progress_template(path: Path) -> bool:
    """Create a user-maintained progress ledger without importing fake examples."""
    if path.exists() and path.stat().st_size:
        return False
    today = datetime.now(SHANGHAI).date().isoformat()
    content = f"""---
title: 求职进展台账
type: jobmaildesk-progress-source
created: {today}
updated: {today}
---

# 求职进展台账

> 每行使用：公司｜岗位｜当前进展｜下一步动作。
> 当公司和岗位能够唯一匹配时，组件完成任务会更新“当前进展”并在行尾写入稳定申请 ID；不会覆盖下一步动作和其他手写区域。
> 保存后点击组件“导入台账修改”，即可让申请卡片读取这里的修改；不要手动编辑 [[求职当前进展]] 的自动生成区。

## 当前进展

### 已投递或已进入流程

> 受控格式：公司｜岗位｜**当前进展**｜下一步动作 `<!-- jobmaildesk:application:稳定申请ID -->`
> 已有稳定 ID 的行可直接修改公司、岗位、当前进展和下一步；需要隐藏申请时请把当前进展改为“已归档”。

<!-- 示例：- [x] 示例公司｜产品经理｜**一面已确认**｜8月6日14:00参加面试；首次成功同步后，程序会在行尾加入稳定申请 ID。 -->

### 当前优先待投

<!-- 可自由记录尚未投递的岗位；这一部分不会自动进入求职进展卡片。 -->

## 状态约定

- `[x]`：已经投递或进入流程。
- 当前进展建议使用：已投递、测评、笔试、一面、二面、群面、Offer、未通过、已结束。
- 已完成节点建议保留日期，例如：`2026-08-03 人才测评已完成，等待后续`；截止时间与完成时间必须分开。
- 同一岗位进展变化时更新原行，不要重复追加相同岗位。

## 可编辑字段

| 字段 | 写法 | 组件行为 |
| --- | --- | --- |
| 公司 | 企业标准名 | 更新申请身份；事业群仍需写在岗位或项目中 |
| 岗位 | 精确岗位名/岗位编号 | 更新申请链岗位，不与同公司其他岗位合并 |
| 当前进展 | 已投递、测评、笔试、面试、群面、Offer、未通过、已结束 | 更新当前阶段；终止类状态停止提醒 |
| 下一步动作 | 简短可执行句子 | 更新卡片行动摘要 |
| 复选框 | `[x]` 已投递/已确认，`[ ]` 待处理 | 不覆盖历史节点完成状态 |

## 归档规则

- 想隐藏一条申请时，把当前进展改为 `已归档`，再点击“导入台账修改”。
- 组件保留本地任务历史，避免旧邮件重新制造重复申请链。
- 直接删除台账行不会删除本地任务；没有稳定 ID 的行会进入“待归属”，等待人工选择。
"""
    _atomic_write(path, content)
    return True


def _event_time(task: JobTask) -> datetime:
    return (
        (task.completed_at if task.status == "done" else None)
        or
        critical_time(task)
        or task.updated_at
        or task.received_at
    ).astimezone(SHANGHAI)


def _status_label(status: str, stage: str = "") -> str:
    if status in {"done", "completed"} and is_terminal_stage(stage):
        return "已结束"
    return {
        "new": "新增",
        "needs_review": "待确认",
        "confirmed": "已确认",
        "planned": "已安排",
        "done": "已完成",
        "cancelled": "已取消",
        "expired": "已过期",
        "irrelevant": "已忽略",
        "tracked": "跟踪中",
        "pending": "未完成",
        "completed": "已完成",
        "waiting_next_step": "等待下一步",
        "ended": "已结束",
        "archived": "已归档",
    }.get(status, status)


def _timeline_time(task: JobTask) -> tuple[datetime | None, str, bool]:
    """Choose the event timestamp without disguising receipt/update fallbacks."""
    if task.completed_at:
        return task.completed_at, "completed_at", bool(task.completed_at_inferred)
    for name in ("start_at", "end_at", "deadline_at"):
        value = getattr(task, name)
        if value:
            return value, name, False
    if task.received_at:
        return task.received_at, "received_at", True
    if task.updated_at:
        return task.updated_at, "updated_at", True
    return None, "unknown", True


def _application_receipt_semantic(task: JobTask) -> str | None:
    text = " ".join((task.event_type, task.stage, task.title)).casefold()
    if task.event_type == "application" or re.search(
        r"网申|投递(?:成功|完成|回执)?|申请(?:成功|回执)", text
    ):
        return "网申回执"
    return None


def _iso_timestamp(value: object) -> float:
    if not value:
        return float("-inf")
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.timestamp()


def build_application_timeline(
    tasks: list[JobTask],
    application: ApplicationRecord | None = None,
    *,
    suppress_task_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    """Build the sole display timeline for one application, preserving sources.

    ``suppress_task_ids`` may name tasks that are not rendered (trashed or
    tombstoned) but whose mail-review nodes must stay hidden too; otherwise a
    node would resurface as an open "未完成" row at mail-receipt time after
    its task was removed.
    """
    events: list[dict[str, object]] = []
    for task in tasks:
        event_at, time_kind, time_inferred = _timeline_time(task)
        source_type = "manual" if task.event_type == "manual" else "mail"
        source = {
            "source_type": source_type,
            "task_id": task.id,
            "source_url": task.source_url,
            "has_mail_locator": bool(task.mail_locator),
            "title": task.title,
            "received_at": task.received_at.isoformat(),
        }
        events.append(
            {
                "task_id": task.id,
                "task_ids": [task.id],
                "stage": task.stage,
                "round": task.round or "",
                "status": task.status,
                "status_label": _status_label(task.status, task.stage),
                "event_at": event_at.isoformat() if event_at else None,
                "time": event_at.isoformat() if event_at else None,
                "time_kind": time_kind,
                "time_inferred": time_inferred,
                "action": task.action_summary,
                "source_type": source_type,
                "source_count": 1,
                "sources": [source],
                "_explicit_time": time_kind
                in {"completed_at", "start_at", "end_at", "deadline_at"},
                "_receipt_semantic": _application_receipt_semantic(task),
            }
        )
    if application:
        for index, item in enumerate(application.manual_progress_history):
            event_at = str(item["event_at"])
            status = str(item["status"])
            source = {
                "source_type": "manual",
                "task_id": None,
                "source_url": None,
                "has_mail_locator": False,
                "title": "手动更新",
                "received_at": event_at,
                "manual_event_index": index,
            }
            events.append(
                {
                    "task_id": None,
                    "task_ids": [],
                    "stage": str(item["stage"]),
                    "round": "",
                    "status": status,
                    "status_label": _status_label(status, str(item["stage"])),
                    "event_at": event_at,
                    "time": event_at,
                    "time_kind": "manual",
                    "time_inferred": False,
                    "action": (
                        f"下一步：{item['next_stage']}"
                        if item.get("next_stage")
                        else ""
                    ),
                    "source_type": "manual",
                    "source_count": 1,
                    "sources": [source],
                    "_explicit_time": True,
                    "_receipt_semantic": None,
                }
            )
        task_ids = {task.id for task in tasks} | set(suppress_task_ids or ())
        task_events_by_id = {
            str(event["task_id"]): event for event in events if event.get("task_id")
        }
        tasks_by_hash = {task.source_message_hash: task for task in tasks}
        tasks_by_stage: dict[tuple[str, str], JobTask] = {}
        for task in sorted(tasks, key=lambda item: item.received_at):
            tasks_by_stage[(task.stage, task.round or "")] = task
        for node in application.progress_nodes:
            source_task_id = str(node.get("source_task_id") or "")
            if source_task_id and source_task_id in task_ids:
                continue
            event_at = str(node["event_at"])
            status = str(node["status"])
            stage = str(node["stage"])
            source = {
                "source_type": "mail-review",
                "task_id": None,
                "source_url": None,
                "source_hash": str(node.get("source_hash") or ""),
                "has_mail_locator": bool(node.get("has_mail_locator", False)),
                "title": "邮件复核",
                "received_at": event_at,
                "progress_node_id": str(node["id"]),
            }
            # A review node that describes a task already in the chain (same
            # mail, or same stage and round) is folded into that task's row
            # instead of showing a second "completed/pending" line whose state
            # can no longer change.
            node_hashes = {str(value) for value in (node.get("source_hashes") or ()) if value}
            if node.get("source_hash"):
                node_hashes.add(str(node["source_hash"]))
            owner = next(
                (tasks_by_hash[value] for value in node_hashes if value in tasks_by_hash),
                None,
            ) or tasks_by_stage.get((stage, str(node.get("round") or "")))
            if owner is not None and owner.id in task_events_by_id:
                owner_event = task_events_by_id[owner.id]
                owner_event["sources"].append(source)
                owner_event["source_count"] = len(owner_event["sources"])
                continue
            events.append(
                {
                    "task_id": None,
                    "task_ids": [],
                    "stage": stage,
                    "round": str(node.get("round") or ""),
                    "status": status,
                    "status_label": _status_label(status, stage),
                    "event_at": event_at,
                    "time": event_at,
                    "time_kind": "mail-review",
                    "time_inferred": False,
                    "action": (
                        f"下一步：{node['next_stage']}"
                        if node.get("next_stage")
                        else ""
                    ),
                    "source_type": "mail-review",
                    "source_count": 1,
                    "sources": [source],
                    "_explicit_time": True,
                    "_receipt_semantic": None,
                }
            )

    grouped: dict[tuple[object, ...], dict[str, object]] = {}
    for event in events:
        if event["_explicit_time"]:
            key = (
                "timed",
                event["stage"],
                event["round"],
                _iso_timestamp(event["event_at"]),
            )
        elif event["_receipt_semantic"]:
            key = (
                "receipt",
                event["_receipt_semantic"],
                event["status"],
            )
        else:
            # Untimed non-receipt events retain one row per source.
            key = ("source", event["source_type"], event["task_id"])
        existing = grouped.get(key)
        if not existing:
            grouped[key] = event
            continue
        existing_sources = existing["sources"]
        existing_task_ids = existing["task_ids"]
        assert isinstance(existing_sources, list)
        assert isinstance(existing_task_ids, list)
        existing_sources.extend(event["sources"])  # type: ignore[arg-type]
        existing_task_ids.extend(event["task_ids"])  # type: ignore[arg-type]
        existing["source_count"] = len(existing_sources)
        if _iso_timestamp(event["event_at"]) > _iso_timestamp(existing["event_at"]):
            for name in (
                "task_id",
                "stage",
                "round",
                "event_at",
                "time",
                "time_kind",
                "time_inferred",
                "action",
            ):
                existing[name] = event[name]
        elif existing["task_id"] is None and existing_task_ids:
            existing["task_id"] = existing_task_ids[0]
        if existing["source_type"] != event["source_type"]:
            existing["source_type"] = "mixed"

    result = list(grouped.values())
    result.sort(
        key=lambda item: (
            item["event_at"] is not None,
            _iso_timestamp(item["event_at"]),
            str(item["stage"]),
            str(item["round"]),
        ),
        reverse=True,
    )
    for event in result:
        event.pop("_explicit_time", None)
        event.pop("_receipt_semantic", None)
    return result


def _company_key(value: str) -> str:
    normalized, _project = normalize_company_project(value)
    cleaned = re.sub(r"[\s（）()【】\[\]]+", "", normalized).casefold()
    cleaned = re.sub(r"(?:校园招聘|校招)$", "", cleaned)
    return {
        "deeproute.ai": "元戎启行",
        "深圳元戎启行科技有限公司": "元戎启行",
    }.get(cleaned, cleaned)


def _role_key(value: str) -> str:
    without_code = JOB_CODE.sub("", value)
    normalized = canonical_role(without_code) or without_code
    return role_key(normalized)


def _job_codes(value: str) -> set[str]:
    return {match.upper() for match in JOB_CODE.findall(value)}


def _same_role(left: str, right: str) -> bool:
    left_codes = _job_codes(left)
    right_codes = _job_codes(right)
    if left_codes and right_codes:
        return bool(left_codes & right_codes)
    left_key = _role_key(left)
    right_key = _role_key(right)
    return bool(left_key and right_key and left_key == right_key)


def _best_role(chain: list[JobTask]) -> str | None:
    roles = [
        role
        for item in chain
        if (role := canonical_role(item.role)) and not is_invalid_role(role)
    ]
    if not roles:
        return None
    counts = {role: roles.count(role) for role in set(roles)}
    return max(roles, key=lambda role: (counts[role], -len(role)))


def _progress_status(task: JobTask) -> str:
    stage = (task.round or task.stage or "流程").strip()
    if task.event_type == "application" and task.status == "confirmed":
        return f"{task.received_at:%Y-%m-%d} 网申已提交，等待简历筛选"
    if task.status == "done":
        completed = ""
        if task.completed_at:
            prefix = "约" if task.completed_at_inferred else ""
            completed = f"{prefix}{task.completed_at:%Y-%m-%d} "
        if any(label in stage for label in ("测评", "笔试", "面试", "群面")):
            return f"{completed}{stage}已完成，等待后续"
        if "投递" in stage or "网申" in stage:
            return f"{completed}已投递，等待筛选"
        return f"{completed}{stage}已完成"
    if task.status == "planned":
        return f"{stage}已安排"
    if task.status == "confirmed":
        return f"{stage}已确认"
    if task.status == "cancelled":
        return f"{stage}已取消"
    if task.status == "expired":
        return f"{stage}已过期"
    return _status_label(task.status)


def sync_task_to_ledger(task: JobTask, path: Path | None) -> int:
    """Update one uniquely matched ledger row without touching user-owned fields."""
    if not path or not path.exists() or task.status == "irrelevant":
        return 0
    content = path.read_text(encoding="utf-8")
    marker = "### 已投递或已进入流程"
    if marker not in content:
        return 0
    prefix, section_and_suffix = content.split(marker, 1)
    if "\n### " in section_and_suffix:
        section, suffix = section_and_suffix.split("\n### ", 1)
        suffix = "\n### " + suffix
    else:
        section, suffix = section_and_suffix, ""

    candidates: list[tuple[int, str, list[str], str]] = []
    lines = section.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r"^- \[[ xX]\] ", line):
            continue
        marker_match = APPLICATION_MARKER.search(line)
        clean_line = APPLICATION_MARKER.sub("", line).rstrip()
        body = clean_line.split("] ", 1)[1]
        fields = body.split("｜")
        if len(fields) < 3:
            continue
        company = re.sub(r"[*_`]", "", fields[0]).strip()
        role = re.sub(r"[*_`]", "", fields[1]).strip()
        location = (
            re.sub(r"[*_`]", "", fields[2]).strip()
            if len(fields) >= 5
            else ""
        )
        marker_id = marker_match.group("id") if marker_match else ""
        if marker_id in _task_marker_ids(task):
            candidates = [(
                index,
                line,
                fields,
                clean_line.split("] ", 1)[0] + "] ",
            )]
            break
        if (
            not marker_id
            and _company_key(company) == _company_key(task.company)
            and task.role
            and _same_role(role, task.role)
            and (
                not location
                or not task.location
                or bool(
                    set(location.replace("、", "/").split("/"))
                    & set(task.location.replace("、", "/").split("/"))
                )
            )
        ):
            candidates.append(
                (
                    index,
                    line,
                    fields,
                    clean_line.split("] ", 1)[0] + "] ",
                )
            )

    if not candidates and task.event_type == "application" and task.role:
        checked = "x" if task.status in {"confirmed", "done"} else " "
        lines.extend(
            [
                "",
                (
                    f"- [{checked}] {task.company}｜{task.role}｜"
                    f"**{_progress_status(task)}**｜{task.action_summary} "
                    f"<!-- jobmaildesk:application:{_application_identity(task)} -->"
                ),
            ]
        )
        updated = prefix + marker + "\n".join(lines) + suffix
        _atomic_write(path, updated)
        return 1
    if len(candidates) != 1:
        return 0
    index, _line, fields, _checkbox_prefix = candidates[0]
    checked = task.status == "done" or (
        task.event_type == "application" and task.status == "confirmed"
    )
    checkbox_prefix = "- [x] " if checked else "- [ ] "
    status_index = 3 if len(fields) >= 5 else 2
    fields[status_index] = f"**{_progress_status(task)}**"
    lines[index] = (
        f"{checkbox_prefix}{'｜'.join(fields)} "
        f"<!-- jobmaildesk:application:{_application_identity(task)} -->"
    )
    updated = prefix + marker + "\n".join(lines) + suffix
    if updated == content:
        return 0
    _atomic_write(path, updated)
    return 1


def sync_current_applications_to_ledger(
    tasks: list[JobTask],
    path: Path | None,
) -> int:
    """Sync each application once, using its current node after batch merging."""
    grouped: dict[str, list[JobTask]] = defaultdict(list)
    for task in tasks:
        if task.status != "irrelevant":
            grouped[_application_identity(task)].append(task)
    updates = 0
    for chain in grouped.values():
        chain.sort(key=_event_time)
        active = [
            item
            for item in chain
            if item.status not in {"done", "cancelled", "expired", "irrelevant"}
        ]
        current = active[-1] if active else chain[-1]
        updates += sync_task_to_ledger(current, path)
    return updates


def _ledger_entries(path: Path | None) -> list[dict[str, str]]:
    if not path or not path.exists():
        return []
    content = path.read_text(encoding="utf-8")
    marker = "### 已投递或已进入流程"
    if marker not in content:
        return []
    section = content.split(marker, 1)[1]
    section = section.split("\n### ", 1)[0]
    entries: list[dict[str, str]] = []
    for line in section.splitlines():
        if not re.match(r"^- \[[ xX]\] ", line):
            continue
        marker_match = APPLICATION_MARKER.search(line)
        clean_line = APPLICATION_MARKER.sub("", line).rstrip()
        fields = [
            re.sub(r"[*_`]", "", item).strip()
            for item in clean_line.split("] ", 1)[1].split("｜")
        ]
        if len(fields) < 3:
            continue
        company, project = normalize_company_project(fields[0])
        has_location = len(fields) >= 5
        raw_role = fields[1]
        job_code_match = JOB_CODE.search(raw_role)
        entries.append(
            {
                "company": company,
                "role": canonical_role(raw_role) or raw_role,
                "role_raw": raw_role,
                "job_code": (
                    job_code_match.group(1).upper() if job_code_match else ""
                ),
                "location": fields[2] if has_location else "",
                "project": project or "",
                "status": fields[3] if has_location else fields[2],
                "action": "｜".join(fields[4:] if has_location else fields[3:]).strip(),
                "application_id": marker_match.group("id") if marker_match else "",
            }
        )
    return entries


def read_progress_entries(path: Path | None) -> list[dict[str, str]]:
    """Return normalized user-ledger rows without modifying the source file."""
    return [dict(entry) for entry in _ledger_entries(path)]


def _record_identities(record: ApplicationRecord) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                record.application_key,
                *record.aliases,
                *record.legacy_application_ids,
            )
        )
    )


def _application_payload(
    application_id: str,
    chain: list[JobTask],
    record: ApplicationRecord | None,
    *,
    now: datetime,
    suppress_task_ids: set[str] | None = None,
) -> dict[str, object]:
    """Combine one registry application with any surviving task facts."""
    chain.sort(key=_event_time)
    active_tasks = [
        item
        for item in chain
        if item.status not in {"done", "cancelled", "expired", "irrelevant"}
    ]
    current = active_tasks[-1] if active_tasks else (chain[-1] if chain else None)
    history = build_application_timeline(
        chain, record, suppress_task_ids=suppress_task_ids
    )
    current_event = history[0] if history else None

    task_company = next(
        (
            item.company
            for item in reversed(chain)
            if item.company not in {"公司待确认", "个人待办"}
        ),
        current.company if current else "公司待确认",
    )
    task_role = _best_role(chain)
    task_project = next(
        (item.recruiting_project for item in reversed(chain) if item.recruiting_project),
        None,
    )
    task_location = next(
        (item.location for item in reversed(chain) if item.location),
        None,
    )
    task_company, task_project = normalize_company_project(
        task_company,
        task_project,
    )

    company = record.company if record else task_company
    role = (record.role if record else None) or task_role or "岗位待确认"
    project = (
        (record.recruiting_project if record else None)
        or task_project
        or ""
    )
    location = (record.location if record else None) or task_location or ""

    if record and record.manual_stage:
        current_stage = record.manual_stage
        current_round = ""
        current_status = record.manual_stage_status
    elif current_event:
        current_stage = str(current_event["stage"])
        current_round = str(current_event["round"])
        current_status = str(current_event["status"])
    elif current:
        current_stage = current.stage
        current_round = current.round or ""
        current_status = current.status
    else:
        current_stage = "进展待更新"
        current_round = ""
        current_status = "tracked"

    if record:
        active = (
            record.status == "active"
            and record.workflow_status not in {"ended", "archived"}
        )
        if record.status == "archived" or record.workflow_status == "archived":
            current_status = "archived"
        elif record.status == "ended" or record.workflow_status == "ended":
            current_status = "ended"
        elif record.workflow_status == "waiting_next_step":
            current_status = "waiting_next_step"
    else:
        active = bool(active_tasks)

    if current_status in {"ended", "archived", "waiting_next_step"}:
        status_label = _status_label(current_status, current_stage)
    elif current_event and not (record and record.manual_stage):
        status_label = str(current_event["status_label"])
    else:
        status_label = _status_label(current_status, current_stage)

    current_action = ""
    if record:
        current_action = record.next_stage or record.next_action
    if not current_action and current:
        current_action = current.action_summary
    if not current_action and current_event:
        current_action = str(current_event.get("action") or "")

    target = critical_time(current) if current else None
    next_time = (
        target.isoformat()
        if active and target is not None and target >= now
        else None
    )
    updated_candidates = [
        value
        for value in (
            record.updated_at.isoformat() if record and record.updated_at else None,
            str(current_event["event_at"])
            if current_event and current_event.get("event_at")
            else None,
            _event_time(current).isoformat() if current else None,
        )
        if value
    ]
    updated_at = (
        max(updated_candidates, key=_iso_timestamp)
        if updated_candidates
        else ""
    )
    stable_id = record.application_key if record else application_id
    legacy_id = (
        current.application_id
        if current
        else (
            record.legacy_application_ids[0]
            if record and record.legacy_application_ids
            else stable_id
        )
    )
    return {
        "application_id": stable_id,
        "application_key": (
            record.application_key
            if record
            else (current.application_key if current else "")
        ),
        "legacy_application_id": legacy_id,
        "legacy_application_ids": (
            list(record.legacy_application_ids) if record else []
        ),
        "company": company,
        "company_key": record.company_key if record else _company_key(company),
        "role": role,
        "location": location,
        "project": project,
        "current_stage": current_stage,
        "current_round": current_round,
        "current_status": current_status,
        "status_label": status_label,
        "received_at": (
            current.received_at.isoformat()
            if current
            else (
                record.submitted_at.isoformat()
                if record and record.submitted_at
                else None
            )
        ),
        "start_at": current.start_at.isoformat() if current and current.start_at else None,
        "end_at": current.end_at.isoformat() if current and current.end_at else None,
        "deadline_at": (
            current.deadline_at.isoformat()
            if current and current.deadline_at
            else None
        ),
        "completed_at": (
            current.completed_at.isoformat()
            if current and current.completed_at
            else None
        ),
        "completed_at_inferred": (
            current.completed_at_inferred if current else False
        ),
        "current_action": current_action,
        "active": active,
        "next_time": next_time,
        "updated_at": updated_at,
        "history": history,
    }


def progress_payload(
    tasks: list[JobTask],
    source_path: Path | None = None,
    application_records: list[ApplicationRecord] | None = None,
) -> list[dict[str, object]]:
    """Build progress from registry applications, augmented by task facts."""
    records_by_key = {
        record.application_key: record for record in application_records or []
    }
    record_by_identity: dict[str, ApplicationRecord] = {}
    for record in records_by_key.values():
        for identity in _record_identities(record):
            record_by_identity[identity] = record
    grouped: dict[str, list[JobTask]] = defaultdict(list)
    hidden_task_ids: dict[str, set[str]] = defaultdict(set)
    for task in tasks:
        record = next(
            (
                record_by_identity[identity]
                for identity in (task.application_key, task.application_id)
                if identity and identity in record_by_identity
            ),
            None,
        )
        group_key = record.application_key if record else _application_identity(task)
        if task.deleted_at or task.tombstoned or task.status == "irrelevant":
            # Not rendered, but its mail-review nodes must not resurface either.
            hidden_task_ids[group_key].add(task.id)
            continue
        grouped[group_key].append(task)

    applications: list[dict[str, object]] = []
    current_time = datetime.now(SHANGHAI)
    for application_id, record in records_by_key.items():
        applications.append(
            _application_payload(
                application_id,
                grouped.pop(application_id, []),
                record,
                now=current_time,
                suppress_task_ids=hidden_task_ids.get(application_id),
            )
        )
    for application_id, chain in grouped.items():
        applications.append(
            _application_payload(
                application_id,
                chain,
                None,
                now=current_time,
            )
        )

    base_applications = list(applications)
    ledger_keys: set[tuple[str, str, str]] = set()
    task_states: dict[str, set[str]] = defaultdict(set)
    for task in tasks:
        task_states[_company_key(task.company)].add(task.status)
    for entry in _ledger_entries(source_path):
        company_key = _company_key(entry["company"])

        def same_program(application: dict[str, object]) -> bool:
            application_text = " ".join(
                (str(application.get("role") or ""), str(application.get("project") or ""))
            )
            ledger_text = " ".join((entry["role"], entry["project"]))
            application_program = re.search(r"(?<![A-Za-z])(JDS|TET)(?![A-Za-z])", application_text, re.I)
            ledger_program = re.search(r"(?<![A-Za-z])(JDS|TET)(?![A-Za-z])", ledger_text, re.I)
            return bool(
                application_program
                and ledger_program
                and application_program.group(1).casefold()
                == ledger_program.group(1).casefold()
            )

        task_matches = [
            application
            for application in base_applications
            if (
                entry["application_id"]
                in {
                    application["application_id"],
                    application.get("application_key"),
                    application.get("legacy_application_id"),
                    *(application.get("legacy_application_ids") or []),
                }
                or (
                    not entry["application_id"]
                    and _company_key(str(application["company"])) == company_key
                    and (
                        _same_role(str(application["role"]), entry["role"])
                        or same_program(application)
                    )
                    and (
                        not entry["project"]
                        or entry["project"] in str(application["project"])
                    )
                    and (
                        not entry["location"]
                        or not application.get("location")
                        or entry["location"] == application.get("location")
                    )
                )
            )
        ]
        if task_matches:
            for application in task_matches:
                # The user-maintained ledger is the editable control plane for
                # application identity and current progress. Stable markers
                # make these overrides deterministic without rewriting task
                # history.
                application["company"] = entry["company"] or application["company"]
                application["role"] = entry["role"] or application["role"]
                if entry["project"] and not application["project"]:
                    application["project"] = entry["project"]
                if entry["location"]:
                    application["location"] = entry["location"]
                application["ledger_status"] = entry["status"]
                application["ledger_action"] = entry["action"]
                # 人工台账是申请结果的权威来源。邮件链可能停留在“测评已完成”
                # 等历史节点；当台账明确写出未通过、应聘终止或已结束时，
                # 当前卡片必须展示终止结果，但保留原有 history 供复盘。
                ledger_ended = is_terminal_stage(entry["status"]) or any(
                    label in entry["status"] for label in ("已过期", "已归档")
                )
                if ledger_ended:
                    application["current_stage"] = entry["status"]
                    application["current_status"] = "done"
                    application["status_label"] = entry["status"]
                    application["current_action"] = entry["action"]
                    application["active"] = False
                    application["next_time"] = None
                elif entry["status"]:
                    application["current_stage"] = entry["status"]
                    application["status_label"] = entry["status"]
                    application["current_action"] = entry["action"]
                if application["role"] == "岗位待确认" and same_program(application):
                    application["role"] = entry["role"]
            continue
        if task_states.get(company_key) == {"irrelevant"}:
            continue
        ledger_key = (company_key, _role_key(entry["role"]), entry["location"])
        if ledger_key in ledger_keys:
            continue
        ended = is_terminal_stage(entry["status"]) or "已归档" in entry["status"]
        generated_identifier = hashlib.sha256(
            f"ledger|{entry['company']}|{entry['role']}|{entry['location']}".encode("utf-8")
        ).hexdigest()[:20]
        identifier = entry["application_id"] or generated_identifier
        applications.append(
            {
                "application_id": identifier,
                "legacy_application_id": identifier,
                "company": entry["company"],
                "company_key": company_key,
                "role": entry["role"],
                "location": entry.get("location") or "",
                "project": entry["project"],
                "current_stage": entry["status"],
                "current_round": "",
                "current_status": "done" if ended else "tracked",
                "status_label": entry["status"],
                "received_at": None,
                "start_at": None,
                "end_at": None,
                "deadline_at": None,
                "completed_at": None,
                "completed_at_inferred": False,
                "current_action": entry["action"],
                "active": not ended,
                "next_time": None,
                "updated_at": "",
                "ledger_status": entry["status"],
                "ledger_action": entry["action"],
                "history": [
                    {
                        "task_id": None,
                        "task_ids": [],
                        "stage": entry["status"],
                        "round": "",
                        "status": "done" if ended else "tracked",
                        "status_label": "已结束" if ended else "跟踪中",
                        "event_at": None,
                        "time": None,
                        "time_kind": "unknown",
                        "time_inferred": True,
                        "action": entry["action"],
                        "source_type": "ledger",
                        "source_count": 1,
                        "sources": [
                            {
                                "source_type": "ledger",
                                "task_id": None,
                                "source_url": None,
                                "has_mail_locator": False,
                                "title": "决策台账",
                                "received_at": None,
                            }
                        ],
                    }
                ],
            }
        )
        ledger_keys.add(ledger_key)
    applications.sort(
        key=lambda item: (
            not bool(item["active"]),
            item["next_time"] is None,
            item["next_time"] or "9999",
            str(item["company"]),
            str(item["role"]),
            str(item["application_id"]),
        )
    )
    return applications


def export_progress(
    tasks: list[JobTask],
    output: Path,
    *,
    now: datetime | None = None,
    source_path: Path | None = None,
    application_records: list[ApplicationRecord] | None = None,
    applications_dir: Path | None = None,
) -> int:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if application_records is None:
        from .application_registry import ApplicationRegistry
        from .config import APPLICATIONS_DIR

        registry_path = applications_dir or APPLICATIONS_DIR
        application_records = (
            ApplicationRegistry(registry_path).all(
                ignore_invalid=True,
                include_merged=False,
                include_deleted=False,
            )
            if registry_path.exists()
            else []
        )
    applications = progress_payload(
        tasks,
        source_path,
        application_records=application_records,
    )
    existing = output.read_text(encoding="utf-8") if output.exists() else ""
    if existing.strip() and not (
        MANAGED_START in existing and MANAGED_END in existing
    ):
        raise ValueError(
            f"进展文件缺少完整 JobMailDesk 受管标记，已拒绝覆盖：{output}"
        )
    if MANAGED_START in existing and MANAGED_END in existing:
        prefix, remainder = existing.split(MANAGED_START, 1)
        _, suffix = remainder.split(MANAGED_END, 1)
    else:
        prefix = (
            "---\n"
            "title: 求职当前进展\n"
            "type: job-progress\n"
            "status: active\n"
            "tags: [求职, 投递跟踪, 进展]\n"
            "---\n\n"
            "# 求职当前进展\n\n"
            "> 由 JobMailDesk 根据本地结构化任务生成。这里记录申请链；"
            "可执行时间仍以桌面待办和官方通知为准。\n\n"
            "关联入口：[[岗位投递决策台账]]\n\n"
        )
        suffix = (
            "\n## 手动补充\n\n"
            "<!-- 本区可手写复盘或决策；自动刷新不会覆盖。 -->\n"
        )

    active_count = sum(bool(item["active"]) for item in applications)
    lines = [
        MANAGED_START,
        "",
        f"> 更新时间：{current:%Y-%m-%d %H:%M}｜进行中 {active_count}｜申请链 {len(applications)}",
        "",
    ]
    if not applications:
        lines.extend(["_暂无流程记录_", ""])

    def cell(value: object) -> str:
        text = redact_text(str(value or "")).replace("|", "\\|").strip()
        return text or "—"

    def display_time(value: object, *, inferred: bool = False) -> str:
        if not value:
            return "—"
        rendered = datetime.fromisoformat(str(value)).astimezone(SHANGHAI).strftime(
            "%Y-%m-%d %H:%M"
        )
        return f"约 {rendered}（历史推定）" if inferred else rendered

    for application in applications:
        company = redact_text(str(application["company"]))
        role = redact_text(str(application["role"]))
        project = redact_text(str(application["project"]))
        location = redact_text(str(application.get("location") or ""))
        summary = f"{company}｜{role} · {application['current_stage']}"
        current_round = (
            str(application["current_round"])
            if application["current_round"]
            else "—"
        )
        next_action = redact_text(
            str(
                application.get("ledger_action")
                or application.get("current_action")
                or ""
            )
        )
        if application.get("start_at") and application.get("end_at"):
            activity_window = (
                f"{display_time(application['start_at'])} – "
                f"{display_time(application['end_at'])}"
            )
        else:
            activity_window = display_time(application.get("start_at"))
        lines.extend(
            [
                f"> [!abstract]- {summary}",
                f"> <!-- jobmaildesk:application:{application['application_id']} -->",
                ">",
                "> | 字段 | 内容 |",
                "> | --- | --- |",
                f"> | 企业 | {cell(company)} |",
                f"> | 岗位 | {cell(role)} |",
                f"> | 岗位地点 | {cell(location)} |",
                f"> | 招聘项目 | {cell(project)} |",
                f"> | 当前阶段 | {cell(application['current_stage'])} |",
                f"> | 当前状态 | {cell(application['status_label'])} |",
                f"> | 轮次 | {cell(current_round)} |",
                f"> | 投递/收到 | {display_time(application.get('received_at'))} |",
                f"> | 活动窗口 | {activity_window} |",
                f"> | 截止时间 | {display_time(application.get('deadline_at'))} |",
                (
                    "> | 完成时间 | "
                    f"{display_time(application.get('completed_at'), inferred=bool(application.get('completed_at_inferred')))} |"
                ),
                f"> | 下一步 | {cell(next_action)} |",
                ">",
                "> **流程记录**",
            ]
        )
        for event in application["history"]:  # type: ignore[union-attr]
            time_label = "时间待确认"
            if event["time"]:
                time_label = datetime.fromisoformat(str(event["time"])).astimezone(
                    SHANGHAI
                ).strftime("%Y-%m-%d %H:%M")
                if event.get("time_inferred"):
                    time_label = f"约 {time_label}（历史推定）"
            round_label = f"｜{event['round']}" if event["round"] else ""
            checkbox = "x" if event["status"] == "done" else " "
            task_marker = (
                f" <!-- jobmaildesk:{event['task_id']} -->"
                if event["task_id"]
                else ""
            )
            lines.append(
                f"> - [{checkbox}] {time_label}｜{event['stage']}"
                f"{round_label}｜{event['status_label']}{task_marker}"
            )
        lines.extend([">", ""])
    lines.extend([MANAGED_END, ""])
    _atomic_write(
        output,
        prefix.rstrip() + "\n\n" + "\n".join(lines) + suffix.lstrip(),
    )
    return len(applications)
