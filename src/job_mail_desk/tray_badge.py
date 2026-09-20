"""Unread badge rendered onto the Windows tray icon.

The macOS Dock badge has no taskbar equivalent for a tray-managed tool
window, so the unique-unread count is painted into the tray image and echoed
in the hover title. Rendering is pure Pillow; wiring to pystray happens in
:class:`TrayBadge`, whose ``apply`` method is registered as the Windows badge
renderer for :func:`job_mail_desk.macos_dock.set_dock_badge`.
"""
from __future__ import annotations

import logging
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from pystray import Icon

from .windows_notify import NIN_BALLOONUSERCLICK, notification_clicked

LOGGER = logging.getLogger(__name__)


class ClickableTrayIcon(Icon):
    """pystray icon whose balloon/toast clicks reach the application.

    pystray dispatches the tray callback message by ``lParam``; it handles
    left/right clicks but ignores ``NIN_BALLOONUSERCLICK``. Intercepting it
    here lets "new mail" notifications open the review list on click.
    """

    def _on_notify(self, wparam, lparam):  # type: ignore[override]
        if lparam == NIN_BALLOONUSERCLICK:
            notification_clicked()
            return None
        parent = getattr(super(), "_on_notify", None)
        return parent(wparam, lparam) if parent is not None else None

BASE_TITLE = "JobMailDesk"
BADGE_FILL = "#e85d3f"
BADGE_TEXT = "#ffffff"
MAX_TITLE_LENGTH = 127  # Shell_NotifyIcon tooltip limit (NOTIFYICONDATA.szTip)


def badge_label(count: int) -> str:
    return str(count) if count < 100 else "99+"


def badge_title(count: int) -> str:
    title = f"{BASE_TITLE} · {count} 条未读" if count > 0 else BASE_TITLE
    return title[:MAX_TITLE_LENGTH]


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):  # Pillow < 10.1 or no bundled TrueType font
        return ImageFont.load_default()


def render_badge(base: Image.Image, count: int) -> Image.Image:
    """Return a copy of ``base`` with a red badge in the bottom-right corner.

    ``count <= 0`` returns the plain icon, which clears the badge.
    """
    image = base.convert("RGBA").copy()
    if count <= 0:
        return image
    width, height = image.size
    diameter = max(12, int(min(width, height) * 0.5))
    label = badge_label(count)
    draw = ImageDraw.Draw(image)
    left, top = width - diameter, height - diameter
    box = (left, top, width - 1, height - 1)
    draw.ellipse(box, fill=BADGE_FILL, outline=BADGE_TEXT, width=max(1, diameter // 16))
    scale = {1: 0.68, 2: 0.56}.get(len(label), 0.42)
    font = _font(max(6, int(diameter * scale)))
    text_box = draw.textbbox((0, 0), label, font=font)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    x = left + (diameter - text_width) / 2 - text_box[0]
    y = top + (diameter - text_height) / 2 - text_box[1]
    draw.text((x, y), label, fill=BADGE_TEXT, font=font)
    return image


class TrayBadge:
    """Apply unread counts to a pystray icon (image + hover title)."""

    def __init__(self, icon: Any, base_image: Image.Image) -> None:
        self._icon = icon
        self._base = base_image.convert("RGBA")
        self.count = 0

    def apply(self, count: int) -> None:
        image = render_badge(self._base, count)
        # pystray stores the new image/title and only pushes them through
        # Shell_NotifyIcon once the icon is visible, so this is safe to call
        # from any thread before or after ``Icon.run``.
        self._icon.icon = image
        self._icon.title = badge_title(count)
        self.count = count
