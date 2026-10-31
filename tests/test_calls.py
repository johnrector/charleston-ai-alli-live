from concurrent.futures import ThreadPoolExecutor

import pytest

from adapters.calls import CallRepository
from adapters.postgres import PostgresRepository
from alli_core.authority import Actor,Conflict,PermissionDenied
from alli_core.profile import Profile


@pytest.fixture
def call_setup(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'))
    repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'sms',True,'owner-proof')
    person=repo.save_person(owner,name='Test friend',phone='+18435550101')
    repo.set_person_status(owner,str(person['id']),'approved',person['revision'])
    task=repo.submit(owner,source_id='call-test',title='Confirm meeting',instruction='Call my friend and confirm the meeting')
    return repo,owner,str(person['id']),task,CallRepository(repo,'+18435550999')


def test_callback_can_bind_before_create_reply_across_instances(call_setup):
    repo,_,person,task,calls=call_setup
    call=calls.queue(task['id'],person,'Confirm the meeting','one-call')
    assert calls.claim()['id']==call['id']
    another=CallRepository(repo,'+18435550999')
    callback=another.bind(call['id'],'CA-test',to='+18435550101',from_='+18435550999',status='in-progress')
    assert callback['provider_id']=='CA-test'
    # The delayed REST create response cannot create another identity or media slot.
    calls.bind(call['id'],'CA-test',to='+18435550101',from_='+18435550999',status='queued')
    assert another.attach_media(call['id'],'CA-test')['id']==call['id']
    with pytest.raises(Conflict):calls.attach_media(call['id'],'CA-test')
    with pytest.raises(Conflict):
        calls.bind(call['id'],'CA-other',to='+18435550101',from_='+18435550999',status='ringing')
    with pytest.raises(PermissionDenied):
        calls.bind(call['id'],'CA-test',to='+18435550102',from_='+18435550999',status='ringing')


