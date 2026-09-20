from __future__ import annotations

import re
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from . import __version__
from .markdown_store import _atomic_write
from .parser import PARSER_VERSION
from .privacy import redact_text


def _safe(value: object, limit: int = 120) -> str:
    text = redact_text(str(value or ""))
    text = re.sub(r"https?://\S+", "[链接已隐藏]", text)
    text = text.replace("|", "\\|").replace("\n", " ")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "—"


def _safe_subject(value: object, limit: int = 120) -> str:
    text = re.sub(
        r"^[^，,]{1,12}[，,]\s*(?=(?:感谢|您好|恭喜|邀请))",
        "[称呼已隐藏]，",
        str(value or ""),
    )
    return _safe(text, limit)


def _time_summary(item: dict[str, object]) -> str:
    values = [
        str(item.get(name) or "")
        for name in ("start_at", "end_at", "deadline_at")
        if item.get(name)
    ]
    return " / ".join(values) or "—"


def _diagnostic_summary(item: dict[str, object]) -> str:
    diagnostics = item.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return "—"
    return ", ".join(
        f"{key}={value}"
        for key, value in sorted(diagnostics.items())
    )


def export_identity_preview(summary, path: Path) -> Path:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    parser_source = Path(__file__).with_name("parser.py")
    parser_sha256 = sha256(parser_source.read_bytes()).hexdigest()
    lines = [
        "---",
        "title: JobMailDesk Identity Resolver 人工验收预览",
        "type: jobmaildesk-identity-preview",
        f"generated_at: {generated_at}",
        f"app_version: {__version__}",
        f"parser_version: {PARSER_VERSION}",
        f"parser_sha256: {parser_sha256}",
        "readonly: true",
        "---",
        "",
        "# Identity Resolver 人工验收预览",
        "",
        "> 本文件由只读邮箱影子扫描生成，不修改任务、扫描状态或邮件。",
        "> 仅包含结构化字段，不包含邮件正文、发件人地址、私人链接或认证参数。",
        "",
        "## 汇总",
        "",
        f"- 搜索 UID：{getattr(summary, 'searched', summary.fetched)}",
        f"- 获取邮件：{summary.fetched}",
        f"- 安全重试：{getattr(summary, 'fetch_failed', 0)}",
        f"- 招聘候选：{summary.candidates}",
        f"- 唯一归属：{summary.identity_matched}",
        f"- 建议新申请：{summary.identity_new_applications}",
        f"- 待归属：{summary.identity_unresolved}",
        f"- 硬冲突：{summary.identity_conflicts}",
        f"- 解析失败：{summary.parse_failed}",
        "",
        "## 全量逐条判断",
        "",
        "| # | UID | 收件时间 | 发件显示名 | 标题 | 解析 | 重复于 | 公司 | 岗位 | 地点 | 项目 | 阶段 | 类型 | 时间 | 归属 | 原因 |",
        "| ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, item in enumerate(summary.preview, start=1):
        lines.append(
            "| "
            + " | ".join(
                (
                    str(index),
                    _safe(item.get("uid"), 20),
                    _safe(item.get("received_at"), 30),
                    _safe(item.get("sender_display_name"), 50),
                    _safe_subject(item.get("subject"), 120),
                    _safe(item.get("parser_status"), 30),
                    _safe(item.get("semantic_duplicate_of"), 12),
                    _safe(item.get("company"), 50),
                    _safe(item.get("role"), 80),
                    _safe(item.get("location"), 40),
                    _safe(item.get("project"), 50),
                    _safe(item.get("stage"), 30),
                    _safe(item.get("event_type"), 24),
                    _safe(_time_summary(item), 90),
                    _safe(item.get("identity_action"), 30),
                    _safe(item.get("resolution_reason"), 40),
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## 字段证据与完整性",
            "",
            "| # | 公司置信度/来源 | 岗位置信度/来源 | 行动链接 | 预计时长 | 安全诊断 |",
            "| ---: | --- | --- | --- | --- | --- |",
        )
    )
    for index, item in enumerate(summary.preview, start=1):
        lines.append(
            "| "
            + " | ".join(
                (
                    str(index),
                    _safe(
                        f"{item.get('company_confidence') or 0} / "
                        f"{item.get('company_source') or '—'}",
                        80,
                    ),
                    _safe(
                        f"{item.get('role_confidence') or 0} / "
                        f"{item.get('role_source') or '—'}",
                        80,
                    ),
                    "是" if item.get("has_action_link") else "否",
                    _safe(item.get("duration_minutes"), 20),
                    _safe(_diagnostic_summary(item), 180),
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## 人工验收重点",
            "",
            "- `matched` / `batch_context_match` 是否指向正确申请。",
            "- `unresolved` 是否确实缺少唯一身份，不能自动判断。",
            "- `new_application` 是否真的是新的独立投递。",
            "- JDS/TET、雷火/互娱以及不同职位编号是否保持分离。",
            "",
            "确认前不要启用正式 Registry 写入或 unresolved 持久化。",
            "",
        )
    )
    _atomic_write(path, "\n".join(lines))
    return path
