from __future__ import annotations

import logging
import sys
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

# Windows has no Dock. The desktop UI registers a renderer that paints the
# count onto the tray icon (``tray_badge.TrayBadge.apply``); until it does,
# badge updates report ``False`` exactly like a missing PyObjC on macOS.
BadgeRenderer = Callable[[int], None]
_WINDOWS_RENDERER: BadgeRenderer | None = None


def register_badge_renderer(renderer: BadgeRenderer | None) -> None:
    global _WINDOWS_RENDERER
    _WINDOWS_RENDERER = renderer


def _validate_count(count: int) -> None:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("Dock badge count must be a non-negative integer")

AppKit: Any | None = None
AppHelper: Any | None = None
if sys.platform == "darwin":
    try:
        import AppKit as _AppKit
        from PyObjCTools import AppHelper as _AppHelper
    except ImportError:
        LOGGER.debug("PyObjC is unavailable; the Dock badge is disabled")
    else:
        AppKit = _AppKit
        AppHelper = _AppHelper


def _apply_badge_label(label: str | None) -> None:
    try:
        application = AppKit.NSApplication.sharedApplication()
        application.dockTile().setBadgeLabel_(label)
    except Exception:
        LOGGER.exception("Unable to update the macOS Dock badge")


def set_dock_badge(count: int) -> bool:
    """Schedule a Dock badge update on the AppKit main thread.

    Returns ``True`` when an update was scheduled and ``False`` when Dock
    integration is unavailable. A zero count removes the badge with ``None``.
    """

    if sys.platform == "win32":
        renderer = _WINDOWS_RENDERER
        if renderer is None:
            return False
        _validate_count(count)
        try:
            renderer(count)
        except Exception:
            LOGGER.exception("Unable to update the Windows tray badge")
            return False
        return True
    if sys.platform != "darwin" or AppKit is None or AppHelper is None:
        return False
    _validate_count(count)
    label = str(count) if count else None
    try:
        AppHelper.callAfter(_apply_badge_label, label)
    except Exception:
        LOGGER.exception("Unable to schedule the macOS Dock badge update")
        return False
    return True
