from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from job_mail_desk import storage_location as storage
from job_mail_desk import bootstrap


@pytest.fixture
def migration(monkeypatch, tmp_path):
    source = tmp_path / "old" / "JobMailDesk"
    source.mkdir(parents=True)
    target_parent = tmp_path / "new"
    target_parent.mkdir()
    destination = target_parent / "JobMailDeskData"
    selected = []
    monkeypatch.setattr(storage, "exclusive_desktop", nullcontext)
    monkeypatch.setattr(storage, "wait_for_exit", lambda pid: None)
    monkeypatch.setattr(storage, "save_root", selected.append)
    (source / "tasks").mkdir()
    (source / "tasks" / "stable-id.md").write_text("id: stable-id\nstatus: done\n", encoding="utf-8")
    (source / "applications").mkdir()
    (source / "applications" / "stable-app.md").write_text("status: ENDED\n", encoding="utf-8")
    (source / "private-links.json").write_text('{"encrypted":"synthetic"}')
    (source / "logs").mkdir()
    (source / "logs" / "app.log").write_text("synthetic")
    (source / "webview2").mkdir()
    (source / "webview2" / "profile").write_text("synthetic")
    return source, destination, selected


def test_migration_preserves_facts_and_rebases_only_internal_paths(migration):
    source, destination, selected = migration
    external = source.parent / "External.md"
    external.write_text("keep")
    (source / "exports").mkdir()
    (source / "exports" / "progress.md").write_text("original")
    (source / "config.toml").write_text(
        '[obsidian]\noutput_path = ' + json.dumps(str(external)) + '\n'
        '[progress]\noutput_path = ' + json.dumps(str(source / 'exports/progress.md')) + '\n'
        'source_path = ""\n[research]\nqueue_path = ' + json.dumps(str(source / 'research-queue.jsonl')) + '\n',
        encoding="utf-8",
    )
    storage.start_move(source, destination, restart=False)
    assert not destination.exists()  # no directories created before the handoff
    assert storage.finish_move(source) == destination
    assert not source.exists()
    assert selected == [destination]
    assert (destination / "tasks/stable-id.md").read_text() == "id: stable-id\nstatus: done\n"
    assert (destination / "applications/stable-app.md").read_text() == "status: ENDED\n"
    import tomllib
    config = tomllib.loads((destination / "config.toml").read_text())
    assert config['progress']['output_path'] == str(destination / 'exports/progress.md')
    assert config['research']['queue_path'] == str(destination / 'research-queue.jsonl')
    assert config['obsidian']['output_path'] == str(external)
    assert external.read_text() == "keep"
    assert (destination / "webview2/profile").read_text() == "synthetic"


def test_copy_failure_preserves_original_and_retry_completes(migration, monkeypatch):
    source, destination, selected = migration
    original = storage.shutil.copytree
    def fail(*args, **kwargs):
        raise OSError("disk full")
    storage.start_move(source, destination, restart=False)
    monkeypatch.setattr(storage.shutil, "copytree", fail)
    with pytest.raises(OSError):
        storage.finish_move(source)
    assert (source / 'tasks/stable-id.md').exists() and not selected
    monkeypatch.setattr(storage.shutil, "copytree", original)
    storage.finish_move(source)
    assert selected == [destination]


def test_registry_failure_resumes_verified_copy_without_reimport(migration, monkeypatch):
    source, destination, selected = migration
    storage.start_move(source, destination, restart=False)
    def denied(root):
        raise PermissionError("registry denied")
    monkeypatch.setattr(storage, "save_root", denied)
    with pytest.raises(PermissionError):
        storage.finish_move(source)
    assert (source / storage.MOVE_REQUEST).exists()
    assert (destination / 'tasks/stable-id.md').exists()
    monkeypatch.setattr(storage, "save_root", selected.append)
    storage.finish_move(source)
    assert selected == [destination] and not source.exists()


def test_changed_verified_target_is_not_used(migration, monkeypatch):
    source, destination, selected = migration
    storage.start_move(source, destination, restart=False)
    original = storage.shutil.rmtree
    def fail_old_delete(path, *args, **kwargs):
        if Path(path).parent == source:
            raise PermissionError("source is locked")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(storage.shutil, "rmtree", fail_old_delete)
    with pytest.raises(PermissionError):
        storage.finish_move(source)
    (destination / 'tasks/stable-id.md').write_text('changed')
    monkeypatch.setattr(storage.shutil, "rmtree", original)
    with pytest.raises(storage.StorageLocationError, match="校验失败"):
        storage.finish_move(source)
    assert not selected
    assert (source / storage.MOVE_REQUEST).exists()


