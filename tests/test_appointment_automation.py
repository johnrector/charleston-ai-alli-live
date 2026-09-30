import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from appointment_automation import AppointmentAutomation, load_configuration
from appointment_calendar import CalendarSelectionPolicy, ContactDirectory, ContactBinding, GoogleCalendarReader

NOW = datetime(2026,10,1,14,tzinfo=timezone.utc)


def configured():
    policy=CalendarSelectionPolicy(enabled=True,approved=True,eligible_event_rule='explicit private opt-in',
        verified_phone_source='owner-directory',quiet_start_hour=20,quiet_end_hour=8,voicemail='generic_message')
    contact=ContactBinding(recipient_id='pat',recipient_name='Pat',phone='+12025550123',phone_verified=True,
        verified_source='owner-directory',verified_at=NOW-timedelta(days=1),recipient_email='pat@example.com',
        recipient_timezone='America/New_York',confirmation_consent=True,follow_up_consent=True)
    directory=ContactDirectory(approved=True,source='owner-directory',revision='1',contacts=(contact,))
    event={'id':'event-one','etag':'v1','status':'confirmed',
        'start':{'dateTime':(NOW+timedelta(hours=1)).isoformat()},
        'end':{'dateTime':(NOW+timedelta(hours=2)).isoformat()},
        'attendees':[{'email':'pat@example.com','responseStatus':'accepted'}],
        'extendedProperties':{'private':{'alli_owner_opt_in':'true','alli_contact_id':'pat','alli_confirmation_opt_in':'true'}}}
    def api(method,path,params=None):
        assert method=='GET'
        return event if path.endswith('/event-one') else {'accessRole':'owner','items':[event]}
    return policy,directory,event,GoogleCalendarReader(api)


def test_disabled_run_does_not_read_or_dial(monkeypatch):
    monkeypatch.delenv('APPOINTMENT_AUTOMATION_ENABLED',raising=False)
    def forbidden(): raise AssertionError('Must not load configuration')
    service=AppointmentAutomation(None,config_loader=forbidden)
    assert asyncio.run(service.run_once())=={'ok':False,'status':'disabled','calls':[]}


def test_empty_configuration_is_not_approved(monkeypatch):
    monkeypatch.delenv('APPOINTMENT_POLICY_JSON',raising=False)
    monkeypatch.delenv('APPOINTMENT_CONTACTS_JSON',raising=False)
    policy,directory=load_configuration()
    assert not policy.enabled and not policy.approved and not directory.approved and not directory.contacts


def test_run_once_revalidates_and_passes_stable_call(monkeypatch):
    monkeypatch.setenv('APPOINTMENT_AUTOMATION_ENABLED','true')
    monkeypatch.setenv('OUTBOUND_CALLS_ENABLED','true')
    policy,directory,event,reader=configured()
    calls=[]
    async def initiate(body,before_dial):
        assert await before_dial()
        calls.append(body)
        return {'ok':True,'request_id':body.request_id,'status':'queued'}
    service=AppointmentAutomation(SimpleNamespace(initiate=initiate),lambda:(policy,directory),reader,lambda:NOW)
    result=asyncio.run(service.run_once())
    assert result['status']=='checked' and len(calls)==1
    assert calls[0].capabilities==[] and calls[0].preset=='confirmation'
    assert calls[0].voicemail_policy=='generic_message'


def test_cancellation_between_selection_and_dispatch_is_rejected(monkeypatch):
    monkeypatch.setenv('APPOINTMENT_AUTOMATION_ENABLED','true')
    monkeypatch.setenv('OUTBOUND_CALLS_ENABLED','true')
    policy,directory,event,reader=configured()
    async def initiate(body,before_dial):
        event['status']='cancelled'
        try:
            assert not await before_dial()
        except ValueError:
            pass
        return {'ok':False,'status':'blocked'}
    service=AppointmentAutomation(SimpleNamespace(initiate=initiate),lambda:(policy,directory),reader,lambda:NOW)
    assert asyncio.run(service.run_once())['calls'][0]['status']=='blocked'


def test_preview_does_not_expose_phone_and_routes_require_admin(monkeypatch):
    monkeypatch.delenv('APPOINTMENT_AUTOMATION_ENABLED',raising=False)
    monkeypatch.setenv('DEMO_KEY','admin')
    policy,directory,event,reader=configured()
    service=AppointmentAutomation(None,lambda:(policy,directory),reader,lambda:NOW)
    app=FastAPI();service.install(app)
    with TestClient(app) as client:
        assert client.get('/appointment-automation/preview').status_code==401
        result=client.get('/appointment-automation/preview',headers={'X-Demo-Key':'admin'})
        assert result.status_code==200
        assert '+12025550123' not in result.text
        assert result.json()['automatic_calls_enabled'] is False
        assert client.post('/appointment-automation/run-once',headers={'X-Demo-Key':'admin'}).json()['status']=='disabled'


def test_automation_cannot_use_owner_only_test_mode(monkeypatch):
    monkeypatch.setenv('APPOINTMENT_AUTOMATION_ENABLED','true')
    monkeypatch.setenv('OUTBOUND_CALLS_ENABLED','false')
    monkeypatch.setenv('OUTBOUND_TEST_PHONE','+12025550123')
    def forbidden(): raise AssertionError('Must not load contacts or scan')
    service=AppointmentAutomation(None,config_loader=forbidden)
    assert asyncio.run(service.run_once())['blockers']==['general_outbound_disabled']
