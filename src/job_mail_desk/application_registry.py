from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime
from pathlib import Path

import yaml

from .frontmatter_cache import load_document
from .markdown_store import FRONTMATTER, _atomic_write
from .models import ApplicationRecord
from .normalization import (
    canonical_company,
    canonical_role,
    is_invalid_role,
    normalize_company_project,
    role_key,
)
from .parser import SHANGHAI
from .progress import read_progress_entries
from .stages import is_same_stage, is_terminal_stage


JOB_CODE = re.compile(r"\b([A-Za-z]{1,4}\d{4,})\b", re.IGNORECASE)
YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
SUBMITTED_DATE = re.compile(
    r"(?P<year>20\d{2})[-./年](?P<month>\d{1,2})[-./月](?P<day>\d{1,2})日?"
    r"[^。；;]{0,20}(?:投递|网申)"
)
APPLICATION_MARKER = re.compile(
    r"<!--\s*jobmaildesk:application:(?P<id>(?:app-)?[0-9a-f]{20,64})\s*-->"
)
PLACEHOLDER_COMPANIES = {"", "公司待确认", "未知公司", "待确认"}


def _normalized_key(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value).casefold()


def _company_key(company: str) -> str:
    normalized, _ = normalize_company_project(company)
    return _normalized_key(normalized) or "unknown-company"


def _program(role: str, project: str | None = None) -> str | None:
    combined = " ".join(item for item in (project, role) if item)
    for label in ("JDS", "TET", "TGT"):
        if re.search(rf"(?<![A-Za-z]){label}(?![A-Za-z])", combined, re.I):
            return label
    return project or None


def _business_unit(company: str, role: str, project: str | None = None) -> str | None:
    combined = f"{company} {role} {project or ''}"
    if "雷火" in combined:
        return "雷火事业群"
    if "互娱" in combined:
        return "互娱事业群"
    return None


def _submitted_at(status: str, action: str) -> datetime | None:
    match = SUBMITTED_DATE.search(f"{status} {action}")
    if not match:
        return None
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            tzinfo=SHANGHAI,
        )
    except ValueError:
        return None


def stable_application_key(
    *,
    company: str,
    role: str | None,
    recruiting_project: str | None,
    recruiting_year: int | None,
    business_unit: str | None,
    job_code: str | None,
    location: str | None = None,
    legacy_application_id: str | None = None,
) -> str:
    if legacy_application_id and re.fullmatch(
        r"app-[0-9a-f]{24}",
        legacy_application_id,
    ):
        return legacy_application_id
    company_value = _company_key(company)
    if job_code:
        identity = f"{company_value}|job-code|{job_code.upper()}"
    elif legacy_application_id:
        identity = f"legacy-application|{legacy_application_id}"
    else:
        identity = "|".join(
            (
                company_value,
                _normalized_key(recruiting_project or "unknown-project"),
                str(recruiting_year or "unknown-year"),
                _normalized_key(business_unit or "unknown-unit"),
                role_key(canonical_role(role) or role or "unknown-role"),
            )
        )
    if location and not legacy_application_id:
        identity += f"|location|{_normalized_key(location)}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"app-{digest}"


def identity_fingerprint(
    *,
    company: str,
    role: str | None,
    recruiting_project: str | None,
    recruiting_year: int | None,
    business_unit: str | None,
    job_code: str | None,
    location: str | None = None,
) -> str:
    """Return a comparable identity digest without using it as the record key."""
    identity = "|".join(
        (
            _company_key(company),
            role_key(canonical_role(role) or role or "unknown-role"),
            _normalized_key(recruiting_project or "unknown-project"),
            str(recruiting_year or "unknown-year"),
            _normalized_key(business_unit or "unknown-unit"),
            (job_code or "unknown-code").upper(),
        )
    )
    if location:
        identity += f"|location|{_normalized_key(location)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _record_fingerprint(record: ApplicationRecord) -> str:
    return identity_fingerprint(
        company=record.company,
        role=record.role,
        recruiting_project=record.recruiting_project,
        recruiting_year=record.recruiting_year,
        business_unit=record.business_unit,
        job_code=record.job_code,
        location=record.location,
    )