def test_call_claim_and_reworded_request_do_not_redial(call_setup):
    repo,_,person,task,calls=call_setup
    call=calls.queue(task['id'],person,'Confirm Friday','source1')
    assert calls.queue(task['id'],person,'Ask about Friday instead','source2')['id']==call['id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims=list(pool.map(lambda _:CallRepository(repo,'+18435550999').claim(),range(4)))
    assert len([claim for claim in claims if claim])==1
    assert CallRepository(repo,'+18435550999').claim() is None
    assert repo.task_result(task['id'])['context']['pending_calls']==[call['id']]


def test_carrier_completion_waits_for_transcript_and_does_not_prove_answer(call_setup):
    repo,owner,person,task,calls=call_setup
    call=calls.queue(task['id'],person,'Confirm meeting','one-call')
    calls.claim()
    calls.bind(call['id'],'CA-test',to='+18435550101',from_='+18435550999',status='completed',duration=0)
    assert calls.unreported()==[]
    repo.save_transcript(str(call['conversation_id']),[],finalized=True)
    assert calls.unreported()[0]['transcript']==[]
    event=[event for event in repo.ledger(owner) if event['kind']=='call.carrier_status'][0]
    assert event['facts']['conversation_confirmed'] is False
    calls.reported(call['id'])
    assert repo.task_result(task['id'])['context']['pending_calls']==[]
    assert calls.unreported()==[]
    # An old ringing callback cannot reverse a terminal call status.
    late=calls.bind(call['id'],'CA-test',to='+18435550101',from_='+18435550999',status='ringing')
    assert late['state']=='finished' and late['carrier_status']=='completed'


def test_name_recognition_is_separate_from_owner_number_verification(call_setup):
    repo,owner,_,_,calls=call_setup
    form={'To':'+18435550999','From':'+18435550100','CallSid':'CA-inbound1','CallStatus':'in-progress'}
    unverified=calls.incoming(form)
    assert calls.get(unverified['id'])['name']=='John Rector'
    assert calls.get(unverified['id'])['scope']=='friend'
    trusted=calls.incoming(dict(form,CallSid='CA-inbound2',StirVerstat='TN-Validation-Passed-A'))
    assert calls.get(trusted['id'])['scope']=='owner'
    duplicate=calls.incoming(dict(form,CallSid='CA-inbound2',StirVerstat='TN-Validation-Passed-A'))
    assert duplicate['id']==trusted['id']


def test_daily_limits_and_suspension_block_new_calls(call_setup):
    repo,owner,person,task,calls=call_setup
    restricted=CallRepository(repo,'+18435550999',daily_per_person=1)
    restricted.queue(task['id'],person,'First call','one')
    task2=repo.submit(owner,source_id='second-task',title='Another call',instruction='Try another call')
    with pytest.raises(PermissionDenied):restricted.queue(task2['id'],person,'Second call','two')
    contact=repo.recognize('+18435550101')
    repo.set_person_status(owner,person,'suspended',contact['revision'])
    assert calls.claim() is None


def test_late_real_call_outcome_recovers_an_uncertain_create_reply(call_setup):
    repo,_,person,task,calls=call_setup
    call=calls.queue(task['id'],person,'Confirm the meeting','lost-reply')
    calls.claim();calls.uncertain(call['id'],'TimeoutError')
    assert calls.unreported()[0]['state']=='uncertain'
    calls.reported(call['id'])
    calls.bind(call['id'],'CA-recovered',to='+18435550101',from_='+18435550999',status='in-progress')
    repo.save_transcript(str(call['conversation_id']),[{'role':'user','text':'Yes, confirmed.'}],finalized=True)
    calls.bind(call['id'],'CA-recovered',to='+18435550101',from_='+18435550999',status='completed',duration=30)
    assert calls.unreported()[0]['transcript'][0]['text']=='Yes, confirmed.'
    assert calls.claim() is None


def test_context_errors_are_correlated_deduped_and_never_rewrite_call_or_transcript(call_setup):
    repo,owner,person,task,calls=call_setup
    call=calls.queue(task['id'],person,'Confirm the appointment','saved-call')
    calls.claim();calls.bind(call['id'],'CA-saved',to='+18435550101',from_='+18435550999',status='completed',duration=98)
    transcript=[{'role':'assistant','text':'The reminder.'},{'role':'user','text':'Your message has been sent.'}]
    repo.save_transcript(str(call['conversation_id']),transcript,finalized=True)
    for event_id in ['saved_1','saved_2','saved_1']:
        calls.error(call['id'],'context_injection_incomplete',details={'client_event_id':event_id,
            'session_ending':True,'context_command':{'purpose':'saved'},'message':'Pending update on close'})
    evidence={'client_event_id':'saved_1','command_type':'session.thinking.append','purpose':'saved',
        'delegation_id':'work','content_bytes':40,'state':'incomplete','speech_or_playback_confirmed':False}
    calls.context_update(call['id'],evidence);calls.context_update(call['id'],evidence)
    assert len(calls.errors(call['id']))==2 and len(calls.context_updates(call['id']))==1
    with pytest.raises(ValueError):calls.context_update(call['id'],{**evidence,'content':'private text'})
    saved=calls.get(call['id'])
    assert saved['state']=='finished' and saved['transcript']==transcript and saved['finalized']
    assert calls.claim() is None



def test_owner_lookup_cannot_read_another_assistant_call(call_setup,database):
    from dataclasses import replace
    repo,owner,person,task,calls=call_setup
    other=PostgresRepository(database,replace(repo.profile,id='separate-assistant'))
    other.initialize()
    another_owner=Actor('separate-assistant',other.bootstrap_owner('+18435550120'),'dot',True,'another-owner')
    report=other.submit(another_owner,source_id='foreign-call',title='Private call',instruction='Private')
    foreign=CallRepository(other,'+18435550998').queue(report['id'],another_owner.person_id,'Private mission','foreign-source')
    assert calls.lookup(foreign['id']) is None and calls.lookup(str(foreign['conversation_id'])) is None


def test_saved_transport_counts_are_scoped_evidence_and_never_change_call_state(call_setup):
    repo,owner,person,task,calls=call_setup
    call=calls.queue(task['id'],person,'One mission','media-test')
    evidence={'live':{'input_packets_forwarded':200,'transcript_events_received':0},
        'channel':{'inbound_packets':200,'last_finite_playback':{'mark_name':'exact-mark','played':True}}}
    calls.save_media_evidence(call['id'],evidence)
    assert calls.media_evidence(call['id'])=={'call_id':call['id'],**evidence}
    assert calls.get(call['id'])['state']=='queued'
