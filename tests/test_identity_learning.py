from datetime import datetime

from job_mail_desk.application_registry import ApplicationRegistry
from job_mail_desk.identity_learning import IdentityLearningStore
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.models import ApplicationRecord, MailRecord, ParsedEvent
from job_mail_desk.parser import SHANGHAI, parse_record
from job_mail_desk.task_service import task_from_event


def event(company: str, role: str) -> ParsedEvent:
    return ParsedEvent(
        company=company,
        role=role,
        recruiting_project=None,
        event_type="application",
        stage="网申",
        round=None,
        title="申请确认",
        start_at=None,
        end_at=None,
        deadline_at=None,
        source_message_id="<sample@example.invalid>",
        source_received_at=datetime(2026, 8, 13, tzinfo=SHANGHAI),
        source_sender="",
        source_url=None,
        action_summary="申请已收到",
        requirements=(),
        matched_keywords=("应聘",),
        confidence=0.8,
        change_type="new",
    )


def test_scoped_learning_reuses_confirmed_correction_without_private_mail(
    tmp_path,
) -> None:
    store = IdentityLearningStore(tmp_path / "manual")
    sender = "样例招聘 <private-person@mail.example.invalid>"
    subject = "您已应聘样例科技，查看最新招聘信息"
    original = store.enrich(
        event("样例科技", "后端开发【27届校招】"),
        sender=sender,
        subject=subject,
    )
    rule = store.learn(
        sender_scope_hash=original.sender_scope_hash,
        subject_shape_hash=original.subject_shape_hash,
        original_company=original.company,
        original_role=original.role,
        corrected_company="样例科技",
        corrected_role="后端开发工程师",
    )

    assert rule is not None and rule.enabled
    learned = store.enrich(
        event("样例科技", "后端开发【27届校招】"),
        sender=sender,
        subject=subject,
    )
    assert learned.company == "样例科技"
    assert learned.role == "后端开发工程师"
    assert learned.role_source == "local-confirmed-correction"

    persisted = store.rules_path.read_text(encoding="utf-8")
    assert "private-person" not in persisted
    assert "查看最新招聘信息" not in persisted
    assert sender not in persisted


def test_conflicting_learning_rules_fail_closed(tmp_path) -> None:
    store = IdentityLearningStore(tmp_path / "manual")
    original = store.enrich(
        event("样例科技", "后端开发"),
        sender="样例招聘 <noreply@example.invalid>",
        subject="样例科技申请确认",
    )
    first = store.learn(
        sender_scope_hash=original.sender_scope_hash,
        subject_shape_hash=original.subject_shape_hash,
        original_company=original.company,
        original_role=original.role,
        corrected_company="样例科技",
        corrected_role="后端开发工程师",
    )
    second = store.learn(
        sender_scope_hash=original.sender_scope_hash,
        subject_shape_hash=original.subject_shape_hash,
        original_company=original.company,
        original_role=original.role,
        corrected_company="样例科技",
        corrected_role="后端研发工程师",
    )

    assert first is not None and second is not None
    rules = store.all()
    assert len(rules) == 2
    assert all(rule.conflict and not rule.enabled for rule in rules)
    unchanged = store.enrich(
        event("样例科技", "后端开发"),
        sender="样例招聘 <noreply@example.invalid>",
        subject="样例科技申请确认",
    )
    assert unchanged.role == "后端开发"


def test_application_assignment_cannot_coarsen_parser_identity(tmp_path) -> None:
    store = IdentityLearningStore(tmp_path / "manual")
    for original_company, original_role, assigned_company, assigned_role in (
        ("样例科技", "AI产品经理", "样例科技", "产品经理"),
        ("样例科技", "数字后端工程师", "样例科技", "后端工程师"),
        ("科大讯飞研究院", "算法工程师", "科大讯飞", "算法工程师"),
    ):
        parsed = store.enrich(
            event(original_company, original_role),
            sender="招聘系统 <noreply@example.invalid>",
            subject=f"{original_company}{original_role}申请确认",
        )
        rule = store.learn(
            sender_scope_hash=parsed.sender_scope_hash,
            subject_shape_hash=parsed.subject_shape_hash,
            original_company=parsed.company,
            original_role=parsed.role,
            corrected_company=assigned_company,
            corrected_role=assigned_role,
        )
        assert rule is None
    assert store.all() == []


def test_recent_mail_learns_only_through_stable_confirmed_application(
    tmp_path,
) -> None:
    from job_mail_desk.scanner import _learn_from_confirmed_records

    mail = MailRecord(
        uid="42",
        subject="【样例科技】校园招聘申请确认",
        message_id="<confirmed-link@example.invalid>",
        sender="样例科技 <noreply@example.invalid>",
        received_at=datetime(2026, 8, 13, tzinfo=SHANGHAI),
        body="岗位名称：后端开发",
    )
    parsed = parse_record(mail)
    assert parsed is not None
    task_store = MarkdownTaskStore(tmp_path / "tasks")
    task = task_from_event(
        parsed,
        task_store,
        application_key="app-confirmed",
        resolved_application_id="legacy-confirmed",
    )
    task_store.save(task)
    registry = ApplicationRegistry(tmp_path / "applications")
    registry.save(
        ApplicationRecord(
            application_key="app-confirmed",
            company_key="sample",
            company="样例科技",
            recruiting_project=None,
            recruiting_year=None,
            business_unit=None,
            role="后端开发工程师",
            role_aliases=[],
            job_code=None,
            submitted_at=mail.received_at,
            status="active",
            source="desktop-ui",
            confirmed_by_user=True,
            identity_locked=True,
        )
    )
    learning = IdentityLearningStore(tmp_path / "dictionaries" / "manual")

    summary = _learn_from_confirmed_records(
        [mail],
        dictionaries=None,
        learning=learning,
        registry=registry,
        store=task_store,
    )

    assert summary == {"eligible": 1, "learned": 1, "conflicts": 0}
    assert learning.all()[0].corrected_role == "后端开发工程师"
