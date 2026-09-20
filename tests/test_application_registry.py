from datetime import datetime

import pytest

from job_mail_desk.application_registry import (
    ApplicationRegistry,
    application_from_progress_entry,
    application_from_user_payload,
    identity_fingerprint,
    preview_progress_applications,
    stable_application_key,
    update_application_from_user_payload,
)
from job_mail_desk.models import ApplicationRecord
from job_mail_desk.parser import SHANGHAI


def test_job_code_is_strong_stable_identity() -> None:
    first = stable_application_key(
        company="百度",
        role="2027 管培生（J101320）",
        recruiting_project=None,
        recruiting_year=2027,
        business_unit=None,
        job_code="J101320",
        legacy_application_id=None,
    )
    renamed = stable_application_key(
        company="百度招聘",
        role="北京-2027管培生",
        recruiting_project="校园招聘",
        recruiting_year=2027,
        business_unit=None,
        job_code="j101320",
        legacy_application_id="different-legacy-id",
    )
    assert first == renamed


def test_progress_entry_becomes_locked_application() -> None:
    record = application_from_progress_entry(
        {
            "company": "京东",
            "role": "2027 JDS 新星计划-产研项目管理",
            "project": "",
            "status": "简历已投递，等待筛选",
            "action": "2026-08-03 完成投递",
            "application_id": "a" * 20,
        },
        now=datetime(2026, 8, 3, 20, 0, tzinfo=SHANGHAI),
    )
    assert record is not None
    assert record.recruiting_project == "JDS"
    assert record.recruiting_year == 2027
    assert record.role == "2027 JDS 新星计划-产研项目管理"
    assert record.submitted_at == datetime(2026, 8, 3, tzinfo=SHANGHAI)
    assert record.identity_locked is True
    assert record.legacy_application_ids == ["a" * 20]


def test_application_progress_controls_round_trip() -> None:
    payload = {
        "application_key": "app-" + "a" * 24,
        "company_key": "sample",
        "company": "样例公司",
        "recruiting_project": "2027校园招聘",
        "recruiting_year": 2027,
        "business_unit": None,
        "role": "数字后端工程师",
        "role_aliases": [],
        "job_code": None,
        "submitted_at": None,
        "status": "active",
        "source": "desktop-ui",
        "confirmed_by_user": True,
        "identity_locked": True,
        "manual_stage": "二面",
        "manual_stage_status": "completed",
        "next_stage": "三面",
        "schema_version": 3,
    }
    record = ApplicationRecord.from_dict(payload)
    restored = ApplicationRecord.from_dict(record.to_dict())
    assert restored.manual_stage == "二面"
    assert restored.manual_stage_status == "completed"
    assert restored.next_stage == "三面"
    assert restored.schema_version == 6


def test_manual_progress_history_is_idempotent_and_strictly_validated() -> None:
    record = application_from_user_payload(
        {
            "company": "样例公司",
            "role": "产品经理",
            "manual_stage": "一面",
            "manual_stage_status": "pending",
            "next_stage": "二面",
        },
        now=datetime(2026, 8, 9, 9, 0, tzinfo=SHANGHAI),
    )
    payload = {
        "manual_stage": "一面",
        "manual_stage_status": "completed",
        "next_stage": "二面",
    }

    update_application_from_user_payload(record, payload)
    update_application_from_user_payload(record, payload)

    assert len(record.manual_progress_history) == 1
    assert record.manual_progress_history[0]["stage"] == "一面"
    assert record.manual_progress_history[0]["status"] == "completed"
    restored = ApplicationRecord.from_dict(record.to_dict())
    assert restored.manual_progress_history == record.manual_progress_history

    invalid = record.to_dict()
    invalid["manual_progress_history"] = [{"stage": "二面", "status": "pending"}]
    with pytest.raises(ValueError, match="event_at"):
        ApplicationRecord.from_dict(invalid)


def test_terminal_application_reopens_on_non_terminal_manual_stage() -> None:
    record = application_from_user_payload(
        {"company": "样例公司", "role": "产品经理", "manual_stage": "一面"}
    )
    update_application_from_user_payload(record, {"manual_stage": "未通过"})
    assert record.status == "ended"
    assert len(record.manual_progress_history) == 1

    update_application_from_user_payload(
        record,
        {"manual_stage": "二面", "manual_stage_status": "pending"},
    )
    assert record.status == "active"
    assert len(record.manual_progress_history) == 2

    update_application_from_user_payload(
        record,
        {"manual_stage": "二面", "manual_stage_status": "pending"},
    )
    assert len(record.manual_progress_history) == 2


def test_explicitly_archived_application_does_not_auto_reopen() -> None:
    record = application_from_user_payload(
        {"company": "样例公司", "role": "产品经理", "manual_stage": "一面"}
    )
    update_application_from_user_payload(
        record,
        {"status": "archived", "manual_stage": "二面"},
    )
    assert record.status == "archived"


