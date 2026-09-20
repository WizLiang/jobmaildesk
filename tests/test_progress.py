from datetime import datetime

import pytest

from job_mail_desk.application_registry import (
    ApplicationRegistry,
    application_from_user_payload,
)
from job_mail_desk.models import JobTask
from job_mail_desk.parser import SHANGHAI
from job_mail_desk.progress import (
    build_application_timeline,
    create_progress_template,
    export_progress,
    progress_payload,
    read_progress_entries,
    sync_task_to_ledger,
    sync_current_applications_to_ledger,
)


def test_progress_ledger_reads_location_and_keeps_legacy_rows(tmp_path) -> None:
    ledger = tmp_path / "岗位投递决策台账.md"
    ledger.write_text(
        "### 已投递或已进入流程\n"
        "- [x] 样例公司｜产品经理｜上海 / 深圳｜**已投递**｜等待筛选\n"
        "- [x] 旧公司｜后端工程师｜**笔试**｜准备考试\n"
        "\n### 当前优先待投\n",
        encoding="utf-8",
    )
    entries = read_progress_entries(ledger)
    assert entries[0]["location"] == "上海 / 深圳"
    assert entries[0]["status"] == "已投递"
    assert entries[1]["location"] == ""
    assert entries[1]["status"] == "笔试"


def task(task_id: str, stage: str, status: str, hour: int) -> JobTask:
    return JobTask(
        id=(task_id + "a") * 12,
        application_id="a" * 20,
        company="样例公司",
        role="产品经理",
        recruiting_project="2027 校招",
        event_type="interview",
        stage=stage,
        round="一面" if stage == "面试" else None,
        received_at=datetime(2026, 8, 1, hour, 0, tzinfo=SHANGHAI),
        start_at=datetime(2026, 8, 6, hour, 0, tzinfo=SHANGHAI),
        end_at=None,
        deadline_at=None,
        priority="high",
        status=status,  # type: ignore[arg-type]
        change_type="new",
        source_message_hash="b" * 32,
        research_status="not_queued",
        confidence=1.0,
        title=stage,
        action_summary=f"处理{stage}",
    )


def test_progress_groups_application_chain_and_preserves_manual_region(tmp_path) -> None:
    written = task("1", "笔试", "done", 10)
    interview = task("2", "面试", "planned", 14)
    applications = progress_payload([written, interview])
    assert len(applications) == 1
    assert applications[0]["current_stage"] == "面试"
    assert [item["stage"] for item in applications[0]["history"]] == [
        "面试",
        "笔试",
    ]

    output = tmp_path / "求职当前进展.md"
    export_progress(
        [written, interview],
        output,
        application_records=[],
    )
    content = output.read_text(encoding="utf-8").replace(
        "<!-- 本区可手写复盘或决策；自动刷新不会覆盖。 -->",
        "我的手动判断",
    )
    output.write_text(content, encoding="utf-8")
    export_progress(
        [written, interview],
        output,
        application_records=[],
    )
    refreshed = output.read_text(encoding="utf-8")
    assert "样例公司｜产品经理" in refreshed
    assert "> [!abstract]- 样例公司｜产品经理 · 面试" in refreshed
    assert "> | 完成时间 | — |" in refreshed
    assert "> **流程记录**" in refreshed
    assert "面试｜一面｜已安排" in refreshed
    assert "<!-- jobmaildesk:application:aaaaaaaaaaaaaaaaaaaa -->" in refreshed
    assert "- [x] 2026-08-06 10:00｜笔试｜已完成 <!-- jobmaildesk:1a1a1a1a1a1a1a1a1a1a1a1a -->" in refreshed
    assert "- [ ] 2026-08-06 14:00｜面试｜一面｜已安排 <!-- jobmaildesk:2a2a2a2a2a2a2a2a2a2a2a2a -->" in refreshed
    assert "我的手动判断" in refreshed


