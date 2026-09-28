import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import Mock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
import google_integration as g


def test_eastern_and_dst():
    assert g.local_time('2026-09-29T15:30:00').isoformat()=='2026-09-29T15:30:00-04:00'
    assert g.local_time('2026-09-29T19:30:00Z').hour==15
    with pytest.raises(ValueError): g.local_time('2027-03-14T02:30:00')
    with pytest.raises(ValueError): g.local_time('2026-11-01T01:30:00')


def test_freebusy_excludes_conflicts(monkeypatch):
    monkeypatch.setattr(g,'api', lambda *a,**kw: {'calendars':{'primary':{'busy':[{'start':'2030-01-07T15:00:00-05:00','end':'2030-01-07T15:30:00-05:00'}]}}})
    result=g.availability('2030-01-07T15:00:00','2030-01-07T17:00:00')
    assert result['suggested_slots'][0]['start']=='2030-01-07T15:30:00-05:00'
    assert result['timezone']=='America/New_York'


def test_calendar_errors_are_not_free(monkeypatch):
    monkeypatch.setattr(g,'api',lambda *a,**k:{'calendars':{'primary':{'errors':[{'reason':'notFound'}]}}})
    with pytest.raises(g.GoogleError): g.availability('2030-01-07T15:00:00','2030-01-07T17:00:00')


def test_confirmation_and_email_validation():
    with pytest.raises(ValueError): g.mail('x@example.com','Hi','Hi')
    with pytest.raises(ValueError): g.meeting('X','x@example.com','2030-01-07T15:00:00',30,'Meet')
    with pytest.raises(ValueError): g.email_address('x@example.com\r\nBcc: y@example.com')


def test_oauth_requires_admin_and_rejects_state(monkeypatch):
    app=FastAPI(); app.include_router(g.router)
    client=TestClient(app,base_url='https://testserver')
    monkeypatch.setenv('DEMO_KEY','test-key')
    monkeypatch.setenv('GOOGLE_TOKEN_ENCRYPTION_KEY',Fernet.generate_key().decode())
    assert client.post('/google/authorize',data={'admin_key':'wrong'}).status_code==401
    assert client.get('/google/status').status_code==401
    assert client.get('/google/callback?state=forged&code=bad').status_code==400
    cookie=g.store.cipher().encrypt(json.dumps({'state':'valid','verifier':'test'}).encode()).decode()
    client.cookies.set('alli_oauth',cookie)
    consume=Mock(return_value=True); monkeypatch.setattr(g.store,'consume_state',consume)
    assert client.get('/google/callback?state=wrong&code=bad').status_code==400
    consume.assert_not_called()


def fake_db(monkeypatch, row=None):
    db=Mock(); db.execute.return_value.fetchone.return_value=row
    @contextmanager
    def locked(): yield db
    monkeypatch.setattr(g.store,'action_lock',locked)
    return db


def test_meeting_invites_and_returns_google_confirmation(monkeypatch):
    fake_db(monkeypatch)
    calls=[]
    def api(method,path,**kw):
        calls.append((method,path,kw))
        if method=='GET': raise g.GoogleError('HTTP 404')
        if path.endswith('freeBusy'): return {'calendars':{'primary':{'busy':[]}}}
        event=kw['body']; return {**event,'htmlLink':'https://calendar.google.com/event/test'}
    monkeypatch.setattr(g,'api',api)
    r=g.meeting('Susan','susan@example.com','2030-01-07T15:30:00',30,'Meeting',confirmed=True)
    assert r['ok'] and r['event_id']
    assert calls[-1][2]['params']['sendUpdates']=='all'
    assert calls[-1][2]['body']['attendees'][0]['email']=='susan@example.com'


def test_busy_meeting_is_not_created(monkeypatch):
    fake_db(monkeypatch)
    def api(method,path,**kw):
        if method=='GET': raise g.GoogleError('HTTP 404')
        if path.endswith('freeBusy'): return {'calendars':{'primary':{'busy':[{'start':'2030-01-07T15:00:00-05:00','end':'2030-01-07T16:30:00-05:00'}]}}}
        pytest.fail('Should never create conflicting event')
    monkeypatch.setattr(g,'api',api)
    with pytest.raises(ValueError): g.meeting('Susan','susan@example.com','2030-01-07T15:30:00',30,'Meeting',confirmed=True)


def test_uncertain_email_not_retried(monkeypatch):
    fake_db(monkeypatch,('pending',None))
    api=Mock(); monkeypatch.setattr(g,'api',api)
    assert g.mail('susan@example.com','Meeting','Confirmed',True)['status']=='unknown'
    api.assert_not_called()


def test_gmail_success_requires_message_id(monkeypatch):
    db=fake_db(monkeypatch)
    monkeypatch.setattr(g,'api',lambda *a,**k:{'id':'gmail123'})
    assert g.mail('susan@example.com','Meeting','Confirmed',True)['message_id']=='gmail123'
    db.commit.assert_called_once()
    monkeypatch.setattr(g,'api',lambda *a,**k:{})
    with pytest.raises(g.GoogleError): g.mail('susan@example.com','Meeting','Confirmed',True)


def test_tool_errors_never_report_success(monkeypatch):
    def fail(*a): raise RuntimeError('secret detail')
    assert asyncio.run(g.safely(fail))=={'ok':False,'error':'Google action could not be verified. Do not claim success or blindly retry a send.'}
