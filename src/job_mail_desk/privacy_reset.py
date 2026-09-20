"""Erase this app's local data only after the old desktop process has exited.

The pending marker is a durable request, not a backup. A failed reset blocks
normal startup and is retried on the next UI launch, before any data recovery,
logging, scheduler, or WebView is started.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from . import credentials
from .data_lock import data_directory_lease
from .private_link_store import SERVICE as LINK_SERVICE, USERNAME as LINK_USERNAME


PENDING = ".privacy-reset.json"
COMPLETE = ".privacy-reset-complete"
CONFIRMATION = "清除"
CREDENTIAL_TARGETS = (
    (credentials.SERVICE, credentials.USERNAME),
    (credentials.LEGACY_SERVICE, credentials.LEGACY_USERNAME),
    (LINK_SERVICE, LINK_USERNAME),
)
# Explicit ownership prevents a custom data root from deleting unrelated files.
OWNED_NAMES = frozenset({
    "tasks", "applications", "unresolved", "digests", "dictionaries",
    "logs", "updates", "webview2", ".transactions", "images",
    "state.db", "state.db-wal", "state.db-shm", "state.db-journal",
    "config.toml", "activity-state.json", "dashboard-cache.json",
    "derived-outbox.json", "private-links.json", "research-queue.jsonl",
    "JobMailDesk.md", "求职硬截止待办集.md", "求职当前进展.md",
    "求职进展台账.md", COMPLETE, ".storage-move-complete",
})


class PrivacyResetError(RuntimeError):
    pass


def validate_root(root: Path) -> Path:
    absolute = Path(os.path.abspath(root))
    resolved = absolute.resolve()
    if absolute != resolved or resolved.parent == resolved:
        raise PrivacyResetError("数据目录不能是磁盘根目录、符号链接或目录联接。")
    if resolved == Path.home().resolve():
        raise PrivacyResetError("不能把用户主目录作为清除目标。")
    return resolved


def request_reset(root: Path, confirmation: str, settings) -> None:
    if confirmation != CONFIRMATION:
        raise PrivacyResetError("请输入“清除”以确认永久删除。")
    if sys.platform != "win32":
        raise PrivacyResetError("此清除流程目前仅支持 Windows。")
    root = validate_root(root)
    if (root / ".storage-move.json").exists():
        raise PrivacyResetError("请先完成数据迁移，再清除个人信息。")
    root.mkdir(parents=True, exist_ok=True)
    # Remember only generated/local files; never follow external export paths.
    local_files = []
    for path in (settings.obsidian_output, settings.progress_output,
                 settings.progress_source, settings.research_queue):
        if path:
            resolved = Path(path).resolve()
            if resolved.is_relative_to(root) and resolved != root:
                local_files.append(str(resolved.relative_to(root)))
    marker = root / PENDING
    try:
        with marker.open("x", encoding="utf-8") as stream:
            json.dump({"schema": 1, "parent_pid": os.getpid(),
                       "local_files": local_files}, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise PrivacyResetError("清除请求已存在，请退出后重新打开程序以继续。") from None
    command = ([sys.executable, "ui"] if getattr(sys, "frozen", False)
               else [sys.executable, "-m", "job_mail_desk", "ui"])
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
    except OSError:
        marker.unlink(missing_ok=True)
        raise PrivacyResetError("无法启动清除进程，尚未删除个人信息。") from None


def wait_for_parent(pid: int, timeout: float = 60) -> None:
    if pid <= 0 or pid == os.getpid():
        raise PrivacyResetError("清除请求的进程信息无效。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: already exited
            return
        raise PrivacyResetError("无法确认原程序已退出，未开始删除。")
    try:
        if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
            raise PrivacyResetError("原程序仍未退出，请退出所有 JobMailDesk 窗口后重试。")
    finally:
        kernel.CloseHandle(handle)


def erase_credentials() -> None:
    try:
        for service, username in CREDENTIAL_TARGETS:
            if credentials.keyring.get_password(service, username) is not None:
                credentials.keyring.delete_password(service, username)
            if credentials.keyring.get_password(service, username) is not None:
                raise PrivacyResetError("系统凭据删除后仍存在，清除未完成。")
    except credentials.KeyringError:
        raise PrivacyResetError("无法清除系统凭据，请检查 Windows 凭据管理器后重试。") from None


def _remove_owned(path: Path) -> None:
    """Never traverse symlinks or Windows junctions, including nested ones."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        if stat.S_ISDIR(info.st_mode):
            path.rmdir()
        else:
            path.unlink()
    elif path.is_dir():
        for child in path.iterdir():
            _remove_owned(child)
        path.rmdir()
    else:
        path.unlink()


def finish_pending_reset(root: Path, claim_instance, close_instance) -> bool:
    root = validate_root(root)
    marker = root / PENDING
    if not marker.exists():
        return False
    try:
        pending = json.loads(marker.read_text(encoding="utf-8"))
        if pending.get("schema") != 1:
            raise ValueError("schema")
        pid = int(pending["parent_pid"])
        local_files = pending.get("local_files", [])
        if not isinstance(local_files, list) or any(
            not isinstance(name, str) or Path(name).is_absolute()
            or ":" in name or ".." in Path(name).parts
            or name in {"", ".", PENDING, ".data.lock", "instance.lock"}
            for name in local_files
        ):
            raise ValueError("local_files")
    except (OSError, ValueError, TypeError, KeyError):
        raise PrivacyResetError("清除请求损坏，未删除数据。") from None
    wait_for_parent(pid)
    handle, primary = claim_instance()
    if not primary:
        raise PrivacyResetError("另一个 JobMailDesk 正在运行，未开始删除。")
    try:
        with data_directory_lease(root / ".data.lock", allow_privacy_reset=True):
            if not marker.exists():
                return False  # another reset finished while we waited
            erase_credentials()
            # Validate configured nested exports before touching any files.
            local_targets = []
            for name in local_files:
                target = root / name
                if target.parent.resolve().is_relative_to(root):
                    local_targets.append(target)
                else:
                    raise PrivacyResetError("本地输出路径已变为外部目录，清除未完成。")
            known_names = OWNED_NAMES | {Path(name).name for name in local_files}
            temporary_prefixes = tuple(f".{Path(name).stem}-" for name in known_names)
            targets = [p for p in root.iterdir()
                       if p.name in OWNED_NAMES or p.name in local_files
                       or (p.suffix.lower() == ".ics" and p.is_file())
                       or p.name == "config.toml.tmp"
                       or (p.suffix == ".tmp" and p.name.startswith(temporary_prefixes))]
            targets += local_targets
            for target in targets:
                for attempt in range(20):
                    try:
                        _remove_owned(target)
                        break
                    except FileNotFoundError:
                        break
                    except OSError:
                        if attempt == 19:
                            raise PrivacyResetError(
                                "部分本地文件仍被占用，清除未完成。关闭其他程序后重新打开本程序重试。"
                            ) from None
                        time.sleep(0.25)
            (root / COMPLETE).write_text("complete\n", encoding="ascii")
            marker.unlink()  # last: recovery/scheduling may resume only now
        return True
    finally:
        close_instance(handle)


def show_reset_error(message: str) -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(
            None, message + "\n清除请求已保留。重新打开程序将重试；不会启动邮箱扫描。",
            "JobMailDesk：清除未完成", 0x10,
        )