def test_completed_task_updates_only_exact_ledger_row(tmp_path) -> None:
    completed = task("6", "人才测评", "done", 12)
    completed.company = "科大讯飞"
    completed.role = "AI产品经理"
    completed.application_id = "c" * 20
    ledger = tmp_path / "岗位投递决策台账.md"
    ledger.write_text(
        """# 岗位投递决策台账

### 已投递或已进入流程

- [ ] 科大讯飞｜AI 产品经理（J13348）｜**已投递**｜保留我的下一步动作
- [x] 科大讯飞｜项目经理（J10000）｜**一面已确认**｜不要修改这一行

### 当前优先待投
""",
        encoding="utf-8",
    )
    assert sync_task_to_ledger(completed, ledger) == 1
    content = ledger.read_text(encoding="utf-8")
    assert "- [x] 科大讯飞｜AI 产品经理（J13348）｜**人才测评已完成，等待后续**｜保留我的下一步动作" in content
    assert "<!-- jobmaildesk:application:cccccccccccccccccccc -->" in content
    assert "项目经理（J10000）｜**一面已确认**｜不要修改这一行" in content


def test_ledger_sync_refuses_ambiguous_role_match(tmp_path) -> None:
    completed = task("7", "笔试", "done", 12)
    ledger = tmp_path / "岗位投递决策台账.md"
    ledger.write_text(
        """### 已投递或已进入流程
- [x] 样例公司｜产品经理｜**已投递**｜第一条
- [x] 样例公司｜产品经理｜**已投递**｜第二条
### 当前优先待投
""",
        encoding="utf-8",
    )
    assert sync_task_to_ledger(completed, ledger) == 0
    assert "笔试已完成" not in ledger.read_text(encoding="utf-8")


def test_confirmed_application_is_added_to_ledger_once(tmp_path) -> None:
    submitted = task("8", "网申", "confirmed", 12)
    submitted.event_type = "application"
    submitted.start_at = None
    submitted.end_at = None
    ledger = tmp_path / "岗位投递决策台账.md"
    ledger.write_text(
        """### 已投递或已进入流程

### 当前优先待投
""",
        encoding="utf-8",
    )
    assert sync_task_to_ledger(submitted, ledger) == 1
    assert sync_task_to_ledger(submitted, ledger) == 0
    content = ledger.read_text(encoding="utf-8")
    assert content.count("jobmaildesk:application:aaaaaaaaaaaaaaaaaaaa") == 1
    assert "- [x] 样例公司｜产品经理｜**2026-08-01 网申已提交，等待简历筛选**" in content


def test_jd_tet_ledger_alias_merges_into_mail_application(tmp_path) -> None:
    interview = task("9", "群面", "planned", 14)
    interview.company = "京东"
    interview.role = "TET 综合方向"
    ledger = tmp_path / "岗位投递决策台账.md"
    ledger.write_text(
        """### 已投递或已进入流程
- [x] 京东｜TET 管理培训生（综合方向）｜**群面已确认**｜准备案例
### 当前优先待投
""",
        encoding="utf-8",
    )
    applications = progress_payload([interview], ledger)
    assert len(applications) == 1
    assert applications[0]["application_id"] == interview.application_id
    assert applications[0]["ledger_status"] == "群面已确认"


def test_netease_business_unit_ledger_merges_with_parent_company_task(tmp_path) -> None:
    submitted = task("b", "网申", "confirmed", 12)
    submitted.company = "网易游戏"
    submitted.role = "游戏AI产品经理"
    submitted.recruiting_project = "雷火事业群 · 2027校园招聘"
    ledger = tmp_path / "岗位投递决策台账.md"
    ledger.write_text(
        """### 已投递或已进入流程
- [x] 网易雷火｜游戏 AI 产品经理｜**已完成投递，等待筛选**｜保留复盘
### 当前优先待投
""",
        encoding="utf-8",
    )
    applications = progress_payload([submitted], ledger)
    assert len(applications) == 1
    assert applications[0]["company"] == "网易游戏"
    assert applications[0]["project"] == "雷火事业群 · 2027校园招聘"
    assert applications[0]["ledger_status"] == "已完成投递，等待筛选"


