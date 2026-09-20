from __future__ import annotations

import os
import ctypes
import json
import logging
import re
import sys
import threading
import time
import unicodedata
import webbrowser
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable
from urllib.parse import quote
from functools import wraps


def _enable_high_dpi_rendering() -> None:
    """Prevent Windows from bitmap-scaling the whole WebView on HiDPI screens."""
    if sys.platform != "win32":
        return
    try:
        set_context = ctypes.windll.user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        set_context(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


_enable_high_dpi_rendering()

import webview

LOGGER = logging.getLogger(__name__)
from apscheduler.schedulers.base import SchedulerNotRunningError
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

from . import __version__
from .application_lifecycle import (
    reconcile_all_applications,
    reconcile_application,
    sync_application_progress_from_task,
)
from .application_registry import (
    ApplicationRegistry,
    application_from_user_payload,
    update_application_from_user_payload,
)
from .activity_store import ACTIVITY_TABS, ActivityStore
from .config import (
    APPLICATIONS_DIR,
    CONFIG_PATH,
    DASHBOARD_FILE,
    DICTIONARIES_DIR,
    IMPORTED_DICTIONARIES_DIR,
    LOCAL_ROOT,
    STATE_DB,
    TASKS_DIR,
    UNRESOLVED_DIR,
    Settings,
    settings_from_payload,
    write_settings,
)
from .confirmation_service import (
    append_progress_node,
    new_request_id,
    validate_confirmation_request,
)
from .agent_bridge import apply_task_update, sync_outputs
from .credentials import MailCredential, load_credential, save_credential
from .dictionary_compiler import compile_workbook
from .data_migrations import migrate_smartsens_exam_task
from .data_lock import data_directory_lease
from .derived_outbox import DerivedOutbox
from .identity_dictionaries import load_identity_dictionaries
from .identity_learning import IdentityLearningStore
from .file_transaction import FileTransaction, recover_file_transactions
from .mail_reader import ImapReader
from .macos_calendar import sync_macos_calendar
from .macos_dock import register_badge_renderer, set_dock_badge
from .ics_calendar import ics_path
from .notifier import send_due_reminders, notify_message
from .tray_badge import ClickableTrayIcon, TrayBadge
from .windows_notify import (
    ensure_toast_registration,
    register_notification_click_handler,
    register_notification_sink,
)
from .dashboard import cached_dashboard_payload
from .markdown_store import MarkdownTaskStore
from .models import ParsedEvent
from .parser import SHANGHAI
from .normalization import canonical_company, canonical_role
from .research import request_states
from .progress import build_application_timeline, create_progress_template
from .private_link_store import PrivateLinkStore
from .review_window import ReviewWindowController
from .runtime_control import (
    RuntimeControl,
    RuntimeScope,
    RuntimeStopping,
    ShutdownCoordinator,
)
from .scanner import bootstrap_identity_learning, scan_once, _source_hash_from_locator
from .review_explanation import explain_review
from .scan_alerts import NEW_MAIL_TITLE, announce_new_reviews, new_pending_reviews, pending_snapshot
from .scheduler import create_background_scheduler
from .state import StateStore
from .task_service import (
    create_manual_task,
    critical_time,
    edit_task_fields,
    legacy_application_id,
    task_from_event,
)
from .unresolved_store import UnresolvedStore


def fact_mutation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._mutation_guard():
            return method(self, *args, **kwargs)

    return guarded


def _resource(name: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return root / "job_mail_desk" / "ui" / name if hasattr(sys, "_MEIPASS") else Path(__file__).parent / "ui" / name


def _open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        webbrowser.open(path.as_uri())


def _open_obsidian_uri(path: Path) -> None:
    webbrowser.open(
        f"obsidian://open?vault={quote(path.parent.name)}&file={quote(path.stem)}"
    )


CAPSULE_WIDTH = 36
CAPSULE_HEIGHT = 88
CAPSULE_VISIBLE_EDGE = 32
INSTANCE_MUTEX_NAME = r"Local\JobMailDesk.Desktop.Singleton.v1"
ERROR_ALREADY_EXISTS = 183


def _task_revision(task: Any) -> str:
    """Return the persisted optimistic-concurrency token for a task."""
    return sha256(
        json.dumps(
            task.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _merge_preview_token(source: Any, target: Any) -> str:
    """Bind a merge preview to both identities and their exact revisions."""
    snapshot = [
        source.application_key,
        int(source.revision),
        source.updated_at.isoformat() if source.updated_at else "",
        target.application_key,
        int(target.revision),
        target.updated_at.isoformat() if target.updated_at else "",
    ]
    return sha256(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _progress_scope(value: object) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).split()
    ).casefold()


def _window_handle(window: Any) -> int:
    handle = window.native.Handle
    return int(handle.ToInt64()) if hasattr(handle, "ToInt64") else int(handle)


def _hide_from_task_switcher(window: Any, *, enabled: bool = True) -> None:
    """Keep the tray-managed widget out of the taskbar and Alt+Tab.

    Tool windows have no minimise/maximise buttons, so this is only applied
    when the user turned the taskbar button off in settings.
    """
    if not enabled:
        return
    if sys.platform != "win32" or os.environ.get("JOBMAILDESK_UI_QA") == "1":
        return
    hwnd = _window_handle(window)
    user32 = ctypes.windll.user32
    get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    extended_style = get_style(hwnd, -20)
    extended_style = (extended_style | 0x00000080) & ~0x00040000
    set_style(hwnd, -20, extended_style)
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0037)


def _set_background_mode(window: Any, hidden: bool) -> None:
    """Tell the page it is (in)visible so it pauses its periodic refresh.

    Runs on a helper thread: ``evaluate_js`` waits for the page and must not
    be called from the WinForms UI thread (FormClosing handler).
    """
    script = (
        "window.setBackgroundMode && window.setBackgroundMode("
        + ("true" if hidden else "false")
        + ")"
    )

    def run() -> None:
        try:
            window.evaluate_js(script)
        except Exception:  # noqa: BLE001 - the page may be gone during shutdown
            LOGGER.debug("背景模式切换脚本未执行", exc_info=True)

    threading.Thread(target=run, name="jobmaildesk-background-mode", daemon=True).start()


def install_close_to_tray(window: Any, quit_state: dict[str, bool]) -> bool:
    """On Windows, the title-bar close button hides the window instead of quitting.

    The app keeps scanning and reminding from the tray; the only way to quit
    is the tray menu, which sets ``quit_state["requested"]`` before destroying
    the window. Implemented at the WinForms level (``FormClosing``) so the
    project rule of never binding pywebview's synchronous ``closing`` event
    for teardown work is untouched: the handler only cancels and hides.
    """
    if sys.platform != "win32" or os.environ.get("JOBMAILDESK_CLOSE_EXITS") == "1":
        return False
    try:
        import clr  # type: ignore[import-not-found]  # pythonnet, present with pywebview on Windows

        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import CloseReason  # type: ignore[import-not-found]

        form = window.native

        def on_form_closing(sender: Any, args: Any) -> None:
            if quit_state.get("requested"):
                return
            if args.CloseReason != CloseReason.UserClosing:
                return  # Windows shutdown / log-off / task manager: let it close
            args.Cancel = True
            sender.Hide()
            _set_background_mode(window, True)

        form.FormClosing += on_form_closing
        return True
    except Exception:  # noqa: BLE001 - fall back to the old close-means-quit behaviour
        LOGGER.warning("无法安装“关闭即隐藏到托盘”，关闭窗口将退出程序", exc_info=True)
        return False


def show_existing_window(
    title: str = "JobMailDesk",
    *,
    wait_seconds: float = 0,
) -> bool:
    """Reveal the existing tray-managed window without launching a duplicate."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            user32.SetForegroundWindow(hwnd)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


WEBVIEW2_CLIENT_KEY = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def webview2_runtime_version() -> str | None:
    """Installed Edge WebView2 Runtime version on Windows, else ``None``.

    Checks the same EdgeUpdate client keys the runtime installer writes
    (64-bit machine-wide, 32-bit machine-wide, per-user).
    """
    if sys.platform != "win32":
        return None
    import winreg

    candidates = (
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + WEBVIEW2_CLIENT_KEY),
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + WEBVIEW2_CLIENT_KEY),
        (winreg.HKEY_CURRENT_USER, "Software\\" + WEBVIEW2_CLIENT_KEY),
    )
    for hive, path in candidates:
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _kind = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if version and str(version) not in {"", "0.0.0.0"}:
            return str(version)
    return None


def ensure_webview2_runtime() -> bool:
    """Refuse to start on the legacy IE engine; explain and open the download.

    pywebview silently falls back to MSHTML when the WebView2 runtime is
    missing, and the interface cannot run there. Returns ``True`` when the UI
    may start (always off Windows or when ``JOBMAILDESK_SKIP_WEBVIEW2_CHECK=1``).
    """
    if sys.platform != "win32" or os.environ.get("JOBMAILDESK_SKIP_WEBVIEW2_CHECK") == "1":
        return True
    if webview2_runtime_version():
        return True
    message = (
        "JobMailDesk 需要 Microsoft Edge WebView2 运行时才能显示界面。\n\n"
        "点击「确定」打开微软官方下载页（Evergreen Bootstrapper），"
        "安装完成后再重新启动 JobMailDesk。"
    )
    LOGGER.error("缺少 Edge WebView2 运行时，界面无法启动")
    try:
        # MB_OKCANCEL | MB_ICONWARNING | MB_SETFOREGROUND
        result = ctypes.windll.user32.MessageBoxW(None, message, "JobMailDesk", 0x00000001 | 0x00000030 | 0x00010000)
    except (AttributeError, OSError):
        result = 1
    if result == 1:  # IDOK
        webbrowser.open(WEBVIEW2_DOWNLOAD_URL)
    return False


def _webview_start_options() -> dict[str, Any]:
    options: dict[str, Any] = {"debug": False, "private_mode": False}
    if sys.platform == "win32":
        # Pin the WebView2 backend so a missing runtime can never degrade to
        # the IE engine, and keep the browser profile (tab/scroll persistence
        # lives in localStorage) inside the app data directory instead of the
        # shared %APPDATA%\pywebview folder.
        options["gui"] = "edgechromium"
        options["storage_path"] = str(LOCAL_ROOT / "webview2")
    return options


def _calendar_backend() -> str:
    if sys.platform == "darwin":
        return "macos_calendar"
    if sys.platform == "win32":
        return "ics_file"
    return "unsupported"


def _claim_single_instance(
    name: str = INSTANCE_MUTEX_NAME,
) -> tuple[Any | None, bool]:
    """Atomically claim the desktop instance before any window is created."""
    if sys.platform == "darwin":
        import fcntl

        lock_path = CONFIG_PATH.parent / "instance.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None, False
        return handle, True
    if sys.platform != "win32":
        return None, True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    handle = create_mutex(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    already_exists = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    if already_exists:
        kernel32.CloseHandle(handle)
        return None, False
    return int(handle), True


def _close_instance_handle(handle: Any | None) -> None:
    if sys.platform == "win32" and handle:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
    elif sys.platform == "darwin" and handle:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _capsule_anchor(
    x: int,
    y: int,
    width: int = CAPSULE_WIDTH,
    height: int = CAPSULE_HEIGHT,
) -> tuple[int, int, int]:
    """Calculate a movable, multi-monitor edge anchor for a capsule."""
    screens = list(webview.screens)
    if not screens:
        return x, x, y
    center_x = x + width // 2
    center_y = y + height // 2

    def distance(screen: Any) -> int:
        nearest_x = min(max(center_x, screen.x), screen.x + screen.width)
        nearest_y = min(max(center_y, screen.y), screen.y + screen.height)
        return (center_x - nearest_x) ** 2 + (center_y - nearest_y) ** 2

    screen = min(screens, key=distance)
    left_distance = abs(x - screen.x)
    right_distance = abs(screen.x + screen.width - (x + width))
    on_left = left_distance <= right_distance
    shown_x = screen.x + 2 if on_left else screen.x + screen.width - width - 2
    hidden_x = (
        screen.x - width + CAPSULE_VISIBLE_EDGE
        if on_left
        else screen.x + screen.width - CAPSULE_VISIBLE_EDGE
    )
    target_y = max(
        screen.y + 28,
        min(y, screen.y + screen.height - height - 40),
    )
    return hidden_x, shown_x, target_y


class DesktopApi:
    def __init__(
        self,
        settings: Settings,
        on_settings_saved: Callable[[Settings], None] | None = None,
        runtime_control: RuntimeControl | None = None,
        on_privacy_reset: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._on_settings_saved = on_settings_saved
        self._runtime_control = runtime_control or RuntimeControl()
        self._on_privacy_reset = on_privacy_reset
        self._privacy_reset_lock = threading.Lock()
        self._review_windows: ReviewWindowController | None = None
        self._window: Any = None
        self._scan_lock = self._runtime_control.scan_lock
        self._expanded_geometry: tuple[int, int, int, int] | None = None
        self._geometry_lock = threading.Lock()
        self._capsule_generation = 0
        self._capsule_positions: tuple[int, int, int] | None = None
        self._capsule_snap_timer: threading.Timer | None = None
        self._dashboard_lock = threading.Lock()
        self._outbox_lock = threading.Lock()
        self._calendar_lock = threading.Lock()
        self._calendar_thread: threading.Thread | None = None
        self._calendar_pending = False
        try:
            MarkdownTaskStore(TASKS_DIR).backfill_completed_times()
        except OSError as exc:
            LOGGER.warning("暂时无法回填历史任务完成时间：%s", exc)

    def get_dashboard(self) -> dict[str, object]:
        self._retry_derived_outbox()
        with self._dashboard_lock:
            local_root = TASKS_DIR.parent
            payload = dict(cached_dashboard_payload(
                self._settings.research_queue,
                self._settings.progress_source,
                cache_path=DASHBOARD_FILE.with_name("dashboard-cache.json"),
                tasks_dir=TASKS_DIR,
                state_db=local_root / "state.db",
                unresolved_dir=local_root / "unresolved",
                applications_dir=local_root / "applications",
            ))
            task_revisions = {
                task.id: _task_revision(task)
                for task in MarkdownTaskStore(TASKS_DIR).all()
            }
            task_collections = [
                payload.get("tasks"),
                payload.get("task_trash"),
                *(
                    (payload.get("overview") or {}).values()
                    if isinstance(payload.get("overview"), dict)
                    else ()
                ),
            ]
            for collection in task_collections:
                if not isinstance(collection, list):
                    continue
                for item in collection:
                    if isinstance(item, dict) and str(item.get("id") or "") in task_revisions:
                        item["revision"] = task_revisions[str(item["id"])]

            application_records = {
                record.application_key: record
                for record in ApplicationRegistry(APPLICATIONS_DIR).all(
                    ignore_invalid=True,
                    include_merged=False,
                    include_deleted=False,
                )
            }
            valid_progress_scopes: set[str] = set()
            for item in payload.get("progress") or []:
                if not isinstance(item, dict):
                    continue
                application_key = str(item.get("application_key") or "")
                record = application_records.get(application_key)
                if record:
                    item["revision"] = record.revision
                    item["progress_scope"] = record.company_key
                else:
                    item["revision"] = None
                    item["progress_scope"] = (
                        str(item.get("company_key") or item.get("company") or "")
                    )
                for value in (item.get("progress_scope"), item.get("company")):
                    if scope := _progress_scope(value):
                        valid_progress_scopes.add(scope)
            try:
                activity_store = self._activity_store()
                unread = activity_store.unread_payload()
                snapshot = int(unread["snapshot_sequence"])
                orphaned_scopes = [
                    str(scope)
                    for scope in unread.get("progress_unread_companies", [])
                    if _progress_scope(scope) not in valid_progress_scopes
                ]
                for orphaned_scope in orphaned_scopes:
                    unread = activity_store.acknowledge_progress_company(
                        orphaned_scope,
                        through_sequence=snapshot,
                    )
            except (OSError, RuntimeError) as exc:
                LOGGER.warning("未读状态暂时不可用：%s", exc)
                unread = {
                    "counts": {tab: 0 for tab in ACTIVITY_TABS},
                    "progress_unread_companies": [],
                    "snapshot_sequence": 0,
                    "unique_unread_count": 0,
                }
            payload["unread"] = unread
            set_dock_badge(int(unread["unique_unread_count"]))
            return payload

    def _retry_derived_outbox(self) -> None:
        if not self._outbox_lock.acquire(blocking=False):
            return
        try:
            outbox = DerivedOutbox(TASKS_DIR.parent / "derived-outbox.json")
            if not outbox.pending():
                return
            store = MarkdownTaskStore(TASKS_DIR)

            def export_handler(_item) -> None:
                self._export(store)

            def runtime_handler(_item) -> None:
                if self._settings.calendar_sync_enabled:
                    result = sync_macos_calendar(
                        store.all(),
                        calendar_name=self._settings.calendar_name,
                    )
                    if result.status not in {"ok", "unsupported"}:
                        raise RuntimeError(result.detail or result.status)
                send_due_reminders(
                    store.all(),
                    self._settings,
                    StateStore(STATE_DB),
                )

            def activity_handler(item) -> None:
                review = next(
                    (
                        record
                        for record in UnresolvedStore(UNRESOLVED_DIR).all()
                        if record.confirmation_operation_id == item.operation_id
                    ),
                    None,
                )
                if not review:
                    return
                application = ApplicationRegistry(APPLICATIONS_DIR).load(
                    review.resolved_application_key or ""
                )
                task = (
                    store.load(review.resolved_task_id)
                    if review.resolved_task_id
                    else None
                )
                tabs = ["review"]
                if application:
                    tabs.append("progress")
                if task:
                    tabs.append("list")
                    if task.start_at or task.end_at or task.deadline_at:
                        tabs.extend(("today", "week", "month"))
                self._record_activity(
                    dedup_key=f"confirmation:{item.operation_id}",
                    kind="review.confirmed",
                    tabs=tuple(dict.fromkeys(tabs)),
                    company=application.company_key if application else None,
                    entity_id=f"review:{review.id}",
                    strict=True,
                )

            outbox.process(
                {
                    "export": export_handler,
                    "runtime": runtime_handler,
                    "activity": activity_handler,
                }
            )
        except (OSError, ValueError, RuntimeError) as exc:
            LOGGER.warning("派生操作 outbox 暂时无法处理：%s", exc)
        finally:
            self._outbox_lock.release()

    def _activity_store(self) -> ActivityStore:
        return ActivityStore(TASKS_DIR.parent / "activity-state.json")

    def _record_activity(
        self,
        *,
        dedup_key: str,
        kind: str,
        tabs: tuple[str, ...],
        company: str | None = None,
        entity_id: str | None = None,
        strict: bool = False,
    ) -> None:
        try:
            store = self._activity_store()
            store.record_event(
                dedup_key=dedup_key,
                kind=kind,
                tabs=tabs,
                company=company,
                entity_id=entity_id,
            )
            set_dock_badge(store.unique_unread_count())
        except (OSError, ValueError, RuntimeError) as exc:
            if strict:
                raise
            LOGGER.warning("未读活动记录失败，业务事实已保留：%s", exc)

    def _record_task_activity(self, task: Any, kind: str) -> None:
        version = int(
            (task.updated_at or datetime.now().astimezone()).timestamp() * 1_000_000
        )
        tabs = ["list"]
        progress_scope: str | None = None
        if task.application_key:
            tabs.append("progress")
            application = ApplicationRegistry(APPLICATIONS_DIR).load(
                task.application_key
            )
            progress_scope = (
                application.company_key if application else task.company
            )
        if task.status == "needs_review" or not (
            task.start_at or task.end_at or task.deadline_at
        ):
            tabs.append("review")
        if task.start_at or task.end_at or task.deadline_at:
            tabs.extend(("today", "week", "month"))
        self._record_activity(
            dedup_key=f"task:{task.id}:{kind}:{version}",
            kind=f"task.{kind}",
            tabs=tuple(dict.fromkeys(tabs)),
            company=progress_scope if "progress" in tabs else None,
            entity_id=f"task:{task.id}",
        )

    def _record_application_activity(self, application: Any, kind: str) -> None:
        version = int(
            (application.updated_at or datetime.now().astimezone()).timestamp()
            * 1_000_000
        )
        self._record_activity(
            dedup_key=f"application:{application.application_key}:{kind}:{version}",
            kind=f"application.{kind}",
            tabs=("progress",),
            company=application.company_key,
            entity_id=f"application:{application.application_key}",
        )

    def acknowledge_tab_updates(
        self,
        tab_id: str,
        through_sequence: int,
    ) -> dict[str, object]:
        unread = self._activity_store().acknowledge_tab(
            tab_id,
            through_sequence=int(through_sequence),
        )
        set_dock_badge(int(unread["unique_unread_count"]))
        return unread

    def acknowledge_progress_update(
        self,
        company: str | list[str],
        through_sequence: int,
    ) -> dict[str, object]:
        scopes = [company] if isinstance(company, str) else list(company)
        if not scopes:
            raise ValueError("至少需要一个进展范围。")
        store = self._activity_store()
        unread: dict[str, object] | None = None
        for scope in dict.fromkeys(str(item) for item in scopes if str(item).strip()):
            unread = store.acknowledge_progress_company(
                scope,
                through_sequence=int(through_sequence),
            )
        if unread is None:
            raise ValueError("进展范围无效。")
        set_dock_badge(int(unread["unique_unread_count"]))
        return unread

    def get_app_settings(self) -> dict[str, object]:
        try:
            credential = load_credential()
            credential_configured = True
            email = credential.email
        except RuntimeError:
            credential_configured = False
            email = ""
        return {
            "privacy_reset_supported": sys.platform == "win32" and self._on_privacy_reset is not None,
            "privacy_reset_completed": (LOCAL_ROOT / ".privacy-reset-complete").exists(),
            "local_data_directory": str(LOCAL_ROOT),
            "storage_change_supported": sys.platform == "win32" and self._on_privacy_reset is not None
                and not os.environ.get("JOBMAILDESK_LOCAL_ROOT"),
            "storage_move_completed": (LOCAL_ROOT / ".storage-move-complete").exists(),
            "credential_configured": credential_configured,
            "email": email,
            "mail_provider": self._settings.mail_provider,
            "provider": self._settings.mail_provider,
            "mail_host": self._settings.mail_host,
            "mail_port": self._settings.mail_port,
            "mail_ssl": self._settings.mail_ssl,
            "ssl": self._settings.mail_ssl,
            "use_ssl": self._settings.mail_ssl,
            "poll_minutes": self._settings.poll_minutes,
            "lookback_days": self._settings.lookback_days,
            "include_onsite_sessions": self._settings.include_onsite_sessions,
            "obsidian_enabled": self._settings.obsidian_enabled,
            "obsidian_output": str(self._settings.obsidian_output),
            "progress_enabled": self._settings.progress_enabled,
            "progress_output": str(self._settings.progress_output),
            "progress_source": str(self._settings.progress_source or ""),
            "config_path": str(CONFIG_PATH),
            "research_enabled": self._settings.research_enabled,
            "app_version": __version__,
            "reminders_enabled": self._settings.reminders_enabled,
            "reminder_offsets_minutes": list(
                self._settings.reminder_offsets_minutes
            ),
            "calendar_sync_enabled": self._settings.calendar_sync_enabled,
            "calendar_name": self._settings.calendar_name,
            "calendar_backend": _calendar_backend(),
            "calendar_export_path": (
                str(ics_path(self._settings.calendar_name)) if sys.platform == "win32" else ""
            ),
            "ui_font_scale": self._settings.ui_font_scale,
            "taskbar_button": self._settings.taskbar_button,
            "taskbar_button_supported": sys.platform == "win32",
            "notify_new_mail": self._settings.notify_new_mail,
            "close_hides_to_tray": bool(
                getattr(self, "_close_to_tray_installed", False)
            ),
        }

    def select_storage_location(self) -> str:
        if not self._window:
            return ""
        from .storage_location import destination_for, validate_move
        selected = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not selected:
            return ""
        destination = destination_for(str(selected[0]))
        validate_move(LOCAL_ROOT, destination)
        return str(destination)

    def change_storage_location(self, destination: str, confirmed: bool = False) -> dict[str, object]:
        if confirmed is not True:
            raise ValueError("请确认迁移数据并重新打开程序。")
        if not self._on_privacy_reset or os.environ.get("JOBMAILDESK_LOCAL_ROOT"):
            raise RuntimeError("当前为隔离测试目录或非桌面模式，不能修改常用数据位置。")
        from .storage_location import start_move
        with self._privacy_reset_lock:
            self._runtime_control.raise_if_stopping()
            start_move(LOCAL_ROOT, Path(destination))
            self._runtime_control.begin_stop()
            timer = threading.Timer(0.5, self._on_privacy_reset)
            timer.daemon = True
            timer.start()
        return {"status": "pending"}

    def clear_personal_information(self, confirmation: str) -> dict[str, object]:
        from .privacy_reset import request_reset
        if self._on_privacy_reset is None:
            raise RuntimeError("请从桌面程序的设置页执行清除。")
        with self._privacy_reset_lock:
            self._runtime_control.raise_if_stopping()
            request_reset(LOCAL_ROOT, confirmation, self._settings)
            self._runtime_control.begin_stop()
            # Give the bridge time to display the pending state before closing.
            timer = threading.Timer(0.5, self._on_privacy_reset)
            timer.daemon = True
            timer.start()
        return {"status": "pending"}

    def save_app_settings(self, payload: dict[str, object]) -> dict[str, object]:
        self._runtime_control.raise_if_stopping()
        authorization_code = str(payload.get("authorization_code") or "").strip()
        updated = settings_from_payload(self._settings, payload)
        for label, enabled, path in (
            ("Obsidian输出", updated.obsidian_enabled, updated.obsidian_output),
            ("求职进展输出", updated.progress_enabled, updated.progress_output),
        ):
            if enabled and path.suffix.lower() != ".md":
                raise ValueError(f"{label}必须是 .md 文件。")
        if updated.progress_source and updated.progress_source.suffix.lower() != ".md":
            raise ValueError("手动进展台账必须是 .md 文件。")
        if updated.obsidian_enabled:
            updated.obsidian_output.parent.mkdir(parents=True, exist_ok=True)
        if updated.progress_enabled:
            updated.progress_output.parent.mkdir(parents=True, exist_ok=True)
        if authorization_code:
            save_credential(str(payload.get("email") or ""), authorization_code)
        write_settings(updated)
        (LOCAL_ROOT / ".privacy-reset-complete").unlink(missing_ok=True)
        self._settings = updated
        self._export(MarkdownTaskStore(TASKS_DIR))
        if self._on_settings_saved:
            self._on_settings_saved(updated)
        return self.get_app_settings()

    def get_calendar_status(self) -> dict[str, object]:
        return {
            "status": StateStore(STATE_DB).metadata("calendar_sync_status")
            or "not_synced",
            "backend": _calendar_backend(),
            "export_path": (
                str(ics_path(self._settings.calendar_name)) if sys.platform == "win32" else ""
            ),
        }

    def open_calendar_export_folder(self) -> bool:
        """Reveal the folder holding the Windows ICS export (no-op elsewhere)."""
        if sys.platform != "win32":
            return False
        target = ics_path(self._settings.calendar_name).parent
        target.mkdir(parents=True, exist_ok=True)
        _open_path(target)
        return True

    def sync_calendar_now(self, calendar_name: str = "") -> dict[str, object]:
        selected_name = calendar_name.strip() or self._settings.calendar_name
        if not selected_name or len(selected_name) > 80:
            raise ValueError("日历名称无效。")
        state = StateStore(STATE_DB)
        state.set_metadata("calendar_sync_status", None)
        result = sync_macos_calendar(
            MarkdownTaskStore(TASKS_DIR).all(),
            calendar_name=selected_name,
        )
        state.set_metadata("calendar_sync_status", result.status)
        return result.to_dict()

    def get_scan_progress(self) -> dict[str, object]:
        # This read must never wait on scan_lock or a disk/database lock.
        return self._runtime_control.scan_progress.snapshot()

    def test_mail_settings(self, payload: dict[str, object]) -> dict[str, object]:
        email = str(payload.get("email") or "").strip()
        authorization_code = str(payload.get("authorization_code") or "").strip()
        if not authorization_code:
            try:
                existing = load_credential()
            except RuntimeError:
                return {"ok": False, "detail": "请先填写IMAP授权码。"}
            email = email or existing.email
            authorization_code = existing.authorization_code
        try:
            temporary_settings = settings_from_payload(self._settings, payload)
            snapshot = ImapReader(
                temporary_settings,
                MailCredential(email=email, authorization_code=authorization_code),
            ).mailbox_snapshot()
            return {
                "ok": True,
                "detail": f"只读连接成功，当前未读 {snapshot['unseen']} 封。",
            }
        except Exception as exc:
            return {"ok": False, "detail": f"连接失败：{exc}"}

    def select_markdown_path(self, kind: str) -> str:
        if not self._window:
            return ""
        defaults = {
            "obsidian_output": self._settings.obsidian_output,
            "progress_output": self._settings.progress_output,
            "progress_source": self._settings.progress_source
            or self._settings.progress_output.with_name("求职进展台账.md"),
        }
        if kind not in defaults:
            raise ValueError("不支持的路径类型")
        default = defaults[kind]
        selected = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=str(default.parent),
            save_filename=default.name,
            file_types=("Markdown (*.md)",),
        )
        return str(selected[0]) if selected else ""

    def create_progress_source_template(self, path_value: str) -> dict[str, object]:
        path = Path(path_value.strip())
        if not path_value.strip() or path.suffix.lower() != ".md":
            raise ValueError("请选择一个 .md 进展台账路径。")
        created = create_progress_template(path)
        return {"created": created, "path": str(path)}

    def get_dictionary_status(self) -> dict[str, object]:
        dictionaries = load_identity_dictionaries(DICTIONARIES_DIR)
        report_path = IMPORTED_DICTIONARIES_DIR / "compilation-report.json"
        report: dict[str, object] = {}
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                report = {}
        return {
            "counts": dictionaries.counts(),
            "user_dictionary_enabled": any(
                (IMPORTED_DICTIONARIES_DIR / name).exists()
                for name in ("companies.yml", "programs.yml", "roles.yml")
            ),
            "source_filename": report.get("source_filename"),
            "compiled_at": report.get("compiled_at"),
            "directory": str(IMPORTED_DICTIONARIES_DIR),
        }

    def select_dictionary_workbook(self) -> str:
        if not self._window:
            return ""
        selected = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=("Excel 工作簿 (*.xlsx)",),
        )
        return str(selected[0]) if selected else ""

    def compile_dictionary_workbook(
        self,
        path_value: str,
        sheet_name: str = "2027秋招信息表",
    ) -> dict[str, object]:
        source = Path(path_value.strip())
        if not source.is_file() or source.suffix.lower() != ".xlsx":
            raise ValueError("请选择有效的 .xlsx 秋招表。")
        DICTIONARIES_DIR.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix="dictionary-staging-",
            dir=DICTIONARIES_DIR,
        ) as temporary:
            staging = Path(temporary)
            report = compile_workbook(
                source,
                staging,
                load_identity_dictionaries(),
                sheet_name=sheet_name.strip() or "2027秋招信息表",
            )
            validated = load_identity_dictionaries(staging)
            IMPORTED_DICTIONARIES_DIR.mkdir(parents=True, exist_ok=True)
            for name in (
                "companies.yml",
                "programs.yml",
                "roles.yml",
                "compilation-report.json",
            ):
                os.replace(staging / name, IMPORTED_DICTIONARIES_DIR / name)
        return {
            "ok": True,
            "compiled": report,
            "counts": validated.counts(),
            "directory": str(IMPORTED_DICTIONARIES_DIR),
        }

    @fact_mutation
    def update_status(
        self,
        task_id: str,
        status: str,
        expected_revision: str = "",
    ) -> dict[str, object]:
        store = MarkdownTaskStore(TASKS_DIR)
        current = store.load(task_id)
        if current is None:
            raise KeyError(task_id)
        if expected_revision and expected_revision != _task_revision(current):
            raise ValueError("待办内容已更新，请刷新后重试。")
        apply_task_update(
            self._settings,
            task_id,
            {"status": status},
            store=store,
            local_dashboard=DASHBOARD_FILE,
            record_activity=False,
        )
        task = store.load(task_id)
        if task and task.application_key:
            reconcile_application(
                task.application_key,
                ApplicationRegistry(APPLICATIONS_DIR),
                store,
            )
            self._export(store)
        if task:
            # Completed/cancelled tasks must leave the calendar export and any
            # pending reminder right away, not at the next 10-minute job.
            self._refresh_task_runtime(task, store)
            self._record_task_activity(task, "status")
        return self.get_dashboard()

    @fact_mutation
    def snooze(
        self,
        task_id: str,
        until: str,
        expected_revision: str = "",
    ) -> dict[str, object]:
        snoozed = datetime.fromisoformat(until)
        store = MarkdownTaskStore(TASKS_DIR)
        task = store.load(task_id)
        if not task:
            raise KeyError(task_id)
        if expected_revision and expected_revision != _task_revision(task):
            raise ValueError("待办内容已更新，请刷新后重试。")
        task = store.update_status(task_id, "planned", snoozed_until=snoozed)
        self._sync_application_for_task(task, store)
        self._export(store)
        self._refresh_task_runtime(task, store)
        self._record_task_activity(task, "snoozed")
        return self.get_dashboard()

    def _sync_application_for_task(self, task: Any, store: MarkdownTaskStore) -> None:
        """Mirror a task edit into its application chain, then reconcile."""
        if not task.application_key:
            return
        registry = ApplicationRegistry(APPLICATIONS_DIR)
        record = registry.load(task.application_key)
        if record and sync_application_progress_from_task(record, task):
            registry.save(record)
        reconcile_application(task.application_key, registry, store)

    def trigger_scan(self, days: int | None = None) -> dict[str, object]:
        if days is not None and (type(days) is not int or days not in {7, 30, 90}):
            raise ValueError("补扫范围请选择 7、30 或 90 天。")
        if self._runtime_control.stopping:
            return {"status": "stopping"}
        if not self._scan_lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            self._runtime_control.raise_if_stopping()
            # A user-initiated scan is an explicit request to re-evaluate the
            # bounded mailbox window. Task and unresolved stores are
            # idempotent, so replay updates existing facts without duplicating
            # cards while avoiding a confusing "fetched but skipped" result.
            unresolved_store = UnresolvedStore(UNRESOLVED_DIR)
            before = pending_snapshot(unresolved_store)
            summary = scan_once(
                self._settings,
                **({"days": days, "force_reprocess": True, "recheck_pending": True} if days else {}),
                runtime_control=self._runtime_control,
            )
            if not self._runtime_control.stopping:
                # The tray's 立即扫描 runs with the window hidden, so it needs
                # the same new-mail alert the scheduled scan gives.
                fresh = new_pending_reviews(before, unresolved_store)
                if fresh:
                    announce_new_reviews(fresh, self._settings, notify_message)
            return {"status": "ok", "summary": summary.to_dict()}
        except RuntimeStopping:
            return {"status": "stopping"}
        finally:
            self._scan_lock.release()

    def attach_review_window_controller(
        self,
        controller: ReviewWindowController,
    ) -> None:
        self._review_windows = controller

    def open_review_window(
        self,
        source_hash: str,
        preferred_key: str = "",
    ) -> dict[str, object]:
        if not self._review_windows:
            raise RuntimeError("原生复核窗口尚未就绪。")
        return self._review_windows.open_window(source_hash, preferred_key)

    def _review_window_saved(self, source_hash: str) -> None:
        if not self._window:
            return
        self._window.show()
        self._window.evaluate_js(
            "window.reviewWindowCompleted && "
            f"window.reviewWindowCompleted({json.dumps(source_hash)})"
        )

    def prepare_shutdown(self) -> None:
        if self._review_windows:
            self._review_windows.close_all()
        timer = self._capsule_snap_timer
        self._capsule_snap_timer = None
        if timer:
            timer.cancel()

    @contextmanager
    def _mutation_guard(self, *, transactional: bool = True):
        with self._scan_lock:
            with data_directory_lease(TASKS_DIR.parent / ".data.lock"):
                if not transactional:
                    yield
                    return
                root = TASKS_DIR.parent
                directories = tuple(
                    directory
                    for directory in (
                        TASKS_DIR,
                        APPLICATIONS_DIR,
                        UNRESOLVED_DIR,
                    )
                    if directory.parent == root
                )
                with FileTransaction(
                    root / ".transactions",
                    directories,
                ) as transaction:
                    yield
                    transaction.commit()

    @fact_mutation
    def sync_ledger(self) -> dict[str, object]:
        """Import manual local ledger edits, reconcile cards and refresh exports."""
        store = MarkdownTaskStore(TASKS_DIR)
        if self._settings.progress_source:
            registry = ApplicationRegistry(APPLICATIONS_DIR)
            registry.import_progress(self._settings.progress_source)
            reconcile_all_applications(registry, store)
        self._export(store)
        return {"status": "ok", "dashboard": self.get_dashboard(),
                "imported": bool(self._settings.progress_source)}

    @fact_mutation
    def ignore_unresolved(self, source_hash: str) -> dict[str, object]:
        record = UnresolvedStore(UNRESOLVED_DIR).ignore(source_hash)
        self._record_activity(
            dedup_key=f"review:{source_hash}:ignored:r{record.revision}",
            kind="review.ignored",
            tabs=("today", "review"),
            company=record.company,
            entity_id=f"review:{source_hash}",
        )
        return self.get_dashboard()

    def list_ignored_reviews(self) -> list[dict[str, object]]:
        with self._scan_lock:
            records = [record for record in UnresolvedStore(UNRESOLVED_DIR).all()
                       if record.status == "ignored" and not record.resolved_task_id
                       and not record.resolved_application_key
                       and not (record.reason == "recruiting-marketing"
                                and record.parser_version == "2026.09.19.1")]
            return [{"id": record.id, "revision": record.revision,
                     "title": record.title, "company": record.company, "role": record.role,
                     "received_at": record.received_at.isoformat()}
                    for record in sorted(records, key=lambda item: item.received_at, reverse=True)]

    @fact_mutation
    def restore_ignored_review(self, source_hash: str, expected_revision: int) -> dict[str, object]:
        reviews = UnresolvedStore(UNRESOLVED_DIR)
        record = reviews.load(source_hash)
        if (not record or record.status != "ignored" or record.revision != expected_revision
                or record.resolved_task_id or record.resolved_application_key):
            raise ValueError("记录已变化，请刷新已忽略列表后重试。")
        keys = {record.id, _source_hash_from_locator(record.mail_locator)} - {None}
        # The Markdown fact remains ignored until this succeeds. If saving it
        # fails, a later scan reconciles pending index rows back to ignored.
        StateStore(STATE_DB).restore_ignored_review(keys)
        restored = reviews.restore_ignored(source_hash, expected_revision)
        self._record_activity(
            dedup_key=f"review:{source_hash}:restored:r{restored.revision}",
            kind="review.restored", tabs=("today", "review"), company=record.company,
            entity_id=f"review:{source_hash}",
        )
        return {"status": "ok"}

    @staticmethod
    def _task_choice_payload(task: Any) -> dict[str, object]:
        target = critical_time(task)
        return {
            "task_id": task.id,
            "revision": _task_revision(task),
            "stage": task.stage,
            "round": task.round or "",
            "status": task.status,
            "event_type": task.event_type,
            "time": target.isoformat() if target else None,
            "title": task.title,
            "action_summary": (task.action_summary or "")[:120],
            "is_actionable": bool(task.is_actionable),
            "open": task.status in {"new", "needs_review", "confirmed", "planned"},
        }

    def _application_tasks(self, application_key: str) -> list[dict[str, object]]:
        """Live tasks of one chain, open ones first, newest time first."""
        tasks = [
            task
            for task in MarkdownTaskStore(TASKS_DIR).all()
            if task.application_key == application_key
            and not task.deleted_at
            and not task.tombstoned
            and task.status != "irrelevant"
        ]

        def sort_key(task: Any) -> tuple[int, float]:
            target = critical_time(task) or task.received_at
            is_open = task.status in {"new", "needs_review", "confirmed", "planned"}
            return (0 if is_open else 1, -target.timestamp())

        return [self._task_choice_payload(task) for task in sorted(tasks, key=sort_key)]

    def list_application_tasks(self, application_key: str) -> list[dict[str, object]]:
        """Tasks of one application chain for the "update existing task" pickers."""
        application = ApplicationRegistry(APPLICATIONS_DIR).load(application_key)
        if not application or application.deleted_at or application.merged_into:
            raise ValueError("目标申请链不存在或已经删除。")
        return self._application_tasks(application.application_key)

    @staticmethod
    def _suggest_task_for_review(
        record: Any,
        tasks: list[dict[str, object]],
    ) -> str | None:
        """Pick the chain task a follow-up mail most plausibly updates."""
        from .stages import is_same_stage

        def matches(task: dict[str, object]) -> bool:
            if not is_same_stage(str(task.get("stage") or ""), record.stage):
                return False
            # A mail that names a round only updates that round's task; a mail
            # without one may update the stage's single task.
            if record.round and (record.round or "") != str(task.get("round") or ""):
                return False
            return True

        for prefer_open in (True, False):
            for task in tasks:
                if bool(task.get("open")) != prefer_open:
                    continue
                if matches(task):
                    return str(task["task_id"])
        return None

    @staticmethod
    def _review_target_payload(
        application: Any,
        *,
        source_company: str = "",
        source_role: str = "",
        dictionaries: Any = None,
    ) -> dict[str, object]:
        lifecycle_labels = {
            "active": "进行中",
            "ended": "已结束",
            "archived": "已归档",
        }
        role = application.role or "岗位待确认"
        project = application.recruiting_project or ""
        job_code = application.job_code or ""
        location = application.location or ""
        current_stage = application.manual_stage or "待确认"
        attempt = max(1, int(application.attempt_sequence or 1))
        lifecycle = lifecycle_labels.get(application.status, application.status)
        dictionaries = dictionaries or load_identity_dictionaries(
            DICTIONARIES_DIR
        )
        source_company_identity = (
            dictionaries.canonical_company(source_company)
            or canonical_company(source_company)
        )
        application_company_identity = (
            dictionaries.canonical_company(application.company)
            or canonical_company(application.company)
        )
        label = (
            f"{application.company}｜{role}"
            f"｜项目：{project or '—'}"
            f"｜职位编号：{job_code or '—'}"
            f"｜地点：{location or '—'}"
            f"｜第 {attempt} 次"
            f"｜{lifecycle}"
            f"｜当前：{current_stage}"
        )
        return {
            "application_key": application.application_key,
            "revision": application.revision,
            "company": application.company,
            "role": role,
            "project": project,
            "recruiting_year": application.recruiting_year,
            "business_unit": application.business_unit or "",
            "job_code": job_code,
            "location": location,
            "attempt_sequence": attempt,
            "status": application.status,
            "lifecycle": lifecycle,
            "current_stage": current_stage,
            "manual_stage_status": application.manual_stage_status,
            "same_company": bool(
                source_company
                and application_company_identity == source_company_identity
            ),
            "same_role": bool(
                source_role
                and canonical_role(application.role)
                == canonical_role(source_role)
            ),
            "label": label,
        }

    def get_review_target(self, application_key: str) -> dict[str, object]:
        application = ApplicationRegistry(APPLICATIONS_DIR).load(application_key)
        if (
            not application
            or application.deleted_at
            or application.merged_into
        ):
            raise ValueError("目标申请链不存在或已经删除。")
        payload = self._review_target_payload(application)
        payload["tasks"] = self._application_tasks(application.application_key)
        return payload

    def search_review_targets(
        self,
        source_hash: str,
        query: str = "",
    ) -> list[dict[str, object]]:
        record = UnresolvedStore(UNRESOLVED_DIR).load(source_hash)
        if not record or record.status != "pending":
            raise ValueError("待处理邮件不存在或已经处理。")
        dictionaries = load_identity_dictionaries(DICTIONARIES_DIR)
        tokens = []
        for raw_token in re.split(r"\s+", str(query).strip()):
            if not raw_token:
                continue
            canonical_token = (
                dictionaries.canonical_company(raw_token) or raw_token
            )
            tokens.append(canonical_token.casefold())
        choices: list[dict[str, object]] = []
        for application in ApplicationRegistry(APPLICATIONS_DIR).all(
            ignore_invalid=True,
            include_merged=False,
            include_deleted=False,
        ):
            choice = self._review_target_payload(
                application,
                source_company=record.company or "",
                source_role=record.role or "",
                dictionaries=dictionaries,
            )
            searchable = " ".join(
                str(choice.get(name) or "")
                for name in (
                    "company",
                    "role",
                    "project",
                    "recruiting_year",
                    "business_unit",
                    "job_code",
                    "location",
                    "attempt_sequence",
                    "lifecycle",
                    "current_stage",
                )
            ).casefold()
            canonical_choice_company = (
                dictionaries.canonical_company(application.company)
                or application.company
            ).casefold()
            searchable = f"{searchable} {canonical_choice_company}"
            if tokens and not all(token in searchable for token in tokens):
                continue
            choices.append(choice)
        status_order = {"active": 0, "ended": 1, "archived": 2}
        choices.sort(
            key=lambda item: (
                not (
                    item["same_company"] is True
                    and item["same_role"] is True
                ),
                item["same_company"] is not True,
                status_order.get(str(item["status"]), 9),
                -int(item["attempt_sequence"]),
                str(item["company"]),
                str(item["role"]),
                str(item["application_key"]),
            )
        )
        return choices[:100]

    def get_review_window_bootstrap(
        self,
        source_hash: str,
        preferred_key: str = "",
    ) -> dict[str, object]:
        record = UnresolvedStore(UNRESOLVED_DIR).load(source_hash)
        if not record or record.status != "pending":
            raise ValueError("待处理邮件不存在或已经处理。")
        targets = self.search_review_targets(source_hash, "")
        target: dict[str, object] | None = None
        target_key = preferred_key or record.recommended_application_key or ""
        if target_key:
            try:
                target = self.get_review_target(target_key)
            except ValueError:
                target = None
        if target is None:
            exact = [
                item
                for item in targets
                if item["same_company"] is True and item["same_role"] is True
            ]
            if len(exact) == 1:
                try:
                    target = self.get_review_target(str(exact[0]["application_key"]))
                except ValueError:
                    target = exact[0]
        if target and not any(
            item["application_key"] == target["application_key"]
            for item in targets
        ):
            targets.insert(0, target)
        recommendation = self.get_review_recommendation(
            source_hash,
            str(target["application_key"]) if target else "",
        )
        review = {**record.to_dict(), **explain_review(record)}
        for private_name in (
            "mail_locator",
            "private_link_ref",
            "sender_scope_hash",
            "subject_shape_hash",
        ):
            review.pop(private_name, None)
        return {
            "review": review,
            "target": target,
            "initial_targets": targets,
            "recommendation": recommendation,
        }

    def get_pending_original_mail(
        self,
        source_hash: str,
        expected_review_revision: int,
        load_remote_images: bool = False,
        *,
        runtime_control: RuntimeScope,
    ) -> dict[str, object]:
        record = UnresolvedStore(UNRESOLVED_DIR).load(source_hash)
        if not record or record.status != "pending":
            raise ValueError("待处理邮件不存在或已经处理。")
        if record.revision != int(expected_review_revision):
            raise ValueError("待处理内容已更新，请重新打开复核窗口。")
        if not record.mail_locator:
            raise ValueError("原邮件定位不可用；重新扫描近期邮件可回填。")
        try:
            credential = load_credential()
        except RuntimeError as exc:
            raise RuntimeError("邮箱账号未配置，无法读取原邮件。") from exc
        return ImapReader(
            self._settings,
            credential,
            runtime_control=runtime_control,
        ).fetch_original(
            record.mail_locator,
            load_remote_images=bool(load_remote_images),
        )

    def get_review_recommendation(
        self,
        source_hash: str,
        application_key: str = "",
    ) -> dict[str, object]:
        record = UnresolvedStore(UNRESOLVED_DIR).load(source_hash)
        if not record or record.status != "pending":
            raise ValueError("待处理邮件不存在或已经处理。")
        key = application_key or record.recommended_application_key or ""
        application = (
            ApplicationRegistry(APPLICATIONS_DIR).load(key) if key else None
        )
        from .stages import is_stage_advance

        previous_stage = application.manual_stage if application else None
        stage_advanced = bool(
            application and is_stage_advance(previous_stage, record.stage)
        )
        reasons = [
            reason
            for reason in record.recommendation_reasons
            if reason != "stage_advanced"
        ]
        if stage_advanced:
            reasons.append("stage_advanced")
        default_create_task = any(
            reason in {
                "explicit_start",
                "explicit_end",
                "explicit_deadline",
                "action_link",
            }
            for reason in reasons
        )
        # A follow-up mail about a stage the chain already tracks (reschedule,
        # reminder, venue change) should update that task instead of adding a
        # second one; suggest the matching task and let the user confirm.
        suggested_task_id = None
        if application and application.status == "active":
            suggested_task_id = self._suggest_task_for_review(
                record,
                self._application_tasks(application.application_key),
            )
        suggest_update_task = bool(
            suggested_task_id
            and not stage_advanced
            and (default_create_task or record.change_type == "update")
        )
        if suggest_update_task:
            reasons.append("existing_task")
        return {
            "source_hash": source_hash,
            "review_revision": record.revision,
            "application_revision": application.revision if application else None,
            "previous_stage": previous_stage,
            "stage_advanced": stage_advanced,
            "recommend_task": bool(reasons),
            "default_create_task": default_create_task and not suggest_update_task,
            "suggested_task_id": suggested_task_id,
            "suggest_update_task": suggest_update_task,
            "reasons": list(dict.fromkeys(reasons)),
            "duration_minutes": record.duration_minutes,
            "source_url": PrivateLinkStore(
                TASKS_DIR.parent / "private-links.json"
            ).get(record.private_link_ref),
        }

    def list_application_choices(
        self,
        company: str = "",
        role: str = "",
    ) -> list[dict[str, object]]:
        target_company = canonical_company(company)
        target_role = canonical_role(role)
        choices = [
            {
                "application_key": item.application_key,
                "company": item.company,
                "company_key": item.company_key,
                "company_suggestion": canonical_company(item.company) or item.company,
                "role": item.role or "岗位待确认",
                "location": item.location or "",
                "project": item.recruiting_project,
                "status": item.status,
                "same_company": bool(
                    target_company
                    and canonical_company(item.company) == target_company
                ),
                "same_role": bool(
                    target_role
                    and canonical_role(item.role) == target_role
                ),
            }
            for item in ApplicationRegistry(APPLICATIONS_DIR).active(
                ignore_invalid=True
            )
        ]
        choices.sort(
            key=lambda item: (
                not (item["same_company"] is True and item["same_role"] is True),
                item["same_company"] is not True,
                str(item["company"]),
                str(item["role"]),
            )
        )
        exact_matches = [
            item
            for item in choices
            if item["same_company"] is True
            and item["same_role"] is True
            and item["status"] == "active"
        ]
        for item in choices:
            item["exact_match"] = item in exact_matches
            item["recommended"] = len(exact_matches) == 1 and item in exact_matches
        return choices

    @staticmethod
    def _application_task_matches(task: object, application: object) -> bool:
        keys = {
            application.application_key,
            *application.aliases,
        }
        legacy_ids = {
            *application.legacy_application_ids,
            legacy_application_id(application.application_key),
        }
        return (
            getattr(task, "application_key", None) in keys
            or getattr(task, "application_id", None) in legacy_ids
        )

    @staticmethod
    def _assert_application_revision(
        application: Any,
        payload: dict[str, object],
    ) -> None:
        if "expected_revision" not in payload:
            return
        expected = payload.get("expected_revision")
        if isinstance(expected, bool) or expected is None or expected == "":
            raise ValueError("申请内容已更新，请重新打开后再保存。")
        try:
            revision = int(expected)
        except (TypeError, ValueError) as exc:
            raise ValueError("申请版本无效，请重新打开后再保存。") from exc
        if revision != int(application.revision):
            raise ValueError("申请内容已更新，请重新打开后再保存。")

    def create_application(self, payload: dict[str, object]) -> dict[str, object]:
        with self._mutation_guard():
            registry = ApplicationRegistry(APPLICATIONS_DIR)
            application = application_from_user_payload(payload)
            matching_attempts = [
                record.attempt_sequence
                for record in registry.all(
                    ignore_invalid=True,
                    include_merged=False,
                    include_deleted=True,
                )
                if record.identity_fingerprint == application.identity_fingerprint
            ]
            if matching_attempts and "attempt_sequence" not in payload:
                application.attempt_sequence = max(matching_attempts) + 1
            store = MarkdownTaskStore(TASKS_DIR)
            task_payload = {
                **payload,
                "company": application.company,
                "role": application.role,
                "recruiting_project": application.recruiting_project,
                "location": application.location,
                "stage": payload.get("manual_stage")
                or payload.get("stage")
                or "申请已创建",
                "action_summary": payload.get("next_action")
                or payload.get("action_summary")
                or "跟进该申请",
            }
            task = create_manual_task(task_payload, store)
            task.application_key = application.application_key
            task.application_id = legacy_application_id(application.application_key)
            task.company = application.company
            task.role = application.role
            task.recruiting_project = application.recruiting_project
            task.location = application.location
            store.save(task)
            application.current_task_id = task.id
            application.legacy_application_ids = [task.application_id]
            registry.save(application)
            reconcile_application(application.application_key, registry, store)
            self._export(store)
            refreshed = registry.load(application.application_key) or application
            version = int(
                (refreshed.updated_at or datetime.now().astimezone()).timestamp()
                * 1_000_000
            )
            self._record_activity(
                dedup_key=f"application:{refreshed.application_key}:created:{version}",
                kind="application.created",
                tabs=("progress", "list", "review"),
                company=refreshed.company_key,
                entity_id=f"application:{refreshed.application_key}",
            )
        return self.get_dashboard()

    def materialize_progress_application(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        legacy_id = str(
            payload.get("legacy_application_id")
            or payload.get("application_id")
            or ""
        ).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", legacy_id):
            raise ValueError("旧申请标识无效，无法正式化。")
        with self._mutation_guard():
            registry = ApplicationRegistry(APPLICATIONS_DIR)
            application = next(
                (
                    record
                    for record in registry.all(
                        ignore_invalid=True,
                        include_merged=False,
                        include_deleted=False,
                    )
                    if legacy_id in record.legacy_application_ids
                ),
                None,
            )
            if application:
                self._assert_application_revision(application, payload)
                update_application_from_user_payload(application, payload)
            else:
                expected_revision = payload.get("expected_revision")
                if expected_revision is not None and expected_revision != "":
                    raise ValueError("申请内容已更新，请刷新进展后再保存。")
                application = application_from_user_payload(payload)
                application.source = "progress-ledger-promoted"
                application.legacy_application_ids = [legacy_id]
                update_application_from_user_payload(application, payload)
            if legacy_id not in application.legacy_application_ids:
                application.legacy_application_ids.append(legacy_id)
                application.legacy_application_ids.sort()

            store = MarkdownTaskStore(TASKS_DIR)
            matching_tasks = [
                task
                for task in store.all()
                if self._application_task_matches(task, application)
            ]
            if matching_tasks:
                task = matching_tasks[0]
            else:
                task = create_manual_task(
                    {
                        **payload,
                        "company": application.company,
                        "role": application.role,
                        "recruiting_project": application.recruiting_project,
                        "location": application.location,
                        "stage": application.manual_stage
                        or payload.get("current_stage")
                        or "申请已正式化",
                        "action_summary": application.next_action
                        or payload.get("current_action")
                        or "跟进该申请",
                    },
                    store,
                )
            task.application_key = application.application_key
            task.application_id = legacy_id
            task.company = application.company
            task.role = application.role
            task.recruiting_project = application.recruiting_project
            task.location = application.location
            if application.status in {"ended", "archived"}:
                task.status = "done"
                task.completed_at = task.completed_at or datetime.now(SHANGHAI)
            store.save(task)
            application.current_task_id = task.id
            registry.save(application)
            reconcile_application(application.application_key, registry, store)
            self._export(store)
            refreshed = registry.load(application.application_key) or application
            self._record_application_activity(refreshed, "materialized")
        dashboard = self.get_dashboard()
        dashboard["materialized_application_key"] = application.application_key
        return dashboard

    def get_application_merge_preview(
        self,
        source: str,
        target: str,
    ) -> dict[str, object]:
        registry = ApplicationRegistry(APPLICATIONS_DIR)
        source_record = registry.raw_load(source)
        target_record = registry.raw_load(target)
        if not source_record or not target_record:
            raise KeyError(source if not source_record else target)
        if source == target:
            raise ValueError("不能合并同一申请。")
        if source_record.merged_into or source_record.deleted_at:
            raise ValueError("来源申请已合并或已删除。")
        if target_record.merged_into or target_record.deleted_at:
            raise ValueError("目标申请已合并或已删除。")
        source_company = canonical_company(source_record.company) or source_record.company
        target_company = canonical_company(target_record.company) or target_record.company
        store = MarkdownTaskStore(TASKS_DIR)
        source_tasks = [
            task
            for task in store.all()
            if self._application_task_matches(task, source_record)
        ]
        unresolved = [
            record
            for record in UnresolvedStore(UNRESOLVED_DIR).all()
            if source in record.candidate_application_keys
            or record.resolved_application_key == source
        ]
        preview_token = _merge_preview_token(source_record, target_record)
        return {
            "source": source_record.to_dict(),
            "target": target_record.to_dict(),
            "task_count": len(source_tasks),
            "unresolved_count": len(unresolved),
            "conflicts": [
                field_name
                for field_name in (
                    "company",
                    "role",
                    "recruiting_project",
                    "job_code",
                    "location",
                    "recruiting_year",
                    "business_unit",
                )
                if getattr(source_record, field_name)
                and getattr(target_record, field_name)
                and getattr(source_record, field_name)
                != getattr(target_record, field_name)
            ],
            "cross_company": source_company.casefold() != target_company.casefold(),
            "preview_token": preview_token,
            "source_revision": source_record.revision,
            "target_revision": target_record.revision,
        }

    def merge_applications(
        self,
        source: str,
        target: str,
        overrides: dict[str, object] | None = None,
        cross_company_confirmed: bool = False,
        preview_token: str = "",
    ) -> dict[str, object]:
        with self._mutation_guard(transactional=False):
            registry = ApplicationRegistry(APPLICATIONS_DIR)
            source_record = registry.raw_load(source)
            target_record = registry.raw_load(target)
            if not source_record or not target_record:
                raise KeyError(source if not source_record else target)
            if source == target:
                raise ValueError("不能合并同一申请。")
            if source_record.merged_into or source_record.deleted_at:
                raise ValueError("来源申请已合并或已删除。")
            if target_record.merged_into or target_record.deleted_at:
                raise ValueError("目标申请已合并或已删除。")
            expected_token = _merge_preview_token(source_record, target_record)
            if not preview_token or preview_token != expected_token:
                raise ValueError("合并预览已失效，请重新预览后确认。")
            source_backup = deepcopy(source_record)
            target_backup = deepcopy(target_record)
            source_company = canonical_company(source_record.company) or source_record.company
            target_company = canonical_company(target_record.company) or target_record.company
            cross_company = source_company.casefold() != target_company.casefold()
            if cross_company and not cross_company_confirmed:
                raise ValueError("跨公司合并必须明确确认。")
            conflicts = [
                field_name
                for field_name in (
                    "company",
                    "role",
                    "recruiting_project",
                    "job_code",
                    "location",
                    "recruiting_year",
                    "business_unit",
                )
                if getattr(source_record, field_name)
                and getattr(target_record, field_name)
                and getattr(source_record, field_name)
                != getattr(target_record, field_name)
            ]
            missing_resolutions = [
                field_name
                for field_name in conflicts
                if not overrides or field_name not in overrides
            ]
            if missing_resolutions:
                raise ValueError(
                    "请先确认冲突字段：" + "、".join(missing_resolutions)
                )
            if overrides:
                for field_name in conflicts:
                    selected = overrides.get(field_name)
                    allowed = {
                        "" if value is None else str(value)
                        for value in (
                            getattr(source_record, field_name),
                            getattr(target_record, field_name),
                        )
                    }
                    if str(selected if selected is not None else "") not in allowed:
                        raise ValueError(f"冲突字段 {field_name} 的选择无效。")
                update_application_from_user_payload(target_record, overrides)
            target_record.aliases = sorted(
                set(target_record.aliases)
                | set(source_record.aliases)
                | {source_record.application_key}
            )
            target_record.legacy_application_ids = sorted(
                set(target_record.legacy_application_ids)
                | set(source_record.legacy_application_ids)
                | {
                    legacy_application_id(target_record.application_key),
                    legacy_application_id(source_record.application_key),
                }
            )
            target_record.identity_evidence = sorted(
                set(target_record.identity_evidence)
                | set(source_record.identity_evidence)
                | {f"merged-from:{source_record.application_key}"}
            )
            target_record.deleted_source_hashes = sorted(
                set(target_record.deleted_source_hashes)
                | set(source_record.deleted_source_hashes)
            )
            target_record.attempt_sequence = max(
                target_record.attempt_sequence,
                source_record.attempt_sequence,
            )
            histories: list[dict[str, object]] = []
            history_keys: set[str] = set()
            for item in (
                *target_record.manual_progress_history,
                *source_record.manual_progress_history,
            ):
                key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                if key in history_keys:
                    continue
                history_keys.add(key)
                histories.append(item)
            target_record.manual_progress_history = histories
            nodes_by_id: dict[str, dict[str, object]] = {}
            for item in (
                *target_record.progress_nodes,
                *source_record.progress_nodes,
            ):
                node_id = str(item.get("id") or "")
                if not node_id:
                    continue
                incoming = dict(item)
                existing = nodes_by_id.get(node_id)
                if existing is None:
                    nodes_by_id[node_id] = incoming
                    continue
                source_hashes = {
                    str(value)
                    for node in (existing, incoming)
                    for value in (
                        node.get("source_hash"),
                        *(node.get("source_hashes") or ()),
                    )
                    if value
                }
                existing["source_hashes"] = sorted(source_hashes)
            target_record.progress_nodes = sorted(
                nodes_by_id.values(),
                key=lambda item: (
                    str(item.get("event_at") or ""),
                    str(item.get("id") or ""),
                ),
            )
            target_record.deleted_source_hashes = sorted(
                set(target_record.deleted_source_hashes)
                | {
                    str(value)
                    for item in target_record.progress_nodes
                    for value in (
                        item.get("source_hash"),
                        *(item.get("source_hashes") or ()),
                    )
                    if value
                }
            )
            store = MarkdownTaskStore(TASKS_DIR)
            unresolved_store = UnresolvedStore(UNRESOLVED_DIR)
            source_keys = {
                source_record.application_key,
                *source_record.aliases,
            }
            affected_tasks = [
                task
                for task in store.all()
                if self._application_task_matches(task, source_record)
            ]
            affected_unresolved = [
                record
                for record in unresolved_store.all()
                if any(key in source_keys for key in record.candidate_application_keys)
                or record.resolved_application_key in source_keys
            ]
            task_backups = [deepcopy(task) for task in affected_tasks]
            unresolved_backups = [deepcopy(record) for record in affected_unresolved]
            transaction = FileTransaction(
                TASKS_DIR.parent / ".transactions",
                (APPLICATIONS_DIR, TASKS_DIR, UNRESOLVED_DIR),
            )
            transaction.__enter__()
            try:
                for task in affected_tasks:
                    task.application_key = target_record.application_key
                    task.application_id = legacy_application_id(
                        target_record.application_key
                    )
                    task.company = target_record.company
                    task.role = target_record.role
                    task.recruiting_project = target_record.recruiting_project
                    task.location = target_record.location
                    store.save(task)
                for record in affected_unresolved:
                    candidates = tuple(
                        dict.fromkeys(
                            target_record.application_key if key in source_keys else key
                            for key in record.candidate_application_keys
                        )
                    )
                    resolved = (
                        target_record.application_key
                        if record.resolved_application_key in source_keys
                        else record.resolved_application_key
                    )
                    unresolved_store.save(
                        replace(
                            record,
                            candidate_application_keys=candidates,
                            resolved_application_key=resolved,
                        )
                    )
                source_record.merged_into = target_record.application_key
                source_record.identity_evidence = sorted(
                    set(source_record.identity_evidence)
                    | {f"merged-into:{target_record.application_key}"}
                )
                target_record.revision += 1
                source_record.revision += 1
                registry.save(target_record)
                registry.save(source_record)
                reconcile_application(target_record.application_key, registry, store)
            except Exception as exc:
                for task in task_backups:
                    store.save(task)
                for record in unresolved_backups:
                    unresolved_store.save(record)
                registry.save(target_backup)
                registry.save(source_backup)
                transaction.__exit__(type(exc), exc, exc.__traceback__)
                raise
            transaction.commit()
            transaction.__exit__(None, None, None)
            try:
                self._export(store)
            except Exception as exc:
                LOGGER.warning("申请合并已提交，派生导出稍后重试：%s", exc)
            refreshed_target = registry.load(target_record.application_key) or target_record
            self._record_application_activity(refreshed_target, "merged")
        return self.get_dashboard()

    def get_application_delete_preview(
        self,
        application_key: str,
    ) -> dict[str, object]:
        registry = ApplicationRegistry(APPLICATIONS_DIR)
        application = registry.raw_load(application_key)
        if not application:
            raise KeyError(application_key)
        tasks = [
            task
            for task in MarkdownTaskStore(TASKS_DIR).all()
            if self._application_task_matches(task, application)
        ]
        unresolved = [
            record
            for record in UnresolvedStore(UNRESOLVED_DIR).all()
            if application_key in record.candidate_application_keys
            or record.resolved_application_key == application_key
        ]
        return {
            "application": application.to_dict(),
            "task_count": len(tasks),
            "reminder_task_count": len(tasks),
            "unresolved_count": len(unresolved),
            "permanent": False,
        }

    def trash_application(self, application_key: str) -> dict[str, object]:
        with self._mutation_guard():
            registry = ApplicationRegistry(APPLICATIONS_DIR)
            application = registry.raw_load(application_key)
            if not application:
                raise KeyError(application_key)
            if application.merged_into:
                raise ValueError("已合并申请不能移入回收站。")
            if application.deleted_at:
                return self.get_dashboard()
            store = MarkdownTaskStore(TASKS_DIR)
            affected = [
                task
                for task in store.all()
                if self._application_task_matches(task, application)
            ]
            application.trashed_task_statuses = {
                task.id: task.status for task in affected
            }
            state = StateStore(STATE_DB)
            for task in affected:
                task.status = "cancelled"
                store.save(task)
                state.clear_task_reminders(task.id)
            application.deleted_at = datetime.now().astimezone()
            application.deletion_reason = "user-trash"
            application.revision += 1
            registry.save(application)
            if affected and self._settings.calendar_sync_enabled:
                sync_macos_calendar(
                    store.all(),
                    calendar_name=self._settings.calendar_name,
                )
            self._export(store)
            self._record_application_activity(application, "trashed")
        return self.get_dashboard()

    def restore_application(self, application_key: str) -> dict[str, object]:
        with self._mutation_guard():
            registry = ApplicationRegistry(APPLICATIONS_DIR)
            application = registry.raw_load(application_key)
            if not application:
                raise KeyError(application_key)
            if application.merged_into:
                raise ValueError("已合并申请不能恢复。")
            store = MarkdownTaskStore(TASKS_DIR)
            for task_id, status in application.trashed_task_statuses.items():
                task = store.load(task_id)
                if not task:
                    continue
                task.status = status  # type: ignore[assignment]
                store.save(task)
            application.deleted_at = None
            application.deletion_reason = None
            application.trashed_task_statuses = {}
            application.revision += 1
            registry.save(application)
            reconcile_application(application.application_key, registry, store)
            if self._settings.calendar_sync_enabled:
                sync_macos_calendar(
                    store.all(),
                    calendar_name=self._settings.calendar_name,
                )
            self._export(store)
            refreshed = registry.load(application.application_key) or application
            self._record_application_activity(refreshed, "restored")
        return self.get_dashboard()

    def permanently_delete_application(
        self,
        application_key: str,
    ) -> dict[str, object]:
        with self._mutation_guard():
            registry = ApplicationRegistry(APPLICATIONS_DIR)
            application = registry.raw_load(application_key)
            if not application:
                raise KeyError(application_key)
            if application.merged_into:
                raise ValueError("已合并申请由目标申请保留别名。")
            if not application.deleted_at:
                raise ValueError("请先将申请移入回收站，再执行永久删除。")
            store = MarkdownTaskStore(TASKS_DIR)
            affected = [
                task
                for task in store.all()
                if self._application_task_matches(task, application)
            ]
            application.deleted_source_hashes = sorted(
                set(application.deleted_source_hashes)
                | {
                    task.source_message_hash
                    for task in affected
                    if task.source_message_hash and task.source_message_hash != "manual"
                }
                | {
                    str(value)
                    for node in application.progress_nodes
                    for value in (
                        node.get("source_hash"),
                        *(node.get("source_hashes") or ()),
                    )
                    if value
                }
            )
            for task in affected:
                task.status = "cancelled"
                store.save(task)
            if affected and self._settings.calendar_sync_enabled:
                try:
                    calendar_result = sync_macos_calendar(
                        store.all(),
                        calendar_name=self._settings.calendar_name,
                    )
                    if calendar_result.status not in {"ok", "unsupported"}:
                        LOGGER.warning(
                            "清理日历事件失败，将保留任务墓碑重试：%s",
                            calendar_result.detail,
                        )
                except OSError as exc:
                    LOGGER.warning("清理日历事件失败，将继续保留删除墓碑：%s", exc)
            state = StateStore(STATE_DB)
            for task in affected:
                state.clear_task_reminders(task.id)
                if not task.deleted_at:
                    store.trash(task.id)
                store.permanently_delete(task.id)
            unresolved_store = UnresolvedStore(UNRESOLVED_DIR)
            for record in unresolved_store.all():
                candidates = tuple(
                    key
                    for key in record.candidate_application_keys
                    if key != application_key
                )
                if candidates != record.candidate_application_keys:
                    unresolved_store.save(
                        replace(record, candidate_application_keys=candidates)
                    )
            application.deleted_at = application.deleted_at or datetime.now().astimezone()
            application.deletion_reason = "permanently-deleted"
            application.status = "archived"
            application.workflow_status = "archived"
            application.current_task_id = None
            application.next_action = ""
            application.manual_notes = ""
            application.manual_progress_history = []
            application.progress_nodes = []
            application.trashed_task_statuses = {}
            application.identity_evidence = ["permanent-delete-tombstone"]
            application.revision += 1
            registry.save(application)
            self._export(store)
            self._record_application_activity(application, "deleted")
        return self.get_dashboard()

    def _resolve_unresolved_record(
        self,
        source_hash: str,
        application_key: str,
        overrides: dict[str, object] | None = None,
        *,
        operation_id: str,
        update_task_id: str | None = None,
    ) -> dict[str, object]:
        overrides = overrides or {}
        unresolved_store = UnresolvedStore(UNRESOLVED_DIR)
        record = unresolved_store.load(source_hash)
        if not record or record.status != "pending":
            raise ValueError("待归属记录不存在或已经处理。")
        registry = ApplicationRegistry(APPLICATIONS_DIR)
        application = registry.load(application_key)
        if not application:
            raise ValueError("申请身份不存在。")
        legacy_id = legacy_application_id(application.application_key)
        if update_task_id:
            # Check the task's optimistic lock before anything in this
            # confirmation is written (the linked-task re-save below would
            # otherwise change the revision the review window pinned).
            expected_task_revision = str(overrides.get("expected_task_revision") or "")
            pinned_task = MarkdownTaskStore(TASKS_DIR).load(update_task_id)
            if not pinned_task or pinned_task.deleted_at or pinned_task.tombstoned:
                raise ValueError("要更新的待办不存在或已删除。")
            if expected_task_revision and expected_task_revision != _task_revision(pinned_task):
                raise ValueError("该待办刚被修改过，请重新打开复核窗口后再确认。")

        def parsed_time(name: str, fallback: datetime | None) -> datetime | None:
            if name not in overrides:
                return fallback
            value = overrides.get(name)
            if value is None or not str(value).strip():
                return None
            return datetime.fromisoformat(str(value))

        source_url = PrivateLinkStore(
            TASKS_DIR.parent / "private-links.json"
        ).get(record.private_link_ref)
        event = ParsedEvent(
            company=str(overrides.get("company") or application.company),
            role=str(overrides.get("role") or application.role or "") or None,
            location=str(
                overrides.get("location")
                if "location" in overrides
                else application.location or record.location or ""
            )
            or None,
            recruiting_project=str(
                overrides.get("recruiting_project")
                or application.recruiting_project
                or ""
            )
            or None,
            event_type=str(overrides.get("event_type") or record.event_type),
            stage=str(overrides.get("stage") or record.stage),
            round=str(overrides.get("round") or record.round or "") or None,
            title=str(overrides.get("title") or record.title),
            start_at=parsed_time("start_at", record.start_at),
            end_at=parsed_time("end_at", record.end_at),
            deadline_at=parsed_time("deadline_at", record.deadline_at),
            source_message_id=f"unresolved:{record.id}",
            source_received_at=record.received_at,
            source_sender="",
            source_url=source_url,
            action_summary=str(
                overrides.get("action_summary") or record.action_summary
            ),
            requirements=record.requirements,
            matched_keywords=(),
            confidence=1.0,
            change_type=record.change_type,  # type: ignore[arg-type]
            duration_minutes=record.duration_minutes,
            role_raw=(
                str(overrides.get("role_raw") or record.role_raw or "")
                or None
            ),
            role_canonical=(
                str(
                    overrides.get("role_canonical")
                    or record.role_canonical
                    or overrides.get("role")
                    or record.role
                    or application.role
                    or ""
                )
                or None
            ),
            job_code=(
                str(
                    overrides.get("job_code")
                    or record.job_code
                    or application.job_code
                    or ""
                )
                or None
            ),
            location_confidence=float(
                overrides.get("location_confidence")
                if overrides.get("location_confidence") not in {None, ""}
                else record.location_confidence
            ),
            location_source=(
                str(
                    overrides.get("location_source")
                    or record.location_source
                    or ""
                )
                or None
            ),
        )
        application_overrides = dict(overrides)
        incoming_stage = str(
            overrides.get("manual_stage") or overrides.get("stage") or record.stage
        )
        from .stages import is_terminal_stage, stage_depth

        if (
            application.manual_stage
            and not is_terminal_stage(incoming_stage)
            and stage_depth(incoming_stage) < stage_depth(application.manual_stage)
        ):
            application_overrides.pop("manual_stage", None)
            application_overrides.pop("stage", None)
        manual_history = list(application.manual_progress_history)
        update_application_from_user_payload(application, application_overrides)
        # Mail confirmation has its own deterministic progress node; do not
        # duplicate it as a generic manual edit event.
        application.manual_progress_history = manual_history
        if (
            record.role_raw
            and record.role_raw != application.role
            and record.role_raw not in application.role_aliases
        ):
            application.role_aliases.append(record.role_raw)
        evidence = set(application.identity_evidence)
        evidence.add("mail-review-confirmed")
        if record.job_code and record.job_code == application.job_code:
            evidence.add("mail-job-code")
        if record.location and record.location == application.location:
            evidence.add("mail-location")
            if record.location_source:
                evidence.add("location-source:" + record.location_source[:80])
            if record.location_confidence:
                evidence.add(
                    "location-confidence:"
                    f"{record.location_confidence:.3f}"
                )
        application.identity_evidence = sorted(evidence)
        if bool(overrides.get("reactivate_confirmed", False)):
            application.manual_progress_history.append(
                {
                    "event_at": datetime.now(SHANGHAI).isoformat(),
                    "stage": incoming_stage,
                    "status": application.manual_stage_status,
                    "next_stage": application.next_stage,
                    "lifecycle_status": "active",
                }
            )
        if legacy_id not in application.legacy_application_ids:
            application.legacy_application_ids.append(legacy_id)
        store = MarkdownTaskStore(TASKS_DIR)
        for linked_task in store.all():
            if self._application_task_matches(linked_task, application):
                linked_task.application_key = application.application_key
                linked_task.application_id = legacy_id
                linked_task.company = application.company
                linked_task.role = application.role
                linked_task.recruiting_project = application.recruiting_project
                linked_task.location = application.location
                store.save(linked_task)
        create_task = bool(overrides.get("create_task", False)) and not update_task_id
        task = None
        if update_task_id:
            task = self._update_task_from_review(
                store,
                update_task_id,
                application,
                legacy_id,
                event,
                record,
                overrides,
            )
        if create_task:
            task = task_from_event(
                event,
                store,
                application_key=application.application_key,
                resolved_application_id=legacy_id,
                mail_locator=record.mail_locator,
                source_hash_override=record.id,
                is_actionable=True,
            )
            task.company = application.company
            task.role = application.role
            task.recruiting_project = application.recruiting_project
            task.location = application.location
            if "task_notes" in overrides:
                task.manual_notes = str(overrides.get("task_notes") or "").strip()[:2000]
            store.save(task)
            if application.manual_stage_status == "completed":
                task = store.update_status(task.id, "done")
            StateStore(STATE_DB).clear_task_reminders(task.id)
        node_id = append_progress_node(
            application,
            record,
            operation_id=operation_id,
            stage=incoming_stage,
            status=application.manual_stage_status,
            next_stage=application.next_stage,
            task_id=task.id if task else None,
            round=event.round,
        )
        registry.save(application)
        unresolved_store.resolve(
            record.id,
            application_key=application.application_key,
            task_id=task.id if task else None,
            operation_id=operation_id,
            progress_node_id=node_id,
        )
        reconcile_application(application.application_key, registry, store)
        return {
            "application_key": application.application_key,
            "task_id": task.id if task else None,
            "progress_node_id": node_id,
        }

    @staticmethod
    def _update_task_from_review(
        store: MarkdownTaskStore,
        task_id_value: str,
        application: Any,
        legacy_id: str,
        event: ParsedEvent,
        record: Any,
        overrides: dict[str, object],
    ) -> Any:
        """Write a follow-up mail into an existing task instead of adding one.

        The task keeps its stable id and source hash; the mail's stage, round,
        times, action text, link and locator move onto it with
        ``change_type="update"``. A schedule reopens a done or cancelled task
        as planned (the user chose this task to carry the new date), reminders
        for the old time are cleared by the caller's runtime refresh.
        """
        task = store.load(task_id_value)
        if not task or task.deleted_at or task.tombstoned:
            raise ValueError("要更新的待办不存在或已删除。")
        if (
            task.application_key != application.application_key
            and task.application_id != legacy_id
        ):
            raise ValueError("该待办不属于所选申请链。")
        changes: dict[str, object] = {
            "stage": event.stage,
            "round": event.round or "",
            "action_summary": event.action_summary,
        }
        mail_has_schedule = bool(event.start_at or event.end_at or event.deadline_at)
        if mail_has_schedule:
            # A rescheduling mail replaces the whole window; a mail without any
            # time (venue change, reminder) must not erase the existing one.
            changes.update(
                {
                    "start_at": event.start_at.isoformat() if event.start_at else "",
                    "end_at": event.end_at.isoformat() if event.end_at else "",
                    "deadline_at": event.deadline_at.isoformat() if event.deadline_at else "",
                }
            )
        task = edit_task_fields(task.id, changes, store)
        task.title = re.sub(r"\s+", " ", event.title).strip() or task.title
        task.event_type = event.event_type or task.event_type
        task.source_url = event.source_url or task.source_url
        task.duration_minutes = event.duration_minutes or task.duration_minutes
        if record.mail_locator:
            task.mail_locator = dict(record.mail_locator)
        task.received_at = max(task.received_at, record.received_at)
        task.is_actionable = True
        task.application_key = application.application_key
        task.application_id = legacy_id
        # Both confirmation forms always carry a (usually empty) notes box; an
        # empty value must not wipe notes the user wrote on the card.
        incoming_notes = str(overrides.get("task_notes") or "").strip()
        if incoming_notes:
            task.manual_notes = incoming_notes[:2000]
        has_schedule = bool(task.start_at or task.end_at or task.deadline_at)
        if mail_has_schedule and has_schedule and task.status in {
            "new",
            "needs_review",
            "confirmed",
            "done",
            "cancelled",
            "expired",
        }:
            task.status = "planned"
            task.completed_at = None
            task.completed_at_inferred = False
            task.snoozed_until = None
        elif not has_schedule and task.status in {"cancelled", "expired"}:
            task.status = "needs_review"
        task.change_type = "update"
        store.save(task)
        StateStore(STATE_DB).clear_task_reminders(task.id)
        return task

    def resolve_unresolved_workflow(
        self,
        source_hash: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        requested_mode = str(payload.get("mode") or "new")
        if requested_mode not in {
            "new",
            "existing",
            "new_identity",
            "new_attempt",
            "update_active",
            "update_task",
            "reactivate",
        }:
            raise ValueError("邮件处理方式无效。")
        update_task_id = (
            str(payload.get("task_id") or "").strip()
            if requested_mode == "update_task"
            else ""
        )
        if requested_mode == "update_task" and not update_task_id:
            raise ValueError("请选择要更新的待办。")
        explicit_request_id = bool(payload.get("request_id"))
        operation_id = str(payload.get("request_id") or new_request_id())
        with self._mutation_guard(transactional=False):
            unresolved = UnresolvedStore(UNRESOLVED_DIR).load(source_hash)
            if not unresolved:
                raise ValueError("待归属记录不存在。")
            if unresolved.status == "resolved":
                if (
                    not explicit_request_id
                    or unresolved.confirmation_operation_id in {None, operation_id}
                ):
                    return self.get_dashboard()
                raise ValueError("该邮件已由其他确认操作处理。")
            if unresolved.status != "pending":
                raise ValueError("待归属记录已经处理。")
            registry = ApplicationRegistry(APPLICATIONS_DIR)
            legacy_new = requested_mode == "new"
            mode = (
                "new_identity"
                if requested_mode == "new"
                else requested_mode
            )
            target_application = None
            if mode in {
                "existing",
                "new_attempt",
                "update_active",
                "update_task",
                "reactivate",
            }:
                application_key = str(payload.get("application_key") or "").strip()
                target_application = (
                    registry.load(application_key) if application_key else None
                )
                if not target_application:
                    raise ValueError("请选择有效的已有申请链。")
            if mode == "update_task":
                if target_application.status != "active":
                    raise ValueError(
                        "只能更新进行中申请链上的待办；已结束的申请请先重新激活。"
                    )
                existing_task = MarkdownTaskStore(TASKS_DIR).load(update_task_id)
                if (
                    not existing_task
                    or existing_task.deleted_at
                    or existing_task.tombstoned
                    or (
                        existing_task.application_key
                        != target_application.application_key
                        and existing_task.application_id
                        != legacy_application_id(target_application.application_key)
                    )
                ):
                    raise ValueError("要更新的待办不存在，或不属于所选申请链。")
                payload = {**payload, "create_task": False}
                mode = "update_active"
            if mode == "existing":
                if target_application.status in {"ended", "archived"}:
                    if not bool(payload.get("reactivate_confirmed", False)):
                        raise ValueError(
                            "已结束或归档申请需要明确重新激活，"
                            "或创建新的申请批次。"
                        )
                    mode = "reactivate"
                else:
                    mode = "update_active"
            if mode == "update_active" and target_application.status != "active":
                raise ValueError("只能更新进行中的申请链。")
            if mode == "reactivate" and target_application.status not in {
                "ended",
                "archived",
            }:
                raise ValueError("该申请仍在进行中，请使用“更新进行中申请”。")

            create_new = mode in {"new_identity", "new_attempt"}
            if target_application and not create_new:
                existing_identity_defaults = (
                    (
                        "role",
                        unresolved.role_canonical or unresolved.role,
                        target_application.role,
                    ),
                    (
                        "recruiting_project",
                        unresolved.recruiting_project,
                        target_application.recruiting_project,
                    ),
                    (
                        "recruiting_year",
                        unresolved.recruiting_year,
                        target_application.recruiting_year,
                    ),
                    (
                        "business_unit",
                        unresolved.business_unit,
                        target_application.business_unit,
                    ),
                    (
                        "job_code",
                        unresolved.job_code,
                        target_application.job_code,
                    ),
                    (
                        "location",
                        unresolved.location,
                        target_application.location,
                    ),
                )
                payload = dict(payload)
                for name, incoming_value, target_value in existing_identity_defaults:
                    supplied_value = payload.get(name)
                    if (
                        target_value in {None, ""}
                        and incoming_value not in {None, ""}
                        and supplied_value in {None, ""}
                    ):
                        payload[name] = incoming_value
                payload.setdefault("role_raw", unresolved.role_raw or "")
                payload.setdefault(
                    "role_canonical",
                    unresolved.role_canonical or unresolved.role or "",
                )
                payload.setdefault(
                    "location_confidence",
                    unresolved.location_confidence,
                )
                payload.setdefault(
                    "location_source",
                    unresolved.location_source or "",
                )
            if create_new:
                creation_payload = dict(payload)

                def fill_identity_default(
                    name: str,
                    pending_value: object,
                    target_value: object = None,
                ) -> None:
                    current = creation_payload.get(name)
                    if current is not None and (
                        not isinstance(current, str) or current.strip()
                    ):
                        return
                    for value in (pending_value, target_value):
                        if value is not None and (
                            not isinstance(value, str) or value.strip()
                        ):
                            creation_payload[name] = value
                            return

                fill_identity_default(
                    "company",
                    unresolved.company,
                    target_application.company if target_application else None,
                )
                fill_identity_default(
                    "role",
                    unresolved.role_canonical or unresolved.role,
                    target_application.role if target_application else None,
                )
                fill_identity_default(
                    "role_raw",
                    unresolved.role_raw,
                )
                fill_identity_default(
                    "role_canonical",
                    unresolved.role_canonical or unresolved.role,
                )
                fill_identity_default(
                    "recruiting_project",
                    unresolved.recruiting_project,
                    (
                        target_application.recruiting_project
                        if target_application
                        else None
                    ),
                )
                fill_identity_default(
                    "recruiting_year",
                    unresolved.recruiting_year,
                    (
                        target_application.recruiting_year
                        if target_application
                        else None
                    ),
                )
                fill_identity_default(
                    "business_unit",
                    unresolved.business_unit,
                    target_application.business_unit if target_application else None,
                )
                fill_identity_default(
                    "job_code",
                    unresolved.job_code,
                    target_application.job_code if target_application else None,
                )
                fill_identity_default(
                    "location",
                    unresolved.location,
                    target_application.location if target_application else None,
                )
                fill_identity_default(
                    "location_confidence",
                    unresolved.location_confidence,
                )
                fill_identity_default(
                    "location_source",
                    unresolved.location_source,
                )
                application = application_from_user_payload(creation_payload)
                application.submitted_at = unresolved.received_at
                if (
                    unresolved.role_raw
                    and unresolved.role_raw != application.role
                    and unresolved.role_raw not in application.role_aliases
                ):
                    application.role_aliases.append(unresolved.role_raw)
                evidence = set(application.identity_evidence)
                evidence.add("mail-review-confirmed")
                if unresolved.job_code:
                    evidence.add("mail-job-code")
                if (
                    unresolved.location
                    and unresolved.location == application.location
                ):
                    evidence.add("mail-location")
                    if unresolved.location_source:
                        evidence.add(
                            "location-source:"
                            + unresolved.location_source[:80]
                        )
                    if unresolved.location_confidence:
                        evidence.add(
                            "location-confidence:"
                            f"{unresolved.location_confidence:.3f}"
                        )
                application.identity_evidence = sorted(evidence)
                payload = {
                    **payload,
                    "company": application.company,
                    "role": application.role or "",
                    "role_raw": unresolved.role_raw or "",
                    "role_canonical": (
                        unresolved.role_canonical
                        or application.role
                        or ""
                    ),
                    "recruiting_project": application.recruiting_project or "",
                    "recruiting_year": application.recruiting_year,
                    "business_unit": application.business_unit or "",
                    "job_code": application.job_code or "",
                    "location": application.location or "",
                    "location_confidence": unresolved.location_confidence,
                    "location_source": unresolved.location_source or "",
                }
                if mode == "new_attempt":
                    matching_attempts = [
                        record.attempt_sequence
                        for record in registry.all(
                            ignore_invalid=True,
                            include_merged=False,
                            include_deleted=True,
                        )
                        if (
                            record.identity_fingerprint
                            == application.identity_fingerprint
                            or record.application_key
                            == target_application.application_key
                        )
                    ]
                    application.attempt_sequence = (
                        max(
                            [
                                target_application.attempt_sequence,
                                *matching_attempts,
                            ]
                        )
                        + 1
                    )
                elif legacy_new:
                    matching_attempts = [
                        record.attempt_sequence
                        for record in registry.all(
                            ignore_invalid=True,
                            include_merged=False,
                            include_deleted=True,
                        )
                        if record.identity_fingerprint
                        == application.identity_fingerprint
                    ]
                    if matching_attempts:
                        application.attempt_sequence = max(matching_attempts) + 1
                application_key = application.application_key
            else:
                application = target_application
                application_key = application.application_key
                if mode == "reactivate":
                    payload = {
                        **payload,
                        "status": "active",
                        "reactivate_confirmed": True,
                    }
            expected_review_revision = int(
                payload.get("expected_review_revision") or unresolved.revision
            )
            expected_application_revision = (
                int(payload["expected_application_revision"])
                if payload.get("expected_application_revision") is not None
                else (
                    target_application.revision
                    if target_application
                    else None
                )
            )
            validate_confirmation_request(
                request_id=operation_id,
                review=unresolved,
                expected_review_revision=expected_review_revision,
                application=target_application,
                expected_application_revision=expected_application_revision,
            )
            transaction_directories = [APPLICATIONS_DIR, UNRESOLVED_DIR]
            if TASKS_DIR.parent == APPLICATIONS_DIR.parent:
                transaction_directories.append(TASKS_DIR)
            with FileTransaction(
                APPLICATIONS_DIR.parent / ".transactions",
                tuple(transaction_directories),
                operation_id=operation_id,
            ) as transaction:
                if create_new:
                    registry.save(application)
                result = self._resolve_unresolved_record(
                    source_hash,
                    application_key,
                    payload,
                    operation_id=operation_id,
                    update_task_id=update_task_id or None,
                )
                transaction.commit()
            resolved_application = registry.load(application_key)
            if resolved_application and bool(payload.get("remember_correction", False)):
                try:
                    IdentityLearningStore(
                        DICTIONARIES_DIR / "manual"
                    ).learn(
                        sender_scope_hash=unresolved.sender_scope_hash,
                        subject_shape_hash=unresolved.subject_shape_hash,
                        original_company=unresolved.company,
                        original_role=unresolved.role,
                        corrected_company=resolved_application.company,
                        corrected_role=resolved_application.role,
                    )
                except Exception as exc:
                    LOGGER.warning("申请已保存，但本机纠错学习失败：%s", exc)
            task_id = str(result.get("task_id") or "")
            derived_warning = ""
            try:
                DerivedOutbox(TASKS_DIR.parent / "derived-outbox.json").enqueue(
                    operation_id=operation_id,
                    entity_id=application_key,
                    kinds=(
                        "export",
                        *(("runtime",) if task_id else ()),
                        "activity",
                    ),
                )
                self._retry_derived_outbox()
            except Exception as exc:
                derived_warning = (
                    "申请事实已保存；导出、日历或未读状态将在下次启动重试。"
                )
                LOGGER.warning("%s 原因：%s", derived_warning, type(exc).__name__)
            dashboard = self.get_dashboard()
            if derived_warning:
                dashboard["derived_warning"] = derived_warning
            return dashboard

    def list_identity_learning_rules(self) -> list[dict[str, object]]:
        return [
            {
                "id": rule.id,
                "original_company": rule.original_company,
                "original_role": rule.original_role,
                "corrected_company": rule.corrected_company,
                "corrected_role": rule.corrected_role,
                "created_at": rule.created_at,
                "enabled": rule.enabled,
                "conflict": rule.conflict,
            }
            for rule in IdentityLearningStore(
                DICTIONARIES_DIR / "manual"
            ).all()
        ]

    def set_identity_learning_rule_enabled(
        self,
        rule_id: str,
        enabled: bool,
    ) -> list[dict[str, object]]:
        IdentityLearningStore(DICTIONARIES_DIR / "manual").set_enabled(
            rule_id,
            bool(enabled),
        )
        return self.list_identity_learning_rules()

    def rebuild_identity_learning(self, days: int = 30) -> dict[str, object]:
        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError("邮件扫描正在进行，请稍后重试。")
        try:
            summary = bootstrap_identity_learning(
                self._settings,
                days=max(1, min(90, int(days))),
                rebuild=True,
                runtime_control=self._runtime_control,
            )
        finally:
            self._scan_lock.release()
        return {
            "summary": summary,
            "rules": self.list_identity_learning_rules(),
        }

    def resolve_unresolved(
        self,
        source_hash: str,
        application_key: str,
    ) -> dict[str, object]:
        return self.resolve_unresolved_workflow(
            source_hash,
            {"mode": "existing", "application_key": application_key},
        )

    def resolve_unresolved_with_overrides(
        self,
        source_hash: str,
        application_key: str,
        overrides: dict[str, object],
    ) -> dict[str, object]:
        return self.resolve_unresolved_workflow(
            source_hash,
            {
                **overrides,
                "mode": "existing",
                "application_key": application_key,
            },
        )

    def create_and_resolve_unresolved(
        self,
        source_hash: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        return self.resolve_unresolved_workflow(
            source_hash,
            {**payload, "mode": "new"},
        )

    def get_application_detail(self, application_key: str) -> dict[str, object]:
        registry = ApplicationRegistry(APPLICATIONS_DIR)
        application = registry.raw_load(application_key)
        if not application:
            raise KeyError(application_key)
        chain = [
            task
            for task in MarkdownTaskStore(TASKS_DIR).all()
            if self._application_task_matches(task, application)
        ]
        visible = [task for task in chain if not task.deleted_at and not task.tombstoned]
        return {
            **application.to_dict(),
            "timeline": build_application_timeline(
                visible,
                application,
                suppress_task_ids={task.id for task in chain},
            ),
        }

    def edit_application(
        self,
        application_key: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        with self._mutation_guard():
            registry = ApplicationRegistry(APPLICATIONS_DIR)
            application = registry.raw_load(application_key)
            if not application:
                raise KeyError(application_key)
            if application.merged_into or application.deleted_at:
                raise ValueError("已合并或已删除的申请不能编辑。")
            self._assert_application_revision(application, payload)
            previous_stage_status = application.manual_stage_status
            update_application_from_user_payload(application, payload)
            registry.save(application)
            store = MarkdownTaskStore(TASKS_DIR)
            for task in store.all():
                if not self._application_task_matches(task, application):
                    continue
                task.application_key = application.application_key
                if task.application_id not in application.legacy_application_ids:
                    task.application_id = legacy_application_id(
                        application.application_key
                    )
                task.company = application.company
                task.role = application.role
                task.recruiting_project = application.recruiting_project
                task.location = application.location
                store.save(task)
            current_task = (
                store.load(application.current_task_id)
                if application.current_task_id
                else None
            )
            # Only an explicit change of the stage status may move the task;
            # a card save that merely re-posts the prefilled value must never
            # reopen a task the user completed elsewhere (human state wins).
            from .stages import is_same_stage as _same_stage

            stage_status_changed = (
                "manual_stage_status" in payload
                and application.manual_stage_status != previous_stage_status
            )
            if (
                current_task
                and stage_status_changed
                and _same_stage(current_task.stage, application.manual_stage)
            ):
                if (
                    application.manual_stage_status == "completed"
                    and current_task.status != "done"
                ):
                    store.update_status(current_task.id, "done")
                elif (
                    application.manual_stage_status == "pending"
                    and current_task.status == "done"
                ):
                    store.update_status(current_task.id, "planned")
            reconcile_application(application_key, registry, store)
            self._export(store)
            refreshed_application = registry.load(application_key) or application
            self._record_application_activity(refreshed_application, "changed")
        return self.get_dashboard()

    def open_source(self, task_id: str) -> bool:
        task = MarkdownTaskStore(TASKS_DIR).load(task_id)
        if not task or not task.source_url:
            return False
        webbrowser.open(task.source_url)
        return True

    def get_original_mail(
        self,
        task_id: str,
        load_remote_images: bool = False,
    ) -> dict[str, object]:
        task = MarkdownTaskStore(TASKS_DIR).load(task_id)
        if not task:
            raise KeyError("任务不存在。")
        if not task.mail_locator:
            raise ValueError("原邮件定位不可用；重新扫描近期邮件可回填。")
        try:
            credential = load_credential()
        except RuntimeError as exc:
            raise RuntimeError("邮箱账号未配置，无法读取原邮件。") from exc
        return ImapReader(
            self._settings,
            credential,
            runtime_control=self._runtime_control,
        ).fetch_original(
            task.mail_locator,
            load_remote_images=bool(load_remote_images),
        )

    def get_original_mail_by_source(
        self,
        source_hash: str,
        load_remote_images: bool = False,
    ) -> dict[str, object]:
        record = UnresolvedStore(UNRESOLVED_DIR).load(source_hash)
        if not record:
            raise KeyError("邮件来源不存在。")
        if not record.mail_locator:
            raise ValueError("原邮件定位不可用；重新扫描近期邮件可回填。")
        try:
            credential = load_credential()
        except RuntimeError as exc:
            raise RuntimeError("邮箱账号未配置，无法读取原邮件。") from exc
        return ImapReader(
            self._settings,
            credential,
            runtime_control=self._runtime_control,
        ).fetch_original(
            record.mail_locator,
            load_remote_images=bool(load_remote_images),
        )

    def open_obsidian(self, task_id: str) -> bool:
        if self._settings.obsidian_enabled and self._settings.obsidian_output.exists():
            _open_obsidian_uri(self._settings.obsidian_output)
            return True
        task_path = MarkdownTaskStore(TASKS_DIR).path_for(task_id)
        if task_path.exists():
            _open_path(task_path)
            return True
        return False

    def open_research(self, task_id: str) -> bool:
        payload = request_states(self._settings.research_queue).get(task_id, {})
        result_path = Path(str(payload.get("result_path") or ""))
        if result_path.is_file():
            _open_path(result_path)
            return True
        return False

    def get_health(self) -> dict[str, str | None]:
        return StateStore(STATE_DB).health()

    @fact_mutation
    def create_task(self, payload: dict[str, object]) -> dict[str, object]:
        application_key = str(payload.get("application_key") or "").strip()
        application = (
            ApplicationRegistry(APPLICATIONS_DIR).load(application_key)
            if application_key
            else None
        )
        if application_key and not application:
            raise ValueError("关联的申请链不存在。")
        if application and application.deleted_at:
            raise ValueError("已删除申请不能创建或关联待办。")
        if application:
            payload = {
                **payload,
                "application_key": application.application_key,
                "company": application.company,
                "role": application.role,
                "recruiting_project": application.recruiting_project,
                "location": application.location,
            }
        store = MarkdownTaskStore(TASKS_DIR)
        task = create_manual_task(payload, store)
        if application:
            reconcile_application(
                application.application_key,
                ApplicationRegistry(APPLICATIONS_DIR),
                store,
            )
        self._export(store)
        self._refresh_task_runtime(task, store)
        self._record_task_activity(task, "created")
        return self.get_dashboard()

    def edit_task(
        self,
        task_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        with self._mutation_guard():
            store = MarkdownTaskStore(TASKS_DIR)
            current_task = store.load(task_id)
            if current_task is None:
                raise KeyError(task_id)
            old_application_key = current_task.application_key
            if "expected_revision" in payload and str(
                payload.get("expected_revision") or ""
            ) != _task_revision(current_task):
                raise ValueError("待办内容已更新，请重新打开后再保存。")

            application_key = str(payload.get("application_key") or "").strip()
            application = (
                ApplicationRegistry(APPLICATIONS_DIR).load(application_key)
                if application_key
                else None
            )
            if application_key and not application:
                raise ValueError("关联的申请链不存在。")
            if application and application.deleted_at:
                raise ValueError("已删除申请不能编辑或关联待办。")
            if application:
                payload = {
                    **payload,
                    "company": application.company,
                    "role": application.role,
                    "recruiting_project": application.recruiting_project,
                    "location": application.location,
                }
            apply_task_update(
                self._settings,
                task_id,
                payload,
                store=store,
                local_dashboard=DASHBOARD_FILE,
                record_activity=False,
            )
            task = store.load(task_id)
            StateStore(STATE_DB).clear_task_reminders(task_id)
            registry = ApplicationRegistry(APPLICATIONS_DIR)
            for affected_key in {
                old_application_key,
                task.application_key if task else None,
            } - {None}:
                reconcile_application(affected_key, registry, store)
            if task and (task.application_key or old_application_key):
                self._export(store)
            if task:
                self._refresh_task_runtime(task, store, clear_reminders=False)
                self._record_task_activity(task, "changed")
        return self.get_dashboard()

    def trash_task(
        self,
        task_id: str,
        expected_revision: str = "",
    ) -> dict[str, object]:
        with self._mutation_guard():
            store = MarkdownTaskStore(TASKS_DIR)
            current = store.load(task_id)
            if current is None:
                raise KeyError(task_id)
            if expected_revision and expected_revision != _task_revision(current):
                raise ValueError("待办内容已更新，请刷新后重试。")
            task = store.trash(task_id)
            if task.application_key:
                reconcile_application(
                    task.application_key,
                    ApplicationRegistry(APPLICATIONS_DIR),
                    store,
                )
            self._export(store)
            self._refresh_task_runtime(task, store)
            self._record_task_activity(task, "trashed")
        return self.get_dashboard()

    def restore_task(
        self,
        task_id: str,
        expected_revision: str = "",
    ) -> dict[str, object]:
        with self._mutation_guard():
            store = MarkdownTaskStore(TASKS_DIR)
            current = store.load(task_id)
            if current is None:
                raise KeyError(task_id)
            if expected_revision and expected_revision != _task_revision(current):
                raise ValueError("待办内容已更新，请刷新后重试。")
            task = store.restore(task_id)
            self._sync_application_for_task(task, store)
            self._export(store)
            self._refresh_task_runtime(task, store)
            self._record_task_activity(task, "restored")
        return self.get_dashboard()

    def permanently_delete_task(
        self,
        task_id: str,
        expected_revision: str = "",
    ) -> dict[str, object]:
        with self._mutation_guard():
            store = MarkdownTaskStore(TASKS_DIR)
            current = store.load(task_id)
            if current is None:
                raise KeyError(task_id)
            if expected_revision and expected_revision != _task_revision(current):
                raise ValueError("待办内容已更新，请刷新后重试。")
            task = store.permanently_delete(task_id)
            if task.application_key:
                reconcile_application(
                    task.application_key,
                    ApplicationRegistry(APPLICATIONS_DIR),
                    store,
                )
            self._export(store)
            self._refresh_task_runtime(task, store)
            self._record_task_activity(task, "deleted")
        return self.get_dashboard()

    def set_capsule(self, compact: bool) -> bool:
        if not self._window:
            return False
        with self._geometry_lock:
            self._capsule_generation += 1
            generation = self._capsule_generation
        threading.Thread(
            target=self._apply_capsule_geometry,
            args=(compact, generation),
            daemon=True,
        ).start()
        return True

    def _apply_capsule_geometry(self, compact: bool, generation: int) -> None:
        with self._geometry_lock:
            if not self._window or generation != self._capsule_generation:
                return
            if compact:
                if self._capsule_positions is None:
                    self._expanded_geometry = (
                        self._window.x,
                        self._window.y,
                        self._window.width,
                        self._window.height,
                    )
                capsule_width, capsule_height = CAPSULE_WIDTH, CAPSULE_HEIGHT
                self._window.resize(capsule_width, capsule_height)
                self._capsule_positions = _capsule_anchor(
                    self._window.x,
                    self._window.y,
                )
                hidden_x, _, target_y = self._capsule_positions
                self._window.move(hidden_x, target_y)
            else:
                if self._capsule_snap_timer:
                    self._capsule_snap_timer.cancel()
                    self._capsule_snap_timer = None
                self._capsule_positions = None
                if self._expanded_geometry:
                    x, y, width, height = self._expanded_geometry
                    self._window.resize(width, height)
                    self._window.move(x, y)
                else:
                    self._window.resize(
                        self._settings.ui_width,
                        self._settings.ui_height,
                    )

    def on_window_moved(self, x: int, y: int) -> None:
        if self._capsule_positions:
            self._capsule_positions = _capsule_anchor(x, y)
            hidden_x, _, target_y = self._capsule_positions
            if abs(x - hidden_x) <= 1 and abs(y - target_y) <= 1:
                return
            if self._capsule_snap_timer:
                self._capsule_snap_timer.cancel()
            self._capsule_snap_timer = threading.Timer(
                0.25,
                self._snap_capsule_after_drag,
            )
            self._capsule_snap_timer.daemon = True
            self._capsule_snap_timer.start()

    def _snap_capsule_after_drag(self) -> None:
        if not self._window or not self._capsule_positions:
            return
        hidden_x, _, target_y = self._capsule_positions
        self._window.move(hidden_x, target_y)

    def _export(self, store: MarkdownTaskStore) -> None:
        sync_outputs(self._settings, store)

    def _refresh_task_runtime(
        self,
        task: Any,
        store: MarkdownTaskStore,
        *,
        clear_reminders: bool = True,
    ) -> None:
        state = StateStore(STATE_DB)
        if clear_reminders:
            state.clear_task_reminders(task.id)
        if not task.deleted_at and not task.tombstoned:
            send_due_reminders([task], self._settings, state)
        self._schedule_calendar_sync(store)

    def _schedule_calendar_sync(self, store: MarkdownTaskStore) -> None:
        """Push the ledger to Calendar without blocking the caller.

        Every event costs an osascript round trip and the very first one waits
        for the macOS permission prompt, so a full sync can take far longer
        than a person is willing to watch a save button spin. Saving a task
        only has to write the fact; the calendar catches up behind it.
        """
        if not self._settings.calendar_sync_enabled:
            return
        with self._calendar_lock:
            self._calendar_pending = True
            if self._calendar_thread and self._calendar_thread.is_alive():
                return
            self._calendar_thread = threading.Thread(
                target=self._drain_calendar_sync,
                args=(store,),
                name="jobmaildesk-calendar-sync",
                daemon=True,
            )
            self._calendar_thread.start()

    def _drain_calendar_sync(self, store: MarkdownTaskStore) -> None:
        while True:
            with self._calendar_lock:
                if not self._calendar_pending:
                    self._calendar_thread = None
                    return
                # Coalesce the edits that arrived while the last run was busy.
                self._calendar_pending = False
            if self._runtime_control.stopping:
                return
            try:
                sync_macos_calendar(
                    store.all(),
                    calendar_name=self._settings.calendar_name,
                )
            except Exception:
                LOGGER.warning("任务已保存，但日历同步失败", exc_info=True)


def _tray_icon_path() -> Path | None:
    custom_icon = Path(sys.executable).resolve().parent / "JobMailDesk.ico"
    return custom_icon if custom_icon.exists() else None


def _tray_image() -> Image.Image:
    custom_icon = _tray_icon_path()
    if custom_icon is not None:
        try:
            return Image.open(custom_icon).convert("RGBA").resize((64, 64))
        except OSError:
            pass
    image = Image.new("RGBA", (64, 64), "#f6f0e6")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 7, 56, 57), radius=10, fill="#25231f")
    draw.rectangle((18, 19, 46, 23), fill="#e85d3f")
    draw.rectangle((18, 30, 42, 34), fill="#f6f0e6")
    draw.rectangle((18, 41, 37, 45), fill="#f6f0e6")
    return image



def _run_ui_primary(settings: Settings) -> None:
    recovered = recover_file_transactions(LOCAL_ROOT / ".transactions")
    if recovered:
        LOGGER.warning("已恢复 %s 个未完成的本地确认事务", recovered)
    if migrate_smartsens_exam_task(
        tasks_dir=TASKS_DIR,
        applications_dir=APPLICATIONS_DIR,
        state_db=STATE_DB,
    ):
        LOGGER.info("已修复思特威笔试待办并保留手工任务稳定 ID")
        migration_activity = ActivityStore(LOCAL_ROOT / "activity-state.json")
        migration_activity.record_event(
            dedup_key="migration:smartsens-exam-20260814-v1",
            kind="task.migrated",
            tabs=("today", "progress", "week", "month", "list"),
            company="思特威电子科技",
            entity_id="task:4b4a651509224cef912b9830",
        )
        set_dock_badge(migration_activity.unique_unread_count())
    scheduler_holder: dict[str, Any] = {"value": None}
    scheduler_lock = threading.Lock()
    runtime_control = RuntimeControl()

    def stop_scheduler() -> None:
        with scheduler_lock:
            current = scheduler_holder["value"]
            scheduler_holder["value"] = None
        if not current:
            return
        try:
            current.shutdown(wait=False)
        except SchedulerNotRunningError:
            pass

    def apply_runtime_settings(updated: Settings) -> None:
        if runtime_control.stopping:
            return
        stop_scheduler()
        scheduler = create_background_scheduler(updated, runtime_control)
        scheduler.start()
        with scheduler_lock:
            scheduler_holder["value"] = scheduler

    api = DesktopApi(
        settings,
        on_settings_saved=apply_runtime_settings,
        runtime_control=runtime_control,
    )
    review_windows = ReviewWindowController(
        api,
        runtime_control,
        _resource("review.html").as_uri(),
        webview.create_window,
        on_saved=api._review_window_saved,
    )
    api.attach_review_window_controller(review_windows)

    def drain_runtime() -> None:
        """Release scheduler, window, and socket resources off the UI thread."""
        try:
            stop_scheduler()
        except Exception:
            LOGGER.exception("退出时停止定时任务失败")
        try:
            api.prepare_shutdown()
        except Exception:
            LOGGER.exception("退出时关闭复核窗口失败")
        runtime_control.stop()

    shutdown = ShutdownCoordinator(runtime_control, drain_runtime)

    # Reminder and calendar jobs are local features and must not depend on
    # mailbox credentials. ScheduledJobs.scan() independently skips IMAP work
    # when no authorization code is configured.
    apply_runtime_settings(settings)
    window = webview.create_window(
        "JobMailDesk",
        _resource("index.html").as_uri(),
        js_api=api,
        width=settings.ui_width,
        height=settings.ui_height,
        # Use the native title bar so macOS provides close, minimize, and
        # zoom controls. The previous frameless, always-on-top window could
        # not be managed with the standard desktop window controls.
        frameless=False,
        on_top=False,
        resizable=True,
        background_color="#f6f0e6",
        easy_drag=False,
        min_size=(36, 82),
        hidden=settings.start_hidden,
    )
    api._window = window
    window.events.moved += api.on_window_moved
    quit_state = {"requested": False}

    def prepare_window_chrome() -> None:
        _hide_from_task_switcher(window, enabled=not settings.taskbar_button)
        api._close_to_tray_installed = install_close_to_tray(window, quit_state)

    def bring_to_front() -> None:
        """Show the window and leave background mode, restoring a minimized one."""
        window.show()
        if settings.taskbar_button:
            try:
                window.restore()
            except Exception:  # noqa: BLE001 - restore is best effort on every backend
                LOGGER.debug("恢复最小化窗口失败", exc_info=True)
        _set_background_mode(window, False)

    window.events.before_show += prepare_window_chrome
    # ``closing`` fires synchronously on the AppKit main thread and a review
    # window may still cancel the quit, so teardown hangs off ``closed``, where
    # the window is already gone and no UI thread can be blocked.
    window.events.closed += shutdown.request
    paused = {"value": False}

    def show() -> None:
        bring_to_front()

    def hide() -> None:
        window.hide()
        _set_background_mode(window, True)

    def open_review_list() -> None:
        """New-mail notification click: bring the window up on the 待处理 tab."""
        bring_to_front()
        try:
            window.evaluate_js(
                "typeof activateView === 'function' && activateView('review', { userInitiated: false })"
            )
        except Exception:  # noqa: BLE001
            LOGGER.debug("切换到待处理页失败", exc_info=True)

    def notification_clicked(title: str) -> None:
        # Only the new-mail alert asks for a review; a task reminder or an
        # update notice just brings the window back.
        if title == NEW_MAIL_TITLE:
            open_review_list()
        else:
            bring_to_front()

    def scan() -> None:
        threading.Thread(target=api.trigger_scan, daemon=True).start()

    def toggle_pause() -> None:
        scheduler = scheduler_holder["value"]
        if not scheduler:
            return
        paused["value"] = not paused["value"]
        for job_id in ("mail-poll", "hourly-refresh"):
            if paused["value"]:
                scheduler.pause_job(job_id)
            else:
                scheduler.resume_job(job_id)

    def open_obsidian() -> None:
        target = (
            api._settings.obsidian_output
            if api._settings.obsidian_enabled
            else DASHBOARD_FILE
        )
        if target.exists():
            if api._settings.obsidian_enabled:
                _open_obsidian_uri(target)
            else:
                _open_path(target)

    def open_settings() -> None:
        bring_to_front()
        window.evaluate_js(
            "setCapsule(false).then(() => "
            "window.openSettingsDialog && window.openSettingsDialog(false))"
        )

    def quit_app(icon: Icon, _item: MenuItem | None = None) -> None:
        # The tray menu is the one real exit; closing the window only hides it.
        quit_state["requested"] = True
        shutdown.request()
        icon.stop()
        window.destroy()

    def quit_for_privacy_reset() -> None:
        quit_state["requested"] = True
        shutdown.request()
        if tray:
            tray.stop()
        window.destroy()

    api._on_privacy_reset = quit_for_privacy_reset

    tray: Icon | None = None
    if sys.platform == "win32":
        tray_image = _tray_image()
        tray = ClickableTrayIcon(
            "JobMailDesk",
            tray_image,
            "JobMailDesk",
            Menu(
                MenuItem("显示", lambda _icon, _item: show(), default=True),
                MenuItem("隐藏", lambda _icon, _item: hide()),
                MenuItem("立即扫描", lambda _icon, _item: scan()),
                MenuItem(
                    lambda _item: "恢复扫描" if paused["value"] else "暂停扫描",
                    lambda _icon, _item: toggle_pause(),
                    checked=lambda _item: paused["value"],
                ),
                MenuItem("设置", lambda _icon, _item: open_settings()),
                MenuItem("打开 Obsidian", lambda _icon, _item: open_obsidian()),
                MenuItem("退出", quit_app),
            ),
        )
        # Windows has no Dock badge and no osascript: the tray icon carries the
        # unique-unread count and delivers reminder balloons. Both hooks are
        # thread-safe and never touch the WinForms UI thread. Registering the
        # AppUserModelID lets the PowerShell toast fallback show our name/icon.
        register_badge_renderer(TrayBadge(tray, tray_image).apply)
        register_notification_sink(lambda title, message: tray.notify(message, title))
        register_notification_click_handler(notification_clicked)
        ensure_toast_registration(_tray_icon_path())
        threading.Thread(target=tray.run, daemon=True).start()

    try:
        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
        webview.start(**_webview_start_options())
    finally:
        shutdown.request()
        if tray:
            tray.stop()
        # Park here instead of returning into interpreter shutdown, which would
        # join pywebview event threads and pool workers that can outlive us.
        shutdown.wait_for_exit(shutdown.exit_budget_seconds + 2)


def run_ui(settings: Settings) -> None:
    if not ensure_webview2_runtime():
        return
    instance_handle, is_primary = _claim_single_instance()
    if not is_primary:
        show_existing_window(wait_seconds=5)
        return
    try:
        _run_ui_primary(settings)
    finally:
        _close_instance_handle(instance_handle)
