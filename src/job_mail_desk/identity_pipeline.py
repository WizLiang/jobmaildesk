from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import timedelta

from .application_registry import stable_application_key
from .identity_dictionaries import IdentityDictionaries
from .identity_resolver import (
    IdentityCandidate,
    IdentityResolver,
    ResolutionResult,
)
from .models import ApplicationRecord, ParsedEvent
from .normalization import canonical_role, normalize_company_project, role_key
from .normalization import is_invalid_role
from .parser import normalize_location


JOB_CODE = re.compile(r"\b([A-Za-z]{1,4}\d{4,})\b", re.IGNORECASE)
RECRUITING_YEAR = re.compile(
    r"(?<!\d)(20\d{2})(?=届?(?:校园招聘|校招|秋招|春招|提前批|正式批))"
)
BATCH_CONTEXT_WINDOW = timedelta(hours=2)
PROJECT_CODE = re.compile(r"(?<![A-Za-z])(JDS|TET|TGT)(?![A-Za-z])", re.I)
APPLICATION_STAGE = re.compile(r"网申|投递|申请|简历")


@dataclass(frozen=True)
class IdentityDecision:
    event: ParsedEvent
    candidate: IdentityCandidate
    resolution: ResolutionResult
    action: str
    application_key: str | None

    def to_preview(self) -> dict[str, object]:
        final_resolution_status = (
            "new_application"
            if self.action == "new_application"
            else self.resolution.status
        )
        final_resolution_reason = (
            "distinct-identity-new-application"
            if self.action == "new_application"
            and self.resolution.status == "conflict"
            else self.resolution.reason
        )
        return {
            "company": self.candidate.company,
            "role": self.candidate.role_canonical or self.candidate.role,
            "role_raw": self.candidate.role_raw,
            "role_canonical": self.candidate.role_canonical,
            "location": self.candidate.location,
            "location_confidence": self.candidate.location_confidence,
            "location_source": self.candidate.location_source,
            "job_code": self.candidate.job_code,
            "project": self.candidate.recruiting_project,
            "recruiting_year": self.candidate.recruiting_year,
            "business_unit": self.candidate.business_unit,
            "stage": self.event.stage,
            "round": self.event.round,
            "received_at": self.event.source_received_at.isoformat(),
            "start_at": self.event.start_at.isoformat()
            if self.event.start_at
            else None,
            "end_at": self.event.end_at.isoformat()
            if self.event.end_at
            else None,
            "deadline_at": self.event.deadline_at.isoformat()
            if self.event.deadline_at
            else None,
            "identity_action": self.action,
            "application_key": self.application_key,
            "resolution_status": final_resolution_status,
            "resolution_reason": final_resolution_reason,
            "raw_resolution_status": self.resolution.status,
            "raw_resolution_reason": self.resolution.reason,
            "candidate_count": len(self.resolution.candidates),
            "candidate_application_keys": [
                item.application_key for item in self.resolution.candidates
            ],
        }


