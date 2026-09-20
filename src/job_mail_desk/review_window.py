from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol

from .confirmation_service import new_request_id
from .runtime_control import RuntimeControl, RuntimeScope


LOGGER = logging.getLogger(__name__)


class ReviewBackend(Protocol):
    def get_review_window_bootstrap(
        self,
        source_hash: str,
        preferred_key: str = "",
    ) -> dict[str, object]: ...

    def search_review_targets(
        self,
        source_hash: str,
        query: str = "",
    ) -> list[dict[str, object]]: ...

    def get_review_target(self, application_key: str) -> dict[str, object]: ...

    def get_review_recommendation(
        self,
        source_hash: str,
        application_key: str = "",
    ) -> dict[str, object]: ...

    def get_pending_original_mail(
        self,
        source_hash: str,
        expected_review_revision: int,
        load_remote_images: bool = False,
        *,
        runtime_control: RuntimeScope,
    ) -> dict[str, object]: ...

    def resolve_unresolved_workflow(
        self,
        source_hash: str,
        payload: dict[str, object],
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class ReviewWindowSession:
    window_id: str
    source_hash: str
    review_revision: int
    target_key: str | None
    application_revision: int | None
    request_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "window_id": self.window_id,
            "source_hash": self.source_hash,
            "review_revision": self.review_revision,
            "target_key": self.target_key,
            "application_revision": self.application_revision,
            "request_id": self.request_id,
        }


@dataclass
class _ManagedReviewWindow:
    session: ReviewWindowSession
    scope: RuntimeScope
    bridge: ReviewWindowApi
    bootstrap: dict[str, object]
    window: Any | None = None
    dirty: bool = False
    allow_close: bool = False
    saved: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    selection_token: int = 0


def _default_mode(
    target: dict[str, object] | None,
    recommendation: dict[str, object] | None = None,
) -> str:
    if not target:
        return "new_identity"
    if str(target.get("status") or "") == "active":
        if recommendation and recommendation.get("suggest_update_task"):
            return "update_task"
        return "update_active"
    return "new_attempt"


class ReviewWindowApi:
    """Narrow bridge exposed to one native review window."""

    def __init__(self, controller: ReviewWindowController, window_id: str) -> None:
        self._controller = controller
        self._window_id = window_id

    def get_bootstrap(self) -> dict[str, object]:
        return self._controller.get_bootstrap(self._window_id)

    def search_targets(self, query: str, token: int) -> dict[str, object]:
        return self._controller.search_targets(
            self._window_id,
            str(query),
            int(token),
        )

    def select_target(self, application_key: str, token: int) -> dict[str, object]:
        return self._controller.select_target(
            self._window_id,
            str(application_key),
            int(token),
        )

    def load_original_mail(
        self,
        load_remote_images: bool = False,
    ) -> dict[str, object]:
        return self._controller.load_original_mail(
            self._window_id,
            bool(load_remote_images),
        )

    def set_dirty(self, dirty: bool) -> bool:
        return self._controller.set_dirty(self._window_id, bool(dirty))

    def confirm(self, payload: dict[str, object]) -> dict[str, object]:
        return self._controller.confirm(self._window_id, dict(payload))

    def close_window(self, force: bool = False) -> dict[str, object]:
        return self._controller.close_window(self._window_id, force=bool(force))


