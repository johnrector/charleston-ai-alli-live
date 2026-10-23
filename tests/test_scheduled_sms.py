import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from twilio.request_validator import RequestValidator
from twilio.base.exceptions import TwilioRestException

import scheduled_sms as sms
import owner_sms
from test_inbound import ACCOUNT, PHONE, CALLER, service

SID = 'SM' + 'd' * 32
NOW = datetime.now(timezone.utc)
OWNER = '+18435550199'


def message(**changes):
    return sms.TextMessage(**{**dict(request_id='test-text-1', phone=CALLER,
        recipient_name='Jane', message='Please reply with your meeting question.',
        ), **changes})


def record(status='scheduled'):
    return ('test-text-1', CALLER, 'Jane', 'Please reply with your meeting question.',
            NOW, NOW + timedelta(minutes=15), status, None, None, NOW, NOW)


def database(monkeypatch, results):
    db = Mock()
    db.execute.side_effect = [Mock(fetchone=Mock(return_value=row)) for row in results]
    @contextmanager
    def connection(): yield db
    monkeypatch.setattr(sms.store, 'initialize', lambda: None)
    monkeypatch.setattr(sms.store, 'connection', connection)
    monkeypatch.setattr(sms.store, 'notify', Mock())
    return db


@pytest.mark.parametrize('values', [dict(phone='garbage'), dict(message=' '),
    dict(send_at='2026-10-23T12:30:00'), dict(request_id='../bad'), dict(message='x'*1601)])
def test_invalid_destination_content_or_naive_time_rejected(values):
    with pytest.raises((ValidationError, TypeError)): message(**values)


def test_repeat_returns_original_even_after_due_time(monkeypatch):
    body = message(send_at=NOW - timedelta(days=1))
    import hashlib, json
    value = body.model_dump(mode='json')
    value['send_at'] = body.send_at.astimezone(timezone.utc).isoformat()
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    db = database(monkeypatch, [None, (digest, *record('delivered'))])
    result = sms.enqueue(body)
    assert result['duplicate'] and result['delivery_confirmed']
    assert db.execute.call_count == 2
    sms.store.notify.assert_not_called()


def test_changed_intent_cannot_reuse_request_id(monkeypatch):
    database(monkeypatch, [None, ('other-fingerprint', *record('uncertain'))])
    with pytest.raises(ValueError, match='different text'): sms.enqueue(message())


def test_queue_persists_before_returning_and_is_not_delivered(monkeypatch):
    db = database(monkeypatch, [None, None, record()])
    result = sms.enqueue(message())
    assert result['status'] == 'scheduled' and not result['delivery_confirmed']
    assert 'callback_token' not in result
    assert db.execute.call_args.args[1][1] # persisted fingerprint
    sms.store.notify.assert_called_once()


def test_missed_time_is_rejected(monkeypatch):
    database(monkeypatch, [None, None])
    with pytest.raises(ValueError, match='future'): sms.enqueue(message(send_at=NOW-timedelta(days=1)))


def test_terminal_delivery_cannot_be_regressed_by_late_create_result(monkeypatch):
    db = database(monkeypatch, [('delivered', SID, 'callback-secret', CALLER)])
    assert not sms.receipt('test-text-1', SID, 'queued')
    assert db.execute.call_count == 1


@pytest.mark.parametrize('token,phone,sid', [('wrong',CALLER,SID),
    ('callback-secret',OWNER,SID), ('callback-secret',CALLER,'SM'+'e'*32)])
def test_callbacks_are_bound_to_token_destination_and_sid(monkeypatch,token,phone,sid):
    db = database(monkeypatch, [('sending', SID, 'callback-secret', CALLER)])
    assert not sms.receipt('test-text-1',sid,'delivered',callback_token=token,phone=phone)
    assert db.execute.call_count == 1


def test_early_callback_can_bind_then_confirm_delivery(monkeypatch):
    db = database(monkeypatch, [('sending',None,'callback-secret',CALLER),None])
    assert sms.receipt('test-text-1',SID,'delivered',callback_token='callback-secret',phone=CALLER)
    assert db.execute.call_args.args[1][0:2] == ('delivered',SID)


def test_cancel_claimed_text_is_refused(monkeypatch):
    database(monkeypatch,[None])
    monkeypatch.setattr(sms,'get',lambda _:sms.row_dict(record('sending')))
    result=sms.cancel('test-text-1')
    assert not result['ok'] and result['status']=='sending'


def test_worker_persists_uncertainty_and_never_retries(service,monkeypatch):
    row=dict(request_id='test-text-1',phone=CALLER,body='Hello',callback_token='secret')
    monkeypatch.setattr(sms,'claim',Mock(side_effect=[row,None]))
    saved=Mock();monkeypatch.setattr(sms,'receipt',saved)
    create=Mock(side_effect=TimeoutError('uncertain'))
    monkeypatch.setattr(service,'client',lambda:SimpleNamespace(messages=SimpleNamespace(create=create)))
    worker=sms.ScheduledSMS(service)
    async def run():
        assert await worker.process_one()
        assert not await worker.process_one()
    asyncio.run(run())
    assert create.call_count==1
    saved.assert_called_once_with('test-text-1',status='uncertain',error_code=None)