def identity_candidate_from_event(
    event: ParsedEvent,
    dictionaries: IdentityDictionaries | None = None,
) -> IdentityCandidate:
    raw_company = event.company or "公司待确认"
    normalized_company, project = normalize_company_project(
        raw_company,
        event.recruiting_project,
    )
    company = (
        dictionaries.canonical_company(raw_company)
        if dictionaries
        else None
    ) or normalized_company
    role_raw = event.role_raw or event.role
    role = event.role_canonical or canonical_role(event.role or role_raw)
    if dictionaries and role:
        role = dictionaries.canonical_role(role) or role
    project_source = " ".join(item for item in (project, role) if item)
    project_code = PROJECT_CODE.search(project_source)
    if project_code:
        project = project_code.group(1).upper()
    elif project:
        project = project.strip(" ·•|｜/-") or None
    combined = " ".join(
        item for item in (role_raw, role, project) if item
    )
    code_match = JOB_CODE.search(combined)
    year_source = " ".join(
        item
        for item in (
            event.role,
            event.role_raw,
            event.recruiting_project,
            role,
            project,
        )
        if item
    )
    year_match = RECRUITING_YEAR.search(year_source)
    business_unit = None
    unit_source = f"{company} {role or ''} {project or ''}"
    if "雷火" in unit_source:
        business_unit = "雷火事业群"
        project = project or business_unit
    elif "互娱" in unit_source:
        business_unit = "互娱事业群"
        project = project or business_unit
    template = None
    if dictionaries:
        template = dictionaries.mail_template_for(
            company=company,
            title=event.title,
            content=" ".join((event.action_summary, *event.requirements)),
        )
    return IdentityCandidate(
        company=company,
        role=role,
        role_raw=role_raw,
        role_canonical=role,
        recruiting_project=project,
        recruiting_year=int(year_match.group(1)) if year_match else None,
        business_unit=business_unit,
        job_code=event.job_code
        or (code_match.group(1).upper() if code_match else None),
        template_id=str(template["id"]) if template else None,
        location=normalize_location(event.location),
        company_confidence=event.company_confidence,
        role_confidence=event.role_confidence,
        location_confidence=event.location_confidence,
        location_source=event.location_source,
    )


def _can_seed_application(
    event: ParsedEvent,
    candidate: IdentityCandidate,
) -> bool:
    trusted_company = bool(
        candidate.company
        and candidate.company != "公司待确认"
        and (
            candidate.company_confidence >= 0.85
            or event.company_source == "local-confirmed-correction"
            or "reviewed-" in str(event.company_source or "")
        )
    )
    explicit_identity = bool(
        candidate.job_code
        or candidate.recruiting_project
        or (candidate.role and candidate.role_confidence >= 0.85)
    )
    if event.event_type in {"assessment", "interview"}:
        explicit_identity = bool(
            candidate.job_code
            or (candidate.role and candidate.role_confidence >= 0.85)
        )
    return bool(
        trusted_company
        and explicit_identity
        and event.event_type
        in {"application", "assessment", "interview", "offer", "rejection"}
    )


def _new_application_key(
    event: ParsedEvent,
    candidate: IdentityCandidate,
    resolution: ResolutionResult,
    dictionaries: IdentityDictionaries,
    applications: list[ApplicationRecord],
) -> str | None:
    if resolution.candidates:
        records = {
            record.application_key: record
            for record in applications
        }
        for scored in resolution.candidates:
            record = records.get(scored.application_key)
            if not record:
                return None
            if candidate.job_code and record.job_code:
                if candidate.job_code.upper() != record.job_code.upper():
                    continue
                return None
            if candidate.role and record.role:
                candidate_role = canonical_role(candidate.role)
                record_role = canonical_role(record.role)
                if (
                    candidate_role
                    and record_role
                    and role_key(candidate_role) != role_key(record_role)
                ):
                    continue
            candidate_locations = set(
                (normalize_location(candidate.location) or "").split(" / ")
            ) - {""}
            record_locations = set(
                (normalize_location(record.location) or "").split(" / ")
            ) - {""}
            if (
                candidate_locations
                and record_locations
                and candidate_locations.isdisjoint(record_locations)
            ):
                continue
            return None
    if not candidate.company or candidate.company == "公司待确认":
        return None
    if not APPLICATION_STAGE.search(event.stage) and not _can_seed_application(
        event,
        candidate,
    ):
        return None
    if candidate.template_id:
        template = next(
            (
                item
                for item in dictionaries.mail_templates
                if item["id"] == candidate.template_id
            ),
            None,
        )
        if template and template.get("creates_application") is False:
            return None
    if candidate.role and (
        is_invalid_role(candidate.role) or len(candidate.role) > 120
    ):
        return None
    if not (
        candidate.role
        or candidate.recruiting_project
        or candidate.job_code
    ):
        return None
    return stable_application_key(
        company=candidate.company,
        role=candidate.role,
        recruiting_project=candidate.recruiting_project,
        recruiting_year=candidate.recruiting_year,
        business_unit=candidate.business_unit,
        job_code=candidate.job_code,
        location=candidate.location,
    )


