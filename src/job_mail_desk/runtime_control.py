from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from .scan_progress import ScanProgress


LOGGER = logging.getLogger(__name__)


class RuntimeStopping(RuntimeError):
    """Raised when work is cancelled because the desktop app is stopping."""


class RuntimeControl:
    """Coordinate scans and interrupt blocking resources during shutdown."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.scan_lock = threading.Lock()
        self.scan_progress = ScanProgress()
        self._resource_lock = threading.Lock()
        self._resources: dict[int, Callable[[], object]] = {}
        self._next_resource_id = 0
        self._interrupts_fired = False

    @property
    def stopping(self) -> bool:
        return self.stop_event.is_set()

    def raise_if_stopping(self) -> None:
        if self.stopping:
            raise RuntimeStopping("应用正在退出，操作已取消。")

    def register_interrupt(self, callback: Callable[[], object]) -> Callable[[], None]:
        with self._resource_lock:
            if self.stopping:
                call_now = True
                resource_id = -1
            else:
                call_now = False
                self._next_resource_id += 1
                resource_id = self._next_resource_id
                self._resources[resource_id] = callback
        if call_now:
            try:
                callback()
            except Exception:
                pass

        def unregister() -> None:
            if resource_id < 0:
                return
            with self._resource_lock:
                self._resources.pop(resource_id, None)

        return unregister

    def create_scope(self) -> RuntimeScope:
        """Create a cancellable child scope backed by this runtime."""
        return RuntimeScope(self)

    def begin_stop(self) -> None:
        """Refuse new work immediately without touching blocking resources.

        Callers on a UI thread need a step that cannot block, so interrupting
        sockets and other registered resources is left to :meth:`stop`.
        """
        self.stop_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        with self._resource_lock:
            if self._interrupts_fired:
                return
            self._interrupts_fired = True
            callbacks = tuple(self._resources.values())
            self._resources.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # Shutdown is best-effort and must remain idempotent.
                pass


class ShutdownCoordinator:
    """Quit the desktop app on a bounded schedule.

    The macOS host runs window teardown callbacks on the AppKit main thread and
    interpreter shutdown joins worker threads the app does not own, so neither
    can be trusted to finish. Draining therefore happens on a worker thread and
    a watchdog ends the process once the fact stores are quiet. Every local
    store writes through a temporary file or a recoverable transaction, so the
    forced exit cannot leave torn data behind.
    """

    def __init__(
        self,
        runtime_control: RuntimeControl,
        drain: Callable[[], None],
        *,
        drain_timeout: float = 6.0,
        quiesce_timeout: float = 6.0,
        exit_process: Callable[[int], object] | None = None,
        flush: Callable[[], object] | None = None,
    ) -> None:
        self._runtime_control = runtime_control
        self._drain = drain
        self._drain_timeout = drain_timeout
        self._quiesce_timeout = quiesce_timeout
        self._exit_process = exit_process or os._exit
        self._flush = flush or logging.shutdown
        self._lock = threading.Lock()
        self._requested = threading.Event()
        self._drained = threading.Event()
        self._exiting = threading.Event()

    @property
    def exit_budget_seconds(self) -> float:
        return self._drain_timeout + self._quiesce_timeout

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    def request(self) -> None:
        """Begin shutdown and return at once. Safe to call repeatedly."""
        with self._lock:
            if self._requested.is_set():
                return
            self._requested.set()
        self._runtime_control.begin_stop()
        self._spawn("jobmaildesk-shutdown-drain", self._run_drain)
        self._spawn("jobmaildesk-shutdown-exit", self._run_exit)

    def wait_for_drain(self, timeout: float | None = None) -> bool:
        return self._drained.wait(timeout)

    def wait_for_exit(self, timeout: float | None = None) -> bool:
        return self._exiting.wait(timeout)

    @staticmethod
    def _spawn(name: str, target: Callable[[], None]) -> threading.Thread:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        return thread

    def _run_drain(self) -> None:
        try:
            self._drain()
        except Exception:
            LOGGER.exception("退出清理失败，仍会结束进程")
        finally:
            self._drained.set()

    def _run_exit(self) -> None:
        self._drained.wait(self._drain_timeout)
        lock = self._runtime_control.scan_lock
        if lock.acquire(timeout=self._quiesce_timeout):
            lock.release()
        else:
            LOGGER.warning("退出前仍有写入未结束，已达到等待上限")
        try:
            self._flush()
        except Exception:
            pass
        self._exiting.set()
        self._exit_process(0)


class RuntimeScope:
    """Cancel one operation without stopping the shared desktop runtime."""

    def __init__(self, parent: RuntimeControl) -> None:
        self._parent = parent
        self.stop_event = threading.Event()
        self.scan_lock = parent.scan_lock
        self._resource_lock = threading.Lock()
        self._resources: dict[
            int,
            tuple[Callable[[], object], Callable[[], None]],
        ] = {}
        self._next_resource_id = 0

    @property
    def stopping(self) -> bool:
        return self.stop_event.is_set() or self._parent.stopping

    def raise_if_stopping(self) -> None:
        if self.stopping:
            raise RuntimeStopping("操作已取消。")

    def register_interrupt(self, callback: Callable[[], object]) -> Callable[[], None]:
        called = False
        called_lock = threading.Lock()

        def interrupt_once() -> object | None:
            nonlocal called
            with called_lock:
                if called:
                    return None
                called = True
            return callback()

        with self._resource_lock:
            if self.stopping:
                resource_id = -1
            else:
                self._next_resource_id += 1
                resource_id = self._next_resource_id
        if resource_id < 0:
            try:
                interrupt_once()
            except Exception:
                pass
            return lambda: None

        parent_unregister = self._parent.register_interrupt(interrupt_once)
        with self._resource_lock:
            if self.stopping:
                cancelled_during_registration = True
            else:
                cancelled_during_registration = False
                self._resources[resource_id] = (
                    interrupt_once,
                    parent_unregister,
                )
        if cancelled_during_registration:
            parent_unregister()
            try:
                interrupt_once()
            except Exception:
                pass

        def unregister() -> None:
            with self._resource_lock:
                registered = self._resources.pop(resource_id, None)
            if registered:
                registered[1]()
            else:
                parent_unregister()

        return unregister

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        with self._resource_lock:
            resources = tuple(self._resources.values())
            self._resources.clear()
        for callback, parent_unregister in resources:
            parent_unregister()
            try:
                callback()
            except Exception:
                pass
