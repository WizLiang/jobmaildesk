from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .fs_utils import copy_tree, fsync_directory, fsync_file, remove_tree

LOGGER = logging.getLogger(__name__)


class FileTransaction:
    """Crash-recoverable snapshot transaction for local Markdown fact stores."""

    def __init__(
        self,
        root: Path,
        directories: tuple[Path, ...],
        *,
        operation_id: str | None = None,
    ) -> None:
        self.root = root
        self.directories = directories
        self.operation_id = operation_id or "op1_" + uuid.uuid4().hex
        self.path = root / self.operation_id
        self.backups = self.path / "backups"
        self._committed = False

    def __enter__(self) -> "FileTransaction":
        self.path.mkdir(parents=True, mode=0o700)
        os.chmod(self.path, 0o700)
        manifest: list[dict[str, str]] = []
        for index, directory in enumerate(self.directories):
            backup = self.backups / str(index)
            if directory.exists():
                copy_tree(directory, backup)
            else:
                backup.mkdir(parents=True)
            manifest.append(
                {
                    "directory": str(directory),
                    "backup": str(backup),
                }
            )
        (self.path / "manifest.json").write_text(
            json.dumps({"directories": manifest}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Backups must be durable before ``prepared`` exists, otherwise a
        # power loss could make recovery restore torn copies. Flush them once,
        # concurrently (hundreds of small files; serial flushes cost seconds on
        # Windows), then the marker.
        self._fsync_tree(self.path)
        (self.path / "prepared").write_text("prepared\n", encoding="ascii")
        fsync_file(self.path / "prepared")
        fsync_directory(self.path)
        return self

    @staticmethod
    def _fsync_tree(path: Path) -> None:
        """Flush every file under ``path`` (backups and markers) and its directories."""
        files = [item for item in path.rglob("*") if item.is_file()]
        directories = [path, *(item for item in path.rglob("*") if item.is_dir())]
        if len(files) > 8:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(fsync_file, files))
        else:
            for item in files:
                fsync_file(item)
        for directory in directories:
            fsync_directory(directory)

    def commit(self) -> None:
        for directory in self.directories:
            if not directory.exists():
                continue
            fsync_directory(directory)
        (self.path / "committed").write_text("committed\n", encoding="ascii")
        fsync_file(self.path / "committed")
        fsync_directory(self.path)
        self._committed = True

    def rollback(self) -> None:
        manifest = json.loads(
            (self.path / "manifest.json").read_text(encoding="utf-8")
        )
        for item in manifest["directories"]:
            directory = Path(item["directory"])
            backup = Path(item["backup"])
            # Both steps retry transient Windows locks; if restoring still
            # fails, the caller keeps the transaction directory so startup
            # recovery can finish the rollback instead of losing the backup.
            remove_tree(directory)
            copy_tree(backup, directory)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        rolled_back = True
        try:
            if exc_type is not None or not self._committed:
                try:
                    self.rollback()
                except OSError:
                    rolled_back = False
                    LOGGER.exception(
                        "本地事务回滚未完成，已保留备份 %s 供下次启动恢复", self.path
                    )
                    raise
        finally:
            if rolled_back:
                shutil.rmtree(self.path, ignore_errors=True)
        return False


def recover_file_transactions(root: Path) -> int:
    if not root.exists():
        return 0
    recovered = 0
    for path in root.glob("op1_*"):
        if not (path / "prepared").exists():
            shutil.rmtree(path, ignore_errors=True)
            continue
        if (path / "committed").exists():
            shutil.rmtree(path, ignore_errors=True)
            continue
        transaction = FileTransaction.__new__(FileTransaction)
        transaction.path = path
        transaction._committed = False
        try:
            transaction.rollback()
        except OSError:
            # Leave the prepared backup in place; the next start retries.
            LOGGER.exception("恢复本地事务失败，保留 %s 待下次重试", path)
            continue
        shutil.rmtree(path, ignore_errors=True)
        recovered += 1
    return recovered
