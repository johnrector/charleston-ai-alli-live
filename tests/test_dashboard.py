import asyncio
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import dashboard as d
import promises as p
from test_inbound import BASE,Memory,SID,CALLER
import inbound_communications as inbound

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('DEMO_KEY','test-owner-key')
    monkeypatch.setenv('TWILIO_VOICE_PUBLIC_DOMAIN','example.com')
    app=FastAPI();d.install_dashboard(app,None,SimpleNamespace(configure=Mock()),SimpleNamespace(ready=True,last_scan=None,last_error=None))
    return TestClient(app,base_url='https://example.com')

def test_private_data_and_writes_are_locked(client,monkeypatch):
    read=Mock();monkeypatch.setattr(d,'data',read)
    for path in ['data','meetings','session','promises/ABC/history']:
        assert client.get('/dashboard/api/'+path).status_code==401
    assert client.post('/dashboard/api/reminders',json={'enabled':True}).status_code==401
    assert client.post('/dashboard/api/attention/123/ack',json={}).status_code==401
    read.assert_not_called()

def test_static_shell_contains_no_private_data_and_has_security_headers(client):
    response=client.get('/dashboard')
    assert response.status_code==200
    for term in ('jsrector@','DEMO_KEY','Blake Miller','8436968121'):
        assert term not in response.text
    assert response.headers['cache-control']=='no-store'
    assert response.headers['x-frame-options']=='DENY'
    assert "script-src 'self'" in response.headers['content-security-policy']
    assert client.get('/dashboard/assets/unknown').status_code==404

def test_cookie_writes_require_csrf_and_exact_origin(client,monkeypatch):
    monkeypatch.setattr(d,'authenticate',lambda req:dict(method='cookie',csrf='csrf-value',token='token'))
    for headers in [{},{'Origin':'https://evil.example','X-CSRF-Token':'csrf-value'},{'Origin':'https://example.com'}]:
        assert client.post('/dashboard/api/attention/1/ack',json={},headers=headers).status_code==403

def test_key_access_uses_current_owner_key_without_creating_sessions(client,monkeypatch):
    monkeypatch.setattr(d,'data',lambda:dict(ok=True))
    response=client.get('/dashboard/api/data',headers={'X-Demo-Key':'test-owner-key'})
    assert response.status_code==200 and response.json()['cloud_worker']['ready']
    assert 'set-cookie' not in response.headers
    assert client.get('/dashboard/api/data',headers={'X-Demo-Key':'wrong'}).status_code==401

def test_code_verification_sets_secure_cookie(client,monkeypatch):
    monkeypatch.setattr(d,'verify_code',lambda challenge,code:'random-session-token')
    response=client.post('/dashboard/api/verify',json={'challenge':'a'*32,'code':'123456'})
    cookie=response.headers['set-cookie']
    assert '__Host-alli_session=' in cookie and 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie
    assert 'random-session-token' not in response.text
    assert response.json()['csrf']==d.digest('random-session-token:csrf')

def test_bad_attempt_is_committed_before_error(monkeypatch):
    db=Mock();db.execute.return_value.fetchone.return_value=('wrong',0,False,datetime.now(timezone.utc)+timedelta(minutes=5))
    completed=[]
    @contextmanager
    def connection():
        yield db
        completed.append(True)
    monkeypatch.setenv('DEMO_KEY','secret');monkeypatch.setattr(d.store,'initialize',lambda:None);monkeypatch.setattr(d.store,'connection',connection)
    with pytest.raises(ValueError):d.verify_code('a'*32,'123456')
    assert completed and any('attempts=attempts+1' in call.args[0] for call in db.execute.call_args_list)

def test_expired_used_and_locked_codes_cannot_create_sessions(monkeypatch):
    monkeypatch.setattr(d.store,'initialize',lambda:None)
    for row in [('digest',5,False,datetime.now(timezone.utc)+timedelta(minutes=1)),('digest',0,True,datetime.now(timezone.utc)+timedelta(minutes=1)),('digest',0,False,datetime.now(timezone.utc)-timedelta(seconds=1)),None]:
        db=Mock();db.execute.return_value.fetchone.return_value=row
        @contextmanager
        def connection():yield db
        monkeypatch.setattr(d.store,'connection',connection)
        with pytest.raises(ValueError):d.verify_code('a'*32,'123456')
        assert not any('INSERT INTO alli_dashboard_session' in c.args[0] for c in db.execute.call_args_list)

def test_promises_only_available_to_owner_and_saved_contacts(monkeypatch):
    monkeypatch.delenv('ALLI_OWNER_PHONE',raising=False)
    async def check():
        unknown,tools=await inbound.build_session(BASE,{},SID,CALLER,'sms',Memory())
        assert not {'get_my_promises','record_promise'}&{t.name for t in tools}
        known,tools=await inbound.build_session(BASE,{'contact':{'name':'Jane','source':'owner_saved_contact'}},SID,CALLER,'voice',Memory())
        assert {'get_my_promises','record_promise'}<={t.name for t in tools}
        assert 'record_promise before' in known['instructions']
    asyncio.run(check())

def test_promise_tools_bind_phone_and_hide_owner_notes(monkeypatch):
    create=Mock(return_value={'ok':True});monkeypatch.setattr(p,'create',create)
    row=dict(id='ABC',title='Question',details='For John',state='queued',due_at=None,delivery_status='scheduled',response='Approved answer',completion_note='private note',phone='someone else')
    monkeypatch.setattr(p,'listing',Mock(return_value=[row]))
    tools={t.name:t for t in p.tools(CALLER,'Jane',SID)}
    async def check():
        await tools['record_promise'](title='Question',details='For John')
        result=await tools['get_my_promises']()
        assert 'completion_note' not in result['promises'][0] and 'phone' not in result['promises'][0]
    asyncio.run(check())
    assert create.call_args.args[2:6]==(CALLER,'Jane','ask_john',None)
    p.listing.assert_called_once_with(CALLER,True)

def test_answer_commands_are_owner_only_and_do_not_accept_quoted_commands(monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE','+18435550199')
    change=Mock(return_value={'promise':{'contact_name':'Jane'}});monkeypatch.setattr(p,'change',change)
    assert p.command(CALLER,'ANSWER ABCDEF123456 yes') is None
    assert p.command('+18435550199','Someone said ANSWER ABCDEF123456 yes') is None
    assert 'Use ANSWER' in p.command('+18435550199','ANSWER wrong')
    assert 'queued' in p.command('+18435550199','ANSWER ABCDEF123456 Yes, bring your laptop.')
    change.assert_called_once_with('ABCDEF123456','answer','Yes, bring your laptop.')

@pytest.mark.parametrize('args',[dict(title='',kind='task'),dict(title='T',kind='magic'),dict(title='T',kind='text',phone='anonymous'),dict(title='T',kind='text',phone=CALLER,response=''),dict(title='T',due_at='2026-10-24T11:00:00')])
def test_invalid_promises_fail_before_database(args,monkeypatch):
    initialize=Mock();monkeypatch.setattr(p.store,'initialize',initialize)
    with pytest.raises(ValueError):p.create(**args)
    initialize.assert_not_called()
