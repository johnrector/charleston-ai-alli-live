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
    assert result['contact']=={'name':'John Rector','source':'owner_configured','is_owner':True,'email':''}

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

def test_owner_saved_contact_overrides_old_ambiguous_mission_names(monkeypatch):
    jane={'recipient_name':'Jane Doe'}
    result=lookup(monkeypatch,[(jane,{})],[(jane,),({'recipient_name':'Bill Doe'},)],('Current Friend','owner_saved_contact','friend@example.com'))
    assert result['contact']['name']=='Current Friend'
    assert result['contact']['email']=='friend@example.com'


def test_saved_contact_survives_without_outbound_missions(monkeypatch):
    result=lookup(monkeypatch,[],[],('Jane Doe','owner_saved_contact','jane@example.com'))
    assert result['mission'] is None
    assert result['contact']['source']=='owner_saved_contact'


def test_self_introduction_cannot_overwrite_owner_approved_record(monkeypatch):
    monkeypatch.setattr(store,'initialize',lambda:None)
    db=Mock()
    @contextmanager
    def connection():yield db
    monkeypatch.setattr(store,'connection',connection)
    assert store.remember_name('+18435550100','John Rector')
    sql,args=db.execute.call_args.args
    assert "WHERE alli_contact_name.source='self_introduced'" in sql


def test_owner_saved_number_normalizes_and_persists_optional_email(monkeypatch):
    monkeypatch.setattr(store,'initialize',lambda:None)
    db=Mock()
    @contextmanager
    def connection():yield db
    monkeypatch.setattr(store,'connection',connection)
    result=store.save_contact('8435550100','Jane Doe','jane@example.com')
    assert result['phone']=='+18435550100'
    assert db.execute.call_args.args[1]==('+18435550100','Jane Doe','jane@example.com')


def test_apple_favorite_is_authoritative_even_without_name(monkeypatch):
    jane={'recipient_name':'Old Contact'}
    result=lookup(monkeypatch,[(jane,{})],[(jane,),({'recipient_name':'Other Contact'},)],('','apple_favorite',''))
    assert result['contact']['source']=='apple_favorite' and result['contact']['name']==''


def test_import_rejects_conflicting_names_before_writing(monkeypatch):
    db=Mock();monkeypatch.setattr(store,'initialize',db)
    import pytest
    with pytest.raises(ValueError,match='conflicting'):
        store.import_apple_favorites([{'phone':'8435550100','name':'Jane'},{'phone':'+18435550100','name':'Bill'}])
    db.assert_not_called()


def test_favorites_import_deduplicates_call_message_and_verifies(monkeypatch):
    monkeypatch.setattr(store,'initialize',lambda:None)
    db=Mock();db.execute.return_value.fetchall.return_value=[('+18435550100','Jane','apple_favorite','jane@example.com')]
    @contextmanager
    def connection():yield db
    monkeypatch.setattr(store,'connection',connection)
    result=store.import_apple_favorites([{'phone':'8435550100','name':'Jane','email':'jane@example.com'},{'phone':'+18435550100','name':'Jane'}])
    assert result=={'ok':True,'imported_numbers':1,'with_name':1,'without_name':0,'automatic_sync':False}
    inserts=[c for c in db.execute.call_args_list if 'INSERT INTO' in c.args[0]]
    assert len(inserts)==1 and inserts[0].args[1]==('+18435550100','Jane','jane@example.com')