def test_explicit_lifecycle_status_wins_and_history_is_idempotent() -> None:
    record = application_from_user_payload(
        {"company": "样例公司", "role": "产品经理", "manual_stage": "网申"}
    )

    update_application_from_user_payload(
        record,
        {"status": "ended", "manual_stage": "网申"},
    )
    assert record.status == "ended"
    assert record.manual_progress_history[-1]["lifecycle_status"] == "ended"
    history_size = len(record.manual_progress_history)

    update_application_from_user_payload(
        record,
        {"status": "ended", "manual_stage": "网申"},
    )
    assert len(record.manual_progress_history) == history_size

    update_application_from_user_payload(
        record,
        {"status": "active", "manual_stage": "网申"},
    )
    assert record.status == "active"
    assert record.manual_progress_history[-1]["lifecycle_status"] == "active"
    restored = ApplicationRecord.from_dict(record.to_dict())
    assert restored.manual_progress_history == record.manual_progress_history


def test_event_date_does_not_become_recruiting_year() -> None:
    record = application_from_progress_entry(
        {
            "company": "京东",
            "role": "TET 综合方向",
            "project": "",
            "status": "群面已安排",
            "action": "2026-08-06 参加群面",
            "application_id": "b" * 20,
        }
    )
    assert record is not None
    assert record.recruiting_year is None
    assert "recruiting-year-unresolved" in record.identity_evidence


def test_registry_round_trip_and_locked_import_is_idempotent(tmp_path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        """# 台账

### 已投递或已进入流程

- [x] 百度｜2027 管培生（J101320）｜**已投递**｜等待后续 <!-- jobmaildesk:application:11111111111111111111 -->
""",
        encoding="utf-8",
    )
    registry = ApplicationRegistry(tmp_path / "applications")
    first = registry.import_progress(ledger)
    second = registry.import_progress(ledger)
    assert len(first) == len(second) == 1
    assert first[0].application_key == second[0].application_key
    loaded = registry.load(first[0].application_key)
    assert loaded is not None
    assert loaded.job_code == "J101320"
    assert loaded.identity_locked is True


def test_preview_deduplicates_same_identity(tmp_path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        """# 台账

### 已投递或已进入流程

- [x] 科大讯飞｜AI 产品经理（J13348）｜**已投递**｜等待测评
- [x] 讯飞招聘｜AI产品经理 J13348｜**测评完成**｜等待后续
""",
        encoding="utf-8",
    )
    records = preview_progress_applications(ledger)
    assert len(records) == 1
    assert records[0].company == "科大讯飞"
    assert records[0].job_code == "J13348"


def test_duplicate_identity_merges_terminal_status(tmp_path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        """# 台账

### 已投递或已进入流程
- [x] 百度｜2027 管培生（J101320）｜**已投递**｜等待后续
- [x] 百度招聘｜管培生 J101320｜**未通过**｜流程结束
""",
        encoding="utf-8",
    )
    records = preview_progress_applications(ledger)
    assert len(records) == 1
    assert records[0].status == "ended"


def test_unidentified_placeholder_row_is_not_locked_or_imported(tmp_path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        """# 台账

### 已投递或已进入流程
- [ ] 公司待确认｜｜待确认｜等待补充
""",
        encoding="utf-8",
    )
    assert preview_progress_applications(ledger) == []


def test_netease_business_unit_survives_progress_normalization(tmp_path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        """# 台账

### 已投递或已进入流程
- [x] 网易雷火｜产品经理｜已投递｜等待后续
""",
        encoding="utf-8",
    )
    records = preview_progress_applications(ledger)
    assert len(records) == 1
    assert records[0].company == "网易游戏"
    assert records[0].business_unit == "雷火事业群"


def test_registry_all_raises_on_corrupt_application(tmp_path) -> None:
    applications = tmp_path / "applications"
    applications.mkdir()
    (applications / "app-deadbeefdeadbeefdeadbeef.md").write_text(
        "not frontmatter",
        encoding="utf-8",
    )
    registry = ApplicationRegistry(applications)
    with pytest.raises(ValueError, match="frontmatter"):
        registry.all()
    assert registry.all(ignore_invalid=True) == []


def test_application_record_rejects_string_boolean(tmp_path) -> None:
    applications = tmp_path / "applications"
    registry = ApplicationRegistry(applications)
    record = application_from_progress_entry(
        {
            "company": "帆软",
            "role": "产品经理",
            "project": "",
            "status": "已投递",
            "action": "",
            "application_id": "c" * 20,
        }
    )
    assert record is not None
    path = registry.save(record)
    content = path.read_text(encoding="utf-8").replace(
        "identity_locked: true",
        "identity_locked: 'false'",
    )
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="identity_locked"):
        registry.load(record.application_key)