class ReviewWindowController:
    """Own independent native windows and their optimistic-lock sessions."""

    def __init__(
        self,
        backend: ReviewBackend,
        runtime_control: RuntimeControl,
        page_url: str,
        window_factory: Callable[..., Any],
        *,
        on_saved: Callable[[str], None] | None = None,
    ) -> None:
        self._backend = backend
        self._runtime_control = runtime_control
        self._page_url = page_url
        self._window_factory = window_factory
        self._on_saved = on_saved
        self._lock = threading.RLock()
        self._windows: dict[str, _ManagedReviewWindow] = {}
        self._closing_all = False

    @property
    def window_count(self) -> int:
        with self._lock:
            return len(self._windows)

    def open_window(
        self,
        source_hash: str,
        preferred_key: str = "",
    ) -> dict[str, object]:
        self._runtime_control.raise_if_stopping()
        bootstrap = self._backend.get_review_window_bootstrap(
            source_hash,
            preferred_key,
        )
        review = dict(bootstrap["review"])  # type: ignore[arg-type]
        target_value = bootstrap.get("target")
        target = dict(target_value) if isinstance(target_value, dict) else None
        recommendation = dict(bootstrap["recommendation"])  # type: ignore[arg-type]
        if int(recommendation["review_revision"]) != int(review["revision"]):
            raise ValueError("待处理内容已更新，请重新打开复核窗口。")
        if target and int(
            recommendation.get("application_revision") or 0
        ) != int(target["revision"]):
            raise ValueError("申请链已更新，请重新打开复核窗口。")
        window_id = f"review-{uuid.uuid4().hex}"
        session = ReviewWindowSession(
            window_id=window_id,
            source_hash=source_hash,
            review_revision=int(review["revision"]),
            target_key=str(target["application_key"]) if target else None,
            application_revision=int(target["revision"]) if target else None,
            request_id=new_request_id(),
        )
        bridge = ReviewWindowApi(self, window_id)
        managed = _ManagedReviewWindow(
            session=session,
            scope=self._runtime_control.create_scope(),
            bridge=bridge,
            bootstrap={
                **bootstrap,
                "default_mode": _default_mode(target, recommendation),
            },
        )
        with self._lock:
            if self._closing_all:
                raise RuntimeError("应用正在退出，不能打开复核窗口。")
            self._windows[window_id] = managed
        title_company = str(review.get("company") or "公司待确认")
        title_stage = str(review.get("stage") or "邮件复核")
        try:
            window = self._window_factory(
                f"{title_company} · {title_stage} · 邮件复核",
                self._page_url,
                js_api=bridge,
                width=1080,
                height=760,
                min_size=(900, 600),
                resizable=True,
                frameless=False,
                on_top=False,
                confirm_close=False,
                easy_drag=False,
                background_color="#f4efe6",
                localization={
                    "global.quit": "放弃修改并关闭",
                    "global.cancel": "继续编辑",
                    "global.quitConfirmation": "有尚未保存的复核修改，确定关闭吗？",
                },
            )
            if window is None:
                raise RuntimeError("原生复核窗口创建失败。")
            managed.window = window
            window.events.closing += (
                lambda *_args, current_id=window_id: self._on_closing(current_id)
            )
            window.events.closed += (
                lambda *_args, current_id=window_id: self._on_closed(current_id)
            )
        except Exception:
            managed.scope.stop()
            with self._lock:
                self._windows.pop(window_id, None)
            raise
        return session.to_dict()

    def _managed(self, window_id: str) -> _ManagedReviewWindow:
        with self._lock:
            managed = self._windows.get(window_id)
        if not managed:
            raise ValueError("复核窗口已经关闭。")
        return managed

    def get_bootstrap(self, window_id: str) -> dict[str, object]:
        managed = self._managed(window_id)
        return {
            **managed.bootstrap,
            "session": managed.session.to_dict(),
        }

    def search_targets(
        self,
        window_id: str,
        query: str,
        token: int,
    ) -> dict[str, object]:
        managed = self._managed(window_id)
        return {
            "token": token,
            "targets": self._backend.search_review_targets(
                managed.session.source_hash,
                query,
            ),
        }

    def select_target(
        self,
        window_id: str,
        application_key: str,
        token: int,
    ) -> dict[str, object]:
        managed = self._managed(window_id)
        with managed.lock:
            if token < managed.selection_token:
                return {"token": token, "stale": True}
            managed.selection_token = token
            target = (
                self._backend.get_review_target(application_key)
                if application_key
                else None
            )
            recommendation = self._backend.get_review_recommendation(
                managed.session.source_hash,
                application_key,
            )
            if (
                int(recommendation["review_revision"])
                != managed.session.review_revision
            ):
                raise ValueError("待处理内容已更新，请关闭窗口后重新核对。")
            if target and int(
                recommendation.get("application_revision") or 0
            ) != int(target["revision"]):
                raise ValueError("申请链已更新，请重新选择。")
            managed.session = replace(
                managed.session,
                target_key=application_key or None,
                application_revision=(
                    int(target["revision"]) if target is not None else None
                ),
            )
            return {
                "token": token,
                "target": target,
                "recommendation": recommendation,
                "default_mode": _default_mode(target, recommendation),
                "session": managed.session.to_dict(),
            }

    def load_original_mail(
        self,
        window_id: str,
        load_remote_images: bool,
    ) -> dict[str, object]:
        managed = self._managed(window_id)
        managed.scope.raise_if_stopping()
        return self._backend.get_pending_original_mail(
            managed.session.source_hash,
            managed.session.review_revision,
            load_remote_images,
            runtime_control=managed.scope,
        )

    def set_dirty(self, window_id: str, dirty: bool) -> bool:
        managed = self._managed(window_id)
        managed.dirty = dirty
        if managed.window is not None:
            managed.window.confirm_close = dirty
        return dirty

    @staticmethod
    def _payload_revision(value: object) -> int | None:
        if value in {None, ""}:
            return None
        return int(value)

    def confirm(
        self,
        window_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        managed = self._managed(window_id)
        with managed.lock:
            session = managed.session
            if str(payload.get("window_id") or "") != session.window_id:
                raise ValueError("复核窗口会话无效，请重新打开。")
            if str(payload.get("source_hash") or "") != session.source_hash:
                raise ValueError("复核邮件已切换，请重新打开。")
            if str(payload.get("request_id") or "") != session.request_id:
                raise ValueError("确认请求已失效，请重新打开。")
            if (
                int(payload.get("expected_review_revision") or 0)
                != session.review_revision
            ):
                raise ValueError("待处理内容已更新，请重新核对后确认。")
            if str(payload.get("application_key") or "") != (
                session.target_key or ""
            ):
                raise ValueError("目标申请已切换，请重新选择。")
            if self._payload_revision(
                payload.get("expected_application_revision")
            ) != session.application_revision:
                raise ValueError("申请链已更新，请重新选择后确认。")

            authoritative_payload = {
                **payload,
                "request_id": session.request_id,
                "expected_review_revision": session.review_revision,
                "expected_application_revision": session.application_revision,
                "application_key": session.target_key or "",
            }
            dashboard = self._backend.resolve_unresolved_workflow(
                session.source_hash,
                authoritative_payload,
            )
            managed.saved = True
            managed.dirty = False
            managed.allow_close = True
            if managed.window is not None:
                managed.window.confirm_close = False
        if self._on_saved:
            try:
                self._on_saved(session.source_hash)
            except Exception:
                LOGGER.exception("复核已保存，但主窗口刷新失败")
        timer = threading.Timer(
            0.08,
            lambda: self._close_saved_window(window_id),
        )
        timer.daemon = True
        timer.start()
        return {"status": "ok", "dashboard": dashboard}

    def _close_saved_window(self, window_id: str) -> None:
        try:
            self.close_window(window_id, force=True)
        except ValueError:
            pass

    def close_window(
        self,
        window_id: str,
        *,
        force: bool = False,
    ) -> dict[str, object]:
        managed = self._managed(window_id)
        if managed.dirty and not force:
            return {"closed": False, "requires_confirmation": True}
        managed.allow_close = True
        if managed.window is not None:
            managed.window.confirm_close = False
        managed.scope.stop()
        if managed.window is not None:
            managed.window.destroy()
        else:
            self._on_closed(window_id)
        return {"closed": True, "requires_confirmation": False}

    def _on_closing(self, window_id: str) -> bool | None:
        try:
            managed = self._managed(window_id)
        except ValueError:
            return None
        if managed.window is not None:
            managed.window.confirm_close = bool(
                managed.dirty
                and not managed.allow_close
                and not self._closing_all
            )
        return None

    def _on_closed(self, window_id: str) -> None:
        with self._lock:
            managed = self._windows.pop(window_id, None)
        if managed:
            managed.scope.stop()

    def close_all(self) -> None:
        with self._lock:
            self._closing_all = True
            window_ids = tuple(self._windows)
        for window_id in window_ids:
            try:
                self.close_window(window_id, force=True)
            except Exception:
                # A window that never finished showing raises from the host
                # toolkit; drop the session so shutdown still completes.
                LOGGER.exception("复核窗口关闭失败，已强制释放会话")
                self._on_closed(window_id)
