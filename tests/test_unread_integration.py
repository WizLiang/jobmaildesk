from job_mail_desk.activity_store import ActivityStore
from job_mail_desk.config import Settings
from job_mail_desk.ui_app import DesktopApi


def test_desktop_dashboard_exposes_persistent_unread_and_acknowledges_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("job_mail_desk.ui_app.TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(
        "job_mail_desk.ui_app.APPLICATIONS_DIR",
        tmp_path / "applications",
    )
    monkeypatch.setattr(
        "job_mail_desk.ui_app.UNRESOLVED_DIR",
        tmp_path / "unresolved",
    )
    monkeypatch.setattr("job_mail_desk.ui_app.STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(
        "job_mail_desk.ui_app.DASHBOARD_FILE",
        tmp_path / "dashboard.md",
    )
    store = ActivityStore(tmp_path / "activity-state.json")
    store.record_event(
        dedup_key="review:one:r1",
        kind="review.created",
        tabs=("today", "review"),
        entity_id="review:one",
    )
    api = DesktopApi(Settings(research_queue=tmp_path / "research.jsonl"))

    payload = api.get_dashboard()
    assert payload["unread"]["counts"]["today"] == 1
    assert payload["unread"]["counts"]["review"] == 1

    unread = api.acknowledge_tab_updates(
        "today",
        payload["unread"]["snapshot_sequence"],
    )
    assert unread["counts"]["today"] == 0
    assert unread["counts"]["review"] == 1
