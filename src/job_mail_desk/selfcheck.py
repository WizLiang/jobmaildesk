"""In-package smoke test for a built JobMailDesk distribution.

The macOS release process learned the hard way that a bundle can import fine
from source and still crash once frozen (``_cffi_backend`` missing). This
module is what the Windows installer runs against the frozen EXE: it imports
every runtime-critical dependency, loads the shipped dictionaries and golden
vectors, exercises each fact store in a temporary directory, optionally
round-trips the system credential store, and (after data import) compares the
task / application / unresolved counts with the expected baseline. Nothing here
touches the mailbox or creates business facts under the real data directory.
"""
from __future__ import annotations

import importlib
import json
import platform
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__

EXPECTED_DICTIONARY_COUNTS = {
    "companies": 533,
    "programs": 129,
    "roles": 2829,
    "mail_templates": 4,
}
CORE_IMPORTS = (
    "cryptography.fernet",
    "_cffi_backend",
    "yaml",
    "apscheduler.schedulers.background",
    "apscheduler.triggers.cron",
    "apscheduler.triggers.interval",
    "PIL.Image",
    "pystray",
    "webview",
    "keyring",
)
WINDOWS_IMPORTS = ("keyring.backends.Windows", "win32ctypes.pywin32", "winreg", "msvcrt")
DOTNET_IMPORTS = ("clr", "webview.platforms.winforms", "webview.platforms.edgechromium")
UI_RESOURCES = ("index.html", "app.js", "style.css", "review.html", "review.js", "review.css")
KEYRING_PROBE_SERVICE = "job-mail-desk.selfcheck"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def _golden_path() -> Path | None:
    frozen_root = getattr(sys, "_MEIPASS", None)
    candidates = []
    if frozen_root:
        candidates.append(Path(frozen_root) / "job_mail_desk" / "golden" / "mail_identity.json")
    candidates.append(Path(__file__).resolve().parents[2] / "tests" / "golden" / "mail_identity.json")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "job_mail_desk"
    return Path(__file__).resolve().parent


def _safe(name: str, action: Callable[[], str]) -> CheckResult:
    try:
        return CheckResult(name, True, action())
    except Exception as exc:  # noqa: BLE001 - every failure must be reported, never raised
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}"[:400])


def _check_imports(names: tuple[str, ...]) -> list[CheckResult]:
    results = []
    for name in names:
        results.append(
            _safe(f"import {name}", lambda name=name: (importlib.import_module(name), "ok")[1])
        )
    return results


def _check_zoneinfo() -> str:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Asia/Shanghai")
    offset = datetime(2026, 9, 1, tzinfo=timezone.utc).astimezone(zone).utcoffset()
    if offset is None or offset.total_seconds() != 8 * 3600:
        raise RuntimeError(f"Asia/Shanghai offset {offset}")
    return "Asia/Shanghai +08:00 (tzdata present)"


def _check_dictionaries() -> str:
    from .identity_dictionaries import load_identity_dictionaries

    counts = load_identity_dictionaries().counts()
    mismatched = {
        key: (counts.get(key), expected)
        for key, expected in EXPECTED_DICTIONARY_COUNTS.items()
        if counts.get(key) != expected
    }
    if mismatched:
        raise RuntimeError(f"dictionary counts differ: {mismatched}")
    return json.dumps(counts, ensure_ascii=False)


def _check_golden_vectors() -> str:
    from .identity_dictionaries import load_identity_dictionaries
    from .models import MailRecord
    from .parser import SHANGHAI, parse_record

    path = _golden_path()
    if path is None:
        return "skipped: golden vectors not shipped"
    dictionaries = load_identity_dictionaries()
    vectors = json.loads(path.read_text(encoding="utf-8"))
    failures = []
    for vector in vectors:
        event = parse_record(
            MailRecord(
                uid=str(vector["name"]),
                subject=str(vector["subject"]),
                message_id=f"<{vector['name']}@example.invalid>",
                sender=str(vector["sender"]),
                received_at=(
                    datetime.fromisoformat(str(vector["received_at"]))
                    if vector.get("received_at")
                    else datetime(2026, 8, 12, 21, 53, tzinfo=SHANGHAI)
                ),
                body=str(vector["body"]),
            ),
            dictionaries,
        )
        if event is None or event.company != vector["company"] or event.role != vector["role"]:
            failures.append(vector["name"])
    if failures:
        raise RuntimeError(f"golden vectors failed: {failures}")
    return f"{len(vectors)} identity vectors parsed with expected company and role"