def test_worker_records_explicit_provider_rejection(service,monkeypatch):
    monkeypatch.setattr(sms,'claim',lambda:dict(request_id='one',phone=CALLER,body='Hello',callback_token='secret'))
    saved=Mock();monkeypatch.setattr(sms,'receipt',saved)
    create=Mock(side_effect=TwilioRestException(400,'/messages',code=21610,msg='Opted out'))
    monkeypatch.setattr(service,'client',lambda:SimpleNamespace(messages=SimpleNamespace(create=create)))
    asyncio.run(sms.ScheduledSMS(service).process_one())
    saved.assert_called_once_with('one',status='failed',error_code=21610)


def test_sender_and_callback_are_our_existing_service(service,monkeypatch):
    monkeypatch.setattr(sms,'claim',lambda:dict(request_id='one',phone=CALLER,body='Hello',callback_token='secret'))
    saved=Mock();monkeypatch.setattr(sms,'receipt',saved)
    create=Mock(return_value=SimpleNamespace(sid=SID,status='queued',error_code=None))
    monkeypatch.setattr(service,'client',lambda:SimpleNamespace(messages=SimpleNamespace(create=create)))
    asyncio.run(sms.ScheduledSMS(service).process_one())
    assert create.call_args.kwargs==dict(to=CALLER,from_=PHONE,body='Hello',
        status_callback='https://example.com/outbound/sms-status/one/secret',validity_period=900)
    saved.assert_called_once_with('one',SID,'queued',None)


def test_admin_endpoints_require_owner_key(service,monkeypatch):
    monkeypatch.setenv('DEMO_KEY','owner-secret')
    worker=sms.ScheduledSMS(service)
    api=FastAPI();worker.install(api)
    save=Mock(return_value={'ok':True,'status':'scheduled'})
    monkeypatch.setattr(sms,'enqueue',save)
    client=TestClient(api,base_url='https://example.com')
    for method,path in [('get','/outbound/sms/one'),('post','/outbound/sms/one/cancel')]:
        assert getattr(client,method)(path).status_code==401
    body=message().model_dump(mode='json')
    assert client.post('/outbound/sms',json=body).status_code==401
    save.assert_not_called()
    assert client.post('/outbound/sms',json=body,headers={'X-Demo-Key':'owner-secret'}).json()['status']=='scheduled'


def test_callback_requires_signature_account_and_sender(service,monkeypatch):
    api=FastAPI();sms.ScheduledSMS(service).install(api)
    client=TestClient(api,base_url='https://example.com')
    saved=Mock();monkeypatch.setattr(sms,'receipt',saved)
    path='/outbound/sms-status/one/token'
    form=dict(AccountSid=ACCOUNT,From=PHONE,To=CALLER,MessageSid=SID,MessageStatus='delivered')
    assert client.post(path,data=form).status_code==403
    for changes in ({},{'AccountSid':'AC'+'0'*32},{'From':OWNER}):
        data={**form,**changes}
        headers={'X-Twilio-Signature':RequestValidator('secret').compute_signature('https://example.com'+path,data)}
        assert client.post(path,data=data,headers=headers).status_code==(403 if changes else 200)
    saved.assert_called_once_with('one',SID,'delivered',None,'token',CALLER)


def test_owner_text_tools_bind_resolved_number_and_stable_intent(monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',OWNER)
    monkeypatch.setattr(owner_sms,'find_text_contacts',lambda _:dict(ok=True,matches=[dict(phone=CALLER,name='Jane')]))
    save=Mock(return_value=dict(ok=True,status='scheduled'))
    monkeypatch.setattr(sms,'enqueue',save)
    async def run():
        tools={t.name:t for t in owner_sms.text_tools(OWNER,'sms','SM-owner','Text Jane tomorrow at noon')}
        assert not (await tools['text_contact'](phone_number=CALLER,recipient_name='Jane',message='Hello',owner_requested=True))['ok']
        await tools['find_text_contact'](query='Jane')
        assert not (await tools['text_contact'](phone_number=CALLER,recipient_name='Jane',message='Hello',owner_requested=False))['ok']
        for _ in range(2):
            assert (await tools['text_contact'](phone_number=CALLER,recipient_name='Jane',message='Hello',owner_requested=True))['ok']
        unknown={t.name:t for t in owner_sms.text_tools(CALLER,'sms','SM-friend','Text John')}
        assert not (await unknown['text_contact'](phone_number=OWNER,recipient_name='John',message='Hello',owner_requested=True))['ok']
    asyncio.run(run())
    assert save.call_count==2
    assert save.call_args_list[0].args[0].request_id==save.call_args_list[1].args[0].request_id


def test_mcp_exposes_authenticated_sms_tools_only_when_installed(monkeypatch):
    from mcp_integration import install_mcp
    import mcp_integration
    from test_mcp import MemoryStore
    from pydantic import BaseModel
    class Dummy(BaseModel): pass
    monkeypatch.setattr(mcp_integration,'store',MemoryStore())
    api=FastAPI()
    mcp=install_mcp(api,None,Dummy,sms_service=object())
    tools={t.name:t for t in asyncio.run(mcp.list_tools())}
    assert {'send_text_message','get_text_message','cancel_text_message'}<=tools.keys()
    assert tools['get_text_message'].annotations.readOnlyHint
    assert not tools['send_text_message'].annotations.readOnlyHint
    assert tools['send_text_message'].securitySchemes
    assert 'send_at' in tools['send_text_message'].inputSchema['properties']
