import asyncio
from copy import deepcopy
from unittest.mock import Mock
import pytest
import owner_sms as owner
import inbound_communications as inbound
from test_inbound import BASE, Memory, SID, CALLER

@pytest.mark.parametrize('channel,phone,allowed',[('sms',CALLER,True),('sms','8435550100',True),('voice',CALLER,False),('sms','+18435550199',False),('sms','anonymous',False)])
def test_owner_channel_binding(monkeypatch,channel,phone,allowed):
    monkeypatch.setenv('ALLI_OWNER_PHONE',CALLER)
    assert owner.is_owner_sms(phone,channel) is allowed

def test_name_claim_cannot_add_owner_tools(monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',CALLER)
    context={'contact':{'name':'John Rector','is_owner':True}}
    for channel,phone in [('voice',CALLER),('sms','+18435550199')]:
        session,tools=asyncio.run(inbound.build_session(BASE,context,SID,phone,channel,Memory()))
        assert 'lookup_my_calendar' not in {t.name for t in tools}

def test_owner_calendar_tool_and_same_models(monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',CALLER)
    session,tools=asyncio.run(inbound.build_session(BASE,{},SID,CALLER,'sms',Memory()))
    assert 'lookup_my_calendar' in {t.name for t in tools}
    assert 'read-only access' in session['instructions']
    assert session['model']==BASE['model']
    assert session['delegation']['responses']['model']==BASE['delegation']['responses']['model']
    assert {'send_email','find_email_recipient'} <= {t.name for t in tools}
    assert not {'create_meeting','update_meeting'} & {t.name for t in tools}

def test_lookup_matches_organizer_and_paginates_without_returning_private_fields(monkeypatch):
    event={'summary':'Charleston AI at Office','organizer':{'displayName':'Blake Miller'},'description':'private full description','start':{'dateTime':'2026-10-02T10:30:00-04:00'},'end':{'dateTime':'2026-10-02T13:00:00-04:00'},'location':'Office'}
    api=Mock(side_effect=[{'items':[],'nextPageToken':'next'},{'items':[event]}])
    monkeypatch.setattr(owner.g,'api',api)
    result=owner.calendar_lookup('Blake','2026-10-02T00:00:00-04:00','2026-10-03T00:00:00-04:00')
    assert result['events'][0]['title']=='Charleston AI at Office'
    assert 'description' not in result['events'][0]
    assert api.call_count==2 and all(c.args[0]=='GET' for c in api.call_args_list)
    assert not result['truncated']

def test_invalid_window_never_calls_google(monkeypatch):
    api=Mock();monkeypatch.setattr(owner.g,'api',api)
    with pytest.raises(ValueError):owner.calendar_lookup('', '2026-10-02T00:00:00-04:00','2026-12-03T00:00:00-05:00')
    api.assert_not_called()

def test_tool_rechecks_owner_configuration(monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',CALLER)
    tool=owner.calendar_tool(CALLER,'sms')
    monkeypatch.delenv('ALLI_OWNER_PHONE')
    assert not asyncio.run(tool(query='Blake'))['ok']

def test_sms_email_requires_owner_request_and_resolved_address(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setenv('ALLI_OWNER_PHONE',CALLER)
    sender=AsyncMock(return_value={'ok':True,'message_id':'test-id'})
    monkeypatch.setattr(owner.g,'send_email',sender)
    tools={t.name:t for t in owner.email_tools(CALLER,'sms','Please email jane@example.com saying hello')}
    async def run():
        send=tools['send_email']
        assert not (await send(recipient_email='jane@example.com',subject='Hello',body='Hello'))['ok']
        assert not (await send(recipient_email='guessed@example.com',subject='Hello',body='Hello',owner_requested=True))['ok']
        assert (await send(recipient_email='jane@example.com',subject='Hello',body='Hello',owner_requested=True))['message_id']=='test-id'
        monkeypatch.setenv('ALLI_OWNER_PHONE','+18435550199')
        assert not (await send(recipient_email='jane@example.com',subject='Hello',body='Hello',owner_requested=True))['ok']
    asyncio.run(run());assert sender.await_count==1

def test_ambiguous_recipient_never_unlocks_sending(monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',CALLER)
    monkeypatch.setattr(owner,'find_email_recipients',lambda q:{'ok':True,'matches':[{'email':'a@example.com'},{'email':'b@example.com'}]})
    tools={t.name:t for t in owner.email_tools(CALLER,'sms')}
    async def run():
        await tools['find_email_recipient'](query='Jane')
        assert not (await tools['send_email'](recipient_email='a@example.com',subject='Hi',body='Hi',owner_requested=True))['ok']
    asyncio.run(run())
