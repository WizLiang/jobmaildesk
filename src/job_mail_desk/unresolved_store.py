from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import json
import re

import yaml

from .frontmatter_cache import load_document
from .identity_pipeline import IdentityDecision
from .markdown_store import FRONTMATTER, _atomic_write
from .models import MailRecord
from .privacy import redact_text
from .stages import is_terminal_stage


def _private_safe(value: str) -> str:
    return re.sub(r"https?://\S+", "[链接已隐藏]", redact_text(value)).strip()


def _semantic_hash(payload: dict[str, object]) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class UnresolvedRecord:
    id: str
    status: str
    resolution_status: str
    reason: str
    company: str | None
    role: str | None
    recruiting_project: str | None
    event_type: str
    stage: str
    round: str | None
    received_at: datetime
    start_at: datetime | None
    end_at: datetime | None
    deadline_at: datetime | None
    action_summary: str
    title: str
    requirements: tuple[str, ...]
    confidence: float
    change_type: str
    candidate_application_keys: tuple[str, ...]
    resolved_application_key: str | None
    resolved_task_id: str | None
    rule_version: str
    location: str | None = None
    mail_locator: dict[str, str] | None = None
    company_confidence: float = 0.0
    company_source: str | None = None
    role_confidence: float = 0.0
    role_source: str | None = None
    sender_scope_hash: str | None = None
    subject_shape_hash: str | None = None
    role_raw: str | None = None
    role_canonical: str | None = None
    job_code: str | None = None
    recruiting_year: int | None = None
    business_unit: str | None = None
    location_confidence: float = 0.0
    location_source: str | None = None
    schema_version: int = 3
    revision: int = 1
    parser_version: str = ""
    semantic_hash: str = ""
    recommended_application_key: str | None = None
    previous_stage: str | None = None
    stage_advanced: bool = False
    recommend_task: bool = False
    recommendation_reasons: tuple[str, ...] = ()
    has_action_link: bool = False
    private_link_ref: str | None = None
    duration_minutes: int | None = None
    confirmation_operation_id: str | None = None
    progress_node_id: str | None = None
    resolved_at: datetime | None = None
    manual_restore: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status,
            "resolution_status": self.resolution_status,
            "reason": self.reason,
            "manual_restore": self.manual_restore,
            "company": self.company,
            "role": self.role,
            "role_raw": self.role_raw,
            "role_canonical": self.role_canonical,
            "location": self.location,
            "location_confidence": self.location_confidence,
            "location_source": self.location_source,
            "recruiting_project": self.recruiting_project,
            "recruiting_year": self.recruiting_year,
            "business_unit": self.business_unit,
            "job_code": self.job_code,
            "event_type": self.event_type,
            "stage": self.stage,
            "round": self.round,
            "received_at": self.received_at.isoformat(),
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "deadline_at": self.deadline_at.isoformat()
            if self.deadline_at
            else None,
            "action_summary": self.action_summary,
            "title": self.title,
            "requirements": list(self.requirements),
            "confidence": self.confidence,
            "change_type": self.change_type,
            "candidate_application_keys": list(
                self.candidate_application_keys
            ),
            "resolved_application_key": self.resolved_application_key,
            "resolved_task_id": self.resolved_task_id,
            "rule_version": self.rule_version,
            "mail_locator": self.mail_locator,
            "company_confidence": self.company_confidence,
            "company_source": self.company_source,
            "role_confidence": self.role_confidence,
            "role_source": self.role_source,
            "sender_scope_hash": self.sender_scope_hash,
            "subject_shape_hash": self.subject_shape_hash,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "parser_version": self.parser_version,
            "semantic_hash": self.semantic_hash,
            "recommended_application_key": self.recommended_application_key,
            "previous_stage": self.previous_stage,
            "stage_advanced": self.stage_advanced,
            "recommend_task": self.recommend_task,
            "recommendation_reasons": list(self.recommendation_reasons),
            "has_action_link": self.has_action_link,
            "private_link_ref": self.private_link_ref,
            "duration_minutes": self.duration_minutes,
            "confirmation_operation_id": self.confirmation_operation_id,
            "progress_node_id": self.progress_node_id,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }


def _pending_semantic_payload(record: UnresolvedRecord) -> dict[str, object]:
    return {
        "resolution_status": record.resolution_status,
        "reason": record.reason,
        "company": record.company,
        "company_confidence": record.company_confidence,
        "company_source": record.company_source,
        "role": record.role,
        "role_raw": record.role_raw,
        "role_canonical": record.role_canonical,
        "role_confidence": record.role_confidence,
        "role_source": record.role_source,
        "recruiting_project": record.recruiting_project,
        "recruiting_year": record.recruiting_year,
        "business_unit": record.business_unit,
        "job_code": record.job_code,
        "location": record.location,
        "location_confidence": record.location_confidence,
        "location_source": record.location_source,
        "event_type": record.event_type,
        "stage": record.stage,
        "round": record.round,
        "received_at": record.received_at.isoformat(),
        "start_at": record.start_at.isoformat() if record.start_at else None,
        "end_at": record.end_at.isoformat() if record.end_at else None,
        "deadline_at": (
            record.deadline_at.isoformat() if record.deadline_at else None
        ),
        "action_summary": record.action_summary,
        "title": record.title,
        "requirements": list(record.requirements),
        "confidence": record.confidence,
        "change_type": record.change_type,
        "candidate_application_keys": list(record.candidate_application_keys),
        "recommended_application_key": record.recommended_application_key,
        "rule_version": record.rule_version,
        "previous_stage": record.previous_stage,
        "stage_advanced": record.stage_advanced,
        "recommend_task": record.recommend_task,
        "recommendation_reasons": list(record.recommendation_reasons),
        "has_action_link": record.has_action_link,
        "duration_minutes": record.duration_minutes,
    }


def _with_current_semantic_hash(record: UnresolvedRecord) -> UnresolvedRecord:
    return replace(
        record,
        semantic_hash=_semantic_hash(_pending_semantic_payload(record)),
    )


def unresolved_from_decision(
    source_hash: str,
    decision: IdentityDecision,
    mail_locator: dict[str, str] | None = None,
    *,
    parser_version: str = "",
    private_link_ref: str | None = None,
) -> UnresolvedRecord:
    event = decision.event
    candidate = decision.candidate
    recommended_key = (
        decision.application_key
        if decision.action == "matched"
        else None
    )
    candidate_keys = tuple(
        item.application_key
        for item in decision.resolution.candidates
        if not (
            decision.action == "batch_context_match"
            and item.application_key == decision.application_key
        )
    )
    critical_time = event.deadline_at or event.end_at or event.start_at
    now = datetime.now(event.source_received_at.tzinfo)
    actionable_recommendation = not (
        event.event_type == "rejection"
        or event.change_type == "cancel"
        or is_terminal_stage(event.stage)
        or (critical_time is not None and critical_time < now)
    )
    reasons = tuple(
        reason
        for reason, present in (
            ("explicit_start", event.start_at is not None),
            ("explicit_end", event.end_at is not None),
            ("explicit_deadline", event.deadline_at is not None),
            ("action_link", event.source_url is not None),
        )
        if present and actionable_recommendation
    )
    record = UnresolvedRecord(
        id=source_hash,
        status="pending",
        resolution_status=(
            "new_application"
            if decision.action == "new_application"
            else decision.resolution.status
        ),
        reason=(
            "distinct-identity-new-application"
            if decision.action == "new_application"
            and decision.resolution.status == "conflict"
            else decision.resolution.reason
        ),
        company=_private_safe(candidate.company or "") or None,
        role=_private_safe(
            candidate.role_canonical or candidate.role or ""
        )
        or None,
        role_raw=_private_safe(candidate.role_raw or "") or None,
        role_canonical=_private_safe(candidate.role_canonical or "") or None,
        location=_private_safe(candidate.location or "") or None,
        location_confidence=candidate.location_confidence,
        location_source=candidate.location_source,
        recruiting_project=(
            _private_safe(candidate.recruiting_project or "") or None
        ),
        recruiting_year=candidate.recruiting_year,
        business_unit=_private_safe(candidate.business_unit or "") or None,
        job_code=_private_safe(candidate.job_code or "") or None,
        event_type=_private_safe(event.event_type),
        stage=_private_safe(event.stage),
        round=_private_safe(event.round or "") or None,
        received_at=event.source_received_at,
        start_at=event.start_at,
        end_at=event.end_at,
        deadline_at=event.deadline_at,
        action_summary=_private_safe(event.action_summary)[:240],
        title=_private_safe(event.title)[:240],
        requirements=tuple(
            _private_safe(item)[:240]
            for item in event.requirements
            if _private_safe(item)
        ),
        confidence=event.confidence,
        change_type=event.change_type,
        candidate_application_keys=candidate_keys,
        resolved_application_key=None,
        resolved_task_id=None,
        rule_version=decision.resolution.rule_version,
        mail_locator=dict(mail_locator) if mail_locator else None,
        company_confidence=event.company_confidence,
        company_source=event.company_source,
        role_confidence=event.role_confidence,
        role_source=event.role_source,
        sender_scope_hash=event.sender_scope_hash,
        subject_shape_hash=event.subject_shape_hash,
        parser_version=parser_version,
        recommended_application_key=recommended_key,
        recommend_task=bool(reasons),
        recommendation_reasons=reasons,
        has_action_link=bool(event.source_url),
        private_link_ref=private_link_ref,
        duration_minutes=event.duration_minutes,
    )
    return _with_current_semantic_hash(record)


