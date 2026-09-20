from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from job_mail_desk.identity_dictionaries import load_identity_dictionaries
from job_mail_desk.identity_pipeline import (
    identity_candidate_from_event,
    resolve_event_batch,
)
from job_mail_desk.models import ApplicationRecord, ParsedEvent
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.unresolved_store import (
    UnresolvedStore,
    unresolved_from_decision,
)


NOW = datetime(2026, 8, 4, 9, 0, tzinfo=SHANGHAI)


def event(
    *,
    company: str = "京东",
    role: str | None = None,
    project: str | None = None,
    event_type: str = "application",
    stage: str = "网申",
    received_at: datetime = NOW,
    action: str = "等待后续",
    title: str = "招聘通知",
) -> ParsedEvent:
    return ParsedEvent(
        company=company,
        role=role,
        recruiting_project=project,
        event_type=event_type,
        stage=stage,
        round=None,
        title=title,
        start_at=None,
        end_at=None,
        deadline_at=None,
        source_message_id=f"<{received_at.timestamp()}-{role}-{project}@example.invalid>",
        source_received_at=received_at,
        source_sender="noreply@example.invalid",
        source_url=None,
        action_summary=action,
        requirements=(),
        matched_keywords=(),
        confidence=0.9,
        change_type="new",
        company_confidence=0.95,
        company_source="test-explicit-company",
        role_confidence=0.95 if role else 0.0,
        role_source="test-explicit-role" if role else None,
    )


def application(
    key: str,
    *,
    project: str,
    role: str,
) -> ApplicationRecord:
    return ApplicationRecord(
        application_key=key,
        company_key="jd",
        company="京东",
        recruiting_project=project,
        recruiting_year=2027,
        business_unit=None,
        role=role,
        role_aliases=[],
        job_code=None,
        submitted_at=NOW,
        status="active",
        source="test",
        confirmed_by_user=True,
        identity_locked=True,
    )


def test_generic_receipt_stays_unresolved_when_jds_and_tet_both_exist() -> None:
    decisions = resolve_event_batch(
        [event()],
        [
            application("app-jds", project="JDS", role="产品经理"),
            application("app-tet", project="TET", role="TET综合方向"),
        ],
        load_identity_dictionaries(),
    )
    assert decisions[0].action == "unresolved"
    assert decisions[0].application_key is None


def test_generic_receipt_keeps_nearby_batch_context_as_suggestion_only() -> None:
    receipt = event(received_at=NOW)
    explicit = event(
        role="产品经理",
        project="JDS",
        event_type="assessment",
        stage="在线测评",
        received_at=NOW + timedelta(minutes=8),
    )
    decisions = resolve_event_batch(
        [receipt, explicit],
        [application("app-jds", project="JDS", role="产品经理")],
        load_identity_dictionaries(),
    )
    assert decisions[1].action == "matched"
    assert decisions[0].action == "unresolved"
    assert decisions[0].application_key is None
    assert (
        decisions[0].resolution.reason
        == "unique-nearby-batch-context-suggestion"
    )
    assert [item.application_key for item in decisions[0].resolution.candidates] == [
        "app-jds"
    ]


def test_explicit_application_can_create_provisional_identity() -> None:
    decisions = resolve_event_batch(
        [event(company="帆软", role="产品经理")],
        [],
        load_identity_dictionaries(),
    )
    assert decisions[0].action == "new_application"
    assert decisions[0].application_key.startswith("app-")


def test_batch_match_to_provisional_identity_is_not_persisted_as_recommendation() -> None:
    decisions = resolve_event_batch(
        [
            event(company="帆软", role="产品经理"),
            event(
                company="帆软",
                role="产品经理",
                event_type="assessment",
                stage="在线笔试",
                received_at=NOW + timedelta(minutes=5),
            ),
        ],
        [],
        load_identity_dictionaries(),
    )

    assert decisions[0].action == "new_application"
    assert decisions[1].action == "batch_context_match"
    assert decisions[1].application_key == decisions[0].application_key
    pending = unresolved_from_decision("9" * 32, decisions[1])
    assert pending.recommended_application_key is None
    assert decisions[1].application_key not in pending.candidate_application_keys


def test_reviewed_receipt_template_cannot_create_application() -> None:
    decision = resolve_event_batch(
        [event(company="帆软", role="产品经理", title="恭喜您网申成功提交")],
        [],
        load_identity_dictionaries(),
    )[0]
    assert decision.candidate.template_id == "generic-application-success"
    assert decision.action == "unresolved"


def test_project_codes_and_business_unit_suffixes_are_normalized() -> None:
    jds = event(role=None, project="JDS · 2027校园招聘", event_type="assessment")
    tet = event(role="TET 综合方向", project="2027校园招聘", event_type="assessment")
    leihuo = event(
        company="网易游戏",
        role="游戏 AI 产品经理",
        project="雷火事业群 · 2027校园招聘",
        event_type="assessment",
    )
    assert identity_candidate_from_event(jds).recruiting_project == "JDS"
    assert identity_candidate_from_event(tet).recruiting_project == "TET"
    candidate = identity_candidate_from_event(leihuo)
    assert candidate.recruiting_project == "雷火事业群 · 2027校园招聘"
    assert candidate.business_unit == "雷火事业群"


def test_event_dates_and_unrelated_codes_do_not_become_identity_metadata() -> None:
    candidate = identity_candidate_from_event(
        event(
            company="样例科技",
            role="芯片工程师",
            project="2027校园招聘",
            title="2026年08月16日面试通知，会议验证码 A12345",
        ),
        load_identity_dictionaries(),
    )
    assert candidate.recruiting_year == 2027
    assert candidate.job_code is None


