from contextlib import contextmanager
from unittest.mock import Mock
import communication_store as store

def lookup(monkeypatch, rows, greetings, contact, owner=''):
    monkeypatch.setenv('ALLI_OWNER_PHONE',owner)
    monkeypatch.setattr(store,'initialize',lambda:None)
    db=Mock()
    db.execute.side_effect=[Mock(fetchall=lambda:rows),Mock(fetchall=lambda:greetings),Mock(fetchone=lambda:contact),Mock(fetchall=lambda:[])]
    @contextmanager
    def connection():yield db
    monkeypatch.setattr(store,'connection',connection)
    return store.context_for('(843) 555-0100')

def test_owner_overrides_recipient_mission(monkeypatch):
    mission={'recipient_name':'Jane Doe'}
    result=lookup(monkeypatch,[(mission,{})],[(mission,)],('Jane','self_introduced'),'+18435550100')
    assert result['mission'] is None
    assert result['contact']=={'name':'John Rector','source':'owner_configured','is_owner':True}

def test_old_outbound_recognized_without_reauthorizing_mission(monkeypatch):
    result=lookup(monkeypatch,[],[({'recipient_name':'Jane Doe'},)],None)
    assert result['mission'] is None
    assert result['contact']['name']=='Jane Doe'
    assert not result['contact']['is_owner']

def test_self_introduced_john_never_becomes_owner(monkeypatch):
    result=lookup(monkeypatch,[],[],('John Rector','self_introduced'))
    assert result['contact']['name']=='John Rector'
    assert not result['contact']['is_owner']
    assert result['mission'] is None

def test_ambiguous_number_does_not_choose_name_or_mission(monkeypatch):
    jane={'recipient_name':'Jane Doe'}
    result=lookup(monkeypatch,[(jane,{})],[(jane,),({'recipient_name':'Bill Doe'},)],('Jane','self_introduced'))
    assert result['contact'] is None and result['mission'] is None
