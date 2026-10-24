def full_instructions(session):
    return session['instructions']+'\n'+session.get('delegation',{}).get('responses',{}).get('instructions','')

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

CONTACT={'number_verified':True,'contact':{'name':'Jane Doe','source':'owner_supplied_mission','email':'jane@example.com','is_owner':False},'history':[]}

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
        assert 'obsolete window' not in full_instructions(session)
        assert '"identity_confirmed": true' in full_instructions(session)
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
    assert "OPENING: Hi John" in full_instructions(session)
    assert '"identity_confirmed": true' in full_instructions(session)
    assert 'that they want to continue the prior mission' not in full_instructions(session)
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
        assert '"identity_confirmed": true' in full_instructions(session)
        assert 'OPENING: Hi Jane' in full_instructions(session)
        assert (await reg['create_meeting'](start_datetime='2026-11-02T13:00:00-05:00',duration_minutes=60,title='Meeting with John',recipient_email_address='jane@example.com',confirmed=True))['event_id']=='booked'
    asyncio.run(run())

@pytest.mark.parametrize('start',["2030-01-04T11:00:00-05:00","2030-01-05T19:00:00-05:00"])
def test_exact_time_free_even_without_office_hour_suggestions(monkeypatch,start):
    lookup=Mock(return_value={'ok':True,'busy':[],'suggested_slots':[]})
    monkeypatch.setattr(g,'availability',lookup)
    result=cc.meeting_time(start)
    assert result['available'] is True and result['duration_minutes']==60
    assert result['end']==g.local_time(start).replace(hour=g.local_time(start).hour+1).isoformat()
    assert 'busy' not in result and lookup.call_count==1


def test_partial_conflict_returns_no_and_safe_alternatives(monkeypatch):
    lookup=Mock(side_effect=[
        {'ok':True,'busy':[{'start':'2030-01-04T11:30:00-05:00','end':'2030-01-04T12:30:00-05:00'}]},
        {'ok':True,'suggested_slots':[{'start':'2030-01-04T12:30:00-05:00','end':'2030-01-04T13:30:00-05:00'}]}])
    monkeypatch.setattr(g,'availability',lookup)
    result=cc.meeting_time('2030-01-04T11:00:00-05:00')
    assert result['available'] is False and len(result['alternatives'])==1
    assert '11:30' not in str(result) and 'busy' not in result


def test_conflict_at_boundary_does_not_block_adjacent_meeting(monkeypatch):
    monkeypatch.setattr(g,'availability',lambda *a:{'ok':True,'busy':[{'start':'2030-01-04T10:00:00-05:00','end':'2030-01-04T11:00:00-05:00'}]})
    assert cc.meeting_time('2030-01-04T11:00:00-05:00')['available']


def test_failed_calendar_check_is_never_a_yes(monkeypatch):
    monkeypatch.setattr(g,'availability',lambda *a:{'ok':False})
    result=asyncio.run(cc.meeting_time_tool()(start_datetime='2030-01-04T11:00:00-05:00'))
    assert not result['ok'] and 'available' not in result


def test_alternative_lookup_failure_preserves_no(monkeypatch):
    lookup=Mock(side_effect=[{'ok':True,'busy':[{'start':'2030-01-04T11:30:00-05:00','end':'2030-01-04T12:30:00-05:00'}]},g.GoogleError('failed')])
    monkeypatch.setattr(g,'availability',lookup)
    result=cc.meeting_time('2030-01-04T11:00:00-05:00')
    assert result['available'] is False and result['alternatives_unavailable']


@pytest.mark.parametrize('duration',[0,True,481,'60'])
def test_invalid_exact_duration_never_reads_calendar(monkeypatch,duration):
    api=Mock();monkeypatch.setattr(g,'availability',api)
    with pytest.raises(ValueError):cc.meeting_time('2030-01-04T11:00:00-05:00',duration)
    api.assert_not_called()


@pytest.mark.parametrize('start,end',[('2030-11-03T01:30:00-04:00','2030-11-03T01:30:00-05:00'),('2030-03-10T01:30:00-05:00','2030-03-10T03:30:00-04:00')])
def test_exact_duration_is_elapsed_across_dst(monkeypatch,start,end):
    monkeypatch.setattr(g,'availability',lambda *a:{'ok':True,'busy':[]})
    assert cc.meeting_time(start)['end']==end


@pytest.mark.parametrize('channel',['sms','voice'])
def test_exact_time_tool_available_on_both_contact_channels(channel):
    session,tools=asyncio.run(inbound.build_session(BASE,CONTACT,SID,CALLER,channel,Memory()))
    assert 'check_meeting_time' in {t.name for t in tools}
    assert 'An availability question is not permission to book' in full_instructions(session)

@pytest.mark.parametrize('channel',['sms','voice'])
def test_new_contacts_require_john_approval_and_have_no_calendar_tools(channel):
    session,tools=asyncio.run(inbound.build_session(BASE,{},SID,CALLER,channel,Memory()))
    assert not {'check_meeting_time','check_calendar','create_meeting','find_meetings'} & {t.name for t in tools}
    assert "Since you're not on John's pre-approved list" in full_instructions(session)
    assert 'save the request with report_call_outcome' in full_instructions(session)

@pytest.mark.parametrize('channel',['sms','voice'])
def test_unnamed_favorite_can_check_then_introduce_and_book(monkeypatch,channel):
    context={'contact':{'name':'','source':'apple_favorite','email':'','is_owner':False},'history':[]}
    memory=Memory();memory.remember_name=Mock(return_value=True);memory.action_result=Mock()
    sender=AsyncMock(return_value={'ok':True,'event_id':'named-booking'})
    monkeypatch.setattr(g,'create_meeting',sender)
    monkeypatch.setattr(g,'availability',lambda *a:{'ok':True,'busy':[]})
    async def run():
        session,tools=await inbound.build_session(BASE,context,SID,CALLER,channel,memory)
        reg={t.name:t for t in tools}
        assert "OPENING: Hi, this is Alli" in full_instructions(session)
        assert '"identity_confirmed": true' in full_instructions(session)
        assert (await reg['check_meeting_time'](start_datetime='2030-01-04T11:00:00-05:00'))['available']
        args=dict(start_datetime='2030-01-04T11:00:00-05:00',duration_minutes=60,title='Meeting',recipient_email_address='jane@example.com',confirmed=True)
        assert not (await reg['create_meeting'](**args))['ok']
        assert (await reg['remember_contact_name'](name='Jane Doe'))['ok']
        assert (await reg['create_meeting'](**args))['event_id']=='named-booking'
        assert sender.await_args.kwargs['attendee_name']=='Jane Doe'
    asyncio.run(run())