def test_nonempty_and_nested_targets_are_rejected(migration):
    source, destination, selected = migration
    with pytest.raises(storage.StorageLocationError):
        storage.start_move(source, source / 'nested', restart=False)
    destination.mkdir()
    (destination / 'keep').write_text('keep')
    with pytest.raises(storage.StorageLocationError, match="已有内容"):
        storage.start_move(source, destination, restart=False)
    assert (destination / 'keep').read_text() == 'keep'


def test_folder_directly_under_drive_is_allowed_but_drive_itself_is_not(migration):
    from job_mail_desk.privacy_reset import validate_root, PrivacyResetError
    source, destination, selected = migration
    drive = Path(source.anchor)
    child = drive / 'JobMailDeskData'
    storage.validate_move(source, child, resuming=True)
    assert validate_root(child) == child
    with pytest.raises(storage.StorageLocationError):
        storage.validate_move(source, drive)
    with pytest.raises(PrivacyResetError):
        validate_root(drive)


def test_move_blocks_writers_and_excludes_privacy_reset(migration):
    from job_mail_desk.data_lock import data_directory_lease, DataLockBusyError
    source, destination, selected = migration
    (source / '.privacy-reset.json').write_text('{}')
    with pytest.raises(storage.StorageLocationError, match="清除"):
        storage.start_move(source, destination, restart=False)
    (source / '.privacy-reset.json').unlink()
    storage.start_move(source, destination, restart=False)
    with pytest.raises(DataLockBusyError, match="迁移"):
        with data_directory_lease(source / '.data.lock'):
            pytest.fail('writer entered')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows junctions')
def test_junction_is_not_copied_or_deleted(migration, tmp_path):
    source, destination, selected = migration
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'keep').write_text('keep')
    junction = source / 'linked'
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(external)],
                   check=True, capture_output=True)
    try:
        storage.start_move(source, destination, restart=False)
        with pytest.raises(storage.StorageLocationError, match="目录联接"):
            storage.finish_move(source)
        assert (external / 'keep').read_text() == 'keep'
    finally:
        junction.rmdir()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows picker')
def test_initial_picker_cancel_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBMAILDESK_LOCAL_ROOT', raising=False)
    monkeypatch.setattr(storage, 'saved_root', lambda: None)
    monkeypatch.setattr(storage, 'legacy_root', lambda: tmp_path / 'legacy')
    monkeypatch.setattr(storage, 'exclusive_desktop', nullcontext)
    monkeypatch.setattr('job_mail_desk.storage_picker.choose_initial_root', lambda old: None)
    assert not bootstrap.prepare_storage(gui=True)
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows picker')
def test_initial_selection_creates_one_root_and_remembers_it(tmp_path, monkeypatch):
    monkeypatch.delenv('JOBMAILDESK_LOCAL_ROOT', raising=False)
    destination = tmp_path / 'selected' / 'JobMailDeskData'
    destination.parent.mkdir()
    remembered = []
    monkeypatch.setattr(storage, 'saved_root', lambda: None)
    monkeypatch.setattr(storage, 'legacy_root', lambda: tmp_path / 'legacy')
    monkeypatch.setattr(storage, 'exclusive_desktop', nullcontext)
    monkeypatch.setattr(storage, 'save_root', remembered.append)
    monkeypatch.setattr('job_mail_desk.storage_picker.choose_initial_root', lambda old: destination)
    assert bootstrap.prepare_storage(gui=True)
    assert remembered == [destination] and destination.is_dir()
    assert not (tmp_path / 'legacy').exists()


def test_isolated_test_root_does_not_read_or_change_normal_selection(monkeypatch, tmp_path):
    monkeypatch.setenv('JOBMAILDESK_LOCAL_ROOT', str(tmp_path / 'isolated'))
    def forbidden():
        pytest.fail('registry should not be read in isolated mode')
    monkeypatch.setattr(storage, 'saved_root', forbidden)
    assert bootstrap.prepare_storage(gui=True)
    assert storage.selected_root() == tmp_path / 'isolated'


def test_bootstrap_import_does_not_bind_configuration():
    result = subprocess.run([sys.executable, '-B', '-c',
        "import sys; import job_mail_desk.bootstrap; assert 'job_mail_desk.config' not in sys.modules"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