def _company_identity(
    dictionaries: IdentityDictionaries,
    company: str | None,
) -> str:
    return dictionaries.company_id(company) or (company or "").casefold().strip()


def resolve_event_batch(
    events: list[ParsedEvent],
    applications: list[ApplicationRecord],
    dictionaries: IdentityDictionaries,
) -> list[IdentityDecision]:
    resolver = IdentityResolver(dictionaries)
    decisions: list[IdentityDecision] = []
    provisional = list(applications)
    persisted_application_keys = {
        application.application_key for application in applications
    }
    for event in events:
        candidate = identity_candidate_from_event(event, dictionaries)
        eligible = [
            record
            for record in provisional
            if record.status != "archived"
            and record.deleted_at is None
            and record.merged_into is None
            and not (event.event_type == "application" and record.status == "ended")
        ]
        resolution = resolver.resolve(candidate, eligible)
        if resolution.status == "matched":
            application_key = resolution.application_key
            action = (
                "matched"
                if application_key in persisted_application_keys
                else "batch_context_match"
            )
        elif resolution.status == "conflict":
            application_key = (
                _new_application_key(
                    event,
                    candidate,
                    resolution,
                    dictionaries,
                    eligible,
                )
                if _can_seed_application(event, candidate)
                else None
            )
            action = "new_application" if application_key else "conflict"
        else:
            application_key = (
                _new_application_key(
                    event,
                    candidate,
                    resolution,
                    dictionaries,
                    eligible,
                )
                if _can_seed_application(event, candidate)
                else None
            )
            action = "new_application" if application_key else "unresolved"
        decisions.append(
            IdentityDecision(
                event=event,
                candidate=candidate,
                resolution=resolution,
                action=action,
                application_key=application_key,
            )
        )
        if action == "new_application" and application_key:
            provisional.append(
                ApplicationRecord(
                    application_key=application_key,
                    company_key=_company_identity(dictionaries, candidate.company),
                    company=candidate.company or "公司待确认",
                    recruiting_project=candidate.recruiting_project,
                    recruiting_year=candidate.recruiting_year,
                    business_unit=candidate.business_unit,
                    role=candidate.role,
                    role_aliases=[],
                    job_code=candidate.job_code,
                    submitted_at=event.source_received_at,
                    status="active",
                    source="mail-batch-preview",
                    confirmed_by_user=False,
                    identity_locked=False,
                    location=candidate.location,
                    identity_evidence=["explicit-application-mail"],
                )
            )

    resolved: list[IdentityDecision] = []
    for decision in decisions:
        if decision.action != "unresolved" or any(
            (
                decision.candidate.role,
                decision.candidate.recruiting_project,
                decision.candidate.job_code,
            )
        ):
            resolved.append(decision)
            continue
        company_id = _company_identity(dictionaries, decision.candidate.company)
        nearby_keys = {
            other.application_key
            for other in decisions
            if other.application_key
            and other is not decision
            and _company_identity(dictionaries, other.candidate.company) == company_id
            and decision.event.source_received_at
            <= other.event.source_received_at
            <= decision.event.source_received_at + BATCH_CONTEXT_WINDOW
            and other.resolution.confidence >= 0.85
        }
        if len(nearby_keys) == 1:
            resolved.append(
                replace(
                    decision,
                    application_key=None,
                    action="unresolved",
                    resolution=ResolutionResult(
                        status="unresolved",
                        application_key=None,
                        confidence=0.0,
                        reason="unique-nearby-batch-context-suggestion",
                        candidates=decision.resolution.candidates,
                    ),
                )
            )
        else:
            resolved.append(decision)
    return resolved