def test_explicit_recruiting_batches_remain_distinct_identity_metadata() -> None:
    early = identity_candidate_from_event(
        event(
            company="样例科技",
            role="芯片工程师",
            project="2027校园招聘 · 提前批",
        ),
        load_identity_dictionaries(),
    )
    regular = identity_candidate_from_event(
        event(
            company="样例科技",
            role="芯片工程师",
            project="2027校园招聘 · 正式批",
        ),
        load_identity_dictionaries(),
    )
    assert early.recruiting_project == "2027校园招聘 · 提前批"
    assert regular.recruiting_project == "2027校园招聘 · 正式批"
    assert early.recruiting_project != regular.recruiting_project


def test_structured_role_metadata_stays_in_separate_identity_fields() -> None:
    parsed = replace(
        event(
            company="江波龙",
            role="IC实现工程师",
            project="2027校园招聘",
        ),
        role_raw="IC实现工程师（上海）(J11374)",
        role_canonical="IC实现工程师",
        location="上海",
        job_code="J11374",
    )
    candidate = identity_candidate_from_event(
        parsed,
        load_identity_dictionaries(),
    )
    assert candidate.role_raw == "IC实现工程师（上海）(J11374)"
    assert candidate.role == candidate.role_canonical == "IC实现工程师"
    assert candidate.location == "上海"
    assert candidate.job_code == "J11374"
    assert candidate.recruiting_project == "2027校园招聘"


def test_pending_handoff_preserves_normalized_identity_metadata(tmp_path) -> None:
    parsed = replace(
        event(
            company="网易游戏",
            role="游戏 AI 产品经理",
            project="雷火事业群 · 2027校园招聘",
        ),
        role_raw="游戏 AI 产品经理（上海）(J12345)",
        role_canonical="游戏 AI 产品经理",
        location="上海市",
        location_confidence=0.97,
        location_source="explicit-job-location",
        job_code="J12345",
    )
    decision = resolve_event_batch(
        [parsed],
        [],
        load_identity_dictionaries(),
    )[0]

    pending = unresolved_from_decision("8" * 32, decision)
    assert pending.role == pending.role_canonical == "游戏 AI 产品经理"
    assert pending.role_raw == "游戏 AI 产品经理（上海）(J12345)"
    assert pending.recruiting_project == "雷火事业群 · 2027校园招聘"
    assert pending.recruiting_year == 2027
    assert pending.business_unit == "雷火事业群"
    assert pending.job_code == "J12345"
    assert pending.location == "上海"
    assert pending.location_confidence == 0.97
    assert pending.location_source == "explicit-job-location"

    store = UnresolvedStore(tmp_path / "unresolved")
    store.save(pending)
    loaded = store.load(pending.id)
    assert loaded
    assert loaded.to_dict() == pending.to_dict()


def test_assessment_with_long_garbage_role_cannot_create_application() -> None:
    garbage = event(
        company="样例集团",
        role="岗位名称：实施工程师；专业要求：请点击官网进行修改" * 8,
        event_type="application",
        stage="测评",
    )
    decision = resolve_event_batch(
        [garbage],
        [],
        load_identity_dictionaries(),
    )[0]
    assert decision.action == "unresolved"


def test_downstream_event_without_role_or_job_code_cannot_seed_application() -> None:
    decision = resolve_event_batch(
        [
            event(
                company="长鑫存储",
                role=None,
                project="长鑫存储校园招聘",
                event_type="interview",
                stage="AI 面试",
            )
        ],
        [],
        load_identity_dictionaries(),
    )[0]
    assert decision.action == "unresolved"
    assert decision.application_key is None


def test_distinct_explicit_role_creates_second_application() -> None:
    incoming = event(company="京东", role="陌生岗位", stage="网申")
    decision = resolve_event_batch(
        [incoming],
        [application("app-jds", project="JDS", role="产品经理")],
        load_identity_dictionaries(),
    )[0]
    assert decision.action == "new_application"
    assert decision.application_key is not None
    pending = unresolved_from_decision("7" * 32, decision)
    assert pending.resolution_status == "new_application"
    assert pending.reason == "distinct-identity-new-application"


def test_missing_role_does_not_create_ambiguous_second_application() -> None:
    incoming = event(company="京东", role=None, stage="网申")
    decision = resolve_event_batch(
        [incoming],
        [application("app-jds", project="JDS", role="产品经理")],
        load_identity_dictionaries(),
    )[0]
    assert decision.action == "unresolved"
    assert decision.application_key is None


def test_unresolved_store_is_idempotent_and_excludes_private_fields(tmp_path) -> None:
    decision = resolve_event_batch(
        [
            event(
                company="京东",
                action=(
                    "联系 candidate@example.com，手机号 13800138000，"
                    "打开 https://example.com/private?token=secret"
                ),
            )
        ],
        [
            application("app-jds", project="JDS", role="产品经理"),
            application("app-tet", project="TET", role="TET综合方向"),
        ],
        load_identity_dictionaries(),
    )[0]
    store = UnresolvedStore(tmp_path)
    record = unresolved_from_decision("a" * 32, decision)
    first = store.save(record)
    second = store.save(record)
    assert first == second
    assert len(store.all()) == 1
    content = first.read_text(encoding="utf-8")
    assert "candidate@example.com" not in content
    assert "13800138000" not in content
    assert "https://" not in content
    assert "secret" not in content
    assert decision.event.source_sender not in content
    resolved = store.resolve(
        record.id,
        application_key="app-jds",
        task_id="task-1",
    )
    assert resolved.status == "resolved"
    assert resolved.resolved_application_key == "app-jds"
    assert resolved.resolved_task_id == "task-1"
