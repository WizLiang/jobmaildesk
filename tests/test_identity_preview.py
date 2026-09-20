from types import SimpleNamespace

from job_mail_desk.identity_preview import export_identity_preview


def test_identity_preview_export_is_local_and_private(tmp_path) -> None:
    summary = SimpleNamespace(
        fetched=1,
        candidates=1,
        identity_matched=0,
        identity_new_applications=0,
        identity_unresolved=1,
        identity_conflicts=0,
        parse_failed=0,
        preview=(
            {
                "uid": "1",
                "received_at": "2026-08-15T11:14:52+08:00",
                "subject": "张三，感谢您投递样例公司岗位",
                "parser_status": "candidate",
                "company": "样例公司",
                "role": "联系 candidate@example.com 13800138000",
                "project": None,
                "stage": "网申",
                "identity_action": "unresolved",
                "resolution_reason": "查看 https://example.com/private?token=secret",
                "application_key": None,
                "diagnostics": {
                    "subject_length": 18,
                    "body_length": 120,
                    "explicit_application_subject": True,
                },
            },
        ),
    )
    path = export_identity_preview(summary, tmp_path / "preview.md")
    content = path.read_text(encoding="utf-8")
    assert "candidate@example.com" not in content
    assert "13800138000" not in content
    assert "https://" not in content
    assert "secret" not in content
    assert "张三" not in content
    assert "[称呼已隐藏]" in content
    assert "body_length=120" in content
    assert "app_version:" in content
    assert "parser_version: 2026.09.20.1" in content
    assert "parser_sha256:" in content
    assert "unresolved" in content
