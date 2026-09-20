from pathlib import Path

import pytest

from job_mail_desk.file_transaction import FileTransaction, recover_file_transactions


def test_file_transaction_rolls_back_all_fact_directories(tmp_path: Path) -> None:
    applications = tmp_path / "applications"
    tasks = tmp_path / "tasks"
    unresolved = tmp_path / "unresolved"
    for directory in (applications, tasks, unresolved):
        directory.mkdir()
        (directory / "original.md").write_text(directory.name, encoding="utf-8")

    with pytest.raises(RuntimeError):
        with FileTransaction(
            tmp_path / ".transactions",
            (applications, tasks, unresolved),
        ):
            (applications / "original.md").write_text("changed", encoding="utf-8")
            (tasks / "new.md").write_text("new", encoding="utf-8")
            (unresolved / "original.md").unlink()
            raise RuntimeError("injected write failure")

    assert (applications / "original.md").read_text(encoding="utf-8") == "applications"
    assert not (tasks / "new.md").exists()
    assert (unresolved / "original.md").read_text(encoding="utf-8") == "unresolved"


def test_startup_recovers_prepared_uncommitted_transaction(tmp_path: Path) -> None:
    facts = tmp_path / "facts"
    facts.mkdir()
    target = facts / "record.md"
    target.write_text("before", encoding="utf-8")
    root = tmp_path / ".transactions"
    transaction = FileTransaction(root, (facts,))
    transaction.__enter__()
    target.write_text("after", encoding="utf-8")

    assert recover_file_transactions(root) == 1
    assert target.read_text(encoding="utf-8") == "before"