def test_ended_ledger_result_overrides_stale_mail_stage(tmp_path) -> None:
    assessment = task("result", "人才测评", "done", 12)
    assessment.company = "科大讯飞"
    assessment.role = "AI产品经理"
    assessment.application_key = "app-iflytek"
    ledger = tmp_path / "岗位投递决策台账.md"
    ledger.write_text(
        """### 已投递或已进入流程
- [x] 科大讯飞｜AI 产品经理（J13348）｜**2026-08-05 未通过（简历筛选未通过）**｜简历挂，停止跟进 <!-- jobmaildesk:application:app-iflytek -->
### 当前优先待投
""",
        encoding="utf-8",
    )
    applications = progress_payload([assessment], ledger)
    assert len(applications) == 1
    assert applications[0]["current_stage"] == "2026-08-05 未通过（简历筛选未通过）"
    assert applications[0]["current_status"] == "done"
    assert applications[0]["status_label"] == "2026-08-05 未通过（简历筛选未通过）"
    assert applications[0]["active"] is False
    assert applications[0]["next_time"] is None
    assert applications[0]["history"][0]["stage"] == "人才测评"


def test_user_ledger_fields_override_progress_card_with_stable_id(tmp_path) -> None:
    interview = task("edit", "AI 面试", "planned", 14)
    interview.company = "旧企业名"
    interview.role = "旧岗位名"
    interview.application_key = "app-1234567890abcdef1234"
    ledger = tmp_path / "岗位投递决策台账.md"
    ledger.write_text(
        """### 已投递或已进入流程
- [ ] OPPO｜AI 产品经理｜**群面已安排**｜准备群面案例 <!-- jobmaildesk:application:app-1234567890abcdef1234 -->
### 当前优先待投
""",
        encoding="utf-8",
    )

    application = progress_payload([interview], ledger)[0]
    assert application["company"] == "OPPO"
    assert application["role"] == "AI 产品经理"
    assert application["current_stage"] == "群面已安排"
    assert application["current_action"] == "准备群面案例"


def test_progress_ignores_instruction_text_when_selecting_role() -> None:
    correct = task("c", "网申", "done", 10)
    correct.company = "网易游戏"
    correct.role = "游戏AI产品经理"
    corrupt = task("d", "招聘通知", "done", 11)
    corrupt.company = "网易游戏"
    corrupt.role = "请到官网 campus.163.com 前往个人中心应聘记录进行修改"
    applications = progress_payload([correct, corrupt])
    assert len(applications) == 1
    assert applications[0]["role"] == "游戏AI产品经理"


def test_batch_ledger_sync_uses_current_application_node(tmp_path) -> None:
    old_assessment = task("e", "AI 面试", "done", 10)
    old_assessment.company = "京东"
    old_assessment.role = "TET 综合方向"
    current_group = task("f", "群面", "planned", 14)
    current_group.company = "京东"
    current_group.role = "TET 综合方向"
    ledger = tmp_path / "岗位投递决策台账.md"
    ledger.write_text(
        """### 已投递或已进入流程
- [x] 京东｜TET 管理培训生（综合方向）｜**AI 面试已完成**｜准备案例
### 当前优先待投
""",
        encoding="utf-8",
    )
    assert sync_current_applications_to_ledger(
        [current_group, old_assessment], ledger
    ) == 1
    content = ledger.read_text(encoding="utf-8")
    assert "**群面已安排**" in content
    assert "**AI 面试已完成**" not in content


def test_irrelevant_items_do_not_enter_progress() -> None:
    ignored = task("3", "招聘通知", "irrelevant", 9)
    assert progress_payload([ignored]) == []


def test_decision_ledger_adds_companies_without_mail_tasks(tmp_path) -> None:
    ledger = tmp_path / "岗位投递决策台账.md"
    ledger.write_text(
        """# 岗位投递决策台账

### 已投递或已进入流程

- [x] 样例甲｜产品经理｜**一面邀请**｜准备项目表达
- [x] 样例甲｜项目经理｜**已投递**｜等待后续
- [x] 样例乙｜管培生｜**已结束：未通过**｜保留复盘

### 当前优先待投
""",
        encoding="utf-8",
    )
    applications = progress_payload([], ledger)
    assert [item["company"] for item in applications] == [
        "样例甲",
        "样例甲",
        "样例乙",
    ]
    assert applications[0]["active"] is True
    assert applications[1]["active"] is True
    assert applications[2]["active"] is False


def test_ignored_company_is_not_reintroduced_from_ledger(tmp_path) -> None:
    ignored = task("4", "招聘通知", "irrelevant", 9)
    ledger = tmp_path / "岗位投递决策台账.md"
    ledger.write_text(
        """### 已投递或已进入流程
- [x] 样例公司｜产品经理｜**已投递**｜等待后续
### 当前优先待投
""",
        encoding="utf-8",
    )
    assert progress_payload([ignored], ledger) == []