def application_from_progress_entry(
    entry: dict[str, str],
    *,
    now: datetime | None = None,
) -> ApplicationRecord | None:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    company, normalized_project = normalize_company_project(
        entry.get("company") or "公司待确认",
        entry.get("project") or None,
    )
    raw_role = entry.get("role_raw") or entry.get("role") or ""
    role_value = entry.get("role") or raw_role
    role = None if is_invalid_role(role_value) else canonical_role(role_value)
    code_match = JOB_CODE.search(raw_role)
    job_code = (
        str(entry.get("job_code") or "").strip().upper()
        or (code_match.group(1).upper() if code_match else None)
    )
    year_match = YEAR.search(" ".join((raw_role, normalized_project or "")))
    recruiting_year = int(year_match.group(1)) if year_match else None
    project = _program(raw_role, normalized_project)
    business_unit = _business_unit(company, raw_role, normalized_project)
    legacy_id = entry.get("application_id") or ""
    identifiable = bool(
        job_code
        or legacy_id
        or (
            company not in PLACEHOLDER_COMPANIES
            and canonical_company(company) is not None
            and (role or project)
        )
    )
    if not identifiable:
        return None
    evidence = ["progress-ledger-row"]
    if job_code:
        evidence.append(f"job-code:{job_code}")
    if project:
        evidence.append(f"project:{project}")
    if legacy_id:
        evidence.append("legacy-application-id")
    if recruiting_year is None:
        evidence.append("recruiting-year-unresolved")
    key = stable_application_key(
        company=company,
        role=role,
        recruiting_project=project,
        recruiting_year=recruiting_year,
        business_unit=business_unit,
        job_code=job_code,
        location=entry.get("location") or None,
        legacy_application_id=legacy_id or None,
    )
    status_text = entry.get("status") or ""
    archived = any(label in status_text for label in ("已归档", "已过期"))
    ended = is_terminal_stage(status_text)
    return ApplicationRecord(
        application_key=key,
        company_key=_company_key(company),
        company=company,
        recruiting_project=project,
        recruiting_year=recruiting_year,
        business_unit=business_unit,
        role=role,
        role_aliases=[raw_role] if raw_role and raw_role != role else [],
        job_code=job_code,
        submitted_at=_submitted_at(
            entry.get("status") or "",
            entry.get("action") or "",
        ),
        status="archived" if archived else ("ended" if ended else "active"),
        source="progress-ledger",
        confirmed_by_user=True,
        identity_locked=True,
        location=entry.get("location") or None,
        legacy_application_ids=[legacy_id] if legacy_id else [],
        identity_evidence=evidence,
        created_at=current,
        updated_at=current,
    )


def application_from_user_payload(
    payload: dict[str, object],
    *,
    now: datetime | None = None,
) -> ApplicationRecord:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    company, project = normalize_company_project(
        str(payload.get("company") or "").strip(),
        str(payload.get("recruiting_project") or "").strip() or None,
    )
    role_value = str(payload.get("role") or "").strip()
    role = None if is_invalid_role(role_value) else canonical_role(role_value)
    job_code = str(payload.get("job_code") or "").strip().upper() or None
    business_unit = str(payload.get("business_unit") or "").strip() or None
    location = str(payload.get("location") or "").strip() or None
    year_value = payload.get("recruiting_year")
    recruiting_year = int(year_value) if year_value not in {None, ""} else None
    if company in PLACEHOLDER_COMPANIES:
        raise ValueError("请填写明确的公司名称。")
    if not any((role, project, job_code)):
        raise ValueError("岗位、招聘项目或职位编号至少填写一项。")
    # User-created records are opaque identities. Their key must not change
    # when the user later corrects company, role, or project fields.
    key = f"app-{secrets.token_hex(12)}"
    return ApplicationRecord(
        application_key=key,
        company_key=_company_key(company),
        company=company,
        recruiting_project=project,
        recruiting_year=recruiting_year,
        business_unit=business_unit,
        role=role,
        role_aliases=[role_value] if role_value and role_value != role else [],
        job_code=job_code,
        submitted_at=current,
        status="active",
        source="desktop-ui",
        confirmed_by_user=True,
        identity_locked=True,
        location=location,
        identity_fingerprint=identity_fingerprint(
            company=company,
            role=role,
            recruiting_project=project,
            recruiting_year=recruiting_year,
            business_unit=business_unit,
            job_code=job_code,
            location=location,
        ),
        attempt_sequence=int(payload.get("attempt_sequence") or 1),
        identity_evidence=["desktop-ui-confirmed"],
        workflow_status="action_required",
        next_action=str(payload.get("next_action") or "").strip(),
        manual_stage=(
            str(payload.get("manual_stage") or payload.get("stage") or "").strip()
            or None
        ),
        manual_stage_status=str(
            payload.get("manual_stage_status") or "pending"
        ),
        next_stage=str(payload.get("next_stage") or "").strip() or None,
        manual_notes=str(payload.get("manual_notes") or "").strip(),
        created_at=current,
        updated_at=current,
    )


