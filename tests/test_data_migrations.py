from datetime import datetime

from job_mail_desk.application_registry import ApplicationRegistry
from job_mail_desk.data_migrations import (
    SMARTSENS_MAIL_TASK_ID,
    SMARTSENS_MANUAL_TASK_ID,
    migrate_smartsens_exam_task,
)
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.models import ApplicationRecord, JobTask
from job_mail_desk.parser import SHANGHAI


def task(task_id: str, *, mail: bool) -> JobTask:
    return JobTask(
        id=task_id,
        application_id="legacy",
        application_key="app-smartsens",
        company="思特威电子科技",
        role="数字后端工程师",
        recruiting_project=None,
        event_type="assessment" if mail else "manual",
        stage="在线笔试" if mail else "笔试",
        round=None,
        received_at=datetime(2026, 8, 14, 12, 0, tzinfo=SHANGHAI),
        start_at=datetime(2026, 8, 14, 0, 0, tzinfo=SHANGHAI) if mail else None,
        end_at=datetime(2026, 8, 14, 1, 30, tzinfo=SHANGHAI) if mail else None,
        deadline_at=None,
        priority="normal",
        status="expired" if mail else "needs_review",
        change_type="new",
        source_message_hash="mail-source" if mail else "manual",
        research_status="not_queued",
        confidence=1.0,
        title="笔试邀请" if mail else "待定",
        action_summary="参加在线笔试" if mail else "待定",
        source_url="https://exam.example.invalid" if mail else None,
        mail_locator={"mailbox": "INBOX", "uid": "1"} if mail else None,
    )


def test_smartsens_migration_keeps_manual_id_and_tombstones_auto_task(tmp_path) -> None:
    tasks_dir = tmp_path / "tasks"
    applications_dir = tmp_path / "applications"
    store = MarkdownTaskStore(tasks_dir)
    store.save(task(SMARTSENS_MANUAL_TASK_ID, mail=False))
    store.save(task(SMARTSENS_MAIL_TASK_ID, mail=True))
    ApplicationRegistry(applications_dir).save(
        ApplicationRecord(
            application_key="app-smartsens",
            company_key="smartsens",
            company="思特威电子科技",
            recruiting_project=None,
            recruiting_year=2027,
            business_unit=None,
            role="数字后端工程师",
            role_aliases=[],
            job_code=None,
            submitted_at=None,
            status="active",
            source="mail",
            confirmed_by_user=True,
            identity_locked=True,
        )
    )

    assert migrate_smartsens_exam_task(
        tasks_dir=tasks_dir,
        applications_dir=applications_dir,
        state_db=tmp_path / "state.db",
    )

    manual = store.load(SMARTSENS_MANUAL_TASK_ID)
    old = store.load(SMARTSENS_MAIL_TASK_ID)
    assert manual and manual.status == "planned"
    assert manual.deadline_at == datetime(2026, 8, 17, 23, 55, tzinfo=SHANGHAI)
    assert manual.duration_minutes == 90
    assert manual.source_message_hash == "mail-source"
    assert old and old.tombstoned
