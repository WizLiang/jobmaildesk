"""Detect and announce newly recognised mails after a scan.

Scanning never creates facts; it only files pending reviews. The user still
wants to know that something new is waiting, so each scan compares the set of
pending review records (and their revisions) before and after, and a visible,
click-to-open notification is raised for anything new. The message carries
company and stage only - never a subject line or body text.
"""
from __future__ import annotations

import logging
from typing import Callable

from .config import Settings
from .unresolved_store import UnresolvedRecord, UnresolvedStore

LOGGER = logging.getLogger(__name__)
NEW_MAIL_TITLE = "JobMailDesk · 有新的招聘邮件待确认"


def pending_snapshot(store: UnresolvedStore) -> dict[str, int]:
    """``{source_hash: revision}`` for every pending review."""
    return {
        record.id: int(record.revision)
        for record in store.all()
        if record.status == "pending"
    }


def new_pending_reviews(
    before: dict[str, int],
    store: UnresolvedStore,
) -> list[UnresolvedRecord]:
    """Pending reviews that appeared or changed since ``before``."""
    fresh: list[UnresolvedRecord] = []
    for record in store.all():
        if record.status != "pending":
            continue
        previous = before.get(record.id)
        if previous is None or int(record.revision) > previous:
            fresh.append(record)
    fresh.sort(key=lambda record: record.received_at, reverse=True)
    return fresh


def describe_new_reviews(records: list[UnresolvedRecord]) -> str:
    """One line naming up to three companies; no mail content."""
    labels: list[str] = []
    for record in records:
        label = record.company or "公司待确认"
        if record.stage:
            label = f"{label}·{record.stage}"
        if label not in labels:
            labels.append(label)
    shown = "、".join(labels[:3])
    more = f" 等 {len(records)} 封" if len(records) > 3 else ""
    return f"{shown}{more}，点击打开待处理列表确认归属。"


def announce_new_reviews(
    records: list[UnresolvedRecord],
    settings: Settings,
    notify: Callable[[str, str], bool],
) -> bool:
    if not records or not getattr(settings, "notify_new_mail", True):
        return False
    try:
        return bool(notify(NEW_MAIL_TITLE, describe_new_reviews(records)))
    except Exception:  # noqa: BLE001 - a notification must never break a scan
        LOGGER.exception("新邮件通知失败")
        return False