def update_application_from_user_payload(
    record: ApplicationRecord,
    payload: dict[str, object],
) -> ApplicationRecord:
    previous_lifecycle_status = record.status
    previous_progress = (
        record.manual_stage,
        record.manual_stage_status,
        record.next_stage,
    )
    company, project = normalize_company_project(
        str(payload.get("company") or record.company).strip(),
        str(
            payload.get("recruiting_project")
            if "recruiting_project" in payload
            else record.recruiting_project or ""
        ).strip()
        or None,
    )
    if company in PLACEHOLDER_COMPANIES:
        raise ValueError("请填写明确的公司名称。")
    role_value = str(
        payload.get("role") if "role" in payload else record.role or ""
    ).strip()
    role = None if is_invalid_role(role_value) else canonical_role(role_value)
    record.company = company
    record.company_key = _company_key(company)
    record.role = role
    if "location" in payload:
        record.location = str(payload.get("location") or "").strip() or None
    record.recruiting_project = project
    record.business_unit = (
        str(payload.get("business_unit") or "").strip() or None
        if "business_unit" in payload
        else record.business_unit
    )
    record.job_code = (
        str(payload.get("job_code") or "").strip().upper() or None
        if "job_code" in payload
        else record.job_code
    )
    if "recruiting_year" in payload:
        value = payload.get("recruiting_year")
        record.recruiting_year = int(value) if value not in {None, ""} else None
    explicit_lifecycle_status: str | None = None
    if "status" in payload:
        status = str(payload["status"])
        if status not in {"active", "ended", "archived"}:
            raise ValueError("申请状态无效。")
        explicit_lifecycle_status = status
        record.status = status  # type: ignore[assignment]
    if "next_action" in payload:
        record.next_action = str(payload.get("next_action") or "").strip()
    if "manual_stage" in payload or "stage" in payload:
        record.manual_stage = (
            str(payload.get("manual_stage") or payload.get("stage") or "").strip()
            or None
        )
        if record.manual_stage and is_terminal_stage(record.manual_stage):
            record.status = "ended"
    if "manual_stage_status" in payload:
        stage_status = str(payload.get("manual_stage_status") or "pending")
        if stage_status not in {"pending", "completed"}:
            raise ValueError("进度状态无效。")
        same_stage_regression = (
            explicit_lifecycle_status is None
            and record.manual_stage_status == "completed"
            and stage_status == "pending"
            and is_same_stage(previous_progress[0], record.manual_stage)
        )
        if not same_stage_regression:
            record.manual_stage_status = stage_status
    if "next_stage" in payload:
        record.next_stage = str(payload.get("next_stage") or "").strip() or None
        record.next_action = (
            f"进入{record.next_stage}阶段" if record.next_stage else ""
        )
    if "manual_notes" in payload:
        record.manual_notes = str(payload.get("manual_notes") or "").strip()
    record.confirmed_by_user = True
    record.identity_locked = True
    record.identity_evidence = sorted(
        set(record.identity_evidence) | {"desktop-ui-edit"}
    )
    record.identity_fingerprint = _record_fingerprint(record)
    current_progress = (
        record.manual_stage,
        record.manual_stage_status,
        record.next_stage,
    )
    progress_fields_present = any(
        name in payload
        for name in ("manual_stage", "stage", "manual_stage_status", "next_stage")
    )
    if (
        explicit_lifecycle_status is None
        and progress_fields_present
        and record.manual_stage
        and not is_terminal_stage(record.manual_stage)
        and record.status != "archived"
    ):
        record.status = "active"
    if explicit_lifecycle_status is not None:
        record.status = explicit_lifecycle_status  # type: ignore[assignment]
    lifecycle_changed = record.status != previous_lifecycle_status
    if (
        (progress_fields_present and current_progress != previous_progress)
        or lifecycle_changed
    ):
        record.manual_progress_history.append(
            {
                "event_at": datetime.now(SHANGHAI).isoformat(),
                "stage": record.manual_stage or "进度更新",
                "status": record.manual_stage_status,
                "next_stage": record.next_stage,
                "lifecycle_status": record.status,
            }
        )
    record.revision += 1
    record.schema_version = 6
    return record


