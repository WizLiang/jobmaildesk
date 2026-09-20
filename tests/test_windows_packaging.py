"""Static contracts of the Windows packaging inputs (spec, version resource, installer)."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "packaging" / "windows"))

from version_info import parse_version, render_version_info, write_version_info  # noqa: E402

from job_mail_desk import __version__, ui_app, windows_notify  # noqa: E402


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.7.0rc1", (0, 7, 0, 1)),
        ("0.7.0", (0, 7, 0, 0)),
        ("1.2.3b4", (1, 2, 3, 4)),
        ("0.6.1rc10", (0, 6, 1, 10)),
        ("2.0.0.post3", (2, 0, 0, 3)),
    ],
)
def test_version_resource_numbers(version, expected) -> None:
    assert parse_version(version) == expected


def test_version_resource_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_version("latest")


def test_version_resource_text_evaluates_like_pyinstaller_does(tmp_path) -> None:
    # PyInstaller eval()s the file inside a namespace of these constructors.
    captured: dict[str, object] = {}

    class FixedFileInfo:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    def passthrough(*args, **kwargs):
        return (args, kwargs)

    namespace = {
        "VSVersionInfo": passthrough,
        "FixedFileInfo": FixedFileInfo,
        "StringFileInfo": passthrough,
        "StringTable": passthrough,
        "StringStruct": passthrough,
        "VarFileInfo": passthrough,
        "VarStruct": passthrough,
    }
    text = render_version_info("0.7.0rc1")
    eval(compile(text, "version_info.txt", "eval"), namespace)  # noqa: S307 - trusted local text
    assert captured["filevers"] == (0, 7, 0, 1)
    assert "StringStruct('ProductVersion', '0.7.0rc1')" in text

    written = Path(write_version_info(PROJECT, tmp_path / "version_info.txt"))
    assert f"'FileVersion', '{__version__}'" in written.read_text(encoding="utf-8")


def test_spec_builds_two_executables_with_icon_version_and_utf8() -> None:
    spec = (PROJECT / "JobMailDesk.spec").read_text(encoding="utf-8")
    ast.parse(spec)
    assert 'name="JobMailDesk"' in spec and 'name="JobMailDesk-cli"' in spec
    assert "console=False" in spec and "console=True" in spec
    assert 'icon=ICON' in spec and "version=VERSION_FILE" in spec
    assert '("X utf8", None, "OPTION")' in spec
    assert 'collect_data_files("tzdata")' in spec
    assert '"_cffi_backend"' in spec
    assert '("tests/golden/mail_identity.json", "job_mail_desk/golden")' in spec
    assert "upx=False" in spec
    assert (PROJECT / "packaging" / "windows" / "JobMailDesk.ico").stat().st_size > 1000


def test_installer_script_matches_runtime_constants() -> None:
    iss = (PROJECT / "packaging" / "windows" / "JobMailDesk.iss").read_text(encoding="utf-8")
    assert f'#define MyAppMutex "{ui_app.INSTANCE_MUTEX_NAME}"' in iss
    assert ui_app.WEBVIEW2_CLIENT_KEY.split("\\", 1)[1] in iss  # same EdgeUpdate client GUID
    assert f'#define MyAppUserModelID "{windows_notify.APP_USER_MODEL_ID}"' in iss
    assert "PrivilegesRequired=lowest" in iss
    assert "DefaultDirName={localappdata}\\Programs\\{#MyAppName}" in iss
    assert "Parameters: \"show\"" in iss
    assert "{userstartup}" in iss and "Tasks: autostart" in iss
    assert "Software\\Classes\\AppUserModelId\\{#MyAppUserModelID}" in iss
    for section in ("[Setup]", "[Tasks]", "[Files]", "[Icons]", "[Registry]", "[Run]", "[Code]"):
        assert section in iss


def test_windows_scripts_are_present_and_ascii_safe() -> None:
    scripts = PROJECT / "scripts"
    # secret-scan.ps1 predates this rule and carries a Chinese placeholder in
    # its regex; every other BOM-less script must stay ASCII because Windows
    # PowerShell 5.1 may decode such files with the ANSI code page.
    for name in ("build.ps1", "package-core.ps1", "install-shortcuts.ps1", "run-ui.ps1"):
        text = (scripts / name).read_text(encoding="utf-8")
        assert text.isascii(), f"{name} must stay ASCII-only"
    assert "uv sync --frozen --group dev" in (scripts / "build.ps1").read_text(encoding="utf-8")
    assert "JobMailDesk-cli.exe" in (scripts / "package-core.ps1").read_text(encoding="utf-8")