def _check_filtered_vectors() -> str:
    from .models import MailRecord
    from .parser import SHANGHAI, parse_record

    identity_path = _golden_path()
    if identity_path is None:
        raise RuntimeError("mail filter golden vectors missing")
    path = identity_path.with_name("mail_non_candidates.json")
    vectors = json.loads(path.read_text(encoding="utf-8"))
    for vector in vectors:
        event = parse_record(MailRecord(
            uid=vector["name"], subject=vector["subject"],
            message_id=f"<{vector['name']}@example.invalid>",
            sender=vector["sender"], body=vector["body"],
            received_at=datetime(2026, 9, 19, 12, tzinfo=SHANGHAI),
        ))
        if event is not None:
            raise RuntimeError(f"mail filter vector failed: {vector['name']}")
    policies = json.loads(identity_path.with_name("mail_filter_policy.json").read_text(encoding="utf-8"))
    for vector in policies:
        record = MailRecord(
            uid=vector["name"], subject=vector["subject"],
            message_id=f"<{vector['name']}@example.invalid>",
            sender="noreply@example.invalid", body=vector["body"],
            received_at=datetime(2026, 9, 19, 12, tzinfo=SHANGHAI),
        )
        if parse_record(record) is not None or (
            (parse_record(record, include_onsite_sessions=True) is not None)
            != vector["keep_onsite"]
        ):
            raise RuntimeError(f"mail policy vector failed: {vector['name']}")
    return f"{len(vectors)} noncandidate vectors filtered; {len(policies)} onsite policy vectors passed"


def _check_ui_resources() -> str:
    root = _resource_root()
    missing = [name for name in UI_RESOURCES if not (root / "ui" / name).exists()]
    missing += [
        name
        for name in ("companies.yml", "programs.yml", "roles.yml", "mail_templates.yml")
        if not (root / "identity_data" / name).exists()
    ]
    if missing:
        raise RuntimeError(f"missing packaged resources: {missing}")
    return f"{len(UI_RESOURCES)} UI files and 4 dictionaries present"


def _check_stores() -> str:
    from .activity_store import ActivityStore
    from .data_lock import data_directory_lease
    from .file_transaction import FileTransaction, recover_file_transactions
    from .ics_calendar import render_ics
    from .markdown_store import MarkdownTaskStore
    from .models import JobTask
    from .parser import SHANGHAI

    with tempfile.TemporaryDirectory(prefix="jobmaildesk-selfcheck-") as temporary:
        root = Path(temporary)
        task = JobTask(
            id="f" * 24,
            application_id="e" * 20,
            company="自检公司",
            role="自检岗位",
            recruiting_project=None,
            event_type="interview",
            stage="一面",
            round="一面",
            received_at=datetime(2026, 9, 1, 10, tzinfo=SHANGHAI),
            start_at=datetime(2026, 9, 2, 10, tzinfo=SHANGHAI),
            end_at=None,
            deadline_at=None,
            priority="high",
            status="planned",
            change_type="new",
            source_message_hash="d" * 32,
            research_status="not_queued",
            confidence=1.0,
            title="自检",
            action_summary="selfcheck; roundtrip, ok",
        )
        with data_directory_lease(root / ".data.lock"):
            store = MarkdownTaskStore(root / "tasks")
            store.save(task)
            loaded = store.load(task.id)
            if loaded is None or loaded.company != task.company:
                raise RuntimeError("MarkdownTaskStore roundtrip failed")
            facts = root / "tasks"
            with FileTransaction(root / ".transactions", (facts,)) as transaction:
                (facts / "extra.md").write_text("x", encoding="utf-8")
                transaction.commit()
            try:
                with FileTransaction(root / ".transactions", (facts,)):
                    (facts / "extra.md").unlink()
                    raise RuntimeError("rollback probe")
            except RuntimeError as exc:
                if str(exc) != "rollback probe":
                    raise
            if not (facts / "extra.md").exists():
                raise RuntimeError("FileTransaction rollback did not restore the file")
            if recover_file_transactions(root / ".transactions") != 0:
                raise RuntimeError("unexpected pending transactions")
            activity = ActivityStore(root / "activity-state.json")
            activity.record_event(
                dedup_key="selfcheck:1",
                kind="task.migrated",
                tabs=("today",),
                company="自检公司",
                entity_id="task:" + task.id,
            )
            if ActivityStore(root / "activity-state.json").unique_unread_count() != 1:
                raise RuntimeError("ActivityStore roundtrip failed")
            content, synced, _removed = render_ics([task], now=task.received_at)
            if synced != 1 or "UID:jobmaildesk:" + task.id not in content:
                raise RuntimeError("ICS rendering failed")
    return "task store, file transaction, activity store, data lock and ICS roundtrips ok"


