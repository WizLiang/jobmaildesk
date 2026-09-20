"""In-memory scan counters shared by the scheduler, tray and desktop bridge."""
from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from typing import TypeVar

T = TypeVar("T")
STAGES = {
    "idle", "preparing", "connecting", "searching", "reading", "preparing_records",
    "parsing", "saving", "exporting", "done", "partial", "error", "cancelled",
}
TERMINAL_STAGES = {"idle", "done", "partial", "error", "cancelled"}


class ScanProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = dict(run_id=0, revision=0, running=False, stage="idle",
                           completed=0, total=None, lookback_days=None)

    def report(self, stage: str, completed: int = 0, total: int | None = None,
               *, lookback_days: int | None = None) -> None:
        if stage not in STAGES:
            raise ValueError("Unknown scan stage")
        with self._lock:
            if stage == "preparing":
                self._value["run_id"] += 1
                self._value["lookback_days"] = None
            if lookback_days is not None:
                self._value["lookback_days"] = lookback_days
            self._value.update(
                revision=self._value["revision"] + 1,
                running=stage not in TERMINAL_STAGES, stage=stage,
                completed=max(0, min(completed, total) if total is not None else completed),
                total=max(0, total) if total is not None else None,
            )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._value)


def report_progress(runtime, stage: str, completed: int = 0,
                    total: int | None = None, *, lookback_days: int | None = None) -> None:
    progress = getattr(runtime, "scan_progress", None)
    if progress is not None:
        progress.report(stage, completed, total, lookback_days=lookback_days)


def track_items(runtime, stage: str, items: Iterable[T], *, total: int) -> Iterator[T]:
    """Count completed attempts, including skips and isolated failures."""
    report_progress(runtime, stage, 0, total)
    for index, item in enumerate(items):
        yield item
        report_progress(runtime, stage, index + 1, total)