def test_company_alias_is_used_when_suppressing_ignored_ledger_entry(tmp_path) -> None:
    ignored = task("5", "未通过", "irrelevant", 9)
    ignored.company = "deeproute.ai"
    ledger = tmp_path / "岗位投递决策台账.md"
    ledger.write_text(
        """### 已投递或已进入流程
- [x] 元戎启行｜产品经理｜**已投递**｜等待后续
### 当前优先待投
""",
        encoding="utf-8",
    )
    assert progress_payload([ignored], ledger) == []


def test_progress_template_is_safe_and_never_overwrites(tmp_path) -> None:
    ledger = tmp_path / "求职进展台账.md"
    assert create_progress_template(ledger) is True
    content = ledger.read_text(encoding="utf-8")
    assert "### 已投递或已进入流程" in content
    assert "公司｜岗位｜当前进展｜下一步动作" in content
    assert progress_payload([], ledger) == []
    ledger.write_text(content + "\n我的内容\n", encoding="utf-8")
    assert create_progress_template(ledger) is False
    assert ledger.read_text(encoding="utf-8").endswith("我的内容\n")


def test_progress_export_without_managed_sentinels_fails_closed(tmp_path) -> None:
    output = tmp_path / "progress.md"
    original = "# 手写进展\n\n不要覆盖。\n"
    output.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="受管标记"):
        export_progress([], output)

    assert output.read_text(encoding="utf-8") == original


def test_timeline_aggregates_only_allowed_semantic_boundaries() -> None:
    first = task("g", "网申", "confirmed", 9)
    first.event_type = "application"
    first.start_at = None
    duplicate = task("h", "网申回执", "confirmed", 10)
    duplicate.event_type = "application"
    duplicate.start_at = None
    timed = task("i", "面试", "planned", 11)
    same_time = task("j", "面试", "confirmed", 12)
    same_time.start_at = timed.start_at
    second_round = task("k", "面试", "planned", 13)
    second_round.round = "二面"
    different_time = task("l", "面试", "planned", 14)

    timeline = build_application_timeline(
        [first, duplicate, timed, same_time, second_round, different_time]
    )

    receipt = next(
        item
        for item in timeline
        if item["source_count"] == 2 and item["status"] == "confirmed"
    )
    assert receipt["source_count"] == 2
    assert receipt["task_ids"] == [first.id, duplicate.id]
    assert receipt["event_at"] == duplicate.received_at.isoformat()
    interview_events = [item for item in timeline if item["stage"] == "面试"]
    assert sorted(item["source_count"] for item in interview_events) == [1, 1, 2]


def test_timeline_uses_real_event_time_and_chinese_status_latest_first() -> None:
    completed = task("m", "笔试", "done", 9)
    completed.completed_at = datetime(2026, 8, 8, 18, 0, tzinfo=SHANGHAI)
    completed.completed_at_inferred = True
    confirmed = task("n", "面试", "confirmed", 10)
    confirmed.start_at = datetime(2026, 8, 9, 9, 0, tzinfo=SHANGHAI)

    timeline = build_application_timeline([completed, confirmed])

    assert [item["stage"] for item in timeline] == ["面试", "笔试"]
    assert timeline[0]["time_kind"] == "start_at"
    assert timeline[0]["status_label"] == "已确认"
    assert timeline[1]["event_at"] == completed.completed_at.isoformat()
    assert timeline[1]["time_kind"] == "completed_at"
    assert timeline[1]["time_inferred"] is True
    assert timeline[1]["status_label"] == "已完成"

    rejected = task("q", "未通过", "done", 11)
    assert build_application_timeline([rejected])[0]["status_label"] == "已结束"


def test_progress_never_merges_different_application_keys() -> None:
    first = task("o", "网申", "confirmed", 9)
    first.event_type = "application"
    first.start_at = None
    first.application_key = "app-" + "1" * 24
    second = task("p", "网申", "confirmed", 10)
    second.event_type = "application"
    second.start_at = None
    second.application_key = "app-" + "2" * 24

    applications = progress_payload([first, second])

    assert len(applications) == 2
    assert all(item["history"][0]["source_count"] == 1 for item in applications)


