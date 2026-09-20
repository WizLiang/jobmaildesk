"""Version changes are bounded to project metadata and fail before partial edits."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("version_management", PROJECT / "scripts" / "version.py")
assert SPEC and SPEC.loader
versions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(versions)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src" / "job_mail_desk").mkdir(parents=True)
    files = {
        "pyproject.toml": '[project]\nname = "job-mail-desk"\nversion = "0.7.0rc2" # keep\n[tool.example]\nversion = "0.7.0rc2"\n',
        "src/job_mail_desk/__init__.py": '"""求职纸片。"""\n\n__version__ = "0.7.0rc2"\n',
        "uv.lock": 'version = 1\n\n[[package]]\nname = "other"\nversion = "0.7.0rc2"\n\n[[package]]\nname = "job-mail-desk"\nversion = "0.7.0rc2"\nsource = { editable = "." }\n\n[package.metadata]\nnote = "0.7.0rc2"\n',
    }
    for name, text in files.items():
        (tmp_path / name).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    (tmp_path / "CHANGELOG.md").write_text("Historical 0.7.0rc2", encoding="utf-8")
    return tmp_path


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_version_set_preserves_dependencies_comments_history_and_crlf(project):
    before = snapshot(project)
    assert versions.check(project) == "0.7.0rc2"
    assert versions.set_version("0.7.0rc3", project) == "0.7.0rc3"
    after = snapshot(project)
    assert versions.check(project) == "0.7.0rc3"
    assert after.keys() == before.keys()
    assert after["CHANGELOG.md"] == before["CHANGELOG.md"]
    for name in versions.VERSION_FILES:
        assert after[name].count(b"0.7.0rc3") == 1
        assert after[name].replace(b"0.7.0rc3", b"0.7.0rc2") == before[name]
    assert versions.set_version("0.7.0rc3", project) == "0.7.0rc3"
    assert snapshot(project) == after


@pytest.mark.parametrize("target", ["v0.7.0rc3", "0.7", "0.7.0-rc3", "0.7.0RC3", "0.7.0rc03",
                                   "0.7.00", "0.7.0rc3\n", "0.7.0+build", "0.7.0rc16384", "65536.0.0",
                                   "0.7.0rc1", "0.7.0b9", "0.6.9"])
def test_invalid_or_lower_target_never_writes(project, target):
    before = snapshot(project)
    with pytest.raises(ValueError):
        versions.set_version(target, project)
    assert snapshot(project) == before


@pytest.mark.parametrize(("name", "suffix"), [
    ("pyproject.toml", '\n[project]\nversion = "0.7.0rc2"\n'),
    ("src/job_mail_desk/__init__.py", '\n__version__ = "0.7.0rc2"\n'),
    ("src/job_mail_desk/__init__.py", '\nif True:\n    __version__ = "0.7.0rc2"\n'),
    ("uv.lock", '\n[[package]]\nname = "job-mail-desk"\nversion = "0.7.0rc2"\nsource = { editable = "." }\n'),
])
def test_duplicate_project_fields_never_write(project, name, suffix):
    with (project / name).open("a", encoding="utf-8") as stream:
        stream.write(suffix)
    before = snapshot(project)
    with pytest.raises(ValueError):
        versions.set_version("0.7.0rc3", project)
    assert snapshot(project) == before


def test_inconsistent_existing_versions_never_write(project):
    module = project / "src/job_mail_desk/__init__.py"
    module.write_bytes(module.read_bytes().replace(b"0.7.0rc2", b"0.7.0rc1"))
    before = snapshot(project)
    with pytest.raises(ValueError, match="version mismatch"):
        versions.set_version("0.7.0rc3", project)
    assert snapshot(project) == before


def test_replacement_failure_restores_already_replaced_files(project, monkeypatch):
    before = snapshot(project)
    replace = versions.os.replace
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("synthetic locked file")
        return replace(source, target)

    monkeypatch.setattr(versions.os, "replace", fail_second)
    with pytest.raises(PermissionError, match="locked file"):
        versions.set_version("0.7.0rc3", project)
    assert snapshot(project) == before


def test_staging_failure_leaves_originals_intact(project, monkeypatch):
    before = snapshot(project)
    stage = versions._stage
    calls = 0

    def fail_third(path, content):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("synthetic disk full")
        return stage(path, content)

    monkeypatch.setattr(versions, "_stage", fail_third)
    with pytest.raises(OSError, match="disk full"):
        versions.set_version("0.7.0rc3", project)
    assert snapshot(project) == before


def test_rollback_failure_preserves_original_backup_for_recovery(project, monkeypatch):
    before = snapshot(project)
    replace = versions.os.replace
    calls = 0

    def fail_update_and_rollback(source, target):
        nonlocal calls
        calls += 1
        if calls in (2, 3):
            raise PermissionError("synthetic locked file during rollback")
        return replace(source, target)

    monkeypatch.setattr(versions.os, "replace", fail_update_and_rollback)
    with pytest.raises(OSError, match="original backups retained at") as failure:
        versions.set_version("0.7.0rc3", project)
    backups = list(project.glob(".pyproject.toml.version-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before["pyproject.toml"]
    assert str(backups[0]) in str(failure.value)
    assert (project / "src/job_mail_desk/__init__.py").read_bytes() == before["src/job_mail_desk/__init__.py"]
    assert (project / "uv.lock").read_bytes() == before["uv.lock"]


def test_final_cannot_be_downgraded_to_prerelease(project):
    versions.set_version("0.7.0", project)
    before = snapshot(project)
    with pytest.raises(ValueError, match="rollback"):
        versions.set_version("0.7.0rc16383", project)
    assert snapshot(project) == before
    assert versions.set_version("0.7.1a0", project) == "0.7.1a0"


def test_check_cli_is_read_only_and_works_outside_project(tmp_path):
    result = subprocess.run([sys.executable, "-B", str(PROJECT / "scripts/version.py"), "check"],
                            cwd=tmp_path, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == versions.check(PROJECT)
    assert result.stderr == ""
