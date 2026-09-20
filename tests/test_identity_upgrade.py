from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from job_mail_desk import scanner
from job_mail_desk.config import Settings
from job_mail_desk.identity_dictionaries import load_identity_dictionaries
from job_mail_desk.identity_pipeline import resolve_event_batch
from job_mail_desk.markdown_store import MarkdownTaskStore
from job_mail_desk.models import MailRecord
from job_mail_desk.state import StateStore
from job_mail_desk.unresolved_store import UnresolvedStore, unresolved_from_decision


@pytest.mark.parametrize('status', ['pending', 'ignored', 'resolved'])
def test_identity_upgrade_repairs_pending_in_place_and_preserves_manual_decisions(
    tmp_path, monkeypatch, status,
):
    received = datetime.now(scanner.SHANGHAI) - timedelta(days=8)
    company = '甲辰微电子（深圳）股份有限公司'
    record = MailRecord(
        uid='7', subject=f'{company}-线上笔试通知',
        sender='services <noreply@example.invalid>', message_id='<fixture@example.invalid>',
        body='简历已通过我司“数字设计工程师-深圳岗”筛选。笔试时间：9月22日14:00。',
        received_at=received, internal_date=received,
        mailbox='INBOX', uidvalidity='1', account_fingerprint='f' * 24,
    )
    dictionaries = load_identity_dictionaries()
    event = scanner.parse_record(record, dictionaries)
    assert event is not None
    old_event = replace(event, company=None, role=None, role_raw=None, role_canonical=None,
                        company_confidence=0, company_source=None, role_confidence=0, role_source=None)
    decision = resolve_event_batch([old_event], [], dictionaries)[0]
    key = scanner._record_source_hashes(record)[0]
    reviews = UnresolvedStore(tmp_path / 'unresolved')
    old = replace(unresolved_from_decision(key, decision, record.locator(),
                  parser_version='2026.09.19.2'), status=status)
    reviews.save(old)
    state = StateStore(tmp_path / 'state.db')
    state.record_outcome(key, status, parser_version='2026.09.19.2',
                         locator=record.locator(), internal_date=received)
    run = state.begin_scan()
    state.finish_scan(run, fetched=1, candidates=1, parser_version='2026.09.19.2')
    windows = []

    class Reader:
        def __init__(self, *args, **kwargs): pass
        def fetch_since(self, days):
            windows.append(days)
            return [record]

    for name, path in {'TASKS_DIR': 'tasks', 'STATE_DB': 'state.db',
                       'DASHBOARD_FILE': 'dashboard.md', 'DICTIONARIES_DIR': 'dict'}.items():
        monkeypatch.setattr(scanner, name, tmp_path / path)
    monkeypatch.setattr(scanner, 'ensure_directories', lambda: None)
    monkeypatch.setattr(scanner, 'ImapReader', Reader)
    monkeypatch.setattr(scanner, 'load_credential', lambda: object())
    monkeypatch.setattr(scanner, '_learn_from_confirmed_records', lambda *a, **kw: {})
    scanner.scan_once(Settings(progress_enabled=False))
    assert windows == [30]
    actual = reviews.load(key)
    if status == 'pending':
        assert actual.id == old.id
        assert actual.company == company and actual.role == '数字设计工程师'
        assert actual.location == '深圳'
        assert actual.parser_version == scanner.PARSER_VERSION
        assert actual.status == 'pending'
    else:
        assert actual == old
    scanner.scan_once(Settings(progress_enabled=False))
    assert reviews.load(key) == actual
    assert len(reviews.all()) == 1
    assert MarkdownTaskStore(tmp_path / 'tasks').all() == []
