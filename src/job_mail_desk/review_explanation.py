"""User-facing explanations derived from existing, redacted identity metadata."""
from __future__ import annotations


def _source_label(source: str | None, value: str | None) -> str:
    if not value:
        return "存在冲突" if source and "conflicting" in source else "尚未识别"
    source = source or ""
    labels = []
    if "local-confirmed-correction" in source:
        labels.append("本机纠错规则")
    if "subject-" in source:
        labels.append("邮件标题")
    if "body-" in source:
        labels.append("邮件正文")
    if "sender" in source:
        labels.append("发件来源")
    if "reviewed" in source or "dictionary" in source:
        labels.append("词典匹配")
    return "、".join(labels) or "邮件结构化识别"


def explain_review(record) -> dict[str, str]:
    conflicts = [label for label, source in (
        ("公司", record.company_source), ("岗位", record.role_source),
    ) if source and "conflicting" in source]
    missing = [label for label, value in (
        ("公司", record.company), ("岗位", record.role),
    ) if not value and label not in conflicts]
    if conflicts:
        reason = "、".join(conflicts) + "信息有冲突，请核对原邮件。"
    elif record.reason in {"multiple-candidates", "strong-identifier-not-unique"}:
        reason = "找到多个可能的申请，请选择这封邮件的归属。"
    elif record.reason == "hard-identity-conflict":
        reason = "与已有申请信息不一致，请核对岗位、地点或招聘项目。"
    elif missing:
        reason = "尚未找到明确的" + "、".join(missing) + "，请补充后确认。"
    elif record.resolution_status == "new_application":
        reason = "公司和岗位已识别，请确认是否创建新申请。"
    else:
        reason = "公司和岗位已识别，请核对申请归属后确认。"
    return {
        "reason_label": reason,
        "identity_sources": (
            f"公司：{_source_label(record.company_source, record.company)}；"
            f"岗位：{_source_label(record.role_source, record.role)}"
        ),
    }
