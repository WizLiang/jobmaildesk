from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import re
from typing import Literal


ChangeType = Literal["new", "update", "cancel"]
TaskStatus = Literal[
    "new",
    "needs_review",
    "confirmed",
    "planned",
    "done",
    "cancelled",
    "expired",
    "irrelevant",
]
Priority = Literal["urgent", "high", "normal", "low"]
ApplicationStatus = Literal["active", "ended", "archived"]
ApplicationWorkflowStatus = Literal[
    "action_required",
    "waiting_next_step",
    "ended",
    "archived",
]


@dataclass(frozen=True)
class MailRecord:
    uid: str
    subject: str
    message_id: str
    sender: str
    received_at: datetime
    body: str
    mailbox: str | None = None
    uidvalidity: str | None = None
    account_fingerprint: str | None = None
    links: tuple[str, ...] = ()
    internal_date: datetime | None = None
    content_truncated: bool = False

    def locator(self) -> dict[str, str] | None:
        if (
            not self.mailbox
            or self.mailbox != self.mailbox.strip()
            or any(ord(character) < 32 for character in self.mailbox)
            or not self.uid.isdigit()
            or int(self.uid) < 1
            or not self.uidvalidity
            or not self.uidvalidity.isdigit()
            or int(self.uidvalidity) < 1
            or not re.fullmatch(
                r"[0-9a-f]{24}",
                self.account_fingerprint or "",
            )
        ):
            return None
        return {
            "mailbox": self.mailbox,
            "uid": self.uid,
            "account_fingerprint": self.account_fingerprint,
            "uidvalidity": self.uidvalidity,
        }


@dataclass(frozen=True)
class ParsedEvent:
    company: str | None
    role: str | None
    recruiting_project: str | None
    event_type: str
    stage: str
    round: str | None
    title: str
    start_at: datetime | None
    end_at: datetime | None
    deadline_at: datetime | None
    source_message_id: str
    source_received_at: datetime
    source_sender: str
    source_url: str | None
    action_summary: str
    requirements: tuple[str, ...]
    matched_keywords: tuple[str, ...]
    confidence: float
    change_type: ChangeType
    location: str | None = None
    location_confidence: float = 0.0
    location_source: str | None = None
    company_confidence: float = 0.0
    company_source: str | None = None
    role_confidence: float = 0.0
    role_source: str | None = None
    sender_scope_hash: str | None = None
    subject_shape_hash: str | None = None
    duration_minutes: int | None = None
    role_raw: str | None = None
    role_canonical: str | None = None
    job_code: str | None = None


