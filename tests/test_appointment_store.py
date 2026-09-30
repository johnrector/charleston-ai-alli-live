"""Ledger transaction/SQL contracts; no database or network is used."""

from unittest.mock import Mock

import pytest

import appointment_store as store


class FakeDatabase:
    def __init__(self, row=('key',)):
        self.row = row
        self.events = []
        self.fail_commit = False
        self.execute = Mock(side_effect=self._execute)

    def _execute(self, sql, params=None):
        self.events.append('execute')
        return Mock(fetchone=lambda: self.row)

    def __enter__(self):
        self.events.append('begin')
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.events.append('rollback' if exc else 'commit')
        if exc is None and self.fail_commit:
            raise RuntimeError('commit failed')


@pytest.fixture
def db(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(store, '_initialized', True)
    monkeypatch.setattr(store, 'connection', Mock(return_value=database))
    return database


def test_initialize_commits_before_marking_success(db, monkeypatch):
    monkeypatch.setattr(store, '_initialized', False)
    db.fail_commit = True
    with pytest.raises(RuntimeError, match='commit failed'):
        store.initialize()
    assert not store._initialized
    db.fail_commit = False
    store.initialize()
    assert store._initialized
    assert db.events[-1] == 'commit'
    assert 'pg_advisory_xact_lock' in db.execute.call_args_list[0].args[0]
    assert 'CREATE TABLE IF NOT EXISTS' in db.execute.call_args_list[1].args[0]
    assert 'voicemail jsonb' in db.execute.call_args_list[1].args[0]
    assert 'ADD COLUMN IF NOT EXISTS disposition' in db.execute.call_args_list[2].args[0]
    assert 'ADD COLUMN IF NOT EXISTS voicemail jsonb' in db.execute.call_args_list[3].args[0]
    calls = store.connection.call_count
    store.initialize()
    assert store.connection.call_count == calls


def test_claim_commits_insert_before_granting_permission(db):
    context = {'event_id': 'event'}
    assert store.claim('key', context)
    assert db.events[-1] == 'commit'
    sql, params = db.execute.call_args.args
    assert 'ON CONFLICT (id) DO NOTHING' in sql
    assert "VALUES (%s, 'dispatching', %s)" in sql
    assert params[0] == 'key'
    assert params[1].obj == context
    assert 'UPDATE' not in sql


def test_existing_claim_never_grants_permission(db):
    db.row = None
    assert store.claim('existing-key', {}) is False


def test_claim_commit_failure_never_grants_permission(db):
    db.fail_commit = True
    with pytest.raises(RuntimeError, match='commit failed'):
        store.claim('key', {})


def test_get_returns_all_independent_states(db):
    voicemail = {'status': 'submitted', 'message': 'Please call us back.'}
    db.row = ('key', 'uncertain', 'CA123', {'event_id': 'event'}, 'confirmed', None, 'completed', voicemail)
    assert store.get('key') == {
        'key': 'key', 'status': 'uncertain', 'call_sid': 'CA123',
        'context': {'event_id': 'event'}, 'outcome': 'confirmed',
        'reason': None, 'disposition': 'completed', 'voicemail': voicemail,
    }
    db.row = None
    assert store.get('missing') is None


def test_queued_does_not_replace_early_callback_sid_or_outcome(db):
    assert store.queued('key', 'CA123')
    sql, params = db.execute.call_args.args
    assert params == ('CA123', 'key', 'CA123', 'CA123')
    assert '(call_sid IS NULL OR call_sid = %s)' in sql
    assert "status = 'dispatching' OR (status = 'queued' AND call_sid = %s)" in sql
    assert 'outcome' not in sql
    assert 'disposition' not in sql


@pytest.mark.parametrize('sid', [None, '', '   ', 123])
def test_queued_rejects_invalid_sid_without_database(db, sid):
    with pytest.raises(ValueError, match='call SID'):
        store.queued('key', sid)
    db.execute.assert_not_called()


def test_uncertain_is_terminal_and_preserves_callback_fields(db):
    assert store.uncertain('key')
    sql, params = db.execute.call_args.args
    assert params == ('key',)
    assert "status IN ('dispatching', 'uncertain')" in sql
    assert 'call_sid' not in sql
    assert 'outcome' not in sql
    assert 'disposition' not in sql


def test_blocked_does_not_replace_terminal_dispatch(db):
    assert store.blocked('key', 'cancelled event')
    sql, params = db.execute.call_args.args
    assert params == ('cancelled event', 'key', 'cancelled event')
    assert "status = 'dispatching' OR (status = 'blocked' AND reason = %s)" in sql


def test_outcome_allows_uncertain_and_repeated_equal_without_status_change(db):
    outcome = {'response': 'confirmed'}
    assert store.record_outcome('key', outcome)
    sql, params = db.execute.call_args.args
    assert params[0].obj == params[2].obj == outcome
    assert params[1] == 'key'
    assert "status IN ('dispatching', 'queued', 'uncertain')" in sql
    assert '(outcome IS NULL OR outcome = %s)' in sql
    assert 'SET status' not in sql
    assert 'call_sid' not in sql
    assert 'disposition' not in sql


def test_null_outcome_rejected_without_database(db):
    with pytest.raises(ValueError, match='outcome'):
        store.record_outcome('key', None)
    db.execute.assert_not_called()


@pytest.mark.parametrize('disposition,rank', [
    ('queued', 0), ('ringing', 1), ('in-progress', 2), ('completed', 3),
    ('busy', 3), ('failed', 3), ('no-answer', 3), ('canceled', 3),
])
def test_disposition_binds_sid_and_never_regresses(db, disposition, rank):
    assert store.record_disposition('key', 'CA123', disposition)
    sql, params = db.execute.call_args.args
    assert params == ('CA123', disposition, 'key', 'CA123', disposition, rank)
    assert 'call_sid = COALESCE(call_sid, %s)' in sql
    assert '(call_sid IS NULL OR call_sid = %s)' in sql
    assert "status IN ('dispatching', 'queued', 'uncertain')" in sql
    assert 'disposition IS NULL OR disposition = %s' in sql
    assert "disposition IN ('queued', 'ringing', 'in-progress')" in sql
    assert "WHEN 'queued' THEN 0" in sql
    assert "WHEN 'ringing' THEN 1" in sql
    assert "WHEN 'in-progress' THEN 2" in sql
    assert 'END < %s' in sql
    assert 'SET status' not in sql
    assert 'outcome' not in sql


def test_unknown_disposition_and_empty_sid_rejected_without_database(db):
    with pytest.raises(ValueError, match='disposition'):
        store.record_disposition('key', 'CA123', 'invented')
    with pytest.raises(ValueError, match='call SID'):
        store.record_disposition('key', '', 'completed')
    db.execute.assert_not_called()


@pytest.mark.parametrize('method,args', [
    (store.queued, ('key', 'CA123')),
    (store.uncertain, ('key',)),
    (store.blocked, ('key', 'reason')),
    (store.record_outcome, ('key', 'confirmed')),
    (store.record_disposition, ('key', 'CA123', 'completed')),
    (store.claim_voicemail, ('key', 'CA123', 'Please call us back.')),
    (store.finish_voicemail, ('key', 'submitted')),
])
def test_guarded_updates_return_false_when_no_row_matches(db, method, args):
    db.row = None
    assert method(*args) is False
    assert db.events[-1] == 'commit'


def test_voicemail_claim_commits_once_binds_sid_and_preserves_other_states(db):
    message = 'This is Alli from Charleston AI. Please call us back.'
    assert store.claim_voicemail('key', 'CA123', message)
    assert db.events[-1] == 'commit'
    sql, params = db.execute.call_args.args
    assert params[0] == params[3] == 'CA123'
    assert params[1].obj == {'status': 'pending', 'message': message}
    assert params[2] == 'key'
    assert 'call_sid = COALESCE(call_sid, %s)' in sql
    assert '(call_sid IS NULL OR call_sid = %s)' in sql
    assert "status IN ('dispatching', 'queued', 'uncertain')" in sql
    assert 'AND voicemail IS NULL' in sql
    assert 'outcome' not in sql
    assert 'disposition' not in sql
    assert 'SET status' not in sql


def test_voicemail_claim_commit_failure_never_grants_redirect_permission(db):
    db.fail_commit = True
    with pytest.raises(RuntimeError, match='commit failed'):
        store.claim_voicemail('key', 'CA123', 'Please call us back.')


@pytest.mark.parametrize('sid,message', [
    (None, 'Hello'), ('', 'Hello'), ('   ', 'Hello'), (123, 'Hello'),
    ('CA123', None), ('CA123', ''), ('CA123', '   '), ('CA123', 123),
])
def test_voicemail_claim_invalid_inputs_do_not_touch_database(db, sid, message):
    with pytest.raises(ValueError):
        store.claim_voicemail('key', sid, message)
    db.execute.assert_not_called()


@pytest.mark.parametrize('status', ['submitted', 'uncertain'])
def test_finish_voicemail_only_finalizes_pending_preserving_message(db, status):
    assert store.finish_voicemail('key', status)
    assert db.events[-1] == 'commit'
    sql, params = db.execute.call_args.args
    assert params[0].obj == status
    assert params[1] == 'key'
    assert "jsonb_set(voicemail, '{status}', %s)" in sql
    assert "voicemail ->> 'status' = 'pending'" in sql
    assert 'outcome' not in sql
    assert 'disposition' not in sql
    assert 'SET status' not in sql


@pytest.mark.parametrize('status', ['pending', 'delivered', '', None])
def test_finish_voicemail_rejects_invalid_status_without_database(db, status):
    with pytest.raises(ValueError, match='voicemail status'):
        store.finish_voicemail('key', status)
    db.execute.assert_not_called()