def filtered_from_mail(
    source_hash: str,
    mail: MailRecord,
    *,
    parser_version: str,
) -> UnresolvedRecord:
    """Keep a reviewable header for marketing mail, never its body or links.

    A filtered message has no parsed event. Do not infer application identity,
    dates or actions just to display it in the separate filtered list.
    """
    title = re.sub(
        r"(?i)(?:\b[a-z][a-z0-9+.-]*://|www\.|mailto:)\S+",
        "[链接已隐藏]",
        mail.subject,
    )
    title = re.sub(
        r"(?:尊敬的?|亲爱的?)\s*[^，,!！:：【】\s]{1,12}(?:同学|先生|女士)",
        "[称呼已隐藏]",
        title,
    )
    # Unicode word boundaries do not separate Chinese labels from an email.
    title = re.sub(
        r"(?i)(?<![a-z0-9._%+-])[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}",
        "[邮箱已隐藏]",
        title,
    )
    title = re.sub(
        r"(?i)(学号|考生号|身份证号)\s*[:：=]?\s*[a-z0-9]{6,}",
        lambda match: f"{match.group(1)}：[已隐藏]",
        title,
    )
    title = re.sub(
        r"(?i)(通行证|验证码|授权码|密码|口令|token|code)\s*[:：=]\s*[^\s，,;；]+",
        lambda match: f"{match.group(1)}：[已隐藏]",
        title,
    )
    record = UnresolvedRecord(
        id=source_hash,
        status="filtered",
        resolution_status="unresolved",
        reason="recruiting-marketing",
        company=None,
        role=None,
        recruiting_project=None,
        event_type="notice",
        stage="招聘通知",
        round=None,
        received_at=mail.received_at,
        start_at=None,
        end_at=None,
        deadline_at=None,
        action_summary="识别为招聘宣传，已自动过滤；可恢复到待处理后人工确认。",
        title=_private_safe(title)[:240],
        requirements=(),
        confidence=0.0,
        change_type="new",
        candidate_application_keys=(),
        resolved_application_key=None,
        resolved_task_id=None,
        rule_version="marketing-filter-v1",
        parser_version=parser_version,
        mail_locator=mail.locator(),
    )
    return _with_current_semantic_hash(record)


