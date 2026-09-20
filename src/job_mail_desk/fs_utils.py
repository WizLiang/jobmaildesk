from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

# Windows antivirus and indexers hold freshly written files open for a
# moment; ``os.replace``/``rmtree``/``copytree`` then fail with
# ``PermissionError`` and must be retried, never treated as fatal at once.
RETRY_ATTEMPTS = 5


def retry_on_permission_error(operation: Callable[[], T]) -> T:
    """Run ``operation`` with the same bounded backoff ``_atomic_write`` uses."""
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return operation()
        except (PermissionError, shutil.Error):
            # shutil.copytree reports per-file PermissionErrors as shutil.Error.
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * (2**attempt))
    raise AssertionError("unreachable")


def _clear_read_only(function, path, _exc) -> None:
    """``shutil.rmtree`` hook: retry after removing a Windows read-only bit."""
    try:
        os.chmod(path, 0o700)
        function(path)
    except OSError:
        raise


def remove_tree(path: Path) -> None:
    """Delete a directory tree, tolerating read-only bits and transient locks."""
    retry_on_permission_error(
        lambda: shutil.rmtree(path, onexc=_clear_read_only) if path.exists() else None
    )


def copy_tree(source: Path, destination: Path) -> None:
    retry_on_permission_error(lambda: shutil.copytree(source, destination))


def fsync_directory(directory: Path | str) -> None:
    """Flush directory metadata after an ``os.replace`` when the OS allows it.

    POSIX lets us open a directory handle and ``fsync`` it so the rename is
    durable. Windows refuses ``os.open`` on a directory (``PermissionError``)
    and ``MoveFileEx`` already commits the rename atomically, so the call is a
    best-effort no-op there. Every fact store shares this helper so no local
    write can fail merely because the platform has no directory handles.
    """
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def fsync_file(path: Path) -> None:
    """Flush a file's content to disk.

    Windows' ``FlushFileBuffers`` requires a handle opened for writing, so the
    file is opened ``r+b`` (no truncation) instead of ``rb``; a file that
    cannot be opened that way is skipped rather than aborting the caller.
    """
    try:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass


def fchmod_private(descriptor: int) -> None:
    """Restrict a freshly created file to the owner where the OS supports it.

    ``os.fchmod`` does not exist on Windows before Python 3.13, so the lookup
    must be guarded; NTFS inherits ACLs from ``%LOCALAPPDATA%`` instead.
    """
    fchmod = getattr(os, "fchmod", None)
    if fchmod is None:
        return
    try:
        fchmod(descriptor, 0o600)
    except OSError:
        pass
