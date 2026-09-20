from __future__ import annotations

import json
import re
from datetime import datetime
from hashlib import sha256

from .models import ApplicationRecord
from .unresolved_store import UnresolvedRecord


REQUEST_ID = re.compile(r"^op1_[0-9a-f]{32}$")


def new_request_id() -> str:
    import uuid

    return "op1_" + uuid.uuid4().hex


def validate_confirmation_request(
    *,
    request_id: str,
    review: UnresolvedRecord,
    expected_review_revision: int,
    application: ApplicationRecord | None,
    expected_application_revision: int | None,
) -> None:
    if not REQUEST_ID.fullmatch(request_id):
        raise ValueError("确认请求 ID 无效。")
    if review.revision != expected_review_revision:
        raise ValueError("待处理内容已更新，请重新核对后确认。")
    if (
        application
        and expected_application_revision is not None
        and application.revision != expected_application_revision
    ):
        raise ValueError("申请链已被更新，请重新核对后确认。")


def progress_node_id(source_hash: str) -> str:
    return "pgn1_" + sha256(f"mail\0{source_hash}".encode("utf-8")).hexdigest()[:24]


def semantic_progress_node_id(review: UnresolvedRecord) -> str:
    payload = {
        "company": review.company,
        "role": review.role_canonical or review.role,
        "project": review.recruiting_project,
        "job_code": review.job_code,
        "event_type": review.event_type,
        "stage": review.stage,
        "round": review.round,
        "start_at": review.start_at.isoformat() if review.start_at else None,
        "end_at": review.end_at.isoformat() if review.end_at else None,
        "deadline_at": (
            review.deadline_at.isoformat() if review.deadline_at else None
        ),
        "duration_minutes": review.duration_minutes,
        "action_summary": " ".join(review.action_summary.split()),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "pgn2_" + sha256(
        f"mail-semantic\0{serialized}".encode("utf-8")
    ).hexdigest()[:24]


def append_progress_node(
    application: ApplicationRecord,
    review: UnresolvedRecord,
    *,
    operation_id: str,
    stage: str,
    status: str,
    next_stage: str | None,
    task_id: str | None,
    round: str | None = None,
) -> str:
    node_id = semantic_progress_node_id(review)
    existing = next(
        (
            item
            for item in application.progress_nodes
            if str(item.get("id") or "") == node_id
        ),
        None,
    )
    if existing is not None:
        source_hashes = {
            str(value)
            for value in existing.get("source_hashes") or ()
            if value
        }
        if existing.get("source_hash"):
            source_hashes.add(str(existing["source_hash"]))
        if review.id not in source_hashes:
            source_hashes.add(review.id)
            existing["source_hashes"] = sorted(source_hashes)
            application.revision += 1
        return node_id
    application.progress_nodes.append(
        {
            "id": node_id,
            "source_hash": review.id,
            "source_hashes": [review.id],
            "has_mail_locator": bool(review.mail_locator),
            "event_at": review.received_at.isoformat(),
            "stage": stage,
            "round": round,
            "status": status,
            "next_stage": next_stage,
            "lifecycle_status": application.status,
            "source_task_id": task_id,
            "operation_id": operation_id,
        }
    )
    application.progress_nodes.sort(
        key=lambda item: (
            datetime.fromisoformat(str(item["event_at"])),
            str(item["id"]),
        )
    )
    application.revision += 1
    return node_id