def test_application_record_rejects_string_role_aliases_and_boolean_year() -> None:
    payload = {
        "application_key": "app-" + "d" * 24,
        "company_key": "demo",
        "company": "示例公司",
        "recruiting_project": None,
        "recruiting_year": True,
        "business_unit": None,
        "role": "产品经理",
        "role_aliases": "产品岗",
        "job_code": None,
        "submitted_at": None,
        "status": "active",
        "source": "test",
        "confirmed_by_user": True,
        "identity_locked": True,
        "legacy_application_ids": [],
        "identity_evidence": [],
    }
    with pytest.raises(ValueError, match="recruiting_year"):
        ApplicationRecord.from_dict(payload)
    payload["recruiting_year"] = 2027
    with pytest.raises(ValueError, match="role_aliases"):
        ApplicationRecord.from_dict(payload)


def test_generic_company_and_invalid_role_are_not_imported(tmp_path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        """# 台账

### 已投递或已进入流程
- [ ] 招聘｜产品经理｜待确认｜等待补充
- [ ] 示例公司｜点击官网进行修改｜待确认｜等待补充
""",
        encoding="utf-8",
    )
    assert preview_progress_applications(ledger) == []


def test_locked_import_refreshes_terminal_status_without_rewriting_identity(
    tmp_path,
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        """# 台账

### 已投递或已进入流程
- [x] 百度｜2027 管培生（J101320）｜**已投递**｜等待后续
""",
        encoding="utf-8",
    )
    registry = ApplicationRegistry(tmp_path / "applications")
    first = registry.import_progress(ledger)[0]
    ledger.write_text(
        """# 台账

### 已投递或已进入流程
- [x] 百度招聘｜管培生 J101320｜**未通过**｜流程结束
""",
        encoding="utf-8",
    )
    updated = registry.import_progress(ledger)[0]
    assert updated.application_key == first.application_key
    assert updated.status == "ended"
    assert updated.company == first.company


def test_schema_v1_migrates_to_schema_v6_with_lifecycle_defaults() -> None:
    payload = {
        "application_key": "app-" + "e" * 24,
        "company": "示例公司",
        "role": "产品经理",
        "role_aliases": [],
        "status": "active",
        "source": "legacy",
        "confirmed_by_user": False,
        "identity_locked": False,
        "schema_version": 1,
    }
    record = ApplicationRecord.from_dict(payload)
    assert record.schema_version == 6
    assert record.aliases == []
    assert record.attempt_sequence == 1
    assert record.merged_into is None
    assert record.deleted_at is None


def test_manual_application_uses_opaque_immutable_key() -> None:
    payload = {"company": "蔚来", "role": "芯片设计工程师", "location": "上海"}
    first = application_from_user_payload(payload)
    second = application_from_user_payload(payload)
    deterministic = stable_application_key(
        company="蔚来",
        role="芯片设计工程师",
        recruiting_project=None,
        recruiting_year=None,
        business_unit=None,
        job_code=None,
    )
    assert first.application_key != second.application_key
    assert first.application_key != deterministic
    original_key = first.application_key
    update_application_from_user_payload(
        first,
        {"company": "蔚来汽车", "role": "数字芯片工程师", "location": "深圳"},
    )
    assert first.application_key == original_key
    assert first.location == "深圳"
    assert identity_fingerprint(
        company=first.company,
        role=first.role,
        recruiting_project=first.recruiting_project,
        recruiting_year=first.recruiting_year,
        business_unit=first.business_unit,
        job_code=first.job_code,
    )


def test_canonical_ledger_marker_preserves_existing_key_even_with_job_code() -> None:
    existing_key = "app-" + "f" * 24
    record = application_from_progress_entry(
        {
            "application_id": existing_key,
            "company": "样例公司",
            "role": "后端工程师（J12345）",
            "project": "2027校园招聘",
            "location": "上海",
            "status": "已投递",
            "action": "等待通知",
        }
    )

    assert record is not None
    assert record.application_key == existing_key


def test_registry_resolves_aliases_and_merge_chain_without_recursion(tmp_path) -> None:
    registry = ApplicationRegistry(tmp_path / "applications")
    source = application_from_user_payload({"company": "样例公司", "role": "旧岗位"})
    target = application_from_user_payload({"company": "样例公司", "role": "新岗位"})
    target.aliases.append("app-" + "f" * 24)
    source.merged_into = target.application_key
    registry.save(source)
    registry.save(target)
    assert registry.load(source.application_key).application_key == target.application_key
    assert registry.load(target.aliases[0]).application_key == target.application_key
    source.merged_into = target.application_key
    target.merged_into = source.application_key
    registry.save(source)
    registry.save(target)
    assert registry.load(source.application_key) is None
