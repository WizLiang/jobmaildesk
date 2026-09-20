"""Visible Windows notifications for task reminders.

Two delivery paths, both bounded and both reporting failure honestly so that
``send_due_reminders`` never marks a threshold as sent when nothing was shown:

1. The pystray tray icon (already created by the desktop UI on Windows) can
   raise a balloon that Windows 10/11 renders as a toast. The UI registers it
   as a *sink*; this module never imports pystray itself.
2. A WinRT toast issued through Windows PowerShell 5.1, used when no tray
   sink exists (CLI ``jobmaildesk run``) or when the sink fails. The toast is
   attributed to the ``JobMailDesk`` AppUserModelID when that ID is
   registered under HKCU (no administrator rights needed); otherwise it falls
   back to PowerShell's own ID so the reminder is still visible.
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import threading
import sys
from pathlib import Path
from typing import Callable

LOGGER = logging.getLogger(__name__)

APP_USER_MODEL_ID = "JobMailDesk"
POWERSHELL_APP_USER_MODEL_ID = (
    r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
)
TOAST_TIMEOUT_SECONDS = 10
_REGISTRY_PATH = rf"Software\Classes\AppUserModelId\{APP_USER_MODEL_ID}"

NotificationSink = Callable[[str, str], None]
_SINK: NotificationSink | None = None
_CLICK_HANDLER: Callable[[str], None] | None = None
_LAST_TITLE: str = ""
_STATE_LOCK = threading.Lock()
NIN_BALLOONUSERCLICK = 0x0405  # Shell_NotifyIcon callback: the user clicked the balloon/toast


def register_notification_click_handler(handler: Callable[[str], None] | None) -> None:
    """Install what happens when the user clicks a tray notification.

    The handler receives the title of the most recent notification, so the
    caller can open the review list for a new-mail alert but merely bring the
    window up for a task reminder.
    """
    global _CLICK_HANDLER
    _CLICK_HANDLER = handler


def _remember_title(title: str) -> None:
    global _LAST_TITLE
    with _STATE_LOCK:
        _LAST_TITLE = title


def last_notification_title() -> str:
    with _STATE_LOCK:
        return _LAST_TITLE


def notification_clicked() -> bool:
    """Invoke the click handler (from the tray thread); never raises."""
    handler = _CLICK_HANDLER
    if handler is None:
        return False
    try:
        handler(last_notification_title())
        return True
    except Exception:  # noqa: BLE001 - tray callbacks must not kill the icon thread
        LOGGER.exception("通知点击处理失败")
        return False

# Title and message travel through environment variables and are XML-escaped
# inside PowerShell, so no reminder text is ever spliced into the script.
_TOAST_SCRIPT = """
$ErrorActionPreference = 'Stop'
$appId = $env:JOBMAILDESK_TOAST_APP_ID
$title = [System.Security.SecurityElement]::Escape($env:JOBMAILDESK_TOAST_TITLE)
$message = [System.Security.SecurityElement]::Escape($env:JOBMAILDESK_TOAST_MESSAGE)
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$title</text><text>$message</text></binding></visual></toast>")
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


def register_notification_sink(sink: NotificationSink | None) -> None:
    """Install (or clear) the in-process sink, e.g. ``tray.notify``."""
    global _SINK
    _SINK = sink


def encoded_toast_command() -> str:
    """The toast script as PowerShell ``-EncodedCommand`` (UTF-16LE base64)."""
    return base64.b64encode(_TOAST_SCRIPT.strip().encode("utf-16-le")).decode("ascii")


def toast_registration_present() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_PATH):
            return True
    except (ImportError, OSError):
        return False


def ensure_toast_registration(icon_path: Path | None = None) -> bool:
    """Register the AppUserModelID under HKCU so toasts carry our name and icon.

    Idempotent and administrator-free; this is the documented route for
    unpackaged desktop apps. Returns ``False`` off Windows or on failure.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REGISTRY_PATH) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "JobMailDesk")
            if icon_path is not None and icon_path.exists():
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon_path))
        return True
    except (ImportError, OSError):
        LOGGER.warning("无法注册 Windows 通知应用标识", exc_info=True)
        return False


def toast_app_id() -> str:
    return APP_USER_MODEL_ID if toast_registration_present() else POWERSHELL_APP_USER_MODEL_ID


def send_toast(title: str, message: str, *, app_id: str | None = None) -> bool:
    """Show a WinRT toast via Windows PowerShell 5.1; ``False`` when not shown."""
    if sys.platform != "win32":
        return False
    environment = dict(os.environ)
    environment.update(
        {
            "JOBMAILDESK_TOAST_APP_ID": app_id or toast_app_id(),
            "JOBMAILDESK_TOAST_TITLE": title,
            "JOBMAILDESK_TOAST_MESSAGE": message,
        }
    )
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded_toast_command(),
            ],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # PowerShell 5.1 writes localized errors in the OEM code page
            timeout=TOAST_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        LOGGER.exception("Windows Toast 通知失败")
        return False
    if completed.returncode != 0:
        LOGGER.warning(
            "Windows Toast 通知失败（退出码 %s）：%s",
            completed.returncode,
            (completed.stderr or "").strip()[:200],
        )
        return False
    return True


def send_windows_notification(title: str, message: str) -> bool:
    """Deliver through the tray sink first, then the toast fallback."""
    _remember_title(title)
    sink = _SINK
    if sink is not None:
        try:
            sink(title, message)
            return True
        except Exception:
            LOGGER.exception("托盘通知失败，改用系统 Toast")
    try:
        return send_toast(title, message)
    except Exception:  # noqa: BLE001 - a reminder failure must never roll back a save
        LOGGER.exception("Windows 通知失败")
        return False
