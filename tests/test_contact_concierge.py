import asyncio
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock
import pytest
import contact_concierge as cc
import inbound_communications as inbound
import google_integration as g
from test_inbound import BASE, Memory, SID, CALLER

CONTACT={'contact':{'name':'Jane Doe','source':'owner_supplied_mission','email':'jane@example.com','is_owner':False},'history':[]}

@pytest.mark.parametrize('source,channel,allowed',[
    ('owner_supplied_mission','sms',True),('owner_saved_contact','sms',True),
    ('self_introduced','sms',False),('owner_saved_contact','voice',True)])
def test_standing_permissions_only_for_saved_contacts(source,channel,allowed):
    ctx=deepcopy(CONTACT);ctx['contact']['source']=source
    assert cc.trusted_contact(ctx,CALLER,channel) is allowed


def test_saved_sms_no_identity_roundtrip_and_old_window_does_not_apply(monkeypatch):
    ctx=deepcopy(CONTACT)
    ctx['mission']={'request_id':'old','phone':CALLER,'recipient_name':'Jane Doe',
        'mission':'Book only in obsolete window October 5–11','capabilities':[]}
    availability=Mock(return_value={'ok':True,'busy':[],'suggested_slots':[]})
    monkeypatch.setattr(g,'availability',availability)
    async def run():
        session,tools=await inbound.build_session(BASE,ctx,SID,CALLER,'sms',Memory())
        reg={t.name:t for t in tools}
        assert {'create_meeting','check_calendar','find_meetings','update_meeting','create_reminder_for_john'}<=reg.keys()
        assert 'send_email' not in reg and 'save_friend_contact' not in reg
        assert 'obsolete window' not in session['instructions']
        assert '"identity_confirmed": true' in session['instructions']
        assert (await reg['check_calendar'](start_datetime='2026-11-02T13:00:00-05:00',end_datetime='2026-11-02T14:00:00-05:00'))['ok']
        assert (await reg['confirm_identity'](name='Jane'))['confirmation_required'] is False
        assert session['model']==BASE['model']
    asyncio.run(run())


def test_booking_bound_to_contact_with_real_agreement_and_receipt(monkeypatch):
    sender=AsyncMock(return_value={'ok':True,'event_id':'booked'})
    monkeypatch.setattr(g,'create_meeting',sender)
    memory=Memory();memory.action_result=Mock()
    async def run():
        _,tools=await inbound.build_session(BASE,CONTACT,SID,CALLER,'sms',memory)
        create={t.name:t for t in tools}['create_meeting']
        args=dict(start_datetime='2026-11-02T13:00:00-05:00',duration_minutes=60,title='Appointment with John',recipient_email_address='jane@example.com')
        assert not (await create(**args))['ok']
        assert not (await create(**{**args,'recipient_email_address':'other@example.com'},identity_confirmed=True,confirmed=True))['ok']
        assert (await create(**args,identity_confirmed=True,confirmed=True))['event_id']=='booked'
        assert sender.await_args.kwargs['attendee_name']=='Jane Doe'
        assert memory.action_result.call_args.args[1]=='create_meeting'
    asyncio.run(run());assert sender.await_count==1


def test_calendar_read_does_not_expose_busy_blocks(monkeypatch):
    monkeypatch.setattr(g,'availability',lambda *a:{'ok':True,'busy':[{'start':'private'}],'suggested_slots':[]})
    result=asyncio.run(cc.availability_tool()(start_datetime='a',end_datetime='b'))
    assert 'busy' not in result and result['requested_window_free'] is False


def test_owner_recognized_on_voice_without_confirming_old_mission(monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',CALLER)
    session,tools=asyncio.run(inbound.build_session(BASE,{},SID,CALLER,'voice',Memory()))
    assert "OPENING: Hi John" in session['instructions']
    assert '"identity_confirmed": true' in session['instructions']
    assert 'that they want to continue the prior mission' not in session['instructions']
    assert asyncio.run({t.name:t for t in tools}['confirm_identity'](name='John'))['confirmation_required'] is False


def test_only_owner_can_save_contacts(monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',CALLER)
    memory=Memory();memory.save_contact=Mock(return_value={'ok':True})
    session,tools=asyncio.run(inbound.build_session(BASE,{},SID,CALLER,'sms',memory))
    save={t.name:t for t in tools}['save_friend_contact']
    args=dict(phone_number='8435550199',name='Friend')
    assert not asyncio.run(save(**args))['ok']
    assert asyncio.run(save(**args,owner_requested=True))['ok']
    monkeypatch.setenv('ALLI_OWNER_PHONE','+18435550199')
    assert not asyncio.run(save(**args,owner_requested=True))['ok']
    assert memory.save_contact.call_count==1


def test_private_reminder_is_verified_and_repeat_does_not_insert(monkeypatch):
    when=(datetime.now(g.TZ)+timedelta(days=1)).replace(microsecond=0).isoformat()
    events={};writes=[]
    @contextmanager
    def lock():yield
    def api(method,path,body=None):
        if method=='POST':
            writes.append(body);events[body['id']]={**body,'status':'confirmed'};return events[body['id']]
        key=path.split('/')[-1]
        if key not in events:raise g.GoogleError('HTTP 404')
        return events[key]
    monkeypatch.setattr(g.store,'action_lock',lock);monkeypatch.setattr(g,'api',api)
    a=cc.reminder(CALLER,'Jane','Bring the laptop',when)
    b=cc.reminder(CALLER,'Jane','Bring the laptop',when)
    assert a['event_id']==b['event_id'] and len(writes)==1
    assert writes[0]['visibility']=='private' and writes[0]['transparency']=='transparent'
    assert not writes[0].get('attendees') and writes[0]['reminders']['overrides'][0]['minutes']==0


def test_uncertain_reminder_is_not_success_or_retried(monkeypatch):
    when=(datetime.now(g.TZ)+timedelta(days=1)).isoformat()
    @contextmanager
    def lock():yield
    api=Mock(side_effect=[g.GoogleError('HTTP 404'),TimeoutError('uncertain')])
    monkeypatch.setattr(g.store,'action_lock',lock);monkeypatch.setattr(g,'api',api)
    receipt=AsyncMock()
    tool=cc.reminder_tool(CALLER,'Jane',receipt)
    result=asyncio.run(tool(text='Laptop',remind_at=when,confirmed=True))
    assert not result['ok'] and api.call_count==2 and receipt.await_count==1

@pytest.mark.parametrize('channel',['sms','voice'])
def test_both_channels_start_identified_and_can_book(monkeypatch,channel):
    sender=AsyncMock(return_value={'ok':True,'event_id':'booked'})
    monkeypatch.setattr(g,'create_meeting',sender)
    memory=Memory();memory.action_result=Mock()
    async def run():
        session,tools=await inbound.build_session(BASE,CONTACT,SID,CALLER,channel,memory)
        reg={t.name:t for t in tools}
        assert '"identity_confirmed": true' in session['instructions']
        assert 'OPENING: Hi Jane' in session['instructions']
        assert (await reg['create_meeting'](start_datetime='2026-11-02T13:00:00-05:00',duration_minutes=60,title='Meeting with John',recipient_email_address='jane@example.com',confirmed=True))['event_id']=='booked'
    asyncio.run(run())
