import imaplib
import threading
from pathlib import Path

from job_mail_desk.config import Settings
from job_mail_desk.credentials import MailCredential
from job_mail_desk.mail_reader import ImapReader
from job_mail_desk.runtime_control import (
    RuntimeControl,
    RuntimeStopping,
    ShutdownCoordinator,
)
from job_mail_desk.scheduler import ScheduledJobs


class BlockingImap:
    entered = threading.Event()
    released = threading.Event()
    instance = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.shutdown_calls = 0
        BlockingImap.instance = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, email, code):
        BlockingImap.entered.set()
        BlockingImap.released.wait(timeout=2)
        return "OK", []

    def logout(self):
        return "BYE", []

    def shutdown(self):
        self.shutdown_calls += 1
        BlockingImap.released.set()


def test_runtime_stop_interrupts_blocked_imap(monkeypatch) -> None:
    BlockingImap.entered.clear()
    BlockingImap.released.clear()
    monkeypatch.setattr(
        "job_mail_desk.mail_reader.imaplib.IMAP4_SSL",
        BlockingImap,
    )
    runtime = RuntimeControl()
    result: list[BaseException] = []

    def fetch() -> None:
        try:
            ImapReader(
                Settings(),
                MailCredential("private@example.invalid", "authorization-code"),
                runtime_control=runtime,
            ).fetch_since(1)
        except BaseException as exc:
            result.append(exc)

    worker = threading.Thread(target=fetch)
    worker.start()
    assert BlockingImap.entered.wait(timeout=1)
    runtime.stop()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert BlockingImap.instance.shutdown_calls == 1
    assert len(result) == 1
    assert isinstance(result[0], RuntimeStopping)


class TornSocketImap:
    """Reproduce a real 163 session interrupted mid-fetch on quit.

    Shutting the socket down makes the blocked read raise a NULL buffer view,
    and the follow-up LOGOUT aborts on a dead descriptor without releasing it.
    """

    instance = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.torn = False
        self.logout_calls = 0
        self.shutdown_calls = 0
        TornSocketImap.instance = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, email, code):
        return "OK", []

    def uid(self, *args):
        if self.torn:
            raise ValueError(
                "PyMemoryView_FromBuffer(): info->buf must not be NULL"
            )
        return "OK", [b""]

    def logout(self):
        self.logout_calls += 1
        if self.torn:
            raise imaplib.IMAP4.abort(
                "socket error: [Errno 9] Bad file descriptor"
            )
        return "BYE", []

    def shutdown(self):
        self.shutdown_calls += 1
        self.torn = True


def test_quitting_mid_fetch_cancels_instead_of_raising_a_socket_abort(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "job_mail_desk.mail_reader.imaplib.IMAP4_SSL",
        TornSocketImap,
    )
    runtime = RuntimeControl()
    reader = ImapReader(
        Settings(),
        MailCredential("private@example.invalid", "authorization-code"),
        runtime_control=runtime,
    )

    raised: list[BaseException] = []
    try:
        with reader._connected_client() as client:
            client.login("private@example.invalid", "authorization-code")
            runtime.stop()
            client.uid("FETCH", "1", "(BODY.PEEK[])")
    except BaseException as exc:
        raised.append(exc)

    session = TornSocketImap.instance
    assert len(raised) == 1
    # The UI used to receive imaplib.IMAP4.abort from the context manager.
    assert isinstance(raised[0], RuntimeStopping)
    assert session.logout_calls == 1
    # Interrupt plus the retry after LOGOUT failed, so no descriptor leaks.
    assert session.shutdown_calls == 2


def test_a_real_imap_failure_is_not_disguised_as_cancellation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "job_mail_desk.mail_reader.imaplib.IMAP4_SSL",
        TornSocketImap,
    )
    reader = ImapReader(
        Settings(),
        MailCredential("private@example.invalid", "authorization-code"),
        runtime_control=RuntimeControl(),
    )

    raised: list[BaseException] = []
    try:
        with reader._connected_client() as client:
            client.login("private@example.invalid", "authorization-code")
            raise OSError("server closed the connection")
    except BaseException as exc:
        raised.append(exc)

    assert len(raised) == 1
    assert isinstance(raised[0], OSError)
    assert not isinstance(raised[0], RuntimeStopping)


