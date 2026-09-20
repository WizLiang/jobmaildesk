"""Regressions for Windows-only platform branches, exercised on any host.

The Linux/macOS test hosts cannot run ``msvcrt`` or open Windows Credential
Manager, so these tests simulate the Windows failure surface (directory
handles refused, ``os.fchmod`` missing, mandatory byte-range locks timing
out) and assert the fact stores keep working exactly as they do on POSIX.
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from job_mail_desk import credentials, data_lock, fs_utils
from job_mail_desk.activity_store import ActivityStore
from job_mail_desk.file_transaction import FileTransaction
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.models import JobTask
from job_mail_desk.parser import SHANGHAI


def _task() -> JobTask:
    return JobTask(
        id="a" * 24,
        application_id="b" * 20,
        company="样例科技",
        role="产品经理",
        recruiting_project=None,
        event_type="assessment",
        stage="在线笔试",
        round=None,
        received_at=datetime(2026, 8, 1, 10, 0, tzinfo=SHANGHAI),
        start_at=datetime(2026, 8, 2, 19, 0, tzinfo=SHANGHAI),
        end_at=None,
        deadline_at=None,
        priority="high",
        status="planned",
        change_type="new",
        source_message_hash="c" * 32,
        research_status="not_queued",
        confidence=0.9,
        title="在线笔试通知",
        action_summary="请按时参加在线笔试",
    )


@pytest.fixture
def windows_refuses_directory_handles(monkeypatch):
    """Windows raises PermissionError for ``os.open`` on a directory."""
    real_open = os.open
    refused: list[str] = []

    def guarded_open(path, flags, *args, **kwargs):
        # Only the fsync pattern (plain read-only open of a directory) is
        # refused; ``shutil.rmtree`` on POSIX opens directories with a
        # ``dir_fd`` and ``O_NONBLOCK``, which Windows never does.
        if flags == os.O_RDONLY and "dir_fd" not in kwargs and Path(path).is_dir():
            refused.append(str(path))
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(fs_utils.os, "open", guarded_open)
    return refused


def test_directory_fsync_is_a_noop_when_windows_refuses_directory_handles(
    tmp_path,
    windows_refuses_directory_handles,
) -> None:
    fs_utils.fsync_directory(tmp_path)

    assert windows_refuses_directory_handles == [str(tmp_path)]


def test_markdown_store_saves_when_directory_fsync_is_unavailable(
    tmp_path,
    windows_refuses_directory_handles,
) -> None:
    store = MarkdownTaskStore(tmp_path / "tasks")

    path = store.save(_task())

    assert path.exists()
    assert store.load("a" * 24) is not None
    assert windows_refuses_directory_handles  # the guard was actually exercised


def test_file_transaction_commits_when_directory_fsync_is_unavailable(
    tmp_path,
    windows_refuses_directory_handles,
) -> None:
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "record.md").write_text("before", encoding="utf-8")

    with FileTransaction(tmp_path / ".transactions", (facts,)) as transaction:
        (facts / "record.md").write_text("after", encoding="utf-8")
        transaction.commit()

    assert (facts / "record.md").read_text(encoding="utf-8") == "after"
    assert not list((tmp_path / ".transactions").glob("op1_*"))
    assert windows_refuses_directory_handles


def test_file_transaction_still_rolls_back_without_directory_handles(
    tmp_path,
    windows_refuses_directory_handles,
) -> None:
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "record.md").write_text("before", encoding="utf-8")

    with pytest.raises(RuntimeError):
        with FileTransaction(tmp_path / ".transactions", (facts,)):
            (facts / "record.md").write_text("after", encoding="utf-8")
            raise RuntimeError("injected")

    assert (facts / "record.md").read_text(encoding="utf-8") == "before"


def test_activity_store_writes_without_fchmod_or_directory_handles(
    tmp_path,
    monkeypatch,
    windows_refuses_directory_handles,
) -> None:
    # Python 3.12 on Windows has no ``os.fchmod`` at all (AttributeError, not
    # OSError), so the helper must look it up defensively.
    monkeypatch.delattr(fs_utils.os, "fchmod", raising=False)
    store = ActivityStore(tmp_path / "activity-state.json")

    store.record_event(
        dedup_key="test:event-1",
        kind="task.migrated",
        tabs=("today",),
        company="样例科技",
        entity_id="task:" + "a" * 24,
    )

    assert (tmp_path / "activity-state.json").exists()
    assert ActivityStore(tmp_path / "activity-state.json").unique_unread_count() == 1


def test_fchmod_private_tolerates_missing_fchmod(monkeypatch, tmp_path) -> None:
    monkeypatch.delattr(fs_utils.os, "fchmod", raising=False)
    descriptor = os.open(tmp_path / "file", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fs_utils.fchmod_private(descriptor)
    finally:
        os.close(descriptor)


class _FakeMsvcrt:
    LK_LOCK = 1
    LK_UNLCK = 0

    def __init__(self, *, fail_lock: bool) -> None:
        self.fail_lock = fail_lock
        self.calls: list[tuple[int, int, int, int]] = []

    def locking(self, descriptor: int, mode: int, nbytes: int) -> None:
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        self.calls.append((descriptor, mode, nbytes, position))
        if mode == self.LK_LOCK and self.fail_lock:
            raise OSError(36, "Resource deadlock avoided")


@pytest.fixture
def windows_lock_semantics(monkeypatch):
    monkeypatch.setattr(data_lock.os, "name", "nt")
    closed: list[int] = []
    real_close = os.close

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(data_lock.os, "close", recording_close)

    def install(fake: _FakeMsvcrt) -> _FakeMsvcrt:
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        return fake

    return install, closed


def test_windows_lock_contention_raises_a_clear_error_and_closes_the_handle(
    tmp_path,
    windows_lock_semantics,
) -> None:
    install, closed = windows_lock_semantics
    fake = install(_FakeMsvcrt(fail_lock=True))

    with pytest.raises(data_lock.DataLockBusyError, match="另一个 JobMailDesk"):
        with data_lock.data_directory_lease(tmp_path / ".data.lock"):
            pytest.fail("the lease must not be granted while another process holds it")

    lock_attempts = [call for call in fake.calls if call[1] == fake.LK_LOCK]
    assert len(lock_attempts) == data_lock.WINDOWS_LOCK_ATTEMPTS
    assert all(position == 0 for *_rest, position in lock_attempts)
    assert len(closed) == 1, "the descriptor must be released after a failed lock"
    # The Windows error is preserved as the cause for diagnostics.
    try:
        with data_lock.data_directory_lease(tmp_path / ".data.lock"):
            pass
    except data_lock.DataLockBusyError as exc:
        assert isinstance(exc.__cause__, OSError)


def test_windows_lock_locks_and_unlocks_the_same_byte_range(
    tmp_path,
    windows_lock_semantics,
) -> None:
    install, closed = windows_lock_semantics
    fake = install(_FakeMsvcrt(fail_lock=False))

    with data_lock.data_directory_lease(tmp_path / ".data.lock"):
        with data_lock.data_directory_lease(tmp_path / ".data.lock"):
            pass  # re-entrant: no second OS lock

    modes = [(mode, position) for _fd, mode, _n, position in fake.calls]
    assert modes == [(fake.LK_LOCK, 0), (fake.LK_UNLCK, 0)]
    assert len(closed) == 1


def test_windows_unlock_failure_does_not_mask_the_caller(
    tmp_path,
    windows_lock_semantics,
) -> None:
    install, closed = windows_lock_semantics

    class _UnlockFails(_FakeMsvcrt):
        def locking(self, descriptor, mode, nbytes):
            super().locking(descriptor, mode, nbytes)
            if mode == self.LK_UNLCK:
                raise OSError(13, "Permission denied")

    install(_UnlockFails(fail_lock=False))

    with pytest.raises(ValueError, match="business error"):
        with data_lock.data_directory_lease(tmp_path / ".data.lock"):
            raise ValueError("business error")

    assert len(closed) == 1


def test_credentials_bind_the_windows_credential_manager_backend(monkeypatch) -> None:
    import keyring

    class WinVaultKeyring:  # stand-in for keyring.backends.Windows.WinVaultKeyring
        pass

    fake_backend = types.ModuleType("keyring.backends.Windows")
    fake_backend.WinVaultKeyring = WinVaultKeyring
    bound: list[object] = []
    monkeypatch.setitem(sys.modules, "keyring.backends.Windows", fake_backend)
    monkeypatch.setattr(keyring, "set_keyring", lambda backend: bound.append(backend))
    monkeypatch.setattr(sys, "platform", "win32")
    try:
        importlib.reload(credentials)
    finally:
        monkeypatch.undo()
        importlib.reload(credentials)

    assert len(bound) == 1
    assert isinstance(bound[0], WinVaultKeyring)
    assert credentials._credential_store_name() in {"macOS Keychain", "Windows 凭据库"}


def test_file_transaction_fsyncs_files_through_a_writable_handle(tmp_path, monkeypatch) -> None:
    """Windows FlushFileBuffers rejects read-only handles; fsync must open r+b."""
    from job_mail_desk import fs_utils

    opened_modes: list[str] = []
    real_open = Path.open

    def recording_open(self, mode="r", *args, **kwargs):
        opened_modes.append(mode)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "record.md").write_text("before", encoding="utf-8")

    with FileTransaction(tmp_path / ".transactions", (facts,)) as transaction:
        transaction.commit()

    assert "rb" not in opened_modes
    assert "r+b" in opened_modes
    fs_utils.fsync_file(tmp_path / "missing.md")  # tolerated, never raises


def test_file_transaction_rollback_retries_transient_windows_locks(tmp_path, monkeypatch) -> None:
    import shutil

    from job_mail_desk import fs_utils

    real_rmtree = shutil.rmtree
    attempts = {"count": 0}

    def flaky_rmtree(path, *args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise PermissionError(32, "The process cannot access the file")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(fs_utils.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(fs_utils.time, "sleep", lambda _delay: None)
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "record.md").write_text("before", encoding="utf-8")

    with pytest.raises(RuntimeError, match="injected"):
        with FileTransaction(tmp_path / ".transactions", (facts,)):
            (facts / "record.md").write_text("after", encoding="utf-8")
            raise RuntimeError("injected")

    assert attempts["count"] >= 3
    assert (facts / "record.md").read_text(encoding="utf-8") == "before"


def test_file_transaction_keeps_the_backup_when_rollback_cannot_finish(tmp_path, monkeypatch) -> None:
    from job_mail_desk import fs_utils
    from job_mail_desk.file_transaction import recover_file_transactions

    monkeypatch.setattr(fs_utils.time, "sleep", lambda _delay: None)
    always_locked = {"active": True}
    real_rmtree = fs_utils.shutil.rmtree

    def locked_rmtree(path, *args, **kwargs):
        if always_locked["active"]:
            raise PermissionError(32, "locked by antivirus")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(fs_utils.shutil, "rmtree", locked_rmtree)
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "record.md").write_text("before", encoding="utf-8")
    root = tmp_path / ".transactions"

    with pytest.raises(PermissionError):
        with FileTransaction(root, (facts,)):
            (facts / "record.md").write_text("after", encoding="utf-8")
            raise RuntimeError("injected")

    # The prepared transaction (and its backup) survives for startup recovery.
    pending = list(root.glob("op1_*"))
    assert len(pending) == 1 and (pending[0] / "prepared").exists()
    assert recover_file_transactions(root) == 0  # still locked: keep waiting
    assert pending[0].exists()

    always_locked["active"] = False
    assert recover_file_transactions(root) == 1
    assert (facts / "record.md").read_text(encoding="utf-8") == "before"
    assert not list(root.glob("op1_*"))
