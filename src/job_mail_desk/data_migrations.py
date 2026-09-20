from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .application_lifecycle import reconcile_application
from .application_registry import ApplicationRegistry
from .file_transaction import FileTransaction
from .markdown_store import MarkdownTaskStore
from .parser import SHANGHAI
from .state import StateStore


SMARTSENS_MIGRATION_KEY = "migration_smartsens_exam_20260814_v1"
SMARTSENS_MANUAL_TASK_ID = "4b4a651509224cef912b9830"
SMARTSENS_MAIL_TASK_ID = "4ce1d91e676101cb5b525a15"


def migrate_smartsens_exam_task(
    *,
    tasks_dir: Path,
    applications_dir: Path,
    state_db: Path,
) -> bool:
    state = StateStore(state_db)
    if state.metadata(SMARTSENS_MIGRATION_KEY) == "done":
        return False
    store = MarkdownTaskStore(tasks_dir)
    manual = store.load(SMARTSENS_MANUAL_TASK_ID)
    mail = store.load(SMARTSENS_MAIL_TASK_ID)
    if not manual or not mail:
        state.set_metadata(SMARTSENS_MIGRATION_KEY, "not-applicable")
        return False
    if (
        manual.application_key != mail.application_key
        or manual.company != "思特威电子科技"
        or mail.company != "思特威电子科技"
        or manual.stage not in {"笔试", "在线笔试"}
        or mail.stage != "在线笔试"
    ):
        state.set_metadata(SMARTSENS_MIGRATION_KEY, "not-applicable")
        return False
    with FileTransaction(
        tasks_dir.parent / ".transactions",
        (tasks_dir, applications_dir),
    ) as transaction:
        manual.event_type = "assessment"
        manual.stage = "在线笔试"
        manual.start_at = datetime(2026, 8, 14, 0, 0, tzinfo=SHANGHAI)
        manual.end_at = None
        manual.deadline_at = datetime(2026, 8, 17, 23, 55, tzinfo=SHANGHAI)
        manual.duration_minutes = 90
        if manual.status not in {"done", "cancelled", "irrelevant", "expired"}:
            manual.status = "planned"
        manual.priority = "urgent"
        manual.change_type = "update"
        manual.source_message_hash = mail.source_message_hash
        manual.source_sender = mail.source_sender
        manual.source_url = mail.source_url
        manual.mail_locator = dict(mail.mail_locator) if mail.mail_locator else None
        manual.title = mail.title
        manual.action_summary = mail.action_summary
        manual.requirements = list(mail.requirements)
        manual.updated_at = datetime.now(SHANGHAI)
        store.save(manual)

        if not mail.deleted_at:
            store.trash(mail.id)
        store.permanently_delete(mail.id)
        if manual.application_key:
            registry = ApplicationRegistry(applications_dir)
            application = registry.load(manual.application_key)
            if application:
                application.current_task_id = manual.id
                registry.save(application)
                reconcile_application(application.application_key, registry, store)
        transaction.commit()
    state.set_metadata(SMARTSENS_MIGRATION_KEY, "done")
    return True
