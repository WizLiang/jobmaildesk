from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from collections.abc import Callable

from .markdown_store import _atomic_write


@dataclass
class OutboxItem:
    id: str
    operation_id: str
    entity_id: str
    kinds: list[str]
    created_at: str
    attempts: int = 0
    last_error_type: str | None = None


class DerivedOutbox:
    """Retry metadata for non-authoritative exports and platform effects."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> list[OutboxItem]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema") != 1 or not isinstance(payload.get("items"), list):
            raise ValueError("派生操作 outbox 格式无效。")
        return [OutboxItem(**item) for item in payload["items"]]

    def _save(self, items: list[OutboxItem]) -> None:
        _atomic_write(
            self.path,
            json.dumps(
                {"schema": 1, "items": [asdict(item) for item in items]},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    def enqueue(
        self,
        *,
        operation_id: str,
        entity_id: str,
        kinds: tuple[str, ...],
    ) -> OutboxItem:
        if not kinds:
            raise ValueError("outbox 至少需要一种派生操作。")
        identifier = "out1_" + sha256(
            f"{operation_id}\0{entity_id}\0{'|'.join(kinds)}".encode("utf-8")
        ).hexdigest()[:24]
        items = self._load()
        existing = next((item for item in items if item.id == identifier), None)
        if existing:
            return existing
        item = OutboxItem(
            id=identifier,
            operation_id=operation_id,
            entity_id=entity_id,
            kinds=list(dict.fromkeys(kinds)),
            created_at=datetime.now().astimezone().isoformat(),
        )
        items.append(item)
        self._save(items)
        return item

    def pending(self) -> list[OutboxItem]:
        return self._load()

    def mark_attempt(self, item_id: str, error: BaseException) -> None:
        items = self._load()
        item = next((entry for entry in items if entry.id == item_id), None)
        if not item:
            return
        item.attempts += 1
        item.last_error_type = type(error).__name__
        self._save(items)

    def complete(self, item_id: str) -> None:
        self._save([item for item in self._load() if item.id != item_id])

    def process(
        self,
        handlers: dict[str, Callable[[OutboxItem], None]],
    ) -> tuple[int, int]:
        completed = failed = 0
        for item in list(self.pending()):
            try:
                for kind in item.kinds:
                    handler = handlers.get(kind)
                    if handler is None:
                        raise KeyError(f"missing outbox handler: {kind}")
                    handler(item)
            except Exception as exc:
                self.mark_attempt(item.id, exc)
                failed += 1
                continue
            self.complete(item.id)
            completed += 1
        return completed, failed
