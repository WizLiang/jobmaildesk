from dataclasses import replace
from datetime import datetime, timedelta

from job_mail_desk.identity_pipeline import IdentityDecision
from job_mail_desk.identity_resolver import IdentityCandidate, ResolutionResult
from job_mail_desk.models import ParsedEvent
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.unresolved_store import UnresolvedStore, unresolved_from_decision


def decision(stage: str = "笔试") -> IdentityDecision:
    event = ParsedEvent(
        company="样例科技",
        role="后端工程师",
        recruiting_project=None,
        event_type="assessment",
        stage=stage,
        round=None,
        title="流程通知",
        start_at=None,
        end_at=None,
        deadline_at=None,
        source_message_id="<review-first@example.invalid>",
        source_received_at=datetime(2026, 8, 14, tzinfo=SHANGHAI),
        source_sender="",
        source_url=None,
        action_summary="核对流程",
        requirements=(),
        matched_keywords=("笔试",),
        confidence=0.9,
        change_type="new",
    )
    candidate = IdentityCandidate(company=event.company, role=event.role)
    resolution = ResolutionResult(
        status="matched",
        application_key="app-existing",
        confidence=0.9,
        reason="unique-company-role",
        candidates=(),
    )
    return IdentityDecision(
        event=event,
        candidate=candidate,
        resolution=resolution,
        action="matched",
        application_key="app-existing",
    )


def test_review_replay_is_idempotent_until_semantics_change(tmp_path) -> None:
    store = UnresolvedStore(tmp_path / "unresolved")
    source_hash = "e" * 32
    first = unresolved_from_decision(
        source_hash,
        decision(),
        parser_version="v1",
    )

    saved = store.put_pending(first)
    replayed = store.put_pending(first)
    changed = unresolved_from_decision(
        source_hash,
        decision("一面"),
        parser_version="v2",
    )
    updated = store.put_pending(changed)

    assert saved.status == "pending"
    assert saved.recommended_application_key == "app-existing"
    assert replayed.revision == 1
    assert updated.revision == 2
    assert updated.stage == "一面"


def test_review_revision_tracks_decision_fields_and_replaces_candidates(
    tmp_path,
) -> None:
    store = UnresolvedStore(tmp_path / "unresolved")
    source_hash = "f" * 32
    original = replace(
        unresolved_from_decision(source_hash, decision()),
        candidate_application_keys=("app-stale",),
    )
    saved = store.put_pending(original)
    changed = replace(
        original,
        action_summary="请携带证件参加笔试",
        title="更新后的笔试安排",
        requirements=("携带证件",),
        role_raw="后端工程师（上海）(J10001)",
        role_canonical="后端工程师",
        job_code="J10001",
        recruiting_year=2027,
        business_unit="基础架构事业群",
        location="上海",
        location_confidence=0.98,
        location_source="explicit-job-location",
        candidate_application_keys=("app-current",),
    )
    updated = store.put_pending(changed)

    assert saved.revision == 1
    assert updated.revision == 2
    assert updated.semantic_hash != saved.semantic_hash
    assert updated.candidate_application_keys == ("app-current",)


def test_terminal_cancelled_and_stale_events_do_not_recommend_tasks() -> None:
    base = decision()
    future = datetime.now(SHANGHAI) + timedelta(days=1)
    terminal = replace(
        base,
        event=replace(
            base.event,
            event_type="rejection",
            stage="未通过",
            deadline_at=future,
            source_url="https://example.invalid/result",
        ),
    )
    cancelled = replace(
        base,
        event=replace(
            base.event,
            change_type="cancel",
            deadline_at=future,
            source_url="https://example.invalid/cancelled",
        ),
    )
    stale = replace(
        base,
        event=replace(
            base.event,
            start_at=datetime.now(SHANGHAI) - timedelta(days=1),
        ),
    )

    assert unresolved_from_decision("1" * 32, terminal).recommend_task is False
    assert unresolved_from_decision("2" * 32, cancelled).recommend_task is False
    assert unresolved_from_decision("3" * 32, stale).recommend_task is False


def test_footer_rule_inside_frontmatter_keeps_the_store_readable(tmp_path) -> None:
    """A mail footer rule must not cut the YAML frontmatter in half."""
    import yaml

    from job_mail_desk.unresolved_store import _frontmatter_block

    payload = {
        "source_hash": "hash-with-footer-rule",
        "company": "海光信息",
        "stage": "面试",
        "requirements": [
            "时间：2026年9月3日（周四）10：00 地点：东南大学九龙湖校区教六-101。",
            "----------------",
        ],
    }
    document = (
        "---\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()
        + "\n---\n\n# 海光信息｜面试\n"
    )
    (tmp_path / "hash-with-footer-rule.md").write_text(document, encoding="utf-8")

    block = _frontmatter_block(document)
    assert block is not None
    restored = yaml.safe_load(block)
    assert restored["company"] == "海光信息"
    assert "----------------" in restored["requirements"]
    # Naive splitting on "---" used to stop inside the quoted scalar.
    assert document.split("---", 2)[1] != block
