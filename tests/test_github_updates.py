from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import zipfile

import pytest

from job_mail_desk import github_updates as updates
from job_mail_desk.config import Settings, load_settings, settings_from_payload, write_settings


def release(version="0.7.0rc2", *, preview=True):
    name = f"JobMailDesk-Core-v{version}-win-x64.zip"
    base = f"https://github.com/{updates.REPOSITORY}/releases/download/v{version}/"
    return {"tag_name": f"v{version}", "prerelease": preview, "body": "<script>not HTML</script>",
            "assets": [{"name": n, "browser_download_url": base + n} for n in (name, name + ".sha256")]}


def archive(path, extra=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("JobMailDesk/JobMailDesk.exe", b"MZ-new")
        z.writestr("JobMailDesk/JobMailDesk-cli.exe", b"MZ-cli")
        z.writestr("JobMailDesk/JobMailDesk.ico", b"icon")
        z.writestr("JobMailDesk/_internal/library.txt", b"library")
        z.writestr("QUICKSTART.zh-CN.md", "Public instructions")
        for name, content in extra or []:
            info = zipfile.ZipInfo(name)
            info.filename = name  # Do not let Windows normalize the hostile separator.
            z.writestr(info, content)
    return path


def installation(path):
    path.mkdir()
    (path / "_internal").mkdir()
    for name in updates.REQUIRED - {"_internal"}:
        (path / name).write_bytes(b"MZ-old")
    return path


def wait(service):
    deadline = time.monotonic() + 3
    while service.snapshot()["busy"] and time.monotonic() < deadline:
        time.sleep(.01)
    assert not service.snapshot()["busy"]
    return service.snapshot()


def test_release_selection_orders_versions_and_honors_channels():
    stable = release("0.7.0", preview=False)
    assert updates.select_release([release(), stable], "0.7.0rc1", "preview").version == "0.7.0"
    assert updates.select_release([release()], "0.7.0rc1", "stable") is None
    assert updates.select_release([stable], "0.7.0", "preview") is None
    assert updates.select_release([release("0.7.0rc10"), release()], "0.7.0rc1", "preview").version == "0.7.0rc10"


def test_untrusted_incomplete_or_draft_releases_are_not_installable():
    bad = release()
    bad["assets"][0]["browser_download_url"] = "https://evil.example/installer.zip"
    assert updates.select_release([bad], "0.7.0rc1", "preview") is None
    bad = release()
    bad["assets"].pop()
    assert updates.select_release([bad], "0.7.0rc1", "preview") is None
    assert updates.select_release([dict(release(), draft=True)], "0.7.0rc1", "preview") is None


@pytest.mark.parametrize("url", ["http://github.com/WizLiang/jobmaildesk/releases/download/v1/x", "https://github.com.evil.example/x", "https://github.com/another/repo/releases/download/v1/x", "https://user@github.com/WizLiang/jobmaildesk/releases/download/v1/x", "https://localhost/x", "file:///tmp/update.zip"])
def test_network_allowlist_rejects_other_destinations(url):
    assert not updates.allowed_url(url, redirected=True)


def test_asset_redirects_only_allow_github_cdn():
    assert updates.allowed_url("https://release-assets.githubusercontent.com/path?signature=placeholder", redirected=True)
    assert not updates.allowed_url("https://release-assets.githubusercontent.com/path")
    with pytest.raises(updates.UpdateError):
        updates._SafeRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example/file")


def test_checksum_rejects_corruption_and_wrong_filename(tmp_path):
    path = archive(tmp_path / "update.zip")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    checksum = f"{digest}  update.zip\n".encode()
    updates.verify_checksum(path, checksum, "update.zip")
    with pytest.raises(updates.UpdateError):
        updates.verify_checksum(path, checksum, "other.zip")
    path.write_bytes(b"tampered")
    with pytest.raises(updates.UpdateError):
        updates.verify_checksum(path, checksum, "update.zip")


@pytest.mark.parametrize("name", ["../escape", "JobMailDesk/../../escape", "C:/escape", "JobMailDesk/x:stream", "JobMailDesk/CON.txt", "JobMailDesk/x. /file", "JobMailDesk\\escape", "JobMailDesk/JobMailDesk.EXE"])
def test_zip_rejects_windows_path_attacks(tmp_path, name):
    path = archive(tmp_path / "bad.zip", [(name, "bad")])
    with pytest.raises(updates.UpdateError):
        updates.unpack_bundle(path, tmp_path / "extracted")
    assert not (tmp_path / "escape").exists()


def test_zip_rejects_symlinks_and_excessive_unpacked_size(tmp_path, monkeypatch):
    path = archive(tmp_path / "bad.zip")
    link = zipfile.ZipInfo("JobMailDesk/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "a") as z:
        z.writestr(link, "outside")
    with pytest.raises(updates.UpdateError):
        updates.unpack_bundle(path, tmp_path / "out")
    path = archive(tmp_path / "large.zip")
    monkeypatch.setattr(updates, "MAX_UNPACKED", 1)
    with pytest.raises(updates.UpdateError):
        updates.unpack_bundle(path, tmp_path / "large")


def test_preparation_preserves_extra_files_and_never_touches_data(tmp_path):
    install = installation(tmp_path / "program")
    data = tmp_path / "data"
    data.mkdir()
    (data / "task.md").write_text("private user task")
    (install / "README.txt").write_text("personal note")
    stage, backup = updates.prepare_install(archive(tmp_path / "update.zip"), install, data)
    assert (stage / "JobMailDesk.exe").read_bytes() == b"MZ-new"
    assert (stage / "README.txt").read_text() == "personal note"
    assert (install / "JobMailDesk.exe").read_bytes() == b"MZ-old"
    assert (data / "task.md").read_text() == "private user task"
    assert not backup.exists()


def test_installation_with_embedded_data_or_unknown_folders_is_refused(tmp_path):
    install = installation(tmp_path / "program")
    with pytest.raises(updates.UpdateError):
        updates.validate_installation(install, install / "JobMailDeskData")
    (install / "personal-documents").mkdir()
    with pytest.raises(updates.UpdateError):
        updates.validate_installation(install, tmp_path / "data")


def test_background_check_is_nonblocking_and_automatic_check_is_throttled(tmp_path, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    calls = []
    def read(url, limit, **kwargs):
        calls.append(url)
        entered.set()
        assert finish.wait(2)
        return json.dumps([release()]).encode()
    monkeypatch.setattr(updates, "read_url", read)
    service = updates.UpdateService(tmp_path, "0.7.0rc1")
    service.check(automatic=True)
    assert entered.wait(1)
    assert service.snapshot()["busy"]
    service.check()
    finish.set()
    assert wait(service)["status"] == "available"
    service.check(automatic=True)
    assert len(calls) == 1
    assert service.snapshot()["notes"] == "<script>not HTML</script>"


def test_download_failure_discards_corrupt_file_and_cannot_install(tmp_path, monkeypatch):
    service = updates.UpdateService(tmp_path, "0.7.0rc1")
    service._release = updates.select_release([release()], "0.7.0rc1", "preview")
    def read(url, limit, *, destination=None, progress=None):
        if destination:
            destination.write_bytes(b"corrupt")
            return b""
        return ("0" * 64 + "  " + service._release.archive_name).encode()
    monkeypatch.setattr(updates, "read_url", read)
    service.download()
    assert wait(service)["status"] == "error"
    assert not list((tmp_path / "updates").glob("*.zip"))
    assert service._archive is None
    with pytest.raises(updates.UpdateError):
        service.install(False, lambda: None)


def test_update_opt_in_roundtrip_and_old_setting_does_not_enable_new_service(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[updates]\nenabled=true\nchannel="preview"\n')
    settings = load_settings(path)
    assert not settings.github_updates_enabled and not settings.updates_enabled
    changed = settings_from_payload(settings, {"github_updates_enabled": True})
    write_settings(changed, path)
    assert load_settings(path).github_updates_enabled
    assert not load_settings(path).updates_enabled


def test_clear_data_waits_for_update_and_auto_check_is_opt_in(tmp_path):
    from job_mail_desk.ui_app import DesktopApi
    from job_mail_desk.runtime_control import RuntimeControl
    api = object.__new__(DesktopApi)
    api._privacy_reset_lock = threading.Lock()
    api._runtime_control = RuntimeControl()
    api._settings = Settings()
    api._on_privacy_reset = lambda: pytest.fail("Must not exit")
    api._github_updater = SimpleNamespace(snapshot=lambda: {"busy": True})
    with pytest.raises(ValueError, match="更新"):
        api.clear_personal_information("清除")
    if sys.platform == "win32":
        assert api.check_github_update(automatic=True) == {"busy": True}


def test_cached_status_cannot_break_startup(tmp_path):
    (tmp_path / "updates").mkdir()
    (tmp_path / "updates/install-result.json").write_text("[]")
    assert updates.UpdateService(tmp_path).snapshot()["status"] == "idle"


def test_failed_automatic_check_can_retry_without_waiting_a_day(tmp_path, monkeypatch):
    attempts = []
    def read(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("temporary network failure")
        return json.dumps([release()]).encode()
    monkeypatch.setattr(updates, "read_url", read)
    service = updates.UpdateService(tmp_path, "0.7.0rc1")
    service.check(automatic=True)
    assert wait(service)["status"] == "error"
    assert not (tmp_path / "updates/last-check.json").exists()
    service.check(automatic=True)
    assert wait(service)["status"] == "available"
    assert len(attempts) == 2


def test_successful_check_restores_public_release_after_restart(tmp_path, monkeypatch):
    public_release = dict(release(), extra="must not persist")
    calls = []
    def read(*args, **kwargs):
        calls.append(True)
        return json.dumps([public_release]).encode()
    monkeypatch.setattr(updates, "read_url", read)
    service = updates.UpdateService(tmp_path, "0.7.0rc1")
    service.check(automatic=True)
    assert wait(service)["status"] == "available"
    assert "must not persist" not in (tmp_path / "updates/last-check.json").read_text()
    restarted = updates.UpdateService(tmp_path, "0.7.0rc1")
    restored = restarted.check(automatic=True)
    assert restored["status"] == "available"
    assert restored["latest_version"] == "0.7.0rc2"
    assert restarted._release.archive_url == service._release.archive_url
    assert calls == [True]
    # The same cached metadata cannot offer a downgrade after upgrading.
    upgraded = updates.UpdateService(tmp_path, "0.7.0rc2")
    assert upgraded.check(automatic=True)["status"] == "current"
    # A different channel does not reuse the preview result.
    restarted.check("stable", automatic=True)
    assert wait(restarted)["status"] == "current"
    assert len(calls) == 2


def test_cached_release_urls_are_revalidated_before_offering_install(tmp_path, monkeypatch):
    folder = tmp_path / "updates"
    folder.mkdir()
    cached_release = release()
    cached_release["assets"][0]["browser_download_url"] = "https://evil.example/update.zip"
    (folder / "last-check.json").write_text(json.dumps({"at": time.time(), "channel": "preview", "releases": [cached_release]}))
    monkeypatch.setattr(updates, "read_url", lambda *args, **kwargs: pytest.fail("Valid cache should avoid a request"))
    service = updates.UpdateService(tmp_path, "0.7.0rc1")
    assert service.check(automatic=True)["status"] == "current"
    with pytest.raises(updates.UpdateError):
        service.download()


def test_legacy_timestamp_cache_is_refreshed_and_ready_download_is_preserved(tmp_path, monkeypatch):
    folder = tmp_path / "updates"
    folder.mkdir()
    marker = folder / "last-check.json"
    marker.write_text(json.dumps({"at": time.time(), "channel": "preview"}))
    calls = []
    def read(*args, **kwargs):
        calls.append(True)
        return json.dumps([release()]).encode()
    monkeypatch.setattr(updates, "read_url", read)
    service = updates.UpdateService(tmp_path, "0.7.0rc1")
    service.check(automatic=True)
    assert wait(service)["status"] == "available"
    service._archive = folder / ("1" * 32 + ".zip")
    service._archive.write_bytes(b"previously verified")
    service._set(status="ready")
    marker.write_text(json.dumps({"at": 0, "channel": "preview", "releases": []}))
    assert service.check(automatic=True)["status"] == "ready"
    assert service._archive.read_bytes() == b"previously verified"
    assert calls == [True]


def test_download_cleanup_only_removes_owned_regular_zips(tmp_path, monkeypatch):
    folder = tmp_path / "updates"
    folder.mkdir()
    old_zip = folder / ("a" * 32 + ".zip")
    old_zip.write_bytes(b"obsolete")
    user_file = folder / "personal.zip"
    user_file.write_bytes(b"keep")
    linked_zip = folder / ("b" * 32 + ".zip")
    linked_zip.write_bytes(b"link sentinel")
    directory = folder / ("c" * 32 + ".zip")
    directory.mkdir()
    (directory / "notes.txt").write_text("keep")
    backup = tmp_path / (".jobmaildesk-backup-" + "d" * 32)
    backup.mkdir()
    (backup / "program.exe").write_bytes(b"old program")
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == linked_zip or original_is_symlink(self))
    service = updates.UpdateService(tmp_path, "0.7.0rc1")
    service._release = updates.select_release([release()], "0.7.0rc1", "preview")
    content = b"verified new bundle"
    checksum = (hashlib.sha256(content).hexdigest() + "  " + service._release.archive_name).encode()
    def read(url, limit, *, destination=None, progress=None):
        if destination:
            destination.write_bytes(content)
            return b""
        return checksum
    monkeypatch.setattr(updates, "read_url", read)
    service.download()
    assert wait(service)["status"] == "ready"
    first_download = service._archive
    service.download()
    assert wait(service)["status"] == "ready"
    assert not old_zip.exists() and not first_download.exists()
    assert service._archive.read_bytes() == content
    assert user_file.read_bytes() == b"keep"
    assert linked_zip.read_bytes() == b"link sentinel"
    assert (directory / "notes.txt").read_text() == "keep"
    assert (backup / "program.exe").read_bytes() == b"old program"


def test_download_cleanup_refuses_a_linked_cache_directory(tmp_path, monkeypatch):
    folder = tmp_path / "updates"
    folder.mkdir()
    sentinel = folder / ("a" * 32 + ".zip")
    sentinel.write_bytes(b"untouched")
    original_is_junction = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda self: self == folder or original_is_junction(self))
    updates._clean_downloads(folder)
    assert sentinel.read_bytes() == b"untouched"


def test_failed_redownload_keeps_previously_ready_bundle(tmp_path, monkeypatch):
    folder = tmp_path / "updates"
    folder.mkdir()
    previous = folder / ("a" * 32 + ".zip")
    previous.write_bytes(b"previously verified")
    service = updates.UpdateService(tmp_path, "0.7.0rc1")
    service._release = updates.select_release([release()], "0.7.0rc1", "preview")
    service._archive = previous
    service._set(status="ready")
    def read(url, limit, *, destination=None, progress=None):
        if destination:
            destination.write_bytes(b"corrupt")
            return b""
        return ("0" * 64 + "  " + service._release.archive_name).encode()
    monkeypatch.setattr(updates, "read_url", read)
    service.download()
    assert wait(service)["status"] == "ready"
    assert service._archive == previous
    assert previous.read_bytes() == b"previously verified"
    assert list(folder.glob("*.zip")) == [previous]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows update helper")
@pytest.mark.parametrize("fail_launch", [False, True])
def test_native_helper_replaces_or_rolls_back_without_changing_data(tmp_path, fail_launch):
    install = installation(tmp_path / "program")
    data = tmp_path / "data"
    data.mkdir()
    marker = data / "task.md"
    marker.write_text("untouched")
    stage, backup = updates.prepare_install(archive(tmp_path / "update.zip"), install, data)
    job = tmp_path / "job.json"
    result = tmp_path / "result.json"
    # Maximum signed PID is a synthetic sentinel, not a running application.
    job.write_text(json.dumps({"install": str(install), "stage": str(stage), "backup": str(backup), "parent_pid": 0x7FFFFFFF, "result": str(result)}))
    helper = Path(updates.__file__).with_name("apply_update.ps1")
    quote = lambda p: str(p).replace("'", "''")
    wrapper = tmp_path / "test-helper.ps1"
    wrapper.write_text("$global:updateTestCalls = 0\nfunction Start-Process { param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle)\n$global:updateTestCalls++\n" + ("if ($global:updateTestCalls -eq 1) { throw 'Simulated launch failure' }\n" if fail_launch else "") + "}\n& '" + quote(helper) + "' -JobFile '" + quote(job) + "'\nexit $LASTEXITCODE\n", encoding="ascii")
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    process = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert process.returncode == (1 if fail_launch else 0), process.stderr
    assert marker.read_text() == "untouched"
    report = json.loads(result.read_text(encoding="utf-8-sig"))
    assert report["status"] == ("failed" if fail_launch else "installed")
    assert (install / "JobMailDesk.exe").read_bytes() == (b"MZ-old" if fail_launch else b"MZ-new")
    if not fail_launch:
        assert (backup / "JobMailDesk.exe").read_bytes() == b"MZ-old"
