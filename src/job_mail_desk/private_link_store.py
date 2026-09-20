from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import keyring
from cryptography.fernet import Fernet, InvalidToken
from keyring.errors import KeyringError

from .markdown_store import _atomic_write


SERVICE = "job-mail-desk.private-links"
USERNAME = "default-key"


class PrivateLinkStore:
    """Encrypt private action URLs and expose only opaque references."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def _cipher() -> Fernet:
        try:
            key = keyring.get_password(SERVICE, USERNAME)
            if not key:
                key = Fernet.generate_key().decode("ascii")
                keyring.set_password(SERVICE, USERNAME, key)
        except KeyringError as exc:
            raise RuntimeError("无法访问系统安全凭据库以保护邮件链接。") from exc
        return Fernet(key.encode("ascii"))

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return {
            str(key): str(value)
            for key, value in payload.get("links", {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def _save(self, links: dict[str, str]) -> None:
        _atomic_write(
            self.path,
            json.dumps(
                {"schema": 1, "links": links},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )

    def put(self, source_hash: str, url: str | None) -> str | None:
        if not url:
            return None
        reference = "pl1_" + sha256(
            f"{source_hash}\0{url}".encode("utf-8")
        ).hexdigest()[:24]
        links = self._load()
        links[reference] = self._cipher().encrypt(url.encode("utf-8")).decode("ascii")
        self._save(links)
        return reference

    def get(self, reference: str | None) -> str | None:
        if not reference:
            return None
        token = self._load().get(reference)
        if not token:
            return None
        try:
            return self._cipher().decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError):
            return None
