import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
import appointment_reminders as reminders
import scheduled_sms as sms
from test_inbound import service, CALLER

NOW=datetime(2026,10,23,10,30,tzinfo=reminders.TZ)
PERSON=dict(phone=CALLER,name='Jane Doe',email='jane@example.com')

@pytest.fixture(autouse=True)
def no_real_booking_reads(monkeypatch):
    monkeypatch.setattr(reminders,'booked_contacts',lambda *a:{})
    import reminder_reasoning
    from unittest.mock import AsyncMock
    monkeypatch.setattr(reminder_reasoning,'draft_followup',AsyncMock(side_effect=lambda item:dict(message=item['message'],mode='test')))


def event(**changes):
    return {**dict(id='meeting-1',status='confirmed',organizer={'self':True},
        start={'dateTime':(NOW+timedelta(hours=2)).isoformat()},
        end={'dateTime':(NOW+timedelta(hours=3)).isoformat()},
        location='405 Jessen Lane, Suite E',attendees=[{'email':PERSON['email'],'responseStatus':'accepted'}]),**changes}


@pytest.mark.parametrize('changes', [dict(status='cancelled'),dict(attendeesOmitted=True),
    dict(start={'date':'2026-10-23'}),dict(attendees=[]),dict(transparency='transparent'),
    dict(attendees=[{'email':'stranger@example.com'}]),
    dict(attendees=[{'email':PERSON['email'],'responseStatus':'declined'}]),
    dict(organizer={'self':False})])
def test_only_current_timed_meetings_with_the_bound_participant_are_candidates(monkeypatch,changes):
    monkeypatch.setattr(reminders,'contacts',lambda:{PERSON['email']:PERSON})
    monkeypatch.setattr(reminders,'upcoming',lambda _:[event(**changes)])
    assert reminders.candidates(NOW)==[]


def test_reminder_facts_do_not_assume_logistical_help_is_needed(monkeypatch):
    monkeypatch.setattr(reminders,'contacts',lambda:{PERSON['email']:PERSON})
    monkeypatch.setattr(reminders,'upcoming',lambda _:[event()])
    result=reminders.candidates(NOW)[0]
    assert result['phone']==CALLER
    assert result['send_at']==NOW.isoformat()
    assert "Hi Jane, it's Alli" in result['message']
    assert '12:30 PM' in result['message'] and '405 Jessen Lane' in result['message']
    assert 'directions' not in result['message'] and 'STOP REMINDERS' in result['message']
    assert reminders.candidates(NOW+timedelta(minutes=10))[0]['request_id']==result['request_id']
    assert reminders.candidates(NOW-timedelta(seconds=1))==[]
    assert reminders.candidates(NOW+timedelta(minutes=16))==[]


def test_verified_booking_receipt_matches_only_its_event_without_a_saved_email(monkeypatch):
    monkeypatch.setattr(reminders,'contacts',lambda:{})
    monkeypatch.setattr(reminders,'booked_contacts',lambda *a:{'meeting-1':[dict(PERSON,booking_receipt=True)]})
    monkeypatch.setattr(reminders,'upcoming',lambda _:[event(),event(id='unrelated-meeting')])
    result=reminders.candidates(NOW)
    assert len(result)==1 and result[0]['event_id']=='meeting-1'
    assert result[0]['booking_receipt']


@pytest.mark.parametrize('hour,expected_day,expected_hour',[(8,22,18),(10,23,8),(23,23,19)])
def test_quiet_hours_shift_early_or_late_meetings(hour,expected_day,expected_hour):
    start=NOW.replace(hour=hour,minute=0)
    due=reminders.due_time(start)
    assert (due.day,due.hour)==(expected_day,expected_hour)
    assert due<start


def test_complete_pagination_required_before_dispatch(monkeypatch):
    monkeypatch.setattr(reminders.google,'api',lambda *a,**k:dict(items=[event()],nextPageToken='another'))
    with pytest.raises(ValueError,match='incomplete'):reminders.upcoming(NOW)


