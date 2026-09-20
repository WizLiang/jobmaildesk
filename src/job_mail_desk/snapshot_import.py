"""Import a JobMailDesk data directory copied from another machine.

Used by the Windows installer to bring the macOS ``~/Library/Application
Support/JobMailDesk`` snapshot into ``%LOCALAPPDATA%\\JobMailDesk``. The fact
layer (tasks, applications, unresolved, digests, dictionaries, activity state,
outbox, private links, ``state.db``) is copied byte for byte; derived caches
and machine-bound files are left behind; ``config.toml`` paths that pointed
into the old data directory are rewritten to the new one, and paths that
cannot be mapped are reset to defaults with a warning rather than silently
creating ``C:\\Users\\<name>\\Library\\...``.
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from .config import Settings, load_settings, write_settings
from .fs_utils import retry_on_permission_error
from .selfcheck import count_facts

LOGGER = logging.getLogger(__name__)

# Never copied: rebuilt on first start or bound to the source machine.
SKIPPED_ENTRIES = frozenset(
    {
        ".transactions",  # manifests hold absolute source-machine paths
        ".data.lock",
        "instance.lock",
        "logs",
        "dashboard-cache.json",  # signature embeds absolute paths; rebuilt
        "updates",  # update-check state; rebuilt
        ".DS_Store",
        ".privacy-reset.json",  # local destructive request must never migrate
        ".privacy-reset-complete",
        ".storage-move.json",
        ".storage-move-complete",
        "webview2",  # browser profile is machine-local
    }
)
FACT_MARKERS = ("tasks", "applications", "unresolved", "config.toml")


class SnapshotImportError(RuntimeError):
    """The snapshot cannot be imported safely."""


@dataclass(frozen=True)
class ImportReport:
    source: str
    destination: str
    backup: str | None
    copied_entries: tuple[str, ...]
    skipped_entries: tuple[str, ...]
    config_rewrites: tuple[str, ...]
    warnings: tuple[str, ...]
    counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "destination": self.destination,
            "backup": self.backup,
            "copied_entries": list(self.copied_entries),
            "skipped_entries": list(self.skipped_entries),
            "config_rewrites": list(self.config_rewrites),
            "warnings": list(self.warnings),
            "counts": self.counts,
        }


def _has_facts(directory: Path) -> bool:
    """True when ``directory`` already holds anything the user could lose.

    A freshly created data directory (default ``config.toml`` and empty
    folders from ``ensure_config``) is not counted, so a first install after
    an accidental launch does not demand ``--replace``.
    """
    if not directory.exists():
        return False
    for name in ("tasks", "applications", "unresolved", "digests"):
        child = directory / name
        if child.is_dir() and any(child.iterdir()):
            return True
    manual = directory / "dictionaries" / "manual"
    if manual.is_dir() and any(manual.iterdir()):
        return True
    return any(
        (directory / name).exists()
        for name in ("private-links.json", "activity-state.json", "state.db")
    )


def desktop_instance_running() -> bool:
    """Best-effort probe for a running JobMailDesk desktop process.

    On Windows the desktop app owns the named mutex created in
    ``ui_app._claim_single_instance``; opening it succeeds only while that
    process lives. Elsewhere (and on any error) the probe answers ``False``.
    """
    if sys.platform != "win32":
        return False
    try:
        from .ui_app import INSTANCE_MUTEX_NAME

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_mutex = kernel32.OpenMutexW
        open_mutex.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
        open_mutex.restype = ctypes.c_void_p
        handle = open_mutex(0x00100000, False, INSTANCE_MUTEX_NAME)  # SYNCHRONIZE
        if not handle:
            return False
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    except Exception:  # noqa: BLE001 - a probe must never break the import
        LOGGER.debug("单实例互斥体探测失败", exc_info=True)
        return False


def _backup_existing(destination: Path, backup_path: Path) -> None:
    """Rename the whole data directory aside, atomically or not at all.

    ``shutil.move`` would fall back to copy-then-delete when Windows refuses
    the rename (any open handle below the directory), and a half-finished
    ``rmtree`` would leave the live directory torn. A plain rename either
    succeeds completely or leaves every fact untouched; transient antivirus
    locks get the same bounded retry the fact stores use.
    """
    if desktop_instance_running():
        raise SnapshotImportError(
            "JobMailDesk 桌面程序正在运行，请先退出再导入（避免数据目录被占用）。"
        )
    try:
        retry_on_permission_error(lambda: os.rename(destination, backup_path))
    except OSError as exc:
        raise SnapshotImportError(
            f"无法把现有数据目录整体备份为 {backup_path}"
            f"（目录或其中文件正被其他程序占用？）：{exc}"
        ) from exc


def _looks_absolute(value: str) -> bool:
    return value.startswith("/") or PureWindowsPath(value).is_absolute()


def _parent_and_name(value: str) -> tuple[str, str]:
    if value.startswith("/"):
        posix = PurePosixPath(value)
        return str(posix.parent), posix.name
    windows = PureWindowsPath(value)
    return str(windows.parent), windows.name


def _rewrite_paths(
    settings: Settings,
    raw: dict,
    destination: Path,
) -> tuple[Settings, list[str], list[str]]:
    """Map old-root paths onto ``destination``; unmappable ones reset to defaults."""
    rewrites: list[str] = []
    warnings: list[str] = []
    # The old data root is wherever the two managed exports lived.
    old_roots: set[str] = set()
    for section, key in (("obsidian", "output_path"), ("progress", "output_path")):
        value = str(raw.get(section, {}).get(key) or "")
        if value and _looks_absolute(value):
            old_roots.add(_parent_and_name(value)[0])

    def mapped(value: str, *, default: Path | None, label: str) -> Path | None:
        if not value:
            return default
        if not _looks_absolute(value):
            return Path(value)
        parent, name = _parent_and_name(value)
        if parent in old_roots:
            target = destination / name
            rewrites.append(f"{label}: {value} -> {target}")
            return target
        if Path(value).exists():
            return Path(value)
        warnings.append(f"{label} 指向无法映射的路径，已恢复默认：{value}")
        rewrites.append(
            f"{label}: {value} -> {default if default is not None else '(unset)'}"
        )
        return default

    obsidian_output = mapped(
        str(raw.get("obsidian", {}).get("output_path") or ""),
        default=destination / "求职硬截止待办集.md",
        label="obsidian.output_path",
    )
    progress_output = mapped(
        str(raw.get("progress", {}).get("output_path") or ""),
        default=destination / "求职当前进展.md",
        label="progress.output_path",
    )
    progress_source = mapped(
        str(raw.get("progress", {}).get("source_path") or ""),
        default=None,
        label="progress.source_path",
    )
    research_queue = mapped(
        str(raw.get("research", {}).get("queue_path") or ""),
        default=destination / "research-queue.jsonl",
        label="research.queue_path",
    )
    updated = replace(
        settings,
        obsidian_output=obsidian_output,
        progress_output=progress_output,
        progress_source=progress_source,
        research_queue=research_queue,
    )
    return updated, rewrites, warnings


def import_snapshot(
    source: Path,
    destination: Path,
    *,
    replace_existing: bool = False,
    now: datetime | None = None,
) -> ImportReport:
    source = Path(source)
    destination = Path(destination)
    if not source.is_dir():
        raise SnapshotImportError(f"快照目录不存在：{source}")
    if not any((source / marker).exists() for marker in FACT_MARKERS):
        raise SnapshotImportError(
            "目录不像 JobMailDesk 数据快照"
            f"（缺少 tasks/applications/unresolved/config.toml）：{source}"
        )
    if destination.exists() and source.resolve() == destination.resolve():
        raise SnapshotImportError("快照目录与目标目录相同。")

    backup: str | None = None
    if _has_facts(destination):
        if not replace_existing:
            raise SnapshotImportError(
                f"目标目录已有本地数据，未导入以免覆盖：{destination}"
                "（如需替换请加 --replace，旧数据会先备份）"
            )
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        backup_path = destination.with_name(f"{destination.name}.bak-{stamp}")
        if backup_path.exists():
            raise SnapshotImportError(f"备份目录已存在，未导入：{backup_path}")
        _backup_existing(destination, backup_path)
        backup = str(backup_path)
    destination.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    skipped: list[str] = []
    for entry in sorted(source.iterdir(), key=lambda item: item.name):
        if entry.name in SKIPPED_ENTRIES:
            skipped.append(entry.name)
            continue
        target = destination / entry.name
        if entry.is_dir():
            shutil.copytree(
                entry,
                target,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".DS_Store"),
            )
        else:
            shutil.copy2(entry, target)
        copied.append(entry.name)

    rewrites: list[str] = []
    warnings: list[str] = []
    config_path = destination / "config.toml"
    if config_path.exists():
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)
        settings = load_settings(config_path)
        settings, rewrites, warnings = _rewrite_paths(settings, raw, destination)
        write_settings(settings, config_path)
    else:
        warnings.append("快照中没有 config.toml，将在首次启动时生成默认配置。")
    if (destination / "private-links.json").exists():
        warnings.append(
            "private-links.json 已复制，但其加密密钥留在原机器的凭据库中：迁移时仍处于待处理状态的记录，"
            "其私人行动链接在本机无法解密，普通重新扫描也不会重新获取（已去重的邮件不再处理）；"
            "需要时在复核窗口按需只读查看原邮件。已确认任务的链接保存在任务文件中，不受影响；"
            "本机新扫描的邮件使用新密钥，正常工作。"
        )
    pending_transactions = sorted(
        path.name
        for path in (source / ".transactions").glob("op1_*")
        if (path / "prepared").exists() and not (path / "committed").exists()
    )
    if pending_transactions:
        warnings.append(
            "快照的 .transactions/ 里有未提交的事务（已跳过，其备份路径绑定原机器）："
            f"{', '.join(pending_transactions)}；请在原机器上启动一次程序完成恢复后重新导出，"
            "或核对相关申请/待办。"
        )

    return ImportReport(
        source=str(source),
        destination=str(destination),
        backup=backup,
        copied_entries=tuple(copied),
        skipped_entries=tuple(skipped),
        config_rewrites=tuple(rewrites),
        warnings=tuple(warnings),
        counts=count_facts(destination),
    )