def test_manual_and_scheduled_scans_share_one_gate() -> None:
    runtime = RuntimeControl()
    jobs = ScheduledJobs(Settings(), runtime)

    assert jobs.lock is runtime.scan_lock


def test_begin_stop_refuses_work_but_keeps_interrupts_pending() -> None:
    runtime = RuntimeControl()
    interrupted: list[str] = []
    runtime.register_interrupt(lambda: interrupted.append("socket"))

    runtime.begin_stop()

    assert runtime.stopping
    assert interrupted == []

    runtime.stop()
    runtime.stop()

    assert interrupted == ["socket"]


def _coordinator(
    runtime: RuntimeControl,
    drain,
    exits: list[int],
    **kwargs,
) -> ShutdownCoordinator:
    return ShutdownCoordinator(
        runtime,
        drain,
        exit_process=exits.append,
        flush=lambda: None,
        **kwargs,
    )


def test_request_returns_before_a_slow_drain_finishes() -> None:
    runtime = RuntimeControl()
    release = threading.Event()
    started = threading.Event()
    exits: list[int] = []

    def drain() -> None:
        started.set()
        release.wait(timeout=5)

    coordinator = _coordinator(runtime, drain, exits, drain_timeout=0.2)
    coordinator.request()

    assert started.wait(timeout=2)
    # The UI thread must be free even while teardown is still blocked.
    assert runtime.stopping
    assert coordinator.requested

    assert coordinator.wait_for_exit(timeout=3)
    assert exits == [0]

    release.set()
    assert coordinator.wait_for_drain(timeout=3)


def test_repeated_requests_drain_once() -> None:
    runtime = RuntimeControl()
    calls: list[str] = []
    exits: list[int] = []
    coordinator = _coordinator(
        runtime,
        lambda: calls.append("drain"),
        exits,
        drain_timeout=0.2,
    )

    coordinator.request()
    coordinator.request()
    coordinator.request()

    assert coordinator.wait_for_exit(timeout=3)
    assert calls == ["drain"]
    assert exits == [0]


def test_a_failing_drain_still_ends_the_process() -> None:
    runtime = RuntimeControl()
    exits: list[int] = []

    def drain() -> None:
        raise RuntimeError("review window teardown exploded")

    coordinator = _coordinator(runtime, drain, exits, drain_timeout=0.2)
    coordinator.request()

    assert coordinator.wait_for_exit(timeout=3)
    assert exits == [0]


def test_exit_waits_for_an_in_flight_fact_mutation() -> None:
    runtime = RuntimeControl()
    exits: list[int] = []
    coordinator = _coordinator(
        runtime,
        lambda: None,
        exits,
        drain_timeout=0.2,
        quiesce_timeout=3.0,
    )

    runtime.scan_lock.acquire()
    coordinator.request()

    assert not coordinator.wait_for_exit(timeout=0.6)
    assert exits == []

    runtime.scan_lock.release()

    assert coordinator.wait_for_exit(timeout=3)
    assert exits == [0]


def test_exit_is_bounded_when_a_mutation_never_releases() -> None:
    runtime = RuntimeControl()
    exits: list[int] = []
    coordinator = _coordinator(
        runtime,
        lambda: None,
        exits,
        drain_timeout=0.2,
        quiesce_timeout=0.3,
    )

    runtime.scan_lock.acquire()
    try:
        coordinator.request()

        assert coordinator.wait_for_exit(timeout=3)
        assert exits == [0]
    finally:
        runtime.scan_lock.release()


def test_desktop_shutdown_is_bound_to_closed_and_never_blocks_the_ui() -> None:
    source = (
        Path(__file__).parents[1] / "src/job_mail_desk/ui_app.py"
    ).read_text(encoding="utf-8")

    assert "shutdown = ShutdownCoordinator(runtime_control, drain_runtime)" in source
    assert "window.events.closed += shutdown.request" in source
    # A cancelled quit must not tear the runtime down, so nothing may run on
    # the synchronous ``closing`` callback.
    assert "window.events.closing" not in source
    assert "shutdown.wait_for_exit(" in source
