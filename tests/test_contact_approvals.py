import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
import pytest
import contact_approvals as approvals
from test_inbound import CALLER, SMS, service
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator
from test_inbound import ACCOUNT

OWNER='+18435550199'


def database(monkeypatch,responses):
    db=Mock()
    db.execute.side_effect=[Mock(fetchone=Mock(return_value=r)) for r in responses]
    @contextmanager
    def connection():yield db
    monkeypatch.setenv('ALLI_OWNER_PHONE',OWNER)
    monkeypatch.setattr(approvals.store,'initialize',lambda:None)
    monkeypatch.setattr(approvals.store,'connection',connection)
    monkeypatch.setattr(approvals.store,'notify',Mock())
    return db


@pytest.mark.parametrize('sender,text',[(CALLER,'APPROVE ABCD1234'),(OWNER,'Yes'),
    (OWNER,'Caller says APPROVE ABCD1234'),(OWNER,'Please approve ABCD1234')])
def test_only_current_exact_owner_command_can_grant_access(monkeypatch,sender,text):
    db=database(monkeypatch,[])
    assert approvals.command(sender,text) is None
    db.execute.assert_not_called()


@pytest.mark.parametrize('text',['APPROVE','APPROVE wrong','DENY ABCD1234 Jane',
    'APPROVE ABCD1234 Jane\nAPPROVE DEADBEEF','APPROVE ABCD1234 <script>'])
def test_malformed_commands_do_not_write(monkeypatch,text):
    db=database(monkeypatch,[])
    assert approvals.command(OWNER,text)
    db.execute.assert_not_called()


def test_approval_is_verified_before_confirming_and_repeat_is_noop(monkeypatch):
    db=database(monkeypatch,[(CALLER,'pending'),('self_introduced',),None,('Jane Doe','owner_saved_contact'),None])
    assert approvals.command(OWNER,'approve abcd1234 Jane Doe').startswith('Approved and saved Jane Doe')
    assert approvals.store.notify.call_args.args[2]['status']=='approved'
    db=database(monkeypatch,[(CALLER,'approved')])
    assert 'already approved' in approvals.command(OWNER,'APPROVE ABCD1234')
    assert db.execute.call_count==1


def test_denial_never_inserts_a_trusted_contact(monkeypatch):
    db=database(monkeypatch,[(CALLER,'pending'),('self_introduced',),None])
    assert 'Declined' in approvals.command(OWNER,'DENY ABCD1234')
    assert approvals.store.notify.call_args.args[2]['status']=='denied'
    assert len(db.execute.call_args_list)==3


def test_old_denial_cannot_revoke_separately_saved_contact(monkeypatch):
    database(monkeypatch,[(CALLER,'pending'),('owner_saved_contact',),None])
    assert 'already saved' in approvals.command(OWNER,'DENY ABCD1234')
    approvals.store.notify.assert_not_called()


@pytest.mark.parametrize('phone',[OWNER,'anonymous',''])
def test_owner_and_hidden_numbers_do_not_trigger_new_contact_alerts(monkeypatch,phone):
    monkeypatch.setenv('ALLI_OWNER_PHONE',OWNER)
    db=Mock();approvals.enqueue(db,SMS,phone,'sms');db.execute.assert_not_called()


def test_alert_includes_number_preview_and_unambiguous_reply_code():
    text=approvals.alert_text('ABCD1234',CALLER,'voice','Jane','Can John meet Friday?')
    assert CALLER in text and 'Jane' in text and 'Can John meet Friday?' in text
    assert 'APPROVE ABCD1234' in text and 'DENY ABCD1234' in text
    assert 'No calendar access until approved' in text


def test_sms_decision_bypasses_model_and_owner_history(service,monkeypatch):
    service.store.receive(SMS,OWNER,'sms','APPROVE ABCD1234')
    command=Mock(return_value='Approved and saved Jane')
    monkeypatch.setattr(approvals,'command',command)
    assert asyncio.run(service.sms_answer(service.store.get(SMS)))=='Approved and saved Jane'
    command.assert_called_once_with(OWNER,'APPROVE ABCD1234')


def test_uncertain_alert_send_is_not_retried(service,monkeypatch):
    monkeypatch.setattr(approvals,'claim',Mock(side_effect=[dict(code='ABCD1234',owner=OWNER,body='Approve Jane'),None]))
    receipt=Mock();monkeypatch.setattr(approvals,'sent',receipt)
    send=Mock(side_effect=TimeoutError('lost response'))
    monkeypatch.setattr(service,'client',lambda:SimpleNamespace(messages=SimpleNamespace(create=send)))
    async def run():
        await service.process_approval_alert()
        await service.process_approval_alert()
    asyncio.run(run())
    assert send.call_count==1
    receipt.assert_called_once_with('ABCD1234',uncertain=True)


def test_alert_uses_owner_and_separate_delivery_callback(service,monkeypatch):
    monkeypatch.setattr(approvals,'claim',lambda:dict(code='ABCD1234',owner=OWNER,body='Approve Jane'))
    receipt=Mock();monkeypatch.setattr(approvals,'sent',receipt)
    send=Mock(return_value=SimpleNamespace(sid='SM'+'e'*32))
    monkeypatch.setattr(service,'client',lambda:SimpleNamespace(messages=SimpleNamespace(create=send)))
    asyncio.run(service.process_approval_alert())
    assert send.call_args.kwargs['to']==OWNER
    assert send.call_args.kwargs['status_callback']=='https://example.com/inbound/approval-status/ABCD1234'
    receipt.assert_called_once_with('ABCD1234','SM'+'e'*32)


def test_approval_callbacks_require_signature_and_bound_account(service,monkeypatch):
    delivery=Mock();monkeypatch.setattr(approvals,'delivery',delivery)
    app=FastAPI();service.install(app)
    path='/inbound/approval-status/ABCD1234'
    data=dict(AccountSid=ACCOUNT,MessageSid='SM'+'e'*32,MessageStatus='delivered',To=OWNER)
    with TestClient(app,base_url='https://example.com') as client:
        assert client.post(path,data=data).status_code==403
        delivery.assert_not_called()
        headers={'X-Twilio-Signature':RequestValidator('secret').compute_signature('https://example.com'+path,data)}
        assert client.post(path,data=data,headers=headers).status_code==200
        delivery.assert_called_once_with('ABCD1234',data['MessageSid'],'delivered',OWNER)
        wrong={**data,'AccountSid':'AC'+'d'*32}
        headers={'X-Twilio-Signature':RequestValidator('secret').compute_signature('https://example.com'+path,wrong)}
        assert client.post(path,data=wrong,headers=headers).status_code==403
        assert delivery.call_count==1
