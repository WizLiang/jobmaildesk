"""Validate unsaved mailbox fields without reading the credential store."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile

from .credentials import MailCredential, _validate_code


def credential_from_form(
    email: str,
    authorization_code: str,
    existing: MailCredential | None,
    *,
    required: bool = False,
) -> MailCredential | None:
    email = email.strip()
    authorization_code = authorization_code.strip()
    if not authorization_code:
        if existing is not None:
            if email != existing.email.strip():
                raise ValueError("更换邮箱账号时，请填写新邮箱的客户端授权码；留空只会保留原账号。")
            return existing
        if email or required:
            raise ValueError("请填写邮箱账号及对应的客户端授权码。")
        return None
    if "@" not in email or not email.isascii():
        raise ValueError("请输入完整邮箱地址。")
    return MailCredential(email=email, authorization_code=_validate_code(authorization_code))


@contextmanager
def config_rollback_on_mail_save_failure(path: Path):
    """Restore exact config bytes if config or secure credential writing fails.

    The caller writes configuration first, then the credential store. No old
    credential is read, copied, deleted, or written to the recovery file.
    """
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        original = None
    backup = None
    if original is not None:
        with tempfile.NamedTemporaryFile(
            # The config-owned temporary prefix is included in privacy reset.
            prefix=".config-mail-settings-", suffix=".tmp", dir=path.parent, delete=False,
        ) as stream:
            backup = Path(stream.name)
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
    try:
        yield
    except Exception as save_error:
        try:
            if backup is not None:
                os.replace(backup, path)
            else:
                path.unlink(missing_ok=True)
        except OSError as restore_error:
            recovery = "原配置备份保留在数据目录的 .config-mail-settings-*.tmp 文件中。" if backup else "请检查数据目录中的配置文件。"
            raise RuntimeError(
                "邮箱设置保存失败，且无法恢复原配置；当前运行设置未改变。" + recovery
            ) from restore_error
        raise RuntimeError("邮箱设置保存失败，已恢复原配置；当前运行设置未改变。") from save_error
    else:
        if backup is not None:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                # A stale config-only recovery file must not turn an applied
                # account change into a misleading save failure.
                pass
