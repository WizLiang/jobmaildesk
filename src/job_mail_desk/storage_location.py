"""Storage selection and restart-time migration. Keep imports config-free.

This module runs before config constants are bound. Only the selected directory
is remembered in HKCU; records, settings, logs and browser data stay in that root.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid

REGISTRY_KEY = r"Software\JobMailDesk"
REGISTRY_VALUE = "DataDirectory"
MOVE_REQUEST = ".storage-move.json"
MOVE_COMPLETE = ".storage-move-complete"
MUTEX = r"Local\JobMailDesk.Desktop.Singleton.v1"
SKIP = {".data.lock", "instance.lock", MOVE_REQUEST, MOVE_COMPLETE,
        ".transactions", "dashboard-cache.json"}


class StorageLocationError(RuntimeError):
    pass


def saved_root() -> Path | None:
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, REGISTRY_VALUE)
    except FileNotFoundError:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise StorageLocationError("保存的数据目录无效，请重新选择。")
    return path


def save_root(root: Path) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
        winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_SZ, str(root))


def legacy_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "JobMailDesk"


def selected_root() -> Path:
    override = os.environ.get("JOBMAILDESK_LOCAL_ROOT")
    return Path(override).expanduser() if override else (saved_root() or legacy_root())


def destination_for(parent: str | Path) -> Path:
    parent = Path(parent).expanduser()
    if not parent.is_absolute() or not parent.is_dir():
        raise StorageLocationError("请选择一个已存在的本地文件夹。")
    if str(parent).startswith("\\\\"):
        raise StorageLocationError("请使用本地磁盘，暂不支持网络共享目录。")
    if parent.resolve() != parent:
        raise StorageLocationError("请选择实际文件夹，不要使用目录联接。")
    return parent / "JobMailDeskData"


def validate_move(source: Path, destination: Path, *, resuming: bool = False) -> None:
    for path in (source, destination):
        if (not path.is_absolute() or path.resolve() != path
                or path.parent == path or path == Path.home().resolve()):
            raise StorageLocationError("数据目录不能是用户主目录、磁盘根目录或目录联接。")
    if source == destination or source in destination.parents or destination in source.parents:
        raise StorageLocationError("新目录不能与原目录相同，也不能互相包含。")
    if not resuming and destination.exists() and any(destination.iterdir()):
        raise StorageLocationError("目标 JobMailDeskData 文件夹已有内容，请选择其他位置，避免覆盖。")


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def start_move(source: Path, destination: Path, *, restart: bool = True) -> None:
    validate_move(source, destination)
    if (source / ".privacy-reset.json").exists():
        raise StorageLocationError("请先完成个人信息清除，再更换数据目录。")
    marker = source / MOVE_REQUEST
    if marker.exists():
        raise StorageLocationError("已有迁移请求，请退出后重新打开程序完成迁移。")
    payload = {"schema": 1, "parent_pid": os.getpid() if restart else 0,
               "destination": str(destination), "token": uuid.uuid4().hex,
               "phase": "copy"}
    _write_json(marker, payload)
    if not restart:
        return
    command = ([sys.executable, "ui"] if getattr(sys, "frozen", False)
               else [sys.executable, "-m", "job_mail_desk", "ui"])
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        marker.unlink(missing_ok=True)
        raise StorageLocationError("无法启动迁移进程，原数据保持不变。") from None


def wait_for_exit(pid: int) -> None:
    if pid == 0:
        return
    if pid <= 0 or pid == os.getpid():
        raise StorageLocationError("迁移请求中的进程信息无效。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return
        raise StorageLocationError("无法确认原程序已退出，未开始迁移。")
    try:
        if kernel.WaitForSingleObject(handle, 60000) != 0:
            raise StorageLocationError("原程序仍在运行，请从托盘退出后重试。")
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def exclusive_desktop():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, MUTEX)
    busy = ctypes.get_last_error() == 183
    if not handle or busy:
        if handle:
            kernel.CloseHandle(handle)
        raise StorageLocationError("请先从托盘退出正在运行的 JobMailDesk，再启动新版本。")
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def _no_links(root: Path) -> None:
    for entry in root.rglob("*"):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise StorageLocationError("数据目录中存在符号链接或目录联接，请先移除链接后重试。")


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] in SKIP or path.is_dir():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _rewrite_config(path: Path, source: Path, destination: Path) -> None:
    if not path.exists():
        return
    import tomllib
    text = path.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    keys = {"obsidian": {"output_path"}, "progress": {"output_path", "source_path"},
            "research": {"queue_path"}}
    section = ""
    lines = []
    for line in text.splitlines(keepends=True):
        header = re.match(r"\s*\[([^]]+)\]", line)
        if header:
            section = header[1]
        assignment = re.match(r"\s*(\w+)\s*=", line)
        if assignment and assignment[1] in keys.get(section, set()):
            key = assignment[1]
            value = parsed.get(section, {}).get(key, "")
            if value:
                original = Path(value)
                if original.is_absolute() and original.is_relative_to(source):
                    mapped = destination / original.relative_to(source)
                    line = f"{key} = {json.dumps(str(mapped), ensure_ascii=False)}\n"
        lines.append(line)
    path.write_text("".join(lines), encoding="utf-8")


def finish_move(source: Path) -> Path:
    """Copy -> verify -> publish -> delete old copy -> remember new root.

    The durable verified phase survives failures while deleting the source or
    updating the registry. Normal startup stays blocked until this completes.
    """
    marker = source / MOVE_REQUEST
    payload = json.loads(marker.read_text(encoding="utf-8"))
    destination = Path(payload["destination"])
    if payload.get("schema") != 1 or not re.fullmatch(r"[a-f0-9]{32}", payload.get("token", "")):
        raise StorageLocationError("迁移请求损坏，未修改数据。")
    validate_move(source, destination, resuming=True)
    wait_for_exit(int(payload["parent_pid"]))
    stage = destination.parent / (".jobmaildesk-move-" + payload["token"])
    from .data_lock import data_directory_lease
    from .file_transaction import recover_file_transactions
    with exclusive_desktop(), data_directory_lease(source / ".data.lock", allow_storage_move=True):
        if payload["phase"] == "copy":
            recover_file_transactions(source / ".transactions")
            _no_links(source)
            if destination.exists() and any(destination.iterdir()):
                raise StorageLocationError("目标文件夹已有内容，未覆盖；请检查迁移请求。")
            if stage.exists():
                if stage.is_symlink() or stage.resolve() != stage:
                    raise StorageLocationError("迁移临时目录无效。")
                _no_links(stage)
                shutil.rmtree(stage)
            stage.mkdir()
            for child in source.iterdir():
                if child.name in SKIP:
                    continue
                target = stage / child.name
                if child.is_dir():
                    shutil.copytree(child, target)
                else:
                    shutil.copy2(child, target)
            if _digest(source) != _digest(stage):
                raise StorageLocationError("迁移副本校验失败，原数据未删除。")
            _rewrite_config(stage / "config.toml", source, destination)
            payload["digest"] = _digest(stage)
            payload["phase"] = "verified"
            # Write the phase before publishing; retry can finish the rename.
            _write_json(marker, payload)
        if payload["phase"] != "verified":
            raise StorageLocationError("迁移阶段无效，未修改数据。")
        if stage.exists():
            if destination.exists():
                if any(destination.iterdir()):
                    raise StorageLocationError("目标目录发生变化，迁移已停止。")
                destination.rmdir()
            stage.rename(destination)
        _no_links(destination)
        if _digest(destination) != payload["digest"]:
            raise StorageLocationError("新目录校验失败，迁移已停止。")
        _no_links(source)
        for child in source.iterdir():
            if child.name in {MOVE_REQUEST, ".data.lock", "instance.lock"}:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        save_root(destination)
        (destination / MOVE_COMPLETE).write_text("complete\n", encoding="ascii")
        marker.unlink()
    # Only empty control files remain; never recursively delete the old root.
    for name in (".data.lock", "instance.lock"):
        (source / name).unlink(missing_ok=True)
    try:
        source.rmdir()
    except OSError:
        pass
    return destination