def preview_progress_applications(path: Path | None) -> list[ApplicationRecord]:
    records: dict[str, ApplicationRecord] = {}
    for entry in read_progress_entries(path):
        record = application_from_progress_entry(entry)
        if record is None:
            continue
        existing = records.get(record.application_key)
        if not existing:
            records[record.application_key] = record
            continue
        existing.role_aliases = sorted(
            set(existing.role_aliases) | set(record.role_aliases)
        )
        existing.legacy_application_ids = sorted(
            set(existing.legacy_application_ids)
            | set(record.legacy_application_ids)
        )
        existing.identity_evidence = sorted(
            set(existing.identity_evidence) | set(record.identity_evidence)
        )
        if not existing.submitted_at and record.submitted_at:
            existing.submitted_at = record.submitted_at
        if record.status == "ended":
            existing.status = "ended"
        for field_name in (
            "recruiting_project",
            "recruiting_year",
            "business_unit",
            "job_code",
            "location",
        ):
            existing_value = getattr(existing, field_name)
            record_value = getattr(record, field_name)
            if existing_value is None and record_value is not None:
                setattr(existing, field_name, record_value)
            elif (
                existing_value is not None
                and record_value is not None
                and existing_value != record_value
            ):
                existing.identity_evidence = sorted(
                    set(existing.identity_evidence)
                    | {f"conflict:{field_name}"}
                )
                existing.confirmed_by_user = False
                existing.identity_locked = False
        if not existing.role and record.role:
            existing.role = record.role
        existing.updated_at = max(
            item for item in (existing.updated_at, record.updated_at) if item
        )
    return sorted(
        records.values(),
        key=lambda item: (item.company, item.role or "", item.application_key),
    )


