from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
_LEASE_STATE = threading.local()

# ``msvcrt.locking(LK_LOCK)`` retries once per second for ten seconds and then
# raises ``OSError`` (EDEADLK); ``fcntl.flock`` would block forever instead.
# Two bounded attempts keep the wait finite while surviving a brief contention.
WINDOWS_LOCK_ATTEMPTS = 2


class DataLockBusyError(RuntimeError):
    """Another JobMailDesk process holds the local data lock."""


def _lock_windows(descriptor: int) -> None:
    import msvcrt

    last_error: OSError | None = None
    for _attempt in range(WINDOWS_LOCK_ATTEMPTS):
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            return
        except OSError as exc:
            last_error = exc
    raise DataLockBusyError(
        "另一个 JobMailDesk 正在写入本地数据，请稍后重试。"
    ) from last_error


def _unlock_windows(descriptor: int) -> None:
    import msvcrt

    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    except OSError:
        # Closing the descriptor releases the range anyway; never let an
        # unlock failure hide the caller's own exception.
        pass


def _process_lock(path: Path) -> threading.RLock:
    key = str(path.resolve(strict=False))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def data_directory_lease(path: Path, *, allow_privacy_reset: bool = False,
                         allow_storage_move: bool = False) -> Iterator[None]:
    """Serialize fact mutations across threads and local processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve(strict=False))
    held = getattr(_LEASE_STATE, "held", set())
    if key in held:
        yield
        return
    thread_lock = _process_lock(path)
    with thread_lock:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                _lock_windows(descriptor)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            held = set(held)
            held.add(key)
            _LEASE_STATE.held = held
            try:
                if not allow_privacy_reset and (path.parent / ".privacy-reset.json").exists():
                    raise DataLockBusyError("个人信息正在清除，已停止本地数据写入。")
                if not allow_storage_move and (path.parent / ".storage-move.json").exists():
                    raise DataLockBusyError("数据正在迁移，已停止本地数据写入。")
                yield
            finally:
                held.remove(key)
                _LEASE_STATE.held = held
        finally:
            try:
                if os.name == "nt":
                    _unlock_windows(descriptor)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
