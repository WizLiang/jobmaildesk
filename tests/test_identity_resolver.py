from __future__ import annotations

from job_mail_desk.identity_dictionaries import load_identity_dictionaries
from job_mail_desk.identity_resolver import IdentityCandidate, IdentityResolver
from job_mail_desk.models import ApplicationRecord


def application(
    key: str,
    *,
    company: str = "京东",
    project: str | None = None,
    year: int | None = 2027,
    role: str | None = None,
    job_code: str | None = None,
    business_unit: str | None = None,
    location: str | None = None,
) -> ApplicationRecord:
    return ApplicationRecord(
        application_key=key,
        company_key=company,
        company=company,
        recruiting_project=project,
        recruiting_year=year,
        business_unit=business_unit,
        role=role,
        role_aliases=[],
        job_code=job_code,
        submitted_at=None,
        status="active",
        source="test",
        confirmed_by_user=True,
        identity_locked=True,
        location=location,
    )


def resolver() -> IdentityResolver:
    return IdentityResolver(load_identity_dictionaries())


def test_job_code_is_a_strong_match_even_if_role_text_changes() -> None:
    result = resolver().resolve(
        IdentityCandidate(company="百度招聘", role="管培方向", job_code="j101320"),
        [application("baidu-role", company="百度", job_code="J101320", role="管培生")],
    )
    assert result.status == "matched"
    assert result.application_key == "baidu-role"
    assert result.reason == "strong-identifier"


def test_structured_job_code_matches_legacy_combined_role() -> None:
    result = resolver().resolve(
        IdentityCandidate(
            company="江波龙",
            role="IC实现工程师",
            role_raw="IC实现工程师（上海）(J11374)",
            job_code="J11374",
            location="上海",
        ),
        [
            application(
                "legacy-longsys",
                company="江波龙",
                role="IC实现工程师（上海）(J11374)",
            )
        ],
    )
    assert result.status == "matched"
    assert result.application_key == "legacy-longsys"
    assert result.reason == "strong-identifier"


def test_company_only_receipt_never_selects_between_jds_and_tet() -> None:
    result = resolver().resolve(
        IdentityCandidate(company="京东校招", template_id="jd-application-received"),
        [
            application("jds", project="JDS", role="产品经理"),
            application("tet", project="TET", role="TET综合方向"),
        ],
    )
    assert result.status == "unresolved"
    assert result.application_key is None
    assert result.reason == "multiple-candidates"


def test_project_conflict_blocks_cross_linking() -> None:
    result = resolver().resolve(
        IdentityCandidate(
            company="京东",
            recruiting_project="TET",
            role="TET综合方向",
            recruiting_year=2027,
        ),
        [application("jds", project="JDS", role="产品经理")],
    )
    assert result.status == "conflict"
    assert result.application_key is None
    assert "recruiting-project" in result.candidates[0].conflicts


def test_generic_cycle_accepts_explicit_batch_enrichment() -> None:
    result = resolver().resolve(
        IdentityCandidate(
            company="兆易创新",
            recruiting_project="2027校园招聘 · 提前批",
            recruiting_year=2027,
            role="数字后端工程师",
        ),
        [
            application(
                "generic-cycle",
                company="兆易创新",
                project="2027校园招聘",
                role="数字后端工程师",
            )
        ],
    )
    assert result.status == "matched"
    assert result.application_key == "generic-cycle"


def test_two_explicit_batches_remain_conflicting_identities() -> None:
    result = resolver().resolve(
        IdentityCandidate(
            company="样例科技",
            recruiting_project="2027校园招聘 · 正式批",
            recruiting_year=2027,
            role="芯片工程师",
        ),
        [
            application(
                "early-batch",
                company="样例科技",
                project="2027校园招聘 · 提前批",
                role="芯片工程师",
            )
        ],
    )
    assert result.status == "conflict"
    assert "recruiting-project" in result.candidates[0].conflicts


def test_same_role_with_disjoint_locations_is_a_conflict() -> None:
    result = resolver().resolve(
        IdentityCandidate(company="京东", role="产品经理", location="深圳"),
        [application("beijing", role="产品经理", location="北京")],
    )
    assert result.status == "conflict"
    assert "location" in result.candidates[0].conflicts