def render_application(record: ApplicationRecord) -> str:
    frontmatter = yaml.safe_dump(
        record.to_dict(),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return (
        f"{FRONTMATTER}\n{frontmatter}\n{FRONTMATTER}\n\n"
        f"# {record.company}｜{record.role or '岗位待确认'}\n\n"
        "## 身份边界\n\n"
        f"- 申请键：`{record.application_key}`\n"
        f"- 招聘项目：{record.recruiting_project or '待确认'}\n"
        f"- 招聘年份：{record.recruiting_year or '待确认'}\n"
        f"- 事业群：{record.business_unit or '待确认'}\n"
        f"- 职位编号：{record.job_code or '待确认'}\n"
        f"- 岗位地点：{record.location or '待确认'}\n"
        f"- 人工锁定：{'是' if record.identity_locked else '否'}\n\n"
        "## 证据\n\n"
        + (
            "\n".join(f"- {item}" for item in record.identity_evidence)
            if record.identity_evidence
            else "- 暂无"
        )
        + "\n\n## 说明\n\n"
        "- 本文件只保存申请身份，不保存邮件正文。\n"
        "- 已人工锁定的身份不得被邮件重放或解析器升级静默覆盖。\n"
    )


def parse_application(path: Path) -> ApplicationRecord:
    def extract(content: str) -> str:
        if not content.startswith(f"{FRONTMATTER}\n"):
            raise ValueError(f"申请文件缺少 frontmatter：{path}")
        _, frontmatter, _ = content.split(FRONTMATTER, maxsplit=2)
        return frontmatter

    _content, payload = load_document(path, extract)
    return ApplicationRecord.from_dict(payload or {})


class ApplicationRegistry:
    def __init__(self, applications_dir: Path) -> None:
        self.applications_dir = applications_dir
        self.applications_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, application_key: str) -> Path:
        return self.applications_dir / f"{application_key}.md"

    def raw_load(self, application_key: str) -> ApplicationRecord | None:
        path = self.path_for(application_key)
        return parse_application(path) if path.exists() else None

    def load(self, application_key: str) -> ApplicationRecord | None:
        """Resolve aliases and merge tombstones to one canonical record."""
        current_key = application_key
        visited: set[str] = set()
        records: list[ApplicationRecord] | None = None
        while current_key and current_key not in visited:
            visited.add(current_key)
            record = self.raw_load(current_key)
            if record is None:
                records = records or self.all(
                    ignore_invalid=True,
                    include_merged=True,
                    include_deleted=True,
                )
                record = next(
                    (item for item in records if current_key in item.aliases),
                    None,
                )
            if record is None:
                return None
            if not record.merged_into:
                return record
            current_key = record.merged_into
        return None

    def all(
        self,
        *,
        ignore_invalid: bool = False,
        include_merged: bool = True,
        include_deleted: bool = True,
    ) -> list[ApplicationRecord]:
        records: list[ApplicationRecord] = []
        for path in sorted(self.applications_dir.glob("app-*.md")):
            try:
                record = parse_application(path)
                if not include_merged and record.merged_into:
                    continue
                if not include_deleted and record.deleted_at:
                    continue
                records.append(record)
            except (OSError, UnicodeError, KeyError, TypeError, ValueError, yaml.YAMLError):
                if not ignore_invalid:
                    raise
        return records

    def active(self, *, ignore_invalid: bool = False) -> list[ApplicationRecord]:
        return [
            record
            for record in self.all(
                ignore_invalid=ignore_invalid,
                include_merged=False,
                include_deleted=False,
            )
            if record.status != "archived"
        ]

    def save(self, record: ApplicationRecord) -> Path:
        current = datetime.now(SHANGHAI)
        record.created_at = record.created_at or current
        record.updated_at = current
        record.aliases = sorted(
            {alias for alias in record.aliases if alias and alias != record.application_key}
        )
        record.identity_fingerprint = _record_fingerprint(record)
        record.schema_version = 6
        path = self.path_for(record.application_key)
        _atomic_write(path, render_application(record))
        return path

    def import_progress(self, path: Path | None) -> list[ApplicationRecord]:
        imported: list[ApplicationRecord] = []
        for candidate in preview_progress_applications(path):
            tombstone = self.raw_load(candidate.application_key)
            if tombstone and (tombstone.deleted_at or tombstone.merged_into):
                imported.append(self.load(candidate.application_key) or tombstone)
                continue
            existing = self.load(candidate.application_key)
            if existing and existing.identity_locked:
                existing.company_key = candidate.company_key
                existing.company = candidate.company
                existing.recruiting_project = candidate.recruiting_project
                existing.recruiting_year = candidate.recruiting_year
                existing.business_unit = candidate.business_unit
                existing.role = candidate.role
                existing.job_code = candidate.job_code
                existing.location = candidate.location
                existing.status = candidate.status
                if candidate.submitted_at and (
                    not existing.submitted_at
                    or candidate.submitted_at < existing.submitted_at
                ):
                    existing.submitted_at = candidate.submitted_at
                existing.role_aliases = sorted(
                    set(existing.role_aliases) | set(candidate.role_aliases)
                )
                existing.legacy_application_ids = sorted(
                    set(existing.legacy_application_ids)
                    | set(candidate.legacy_application_ids)
                )
                existing.identity_evidence = sorted(
                    set(existing.identity_evidence)
                    | set(candidate.identity_evidence)
                )
                if any(
                    evidence.startswith("conflict:")
                    for evidence in candidate.identity_evidence
                ):
                    existing.confirmed_by_user = False
                    existing.identity_locked = False
                self.save(existing)
                imported.append(existing)
                continue
            self.save(candidate)
            imported.append(candidate)
        return imported
