import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from appointment_confirmation import Appointment, ConfirmationPolicy, eligibility, dispatch, confirmation_session

NOW = datetime(2026, 10, 1, 14, tzinfo=timezone.utc)


def appointment(**changes):
    data = dict(calendar_id='primary', event_id='event-instance-1', revision='etag-1',
                start=NOW+timedelta(hours=1), end=NOW+timedelta(hours=2), status='confirmed',
                eligible=True, recipient_id='contact-1', recipient_name='Pat', phone='+12025550123',
                phone_verified=True, phone_source='verified-address-book', recipient_timezone='America/New_York')
    return Appointment(**(data | changes))


def policy(**changes):
    data = dict(enabled=True, approved=True, eligible_event_rule='explicitly opted-in Charleston AI appointments',
                verified_phone_source='verified-address-book', quiet_start_hour=20, quiet_end_hour=9, voicemail='hang_up')
    return ConfirmationPolicy(**(data | changes))


class MemoryLedger:
    def __init__(self): self.rows = {}
    def claim(self, key, context):
        if key in self.rows: return False
        self.rows[key] = {'status':'dispatching', 'context':context}
        return True
    def queued(self,key,sid): self.rows[key].update(status='queued', call_sid=sid)
    def uncertain(self,key): self.rows[key]['status']='uncertain'
    def blocked(self,key,reason): self.rows[key].update(status='blocked', reason=reason)


def test_profile_replaces_demo_context_without_sharing_state():
    base = {'instructions':'PROPERTY SECRET', 'delegation':{'responses':{'tools':['send_email']}}, 'audio':{}}
    session = confirmation_session(appointment(), base)
    assert 'PROPERTY SECRET' not in session['instructions']
    assert 'delegation' not in session
    assert base['delegation']['responses']['tools'] == ['send_email']


@pytest.mark.parametrize('changes,reason', [
    ({'status':'cancelled'}, 'event_not_confirmed'),
    ({'status':'tentative'}, 'event_not_confirmed'),
    ({'eligible':False}, 'event_not_eligible'),
    ({'phone_verified':False}, 'phone_not_verified'),
    ({'phone_source':'guessed'}, 'phone_not_verified'),
    ({'recipient_timezone':''}, 'recipient_timezone_missing_or_invalid'),
    ({'recipient_timezone':'Asia/Tokyo'}, 'quiet_hours'),
    ({'start':NOW+timedelta(minutes=30)}, 'outside_dispatch_window'),
])
def test_fail_closed(changes, reason):
    assert reason in eligibility(appointment(**changes), policy(), NOW)


def test_defaults_and_window_boundaries():
    assert 'disabled' in eligibility(appointment(), ConfirmationPolicy(), NOW)
    assert eligibility(appointment(), policy(), NOW) == []
    assert 'outside_dispatch_window' in eligibility(appointment(), policy(), NOW-timedelta(microseconds=1))
    assert 'outside_dispatch_window' in eligibility(appointment(), policy(), NOW+timedelta(minutes=10))


def test_claim_before_dial_and_no_repeat():
    row, store, calls = appointment(), MemoryLedger(), []
    async def refresh(item): return item
    async def dial(item,key):
        assert store.rows[key]['status']=='dispatching'
        calls.append(item)
        return {'call_sid':'CA-test'}
    async def run():
        first=await dispatch(row,policy(),store,refresh,dial,lambda:NOW)
        second=await dispatch(row,policy(),store,refresh,dial,lambda:NOW)
        return first,second
    first,second=asyncio.run(run())
    assert first['status']=='queued' and second['status']=='duplicate' and len(calls)==1


def test_uncertain_never_retries():
    row, store = appointment(), MemoryLedger()
    async def refresh(item): return item
    async def dial(item,key): raise TimeoutError('response lost')
    with pytest.raises(TimeoutError): asyncio.run(dispatch(row,policy(),store,refresh,dial,lambda:NOW))
    assert store.rows[row.dispatch_key()]['status']=='uncertain'
    assert asyncio.run(dispatch(row,policy(),store,refresh,dial,lambda:NOW))['status']=='duplicate'


@pytest.mark.parametrize('change', [{'status':'cancelled'}, {'revision':'new'}, {'phone':'+12025550124'}])
def test_revalidates_after_claim(change):
    row, store, reads = appointment(), MemoryLedger(), []
    async def refresh(item):
        reads.append(1)
        return item if len(reads)==1 else appointment(**change)
    async def dial(*args): pytest.fail('Must not dial changed appointment')
    assert asyncio.run(dispatch(row,policy(),store,refresh,dial,lambda:NOW))['status']=='blocked'
    assert store.rows[row.dispatch_key()]['status']=='blocked'


def test_dedup_stable_across_reschedule_and_contact_change():
    assert appointment().dispatch_key()==appointment(revision='new',phone='+12025550124',start=NOW+timedelta(minutes=90)).dispatch_key()
    assert appointment().dispatch_key()!=appointment(recipient_id='another-contact').dispatch_key()