def test_registry_progress_only_jiangbolong_is_visible_without_fake_task() -> None:
    received_at = datetime(2026, 8, 15, 11, 27, tzinfo=SHANGHAI)
    record = application_from_user_payload(
        {
            "company": "江波龙",
            "role": "IC实现工程师（上海）(J11374)",
            "job_code": "J11374",
            "location": "上海",
        },
        now=received_at,
    )
    record.progress_nodes = [
        {
            "id": "progress-" + "1" * 24,
            "source_hash": "2" * 32,
            "event_at": received_at.isoformat(),
            "stage": "网申",
            "status": "completed",
            "next_stage": "简历筛选",
        }
    ]

    applications = progress_payload([], application_records=[record])

    assert len(applications) == 1
    application = applications[0]
    assert application["application_id"] == record.application_key
    assert application["company"] == "江波龙"
    assert application["role"] == "IC实现工程师（上海）"
    assert application["current_stage"] == "网申"
    assert application["history"][0]["task_id"] is None
    assert application["history"][0]["task_ids"] == []


def test_registry_keeps_two_taskless_roles_at_same_company_separate() -> None:
    current = datetime(2026, 8, 15, 12, 0, tzinfo=SHANGHAI)
    first = application_from_user_payload(
        {
            "company": "翱捷科技股份有限公司",
            "role": "数字后端工程师",
        },
        now=current,
    )
    second = application_from_user_payload(
        {
            "company": "翱捷科技股份有限公司",
            "role": "数字中端实现工程师",
        },
        now=current,
    )
    first.manual_progress_history = [
        {
            "event_at": current.isoformat(),
            "stage": "网申",
            "status": "completed",
            "next_stage": "简历筛选",
        }
    ]
    second.manual_progress_history = [
        {
            "event_at": current.isoformat(),
            "stage": "网申",
            "status": "completed",
            "next_stage": "简历筛选",
        }
    ]

    applications = progress_payload(
        [],
        application_records=[first, second],
    )

    assert len(applications) == 2
    assert {item["application_id"] for item in applications} == {
        first.application_key,
        second.application_key,
    }
    assert {item["role"] for item in applications} == {
        "数字后端工程师",
        "数字中端实现工程师",
    }


def test_registry_application_and_task_merge_into_one_progress_card() -> None:
    source = task("r", "笔试", "planned", 14)
    record = application_from_user_payload(
        {
            "company": source.company,
            "role": source.role,
        },
        now=source.received_at,
    )
    source.application_key = record.application_key
    record.progress_nodes = [
        {
            "id": "progress-" + "3" * 24,
            "source_hash": source.source_message_hash,
            "source_task_id": source.id,
            "event_at": source.received_at.isoformat(),
            "stage": source.stage,
            "status": "pending",
            "next_stage": None,
        }
    ]

    applications = progress_payload(
        [source],
        application_records=[record],
    )

    assert len(applications) == 1
    assert applications[0]["application_id"] == record.application_key
    assert applications[0]["history"][0]["task_ids"] == [source.id]


def test_progress_export_includes_registry_application_without_tasks(
    tmp_path,
) -> None:
    current = datetime(2026, 8, 15, 11, 27, tzinfo=SHANGHAI)
    record = application_from_user_payload(
        {
            "company": "江波龙",
            "role": "IC实现工程师（上海）(J11374)",
        },
        now=current,
    )
    record.progress_nodes = [
        {
            "id": "progress-" + "8" * 24,
            "source_hash": "9" * 32,
            "event_at": current.isoformat(),
            "stage": "网申",
            "status": "completed",
            "next_stage": "简历筛选",
        }
    ]
    output = tmp_path / "求职当前进展.md"
    applications_dir = tmp_path / "applications"
    ApplicationRegistry(applications_dir).save(record)

    assert export_progress(
        [],
        output,
        applications_dir=applications_dir,
    ) == 1

    content = output.read_text(encoding="utf-8")
    assert "江波龙｜IC实现工程师（上海） · 网申" in content
    assert f"jobmaildesk:application:{record.application_key}" in content
    assert "进行中 1｜申请链 1" in content
    assert "网申｜已完成" in content