def test_calendar_recheck_suppresses_a_moved_meeting_or_disabled_policy(monkeypatch):
    current=event()
    context=dict(event_id=current['id'],email=PERSON['email'],snapshot=reminders.snapshot(current,PERSON))
    monkeypatch.setattr(reminders,'settings',lambda:dict(enabled=True))
    monkeypatch.setattr(reminders,'contacts',lambda:{PERSON['email']:PERSON})
    monkeypatch.setattr(reminders.google,'api',lambda *a,**k:{**current,'location':'Different place'})
    assert not reminders.revalidate(context,CALLER)
    monkeypatch.setattr(reminders,'settings',lambda:dict(enabled=False))
    assert not reminders.revalidate(context,CALLER)


def test_scanner_does_not_recreate_prior_or_uncertain_request(monkeypatch):
    monkeypatch.setattr(reminders,'settings',lambda:dict(enabled=True))
    item=dict(request_id='one',phone=CALLER,recipient_name='Jane',message='Reminder',event_id='event',email=PERSON['email'],snapshot='snap')
    monkeypatch.setattr(reminders,'candidates',lambda _:[item])
    monkeypatch.setattr(sms,'get',lambda _:dict(status='uncertain'))
    save=Mock();monkeypatch.setattr(sms,'enqueue',save)
    result=asyncio.run(reminders.AppointmentReminders().run_once())
    assert result['scheduled']==[]
    save.assert_not_called()


def test_scanner_passes_server_owned_event_binding_to_outbox(monkeypatch):
    monkeypatch.setattr(reminders,'settings',lambda:dict(enabled=True))
    item=dict(request_id='one',phone=CALLER,recipient_name='Jane',message='Reminder',event_id='event',email=PERSON['email'],snapshot='snap')
    monkeypatch.setattr(reminders,'candidates',lambda _:[item])
    monkeypatch.setattr(sms,'get',lambda _:dict(status='not_found'))
    monkeypatch.setattr(reminders,'revalidate',lambda *a:True)
    save=Mock(return_value=dict(status='scheduled'));monkeypatch.setattr(sms,'enqueue',save)
    asyncio.run(reminders.AppointmentReminders().run_once())
    context=save.call_args.kwargs['context']
    assert {k:v for k,v in context.items() if k!='conversation_draft'}==dict(kind='appointment_reminder',event_id='event',email=PERSON['email'],snapshot='snap')


def test_changed_calendar_is_checked_before_any_twilio_send(service,monkeypatch):
    row=dict(request_id='reminder',phone=CALLER,body='Hello',callback_token='token',context={'kind':'appointment_reminder'})
    monkeypatch.setattr(sms,'claim',lambda:row)
    monkeypatch.setattr(reminders,'revalidate',lambda *a:False)
    receipt=Mock();monkeypatch.setattr(sms,'receipt',receipt)
    client=Mock();monkeypatch.setattr(service,'client',client)
    asyncio.run(sms.ScheduledSMS(service).process_one())
    client.assert_not_called()
    receipt.assert_called_once_with('reminder',status='cancelled',error_code='event_changed')


def test_ordinary_text_does_not_write_preferences(monkeypatch):
    initialize=Mock();monkeypatch.setattr(reminders.store,'initialize',initialize)
    assert reminders.preference_command(CALLER,'Do you remember me?') is None
    initialize.assert_not_called()


def test_owner_policy_changes_require_explicit_owner_request(monkeypatch):
    import owner_sms
    owner='+18435550199'
    monkeypatch.setenv('ALLI_OWNER_PHONE',owner)
    setting=Mock(return_value=dict(enabled=False));monkeypatch.setattr(reminders,'settings',setting)
    async def run():
        for number in (owner,CALLER):
            tools={t.name:t for t in owner_sms.text_tools(number,'sms','policy-test')}
            assert not (await tools['set_appointment_reminders'](enabled=True,owner_requested=False))['ok']
            if number==CALLER:
                assert not (await tools['set_appointment_reminders'](enabled=True,owner_requested=True))['ok']
    asyncio.run(run())
    setting.assert_not_called()