def test_empty_location_can_match_and_be_completed_later() -> None:
    result = resolver().resolve(
        IdentityCandidate(company="京东", role="产品经理", location="深圳"),
        [application("unknown-city", role="产品经理", location=None)],
    )
    assert result.status == "matched"
    assert result.application_key == "unknown-city"


def test_unique_project_and_role_combination_can_match() -> None:
    result = resolver().resolve(
        IdentityCandidate(
            company="京东",
            recruiting_project="JDS新星计划",
            role="产品岗",
            recruiting_year=2027,
        ),
        [
            application("jds", project="JDS", role="产品经理"),
            application("tet", project="TET", role="TET综合方向"),
        ],
    )
    assert result.status == "matched"
    assert result.application_key == "jds"
    assert result.reason == "unique-company-role"


def test_business_unit_conflict_keeps_netease_units_separate() -> None:
    result = resolver().resolve(
        IdentityCandidate(
            company="网易游戏",
            business_unit="雷火事业群",
            role="产品经理",
        ),
        [
            application(
                "netease-huyu",
                company="网易游戏",
                role="产品经理",
                business_unit="互娱事业群",
            )
        ],
    )
    assert result.status == "conflict"
    assert "business-unit" in result.candidates[0].conflicts


def test_netease_business_unit_and_role_can_match_unique_application() -> None:
    result = resolver().resolve(
        IdentityCandidate(
            company="网易游戏",
            business_unit="雷火事业群",
            role="AI产品经理",
        ),
        [
            application(
                "netease-leihuo",
                company="网易游戏",
                role="AI 产品经理",
                business_unit="雷火事业群",
            ),
            application(
                "netease-huyu",
                company="网易游戏",
                role="产品经理",
                business_unit="互娱事业群",
            ),
        ],
    )
    assert result.status == "matched"
    assert result.application_key == "netease-leihuo"


def test_company_only_single_candidate_is_still_insufficient() -> None:
    result = resolver().resolve(
        IdentityCandidate(company="帆软招聘"),
        [application("fanruan", company="帆软", role="产品经理")],
    )
    assert result.status == "unresolved"
    assert result.reason == "insufficient-identity-evidence"


def test_project_without_year_stays_unresolved_across_two_recruiting_cycles() -> None:
    result = resolver().resolve(
        IdentityCandidate(company="京东", recruiting_project="JDS"),
        [
            application("jds-2027", project="JDS", year=2027),
            application("jds-2028", project="JDS", year=2028),
        ],
    )
    assert result.status == "unresolved"
    assert result.reason == "multiple-candidates"


def test_known_company_never_matches_unknown_company_with_similar_role() -> None:
    result = resolver().resolve(
        IdentityCandidate(company="科大讯飞", role="AI产品经理"),
        [
            application(
                "iflytek",
                company="科大讯飞",
                role="AI 产品经理（J13348）",
            ),
            application(
                "unknown",
                company="未收录科技公司",
                role="AI产品经理",
            ),
        ],
    )
    assert result.status == "matched"
    assert result.application_key == "iflytek"


def test_company_year_and_batch_without_role_are_suggestions_only() -> None:
    result = resolver().resolve(
        IdentityCandidate(
            company="京东",
            recruiting_project="JDS",
            recruiting_year=2027,
        ),
        [application("jds", project="JDS", year=2027, role="产品经理")],
    )
    assert result.status == "unresolved"
    assert result.application_key is None
    assert result.reason == "insufficient-identity-evidence"


def test_role_substrings_do_not_match_distinct_applications() -> None:
    for incoming, existing in (
        ("AI产品经理", "产品经理"),
        ("后端工程师", "数字后端工程师"),
    ):
        result = resolver().resolve(
            IdentityCandidate(company="京东", role=incoming),
            [application("existing", role=existing)],
        )
        assert result.status == "conflict"
        assert result.application_key is None
        assert "role" in result.candidates[0].conflicts
        assert "role-compatible" not in result.candidates[0].evidence


def test_parent_company_does_not_match_reviewed_subsidiary() -> None:
    result = resolver().resolve(
        IdentityCandidate(company="科大讯飞研究院招聘", role="AI产品经理"),
        [
            application(
                "parent",
                company="科大讯飞",
                role="AI产品经理",
            )
        ],
    )
    assert result.status == "unresolved"
    assert result.application_key is None
    assert result.candidates == ()
