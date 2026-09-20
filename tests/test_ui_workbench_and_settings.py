from pathlib import Path

from job_mail_desk.config import Settings, load_settings, settings_from_payload, write_settings


def test_reminder_and_calendar_settings_round_trip(tmp_path) -> None:
    path = tmp_path / "config.toml"
    expected = Settings(
        reminders_enabled=True,
        reminder_offsets_minutes=(1440, 120, 30),
        calendar_sync_enabled=True,
        calendar_name="求职日程",
        ui_font_scale=117,
    )
    write_settings(expected, path)
    actual = load_settings(path)
    assert actual.reminder_offsets_minutes == (1440, 120, 30)
    assert actual.calendar_sync_enabled is True
    assert actual.calendar_name == "求职日程"
    assert actual.ui_font_scale == 117


def test_settings_payload_normalizes_reminder_offsets() -> None:
    updated = settings_from_payload(
        Settings(),
        {
            "reminder_offsets_minutes": "30, 1440, 120, 30",
            "calendar_name": "JobMailDesk",
        },
    )
    assert updated.reminder_offsets_minutes == (1440, 120, 30)
    assert settings_from_payload(
        Settings(),
        {"ui_font_scale": 200, "calendar_name": "JobMailDesk"},
    ).ui_font_scale == 125


def test_review_and_application_dialogs_are_wired() -> None:
    from job_mail_desk import ui_app

    ui_dir = Path(ui_app.__file__).parent / "ui"
    html = (ui_dir / "index.html").read_text(encoding="utf-8")
    javascript = (ui_dir / "app.js").read_text(encoding="utf-8")
    for element_id in (
        "quickCreateDialog",
        "createApplicationChoice",
        "createTaskChoice",
        "ownershipDialog",
        "ownershipForm",
        "applicationDialog",
        "applicationForm",
        "applicationActionDialog",
        "applicationActionForm",
        "applicationCompanyChoices",
        "applicationRoleChoices",
        "syncCalendarNow",
        "rebuildIdentityLearning",
        "identityLearningRules",
        "ownershipRecommendation",
    ):
        assert f'id="{element_id}"' in html
        assert f'"#{element_id}"' in javascript
    for api_name in (
        "resolve_unresolved_workflow",
        "get_review_recommendation",
        "create_application",
        "edit_application",
        "list_application_choices",
        "get_application_merge_preview",
        "merge_applications",
        "get_application_delete_preview",
        "trash_application",
        "restore_application",
        "permanently_delete_application",
        "trash_task",
        "restore_task",
        "permanently_delete_task",
        "sync_calendar_now",
        "list_identity_learning_rules",
        "set_identity_learning_rule_enabled",
        "rebuild_identity_learning",
        "acknowledge_tab_updates",
        "acknowledge_progress_update",
    ):
        assert api_name in javascript


def test_application_ui_keeps_stage_values_and_guards_refreshes() -> None:
    from job_mail_desk import ui_app

    ui_dir = Path(ui_app.__file__).parent / "ui"
    html = (ui_dir / "index.html").read_text(encoding="utf-8")
    javascript = (ui_dir / "app.js").read_text(encoding="utf-8")
    stylesheet = (ui_dir / "style.css").read_text(encoding="utf-8")

    for stage in (
        "网申",
        "测评",
        "笔试",
        "一面",
        "二面",
        "三面",
        "HR 面",
        "Offer",
        "已拒绝",
        "已结束",
    ):
        assert f'"{stage}"' in javascript
        assert f'value="{stage}"' in html

    assert "ensureSelectValue" in javascript
    assert 'creating ? (detail.next_stage' in javascript
    assert 'detail.next_stage ?? ""' in javascript
    assert "item.recommended" in javascript
    assert "loadOwnershipChoices" in javascript
    assert "ownershipTaskDetails" in html
    assert 'name="create_task"' in html
    assert 'id="taskDialog" class="workflow-dialog"' in html
    assert 'name="application_key"' in html
    assert ".workflow-form [hidden]" in stylesheet
    assert "dashboardRequestSeq" in javascript
    assert "mutationVersion" in javascript
    assert "state.cardSaving.size" in javascript
    assert "@media (max-width: 680px)" in stylesheet
    assert '"has-events"' in javascript
    assert '"urgent-day"' in javascript
    assert ".month-cell.today.selected" in stylesheet
    assert ".month-cell.selected.urgent-day" in stylesheet
    assert ".week-day.urgent-day" in stylesheet
    assert ".unread-badge" in stylesheet
    assert ".progress-company.has-unread" in stylesheet
    assert "acknowledge_tab_updates" in javascript
    assert "acknowledge_progress_update" in javascript
    assert html.count('class="unread-badge"') == 6
    assert "captureScrollState" in javascript
    assert "restoreScrollState" in javascript
    assert "set_editor_mode" not in javascript
    startup = Path(ui_app.__file__).read_text(encoding="utf-8").split(
        "def _run_ui_primary", 1
    )[1]
    assert "apply_runtime_settings(settings)" in startup