def _frontmatter_block(content: str) -> str | None:
    """Return the YAML block, ending only at a line that is exactly '---'.

    Mail footers contain rules such as "----------------", and a quoted scalar
    holding one would otherwise cut the frontmatter in half and make the whole
    pending store unreadable.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER:
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONTMATTER:
            return "\n".join(lines[1:index])
    return None


def _unresolved_frontmatter(path: Path) -> "callable":
    def extract(content: str) -> str | None:
        if not content.startswith(f"{FRONTMATTER}\n"):
            raise ValueError(f"待归属文件缺少 frontmatter：{path}")
        return _frontmatter_block(content)

    return extract


def render_unresolved(record: UnresolvedRecord) -> str:
    frontmatter = yaml.safe_dump(
        record.to_dict(),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return (
        f"{FRONTMATTER}\n{frontmatter}\n{FRONTMATTER}\n\n"
        f"# {record.company or '公司待确认'}｜{record.stage}\n\n"
        "## 邮件摘要\n\n"
        f"- 标题：{record.title or '未提供'}\n"
        f"- 类型：{record.event_type}\n"
        f"- 岗位地点：{record.location or '待确认'}\n"
        f"- 动作：{record.action_summary or '待人工确认'}\n\n"
        "## 待归属原因\n\n"
        f"- {record.reason}\n\n"
        "## 候选申请\n\n"
        + (
            "\n".join(
                f"- `{key}`" for key in record.candidate_application_keys
            )
            if record.candidate_application_keys
            else "- 暂无唯一候选"
        )
        + "\n\n## 隐私边界\n\n"
        "- 不保存邮件正文、发件人地址、私人通知链接或认证参数。\n"
    )


class UnresolvedStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, source_hash: str) -> Path:
        return self.directory / f"{source_hash}.md"

    def save(self, record: UnresolvedRecord) -> Path:
        path = self.path_for(record.id)
        _atomic_write(path, render_unresolved(record))
        return path

    def put_pending(self, record: UnresolvedRecord) -> UnresolvedRecord:
        existing = self.load(record.id)
        if existing and (existing.status in {"resolved", "ignored"} or existing.manual_restore):
            return existing
        record = _with_current_semantic_hash(record)
        if existing:
            existing_hash = _semantic_hash(_pending_semantic_payload(existing))
            semantic_changed = record.semantic_hash != existing_hash
            record = replace(
                record,
                revision=existing.revision + 1 if semantic_changed or existing.status == "filtered" else existing.revision,
                mail_locator=record.mail_locator or existing.mail_locator,
                private_link_ref=record.private_link_ref or existing.private_link_ref,
            )
        self.save(record)
        return record

    def put_filtered(self, record: UnresolvedRecord) -> UnresolvedRecord:
        """Create a filtered header once; existing human choices remain final."""
        if record.status != "filtered" or record.reason != "recruiting-marketing":
            raise ValueError("只能保存自动过滤的招聘宣传记录。")
        existing = self.load(record.id)
        if existing:
            return self.filter_marketing(record.id, parser_version=record.parser_version)
        record = _with_current_semantic_hash(record)
        self.save(record)
        return record

    def load(self, source_hash: str) -> UnresolvedRecord | None:
        return next((item for item in self.all() if item.id == source_hash), None)

    def resolve(
        self,
        source_hash: str,
        *,
        application_key: str,
        task_id: str | None,
        operation_id: str | None = None,
        progress_node_id: str | None = None,
    ) -> UnresolvedRecord:
        record = self.load(source_hash)
        if not record:
            raise KeyError(source_hash)
        if record.status == "resolved":
            if (
                record.resolved_application_key == application_key
                and record.resolved_task_id == task_id
            ):
                return record
            raise ValueError("待归属记录已经归入其他申请。")
        if record.status != "pending":
            raise ValueError("已忽略或已过滤的记录不能直接归属。")
        updated = replace(
            record,
            status="resolved",
            resolved_application_key=application_key,
            resolved_task_id=task_id,
            confirmation_operation_id=operation_id,
            progress_node_id=progress_node_id,
            resolved_at=datetime.now().astimezone(),
        )
        self.save(updated)
        return updated

    def ignore(self, source_hash: str) -> UnresolvedRecord:
        record = self.load(source_hash)
        if not record:
            raise KeyError(source_hash)
        if record.status == "ignored":
            return record
        if record.status == "resolved":
            raise ValueError("已归属的记录不能忽略。")
        updated = replace(record, status="ignored", revision=record.revision + 1, manual_restore=False)
        self.save(updated)
        return updated

    def restore_ignored(self, source_hash: str, expected_revision: int) -> UnresolvedRecord:
        record = self.load(source_hash)
        if not record or record.status != "ignored" or record.revision != expected_revision:
            raise ValueError("记录已变化，请刷新已忽略列表后重试。")
        if record.resolved_application_key or record.resolved_task_id:
            raise ValueError("已有归属的邮件不能从这里恢复。")
        updated = replace(record, status="pending", revision=record.revision + 1, manual_restore=True)
        self.save(updated)
        return updated

    def restore_filtered(self, source_hash: str, expected_revision: int) -> UnresolvedRecord:
        """Restore only after the service has checked protected outcome indexes."""
        record = self.load(source_hash)
        if (not record or record.status != "filtered"
                or type(expected_revision) is not int or record.revision != expected_revision):
            raise ValueError("记录已变化，请刷新自动过滤列表后重试。")
        if (record.resolved_application_key or record.resolved_task_id
                or record.confirmation_operation_id or record.progress_node_id or record.resolved_at):
            raise ValueError("已有归属的邮件不能从这里恢复。")
        updated = replace(record, status="pending", revision=record.revision + 1, manual_restore=True)
        self.save(updated)
        return updated

    def filter_marketing(self, source_hash: str, *, parser_version: str) -> UnresolvedRecord:
        """Withdraw only an unconfirmed broadcast, preserving its source ID."""
        record = self.load(source_hash)
        if not record:
            raise KeyError(source_hash)
        if (record.status not in {"pending", "filtered"} or record.manual_restore
                or record.resolved_application_key or record.resolved_task_id
                or record.confirmation_operation_id or record.progress_node_id or record.resolved_at):
            return record
        updated = _with_current_semantic_hash(replace(
            record,
            status="filtered",
            reason="recruiting-marketing",
            parser_version=parser_version,
            revision=record.revision + (record.status != "filtered"),
            recommend_task=False,
            recommendation_reasons=(),
        ))
        self.save(updated)
        return updated

    def all(self) -> list[UnresolvedRecord]:
        records: list[UnresolvedRecord] = []
        for path in sorted(self.directory.glob("*.md")):
            _content, payload = load_document(path, _unresolved_frontmatter(path))
            if payload is None:
                continue
            records.append(
                UnresolvedRecord(
                    id=str(payload["id"]),
                    status=str(payload.get("status") or "pending"),
                    resolution_status=str(
                        payload.get("resolution_status") or "unresolved"
                    ),
                    reason=str(payload.get("reason") or "unknown"),
                    manual_restore=payload.get("manual_restore") is True,
                    company=str(payload["company"])
                    if payload.get("company")
                    else None,
                    role=str(payload["role"])
                    if payload.get("role")
                    else None,
                    role_raw=(
                        str(payload["role_raw"])
                        if payload.get("role_raw")
                        else None
                    ),
                    role_canonical=(
                        str(payload["role_canonical"])
                        if payload.get("role_canonical")
                        else None
                    ),
                    location=str(payload["location"])
                    if payload.get("location")
                    else None,
                    location_confidence=float(
                        payload.get("location_confidence") or 0
                    ),
                    location_source=(
                        str(payload["location_source"])
                        if payload.get("location_source")
                        else None
                    ),
                    recruiting_project=str(payload["recruiting_project"])
                    if payload.get("recruiting_project")
                    else None,
                    recruiting_year=(
                        int(payload["recruiting_year"])
                        if payload.get("recruiting_year") not in {None, ""}
                        else None
                    ),
                    business_unit=(
                        str(payload["business_unit"])
                        if payload.get("business_unit")
                        else None
                    ),
                    job_code=(
                        str(payload["job_code"])
                        if payload.get("job_code")
                        else None
                    ),
                    event_type=str(payload.get("event_type") or "notice"),
                    stage=str(payload.get("stage") or "招聘通知"),
                    round=str(payload["round"])
                    if payload.get("round")
                    else None,
                    received_at=datetime.fromisoformat(
                        str(payload["received_at"])
                    ),
                    start_at=datetime.fromisoformat(str(payload["start_at"]))
                    if payload.get("start_at")
                    else None,
                    end_at=datetime.fromisoformat(str(payload["end_at"]))
                    if payload.get("end_at")
                    else None,
                    deadline_at=datetime.fromisoformat(
                        str(payload["deadline_at"])
                    )
                    if payload.get("deadline_at")
                    else None,
                    action_summary=str(payload.get("action_summary") or ""),
                    title=str(payload.get("title") or ""),
                    requirements=tuple(
                        str(item) for item in payload.get("requirements", [])
                    ),
                    confidence=float(payload.get("confidence") or 0),
                    change_type=str(payload.get("change_type") or "new"),
                    candidate_application_keys=tuple(
                        str(item)
                        for item in payload.get(
                            "candidate_application_keys", []
                        )
                    ),
                    resolved_application_key=(
                        str(payload["resolved_application_key"])
                        if payload.get("resolved_application_key")
                        else None
                    ),
                    resolved_task_id=(
                        str(payload["resolved_task_id"])
                        if payload.get("resolved_task_id")
                        else None
                    ),
                    rule_version=str(
                        payload.get("rule_version")
                        or "identity-registry-v1"
                    ),
                    mail_locator=(
                        {
                            str(key): str(value)
                            for key, value in payload["mail_locator"].items()
                            if value is not None
                        }
                        if isinstance(payload.get("mail_locator"), dict)
                        else None
                    ),
                    company_confidence=float(
                        payload.get("company_confidence") or 0
                    ),
                    company_source=(
                        str(payload["company_source"])
                        if payload.get("company_source")
                        else None
                    ),
                    role_confidence=float(payload.get("role_confidence") or 0),
                    role_source=(
                        str(payload["role_source"])
                        if payload.get("role_source")
                        else None
                    ),
                    sender_scope_hash=(
                        str(payload["sender_scope_hash"])
                        if payload.get("sender_scope_hash")
                        else None
                    ),
                    subject_shape_hash=(
                        str(payload["subject_shape_hash"])
                        if payload.get("subject_shape_hash")
                        else None
                    ),
                    schema_version=int(payload.get("schema_version") or 1),
                    revision=max(1, int(payload.get("revision") or 1)),
                    parser_version=str(payload.get("parser_version") or ""),
                    semantic_hash=str(payload.get("semantic_hash") or ""),
                    recommended_application_key=(
                        str(payload["recommended_application_key"])
                        if payload.get("recommended_application_key")
                        else None
                    ),
                    previous_stage=(
                        str(payload["previous_stage"])
                        if payload.get("previous_stage")
                        else None
                    ),
                    stage_advanced=bool(payload.get("stage_advanced", False)),
                    recommend_task=bool(payload.get("recommend_task", False)),
                    recommendation_reasons=tuple(
                        str(item)
                        for item in payload.get("recommendation_reasons", [])
                    ),
                    has_action_link=bool(payload.get("has_action_link", False)),
                    private_link_ref=(
                        str(payload["private_link_ref"])
                        if payload.get("private_link_ref")
                        else None
                    ),
                    duration_minutes=(
                        int(payload["duration_minutes"])
                        if payload.get("duration_minutes") is not None
                        else None
                    ),
                    confirmation_operation_id=(
                        str(payload["confirmation_operation_id"])
                        if payload.get("confirmation_operation_id")
                        else None
                    ),
                    progress_node_id=(
                        str(payload["progress_node_id"])
                        if payload.get("progress_node_id")
                        else None
                    ),
                    resolved_at=(
                        datetime.fromisoformat(str(payload["resolved_at"]))
                        if payload.get("resolved_at")
                        else None
                    ),
                )
            )
        return records
