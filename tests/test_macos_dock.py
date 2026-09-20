from __future__ import annotations

from types import SimpleNamespace

import pytest

from job_mail_desk import macos_dock


class _FakeDockTile:
    def __init__(self) -> None:
        self.labels: list[str | None] = []

    def setBadgeLabel_(self, label: str | None) -> None:
        self.labels.append(label)


class _FakeApplication:
    def __init__(self, dock_tile: _FakeDockTile) -> None:
        self._dock_tile = dock_tile

    def dockTile(self) -> _FakeDockTile:
        return self._dock_tile


class _FakeAppHelper:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    def callAfter(self, callback, *args) -> None:
        self.calls.append((callback, args))
        callback(*args)


def _install_fake_appkit(monkeypatch):
    dock_tile = _FakeDockTile()
    application = _FakeApplication(dock_tile)
    app_helper = _FakeAppHelper()
    appkit = SimpleNamespace(
        NSApplication=SimpleNamespace(
            sharedApplication=lambda: application,
        )
    )
    monkeypatch.setattr(macos_dock, "AppKit", appkit)
    monkeypatch.setattr(macos_dock, "AppHelper", app_helper)
    return dock_tile, app_helper


def test_dock_badge_is_noop_outside_darwin(monkeypatch) -> None:
    monkeypatch.setattr(macos_dock.sys, "platform", "linux")
    _, app_helper = _install_fake_appkit(monkeypatch)

    assert macos_dock.set_dock_badge(4) is False
    assert app_helper.calls == []


def test_dock_badge_uses_call_after_and_zero_clears_with_none(monkeypatch) -> None:
    monkeypatch.setattr(macos_dock.sys, "platform", "darwin")
    dock_tile, app_helper = _install_fake_appkit(monkeypatch)

    assert macos_dock.set_dock_badge(7) is True
    assert macos_dock.set_dock_badge(0) is True

    assert len(app_helper.calls) == 2
    assert app_helper.calls[0][1] == ("7",)
    assert app_helper.calls[1][1] == (None,)
    assert dock_tile.labels == ["7", None]


def test_dock_badge_is_noop_when_pyobjc_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(macos_dock.sys, "platform", "darwin")
    monkeypatch.setattr(macos_dock, "AppKit", None)
    monkeypatch.setattr(macos_dock, "AppHelper", None)

    assert macos_dock.set_dock_badge(3) is False


def test_dock_badge_rejects_negative_counts_on_darwin(monkeypatch) -> None:
    monkeypatch.setattr(macos_dock.sys, "platform", "darwin")
    _install_fake_appkit(monkeypatch)

    with pytest.raises(ValueError, match="non-negative"):
        macos_dock.set_dock_badge(-1)