@dataclass
class JobTask:
    id: str
    application_id: str
    company: str
    role: str | None
    recruiting_project: str | None
    event_type: str
    stage: str
    round: str | None
    received_at: datetime
    start_at: datetime | None
    end_at: datetime | None
    deadline_at: datetime | None
    priority: Priority
    status: TaskStatus
    change_type: ChangeType
    source_message_hash: str
    research_status: str
    confidence: float
    title: str
    action_summary: str
    application_key: str | None = None
    location: str | None = None
    requirements: list[str] = field(default_factory=list)
    manual_notes: str = ""
    source_sender: str | None = None
    source_url: str | None = None
    mail_locator: dict[str, str] | None = None
    is_ghost: bool = False
    is_actionable: bool = True
    snoozed_until: datetime | None = None
    completed_at: datetime | None = None
    completed_at_inferred: bool = False
    updated_at: datetime | None = None
    deleted_at: datetime | None = None
    deleted_status: str | None = None
    tombstoned: bool = False
    duration_minutes: int | None = None
    application_paused: bool = False

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        for name in (
            "received_at",
            "start_at",
            "end_at",
            "deadline_at",
            "snoozed_until",
            "completed_at",
            "updated_at",
            "deleted_at",
        ):
            value = payload[name]
            payload[name] = value.isoformat() if value else None
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "JobTask":
        def parse_time(name: str) -> datetime | None:
            value = payload.get(name)
            return datetime.fromisoformat(str(value)) if value else None

        return cls(
            id=str(payload["id"]),
            application_id=str(payload["application_id"]),
            company=str(payload.get("company") or "公司待确认"),
            role=str(payload["role"]) if payload.get("role") else None,
            recruiting_project=(
                str(payload["recruiting_project"])
                if payload.get("recruiting_project")
                else None
            ),
            event_type=str(payload.get("event_type") or "notice"),
            stage=str(payload.get("stage") or "招聘通知"),
            round=str(payload["round"]) if payload.get("round") else None,
            received_at=parse_time("received_at"),  # type: ignore[arg-type]
            start_at=parse_time("start_at"),
            end_at=parse_time("end_at"),
            deadline_at=parse_time("deadline_at"),
            priority=str(payload.get("priority") or "normal"),  # type: ignore[arg-type]
            status=str(payload.get("status") or "needs_review"),  # type: ignore[arg-type]
            change_type=str(payload.get("change_type") or "new"),  # type: ignore[arg-type]
            source_message_hash=str(payload.get("source_message_hash") or ""),
            research_status=str(payload.get("research_status") or "not_queued"),
            confidence=float(payload.get("confidence") or 0),
            title=str(payload.get("title") or ""),
            action_summary=str(payload.get("action_summary") or ""),
            application_key=(
                str(payload["application_key"])
                if payload.get("application_key")
                else None
            ),
            location=str(payload["location"]) if payload.get("location") else None,
            requirements=list(payload.get("requirements") or []),  # type: ignore[arg-type]
            manual_notes=str(payload.get("manual_notes") or ""),
            source_sender=(
                str(payload["source_sender"]) if payload.get("source_sender") else None
            ),
            source_url=(
                str(payload["source_url"]) if payload.get("source_url") else None
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
            is_ghost=bool(payload.get("is_ghost", False)),
            is_actionable=bool(payload.get("is_actionable", True)),
            snoozed_until=parse_time("snoozed_until"),
            completed_at=parse_time("completed_at"),
            completed_at_inferred=bool(payload.get("completed_at_inferred", False)),
            updated_at=parse_time("updated_at"),
            deleted_at=parse_time("deleted_at"),
            deleted_status=(
                str(payload["deleted_status"])
                if payload.get("deleted_status")
                else None
            ),
            tombstoned=bool(payload.get("tombstoned", False)),
            duration_minutes=(
                int(payload["duration_minutes"])
                if payload.get("duration_minutes") is not None
                else None
            ),
            application_paused=bool(payload.get("application_paused", False)),
        )


@dataclass
class ApplicationRecord:
    application_key: str
    company_key: str
    company: str
    recruiting_project: str | None
    recruiting_year: int | None
    business_unit: str | None
    role: str | None
    role_aliases: list[str]
    job_code: str | None
    submitted_at: datetime | None
    status: ApplicationStatus
    source: str
    confirmed_by_user: bool
    identity_locked: bool
    location: str | None = None
    aliases: list[str] = field(default_factory=list)
    identity_fingerprint: str = ""
    attempt_sequence: int = 1
    merged_into: str | None = None
    deleted_at: datetime | None = None
    deletion_reason: str | None = None
    deleted_source_hashes: list[str] = field(default_factory=list)
    trashed_task_statuses: dict[str, str] = field(default_factory=dict)
    legacy_application_ids: list[str] = field(default_factory=list)
    identity_evidence: list[str] = field(default_factory=list)
    workflow_status: ApplicationWorkflowStatus = "action_required"
    current_task_id: str | None = None
    waiting_since: datetime | None = None
    workflow_reason: str = ""
    next_action: str = ""
    manual_stage: str | None = None
    manual_stage_status: str = "pending"
    next_stage: str | None = None
    manual_notes: str = ""
    manual_progress_history: list[dict[str, object]] = field(default_factory=list)
    progress_nodes: list[dict[str, object]] = field(default_factory=list)
    revision: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    schema_version: int = 6
    rule_version: str = "identity-registry-v1"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        for name in (
            "submitted_at",
            "waiting_since",
            "created_at",
            "updated_at",
            "deleted_at",
        ):
            value = payload[name]
            payload[name] = value.isoformat() if value else None
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ApplicationRecord":
        def parse_time(name: str) -> datetime | None:
            value = payload.get(name)
            return datetime.fromisoformat(str(value)) if value else None

        def parse_bool(name: str, default: bool = False) -> bool:
            value = payload.get(name, default)
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean")
            return value

        def parse_string_list(name: str) -> list[str]:
            value = payload.get(name, [])
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError(f"{name} must be a list of strings")
            return list(value)

        def parse_string_dict(name: str) -> dict[str, str]:
            value = payload.get(name, {})
            if not isinstance(value, dict) or not all(
                isinstance(key, str) and isinstance(item, str)
                for key, item in value.items()
            ):
                raise ValueError(f"{name} must be a string dictionary")
            return dict(value)

        def parse_manual_progress_history() -> list[dict[str, object]]:
            value = payload.get("manual_progress_history", [])
            if not isinstance(value, list):
                raise ValueError("manual_progress_history must be a list")
            result: list[dict[str, object]] = []
            allowed = {
                "event_at",
                "stage",
                "status",
                "next_stage",
                "lifecycle_status",
            }
            for item in value:
                if not isinstance(item, dict) or set(item) - allowed:
                    raise ValueError("invalid manual_progress_history event")
                event_at = item.get("event_at")
                stage = item.get("stage")
                status_value = item.get("status")
                next_stage = item.get("next_stage")
                lifecycle_status = item.get("lifecycle_status")
                if not isinstance(event_at, str):
                    raise ValueError("manual progress event_at must be an ISO string")
                try:
                    datetime.fromisoformat(event_at)
                except ValueError as exc:
                    raise ValueError("invalid manual progress event_at") from exc
                if not isinstance(stage, str) or not stage.strip():
                    raise ValueError("manual progress stage must be a non-empty string")
                if status_value not in {"pending", "completed"}:
                    raise ValueError("invalid manual progress status")
                if next_stage is not None and not isinstance(next_stage, str):
                    raise ValueError("manual progress next_stage must be a string or null")
                if lifecycle_status is not None and lifecycle_status not in {
                    "active",
                    "ended",
                    "archived",
                }:
                    raise ValueError("invalid manual progress lifecycle_status")
                result.append(
                    {
                        "event_at": event_at,
                        "stage": stage.strip(),
                        "status": status_value,
                        "next_stage": next_stage.strip() if next_stage else None,
                        **(
                            {"lifecycle_status": lifecycle_status}
                            if lifecycle_status is not None
                            else {}
                        ),
                    }
                )
            return result

        def parse_progress_nodes() -> list[dict[str, object]]:
            value = payload.get("progress_nodes", [])
            if not isinstance(value, list):
                raise ValueError("progress_nodes must be a list")
            allowed = {
                "id",
                "source_hash",
                "source_hashes",
                "has_mail_locator",
                "event_at",
                "stage",
                "round",
                "status",
                "next_stage",
                "lifecycle_status",
                "source_task_id",
                "operation_id",
            }
            result: list[dict[str, object]] = []
            for item in value:
                if not isinstance(item, dict) or set(item) - allowed:
                    raise ValueError("invalid progress node")
                if not isinstance(item.get("id"), str) or not item["id"]:
                    raise ValueError("progress node id must be a non-empty string")
                if not isinstance(item.get("event_at"), str):
                    raise ValueError("progress node event_at must be an ISO string")
                datetime.fromisoformat(str(item["event_at"]))
                if not isinstance(item.get("stage"), str) or not item["stage"]:
                    raise ValueError("progress node stage must be a non-empty string")
                if item.get("status") not in {"pending", "completed"}:
                    raise ValueError("invalid progress node status")
                source_hashes = item.get("source_hashes")
                if source_hashes is not None and (
                    not isinstance(source_hashes, list)
                    or not all(
                        isinstance(source_hash, str) and source_hash
                        for source_hash in source_hashes
                    )
                ):
                    raise ValueError("progress node source_hashes must be strings")
                if item.get("round") is not None and not isinstance(
                    item.get("round"),
                    str,
                ):
                    raise ValueError("progress node round must be a string or null")
                result.append(dict(item))
            return result

        year = payload.get("recruiting_year")
        if isinstance(year, bool):
            raise ValueError("recruiting_year must be an integer year or null")
        if year not in {None, ""}:
            try:
                parsed_year = int(year)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "recruiting_year must be an integer year or null"
                ) from exc
            if not 2000 <= parsed_year <= 2100:
                raise ValueError("recruiting_year must be between 2000 and 2100")
        else:
            parsed_year = None
        if parsed_year is None and payload.get("recruiting_project"):
            project_year = re.search(
                r"(?<!\d)(20\d{2})(?!\d)",
                str(payload["recruiting_project"]),
            )
            if project_year:
                parsed_year = int(project_year.group(1))
        status = str(payload.get("status") or "active")
        if status not in {"active", "ended", "archived"}:
            raise ValueError("invalid application status")
        schema_version = payload.get("schema_version", 1)
        if not isinstance(schema_version, int) or schema_version not in {1, 2, 3, 4, 5, 6}:
            raise ValueError("unsupported application schema_version")
        revision = payload.get("revision", 1)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("application revision must be a positive integer")
        attempt_sequence = payload.get("attempt_sequence", 1)
        if (
            isinstance(attempt_sequence, bool)
            or not isinstance(attempt_sequence, int)
            or attempt_sequence < 1
        ):
            raise ValueError("attempt_sequence must be a positive integer")
        workflow_status = str(
            payload.get("workflow_status")
            or ("ended" if status == "ended" else "action_required")
        )
        if workflow_status not in {
            "action_required",
            "waiting_next_step",
            "ended",
            "archived",
        }:
            raise ValueError("invalid application workflow_status")
        manual_stage_status = str(
            payload.get("manual_stage_status") or "pending"
        )
        if manual_stage_status not in {"pending", "completed"}:
            raise ValueError("invalid application manual_stage_status")
        return cls(
            application_key=str(payload["application_key"]),
            company_key=str(payload.get("company_key") or "unknown-company"),
            company=str(payload.get("company") or "公司待确认"),
            recruiting_project=(
                str(payload["recruiting_project"])
                if payload.get("recruiting_project")
                else None
            ),
            recruiting_year=parsed_year,
            business_unit=(
                str(payload["business_unit"])
                if payload.get("business_unit")
                else None
            ),
            role=str(payload["role"]) if payload.get("role") else None,
            role_aliases=parse_string_list("role_aliases"),
            job_code=str(payload["job_code"]) if payload.get("job_code") else None,
            submitted_at=parse_time("submitted_at"),
            status=status,  # type: ignore[arg-type]
            source=str(payload.get("source") or "unknown"),
            confirmed_by_user=parse_bool("confirmed_by_user"),
            identity_locked=parse_bool("identity_locked"),
            location=str(payload["location"]) if payload.get("location") else None,
            aliases=parse_string_list("aliases"),
            identity_fingerprint=str(payload.get("identity_fingerprint") or ""),
            attempt_sequence=attempt_sequence,
            merged_into=(
                str(payload["merged_into"]) if payload.get("merged_into") else None
            ),
            deleted_at=parse_time("deleted_at"),
            deletion_reason=(
                str(payload["deletion_reason"])
                if payload.get("deletion_reason")
                else None
            ),
            deleted_source_hashes=parse_string_list("deleted_source_hashes"),
            trashed_task_statuses=parse_string_dict("trashed_task_statuses"),
            legacy_application_ids=parse_string_list("legacy_application_ids"),
            identity_evidence=parse_string_list("identity_evidence"),
            workflow_status=workflow_status,  # type: ignore[arg-type]
            current_task_id=(
                str(payload["current_task_id"])
                if payload.get("current_task_id")
                else None
            ),
            waiting_since=parse_time("waiting_since"),
            workflow_reason=str(payload.get("workflow_reason") or ""),
            next_action=str(payload.get("next_action") or ""),
            manual_stage=(
                str(payload["manual_stage"])
                if payload.get("manual_stage")
                else None
            ),
            manual_stage_status=manual_stage_status,
            next_stage=(
                str(payload["next_stage"])
                if payload.get("next_stage")
                else None
            ),
            manual_notes=str(payload.get("manual_notes") or ""),
            manual_progress_history=parse_manual_progress_history(),
            progress_nodes=parse_progress_nodes(),
            revision=revision,
            created_at=parse_time("created_at"),
            updated_at=parse_time("updated_at"),
            schema_version=6,
            rule_version=str(
                payload.get("rule_version") or "identity-registry-v1"
            ),
        )


@dataclass(frozen=True)
class ResearchRequest:
    id: str
    task_id: str
    company: str
    role: str | None
    recruiting_project: str | None
    year: int | None
    stage: str
    topics: tuple[str, ...]
    created_at: datetime
    status: str = "pending"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["topics"] = list(self.topics)
        payload["created_at"] = self.created_at.isoformat()
        return payload
