"""Choose/process storage before importing modules with bound data paths."""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from . import storage_location as storage


def prepare_storage(*, gui: bool) -> bool:
    if sys.platform != "win32" or os.environ.get("JOBMAILDESK_LOCAL_ROOT"):
        return True
    saved = storage.saved_root()
    current = saved or storage.legacy_root()
    if (current / storage.MOVE_REQUEST).exists():
        if not gui:
            raise storage.StorageLocationError("数据迁移尚未完成，请打开桌面程序完成迁移。")
        storage.finish_move(current)
        return True
    if saved:
        if not saved.is_dir():
            raise storage.StorageLocationError("找不到已选择的数据目录，请连接对应磁盘或恢复该目录后重试。")
        return True
    if not gui or (current / ".privacy-reset.json").exists():
        return True
    # Never ask about migration while the old desktop is still writing.
    with storage.exclusive_desktop():
        from .storage_picker import choose_initial_root
        existing = current if current.exists() and any(current.iterdir()) else None
        selected = choose_initial_root(existing)
        if selected is None:
            return False
        if selected == current:
            storage.save_root(current)
            return True
        storage.validate_move(current, selected)
        if existing:
            storage.start_move(current, selected, restart=False)
        else:
            selected.mkdir(parents=True, exist_ok=True)
            storage.save_root(selected)
            return True
    storage.finish_move(current)
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    gui = (bool(args) and args[0] in {"ui", "show"}) or (
        not args and getattr(sys, "frozen", False)
        and not Path(sys.executable).stem.lower().endswith("-cli")
    )
    try:
        if not prepare_storage(gui=gui):
            return 0
    except Exception as exc:
        message = "数据目录设置或迁移未完成。请检查磁盘空间、目录权限，并退出其他 JobMailDesk 后重试。"
        if isinstance(exc, storage.StorageLocationError):
            message = str(exc)
        if gui and sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, message, "JobMailDesk · 数据目录", 0x10)
        else:
            print(message)
        return 1
    from .cli import main as run
    return run(argv)
