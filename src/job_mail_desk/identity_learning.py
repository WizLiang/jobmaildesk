from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

from .markdown_store import _atomic_write
from .models import ParsedEvent
from .normalization import canonical_company, canonical_role, role_key


LEARNING_SCHEMA = 1


@dataclass
class IdentityLearningRule:
    id: str
    sender_scope_hash: str
    subject_shape_hash: str
    original_company: str | None
    original_role: str | None
    corrected_company: str | None
    corrected_role: str | None
    created_at: str
    enabled: bool = True
    conflict: bool = False


class IdentityLearningStore:
    """Privacy-safe, local correction rules scoped to one sender/template."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.rules_path = directory / "identity-learning.json"
        self.key_path = directory / "identity-learning.key"

    def _key(self) -> bytes:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            return bytes.fromhex(self.key_path.read_text(encoding="ascii").strip())
        key = secrets.token_bytes(32)
        _atomic_write(self.key_path, key.hex() + "\n")
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass
        return key

    def _digest(self, value: str) -> str:
        return hmac.new(
            self._key(),
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _normalized_sender(sender: str) -> str:
        display, address = parseaddr(sender)
        return "|".join(
            (
                re.sub(r"\s+", "", display).casefold(),
                address.strip().casefold(),
            )
        )

    @staticmethod
    def _subject_shape(
        subject: str,
        company: str | None,
        role: str | None,
    ) -> str:
        shaped = subject
        for value in (company, role):
            if value:
                shaped = re.sub(re.escape(value), "{identity}", shaped, flags=re.I)
        shaped = re.sub(r"20\d{2}|(?<!\d)\d{2}届", "{year}", shaped)
        shaped = re.sub(r"\d+", "{number}", shaped)
        return re.sub(r"\s+", "", shaped).casefold()

    def scope(
        self,
        *,
        sender: str,
        subject: str,
        company: str | None,
        role: str | None,
    ) -> tuple[str, str]:
        return (
            self._digest(self._normalized_sender(sender)),
            self._digest(self._subject_shape(subject, company, role)),
        )

    def all(self) -> list[IdentityLearningRule]:
        if not self.rules_path.exists():
            return []
        try:
            payload = json.loads(self.rules_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        if payload.get("schema") != LEARNING_SCHEMA:
            return []
        return [
            IdentityLearningRule(**item)
            for item in payload.get("rules", [])
            if isinstance(item, dict)
        ]

    def _save(self, rules: list[IdentityLearningRule]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            self.rules_path,
            json.dumps(
                {
                    "schema": LEARNING_SCHEMA,
                    "rules": [asdict(rule) for rule in rules],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    @staticmethod
    def _same_value(left: str | None, right: str | None, *, role: bool) -> bool:
        if not left and not right:
            return True
        if not left or not right:
            return False
        if role:
            return role_key(canonical_role(left) or left) == role_key(
                canonical_role(right) or right
            )
        return (canonical_company(left) or left).casefold() == (
            canonical_company(right) or right
        ).casefold()

    @staticmethod
    def _coarsens_value(
        original: str | None,
        corrected: str | None,
        *,
        role: bool,
    ) -> bool:
        if not original or not corrected:
            return False
        if role:
            original_key = role_key(canonical_role(original) or original)
            corrected_key = role_key(canonical_role(corrected) or corrected)
        else:
            original_key = role_key(canonical_company(original) or original)
            corrected_key = role_key(canonical_company(corrected) or corrected)
        return bool(
            original_key != corrected_key
            and len(corrected_key) >= 2
            and corrected_key in original_key
        )

    @classmethod
    def _coarsens_identity(
        cls,
        original_company: str | None,
        original_role: str | None,
        corrected_company: str | None,
        corrected_role: str | None,
    ) -> bool:
        return cls._coarsens_value(
            original_company,
            corrected_company,
            role=False,
        ) or cls._coarsens_value(
            original_role,
            corrected_role,
            role=True,
        )

    def enrich(
        self,
        event: ParsedEvent,
        *,
        sender: str,
        subject: str,
    ) -> ParsedEvent:
        sender_hash, shape_hash = self.scope(
            sender=sender,
            subject=subject,
            company=event.company,
            role=event.role,
        )
        matches = [
            rule
            for rule in self.all()
            if rule.enabled
            and not rule.conflict
            and rule.sender_scope_hash == sender_hash
            and rule.subject_shape_hash == shape_hash
            and self._same_value(
                rule.original_company,
                event.company,
                role=False,
            )
            and self._same_value(rule.original_role, event.role, role=True)
            and not self._coarsens_identity(
                event.company,
                event.role,
                rule.corrected_company,
                rule.corrected_role,
            )
        ]
        outputs = {
            (rule.corrected_company, rule.corrected_role) for rule in matches
        }
        if len(outputs) != 1:
            return replace(
                event,
                sender_scope_hash=sender_hash,
                subject_shape_hash=shape_hash,
            )
        company, role = next(iter(outputs))
        return replace(
            event,
            company=company or event.company,
            role=role or event.role,
            role_canonical=role or event.role_canonical,
            company_confidence=0.99 if company else event.company_confidence,
            company_source=(
                "local-confirmed-correction" if company else event.company_source
            ),
            role_confidence=0.99 if role else event.role_confidence,
            role_source="local-confirmed-correction" if role else event.role_source,
            sender_scope_hash=sender_hash,
            subject_shape_hash=shape_hash,
        )

    def learn(
        self,
        *,
        sender_scope_hash: str | None,
        subject_shape_hash: str | None,
        original_company: str | None,
        original_role: str | None,
        corrected_company: str | None,
        corrected_role: str | None,
    ) -> IdentityLearningRule | None:
        if not sender_scope_hash or not subject_shape_hash:
            return None
        if self._coarsens_identity(
            original_company,
            original_role,
            corrected_company,
            corrected_role,
        ):
            return None
        company_changed = not self._same_value(
            original_company,
            corrected_company,
            role=False,
        )
        role_changed = not self._same_value(
            original_role,
            corrected_role,
            role=True,
        )
        if not company_changed and not role_changed:
            return None
        rule_seed = "|".join(
            (
                sender_scope_hash,
                subject_shape_hash,
                original_company or "",
                original_role or "",
                corrected_company or "",
                corrected_role or "",
            )
        )
        new_rule = IdentityLearningRule(
            id=hashlib.sha256(rule_seed.encode("utf-8")).hexdigest()[:24],
            sender_scope_hash=sender_scope_hash,
            subject_shape_hash=subject_shape_hash,
            original_company=original_company,
            original_role=original_role,
            corrected_company=corrected_company if company_changed else None,
            corrected_role=corrected_role if role_changed else None,
            created_at=datetime.now().astimezone().isoformat(),
        )
        rules = self.all()
        same_scope = [
            rule
            for rule in rules
            if rule.sender_scope_hash == sender_scope_hash
            and rule.subject_shape_hash == subject_shape_hash
            and self._same_value(
                rule.original_company,
                original_company,
                role=False,
            )
            and self._same_value(rule.original_role, original_role, role=True)
        ]
        conflicts = [
            rule
            for rule in same_scope
            if (
                rule.corrected_company,
                rule.corrected_role,
            )
            != (new_rule.corrected_company, new_rule.corrected_role)
        ]
        if conflicts:
            for rule in conflicts:
                rule.enabled = False
                rule.conflict = True
            new_rule.enabled = False
            new_rule.conflict = True
        rules = [rule for rule in rules if rule.id != new_rule.id]
        rules.append(new_rule)
        self._save(rules)
        return new_rule

    def set_enabled(self, rule_id: str, enabled: bool) -> IdentityLearningRule:
        rules = self.all()
        rule = next((item for item in rules if item.id == rule_id), None)
        if not rule:
            raise KeyError(rule_id)
        if rule.conflict and enabled:
            raise ValueError("冲突规则不能直接启用，请重新确认对应邮件。")
        rule.enabled = enabled
        self._save(rules)
        return rule

    def clear(self) -> None:
        if self.rules_path.exists():
            self.rules_path.unlink()