def _check_keyring_roundtrip() -> str:
    import keyring

    probe = uuid.uuid4().hex
    token = uuid.uuid4().hex
    keyring.set_password(KEYRING_PROBE_SERVICE, probe, token)
    try:
        if keyring.get_password(KEYRING_PROBE_SERVICE, probe) != token:
            raise RuntimeError("credential store returned a different value")
    finally:
        try:
            keyring.delete_password(KEYRING_PROBE_SERVICE, probe)
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass
    backend = keyring.get_keyring()
    return f"{type(backend).__module__}.{type(backend).__name__} roundtrip ok"


def count_facts(local_root: Path) -> dict[str, int]:
    from .application_registry import ApplicationRegistry
    from .markdown_store import MarkdownTaskStore
    from .unresolved_store import UnresolvedStore

    return {
        "tasks": len(MarkdownTaskStore(local_root / "tasks").all()),
        "applications": len(
            ApplicationRegistry(local_root / "applications").all(ignore_invalid=True)
        ),
        "unresolved": len(UnresolvedStore(local_root / "unresolved").all()),
    }


def _check_counts(local_root: Path, expected: dict[str, int]) -> str:
    actual = count_facts(local_root)
    mismatched = {
        key: (actual.get(key), value)
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatched:
        raise RuntimeError(f"fact counts differ (actual, expected): {mismatched}")
    return json.dumps(actual)


def run_selfcheck(
    *,
    expect_counts: dict[str, int] | None = None,
    local_root: Path | None = None,
    keyring_roundtrip: bool = False,
    include_dotnet: bool = False,
) -> dict[str, object]:
    checks: list[CheckResult] = []
    checks.extend(_check_imports(CORE_IMPORTS))
    if sys.platform == "win32":
        checks.extend(_check_imports(WINDOWS_IMPORTS))
        if include_dotnet:
            checks.extend(_check_imports(DOTNET_IMPORTS))
    checks.append(_safe("zoneinfo Asia/Shanghai", _check_zoneinfo))
    checks.append(_safe("packaged resources", _check_ui_resources))
    checks.append(_safe("identity dictionaries", _check_dictionaries))
    checks.append(_safe("identity golden vectors", _check_golden_vectors))
    checks.append(_safe("mail filter golden vectors", _check_filtered_vectors))
    checks.append(_safe("fact store roundtrips", _check_stores))
    if keyring_roundtrip:
        checks.append(_safe("system credential store", _check_keyring_roundtrip))
    if expect_counts:
        if local_root is None:
            from .config import LOCAL_ROOT

            local_root = LOCAL_ROOT
        checks.append(_safe("fact counts", lambda: _check_counts(local_root, expect_counts)))
    return {
        "ok": all(check.ok for check in checks),
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": f"{sys.platform} {platform.machine()}",
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "checks": [asdict(check) for check in checks],
    }
