"""Real isolated Postgres, fake Google/native timing; never makes a real call."""
import copy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace as NS

from cryptography.fernet import Fernet
import pytest

from adapters.calls import CallRepository
from adapters.calendar import CalendarService
from adapters.oversight import Oversight
from adapters.people_workflow import PeopleWorkflow, PREP_TOOLS, CALL_TOOLS, digest
from adapters.postgres import PostgresRepository
from adapters.relationships import Relationships
from adapters.scheduling import Scheduler
from alli_core.authority import Actor,PermissionDenied
from alli_core.profile import Profile
from service import dashboard_views
from test_scheduling import Native


class Google:
    owner_email='owner@example.com';calendar_id='primary'
    def __init__(self):self.events=[];self.ids=['message1'];self.booked=[]
    def request(self,method,path,params=None):
        if 'gmail/' in path:return {'messages':[{'id':x} for x in self.ids]}
        if path.endswith('/events'):return {'items':copy.deepcopy(self.events)}
        return copy.deepcopy(next(e for e in self.events if e['id']==path.split('/')[-1]))
    def search_email(self,*a,**k):return {'threads':[]}
    def read_email_thread(self,*a,**k):return {'messages':[]}
    def create_event(self,**values):self.booked.append(values);return {'ok':True,'event_id':'new-event'}


@pytest.fixture
def setup(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'));repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'dot',True,'test-owner')
    person=repo.save_person(owner,name='Karen Example',phone='+18435550101',email='karen@example.com')
    person=repo.set_person_status(owner,str(person['id']),'approved',1)
    source=repo.begin_conversation('+18435550101','prior','sms')['conversation']['id']
    repo.save_transcript(str(source),[{'role':'user','text':'I will meet at Jessen.'}],finalized=True)
    task=repo.submit(owner,source_id='configure',title='Configure policy',instruction='Configure the authorized appointment policy.')
    api=NS(with_options=lambda **kw:api)
    scheduler=Scheduler(repo,api,Fernet.generate_key(),'https://example.com');scheduler.native=Native()
    calls=CallRepository(repo,'+18435550199');google=Google()
    flow=PeopleWorkflow(repo,google,scheduler,calls);calls.people_workflow=flow
    event={'id':'appointment1','etag':'rev1','updated':'2026-10-31T10:00:00Z','status':'confirmed',
        'summary':'Appointment','organizer':{'email':google.owner_email},'location':'Charleston AI 405 Jessen Lane Suite E',
        'start':{'dateTime':'2026-11-02T09:30:00-05:00'},'end':{'dateTime':'2026-11-02T10:30:00-05:00'},
        'attendees':[{'email':google.owner_email,'self':True,'responseStatus':'accepted'},
                     {'email':'karen@example.com','responseStatus':'needsAction'}]}
    google.events=[event]
    return repo,owner,str(person['id']),task,flow,google,event


def ready(setup,prep,*,brief='Confirm the saved meeting at Jessen.'):
    repo,owner,person,task,flow,google,event=setup
    return flow.save_preparation(owner,prep['task_id'],state='ready',safe_brief=brief,
        facts=[{'text':'Saved contact and appointment logistics','source':prep['task_id'],'observed_at':'2026-10-31'}],
        sources=[{'reference':prep['task_id'],'observed_at':'2026-10-31'}],observation='Reports meeting at Jessen.')


@pytest.mark.asyncio
async def test_policy_activation_inside_current_write_and_native_clock_once(setup):
    repo,owner,person,task,flow,google,event=setup
    assert repo.claim_action(task['id'],'configure-call','configure_appointment_policy',{'enabled':True},True)['state']=='claimed'
    result=await flow.configure(owner,task['id'])
    assert result['planner']['scheduled']
    assert await flow.configure(owner,task['id'])
    assert flow.scheduler.native.creates==1
    policy=Oversight(repo).policies(owner)['appointment_confirmation']
    assert policy['automated'] and policy['state']=='enabled'
    row=flow.scheduler.raw_schedule(result['planner']['schedule']['id'])
    invocation=flow.scheduler.native.fire(row['provider_id'])
    launch=flow.scheduler.materialize(row,invocation,{'title':row['title'],'instruction':row['instruction'],'revision':row['revision']})
    assert repo.task_result(launch['task_id'])['context']['appointment_planner']
    friend=Actor('alli',person,'sms')
    with pytest.raises(PermissionDenied):await flow.configure(friend,task['id'])


@pytest.mark.asyncio
async def test_dst_native_schedule_dedupe_and_inspectable_coverage(setup):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id'])
    result=await flow.plan(owner,task['id'])
    assert result['calendar_events_examined']==1
    assert (await flow.plan(owner,task['id']))['outcomes']==[]
    with repo.connection() as db:
        row=db.execute('SELECT * FROM ea_appointment_intent').fetchone()
    assert row['due_at'].astimezone(timezone.utc).isoformat()=='2026-11-01T16:00:00+00:00' # DST has ended.
    assert row['state']=='scheduled'
    native=flow.scheduler.raw_schedule(row['schedule_id'])
    assert native['authority']['allowed_tools']==list(CALL_TOOLS)
    invocation=flow.scheduler.native.fire(native['provider_id'],2)
    launched=flow.scheduler.materialize(native,invocation,{'title':native['title'],'instruction':native['instruction'],'revision':1})
    assert repo.task_result(launched['task_id'])['context']['appointment_id']==row['id']
    coverage=Oversight(repo).coverage(owner)
    assert coverage['events'][0]['schedule_id']==row['schedule_id']
    assert 'preparation pending' in dashboard_views.coverage(coverage,'America/New_York')
    assert flow.scheduler.native.creates==2 # hourly planner and one call


@pytest.mark.asyncio
async def test_pending_direct_call_survives_and_mission_idempotency_is_unchanged(setup):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id'])
    restricted=replace(owner,allowed_tools=('get_context',))
    report=repo.submit(restricted,source_id='direct',title='Call',instruction='Report actual call evidence')
    call=flow.calls.queue(report['id'],person,'Ask a concise logistical question.','direct-call')
    assert call['state']=='queued' and call['preparation_id']
    assert call['instruction']=='Ask a concise logistical question.'
    assert flow.pre_dial(call) is None
    with repo.connection() as db:prep=db.execute('SELECT * FROM ea_person_preparation WHERE id=%s',(call['preparation_id'],)).fetchone()
    ready(setup,prep)
    assert flow.pre_dial(call) is True
    assert flow.calls.queue(report['id'],person,call['instruction'],'direct-call')['id']==call['id']
    assert flow.calls.get(call['id'])['instruction']==call['instruction']


@pytest.mark.asyncio
async def test_multiple_waiters_and_canceled_waiter_do_not_lose_continuation(setup):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id'])
    a=repo.submit(owner,source_id='a',title='A',instruction='Prepare A')
    b=repo.submit(owner,source_id='b',title='B',instruction='Prepare B')
    prep=flow.request_preparation(owner,a['id'],person)
    assert flow.request_preparation(owner,b['id'],person)['id']==prep['id']
    with repo.connection() as db:db.execute("UPDATE ea_task SET state='cancelled' WHERE id=%s",(a['id'],))
    ready(setup,prep)
    assert len(repo.pending_inputs(b['id']))==1
    flow.deliver_preparation_waiters()
    assert len(repo.pending_inputs(b['id']))==1
    with repo.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM ea_preparation_waiter WHERE delivered').fetchone()['n']==2
    assert Relationships(repo).read(owner,person)['preparation']['state']=='ready'
    with pytest.raises(PermissionDenied):flow.preparation_evidence(owner,task['id'])
    friend=Actor('alli',person,'sms')
    with pytest.raises(PermissionDenied):flow.preparation_evidence(friend,prep['task_id'])


@pytest.mark.asyncio
async def test_booking_waits_and_same_arguments_can_resume(setup):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id'])
    calendar=CalendarService(repo,google,people_workflow=flow)
    prep=calendar.prepare_booking(task['id'],'karen@example.com')
    values=dict(action_key='booking',title='Meet',start=event['start']['dateTime'],end=event['end']['dateTime'],invitation_email='karen@example.com')
    with pytest.raises(ValueError):calendar.book(task['id'],**values)
    assert not google.booked
    ready(setup,prep)
    assert calendar.book(task['id'],**values)['ok'] and len(google.booked)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['calendar','mail','sms','disabled','endpoint','prep_binding'])
async def test_fresh_pre_dial_guard_holds_without_carrier_and_no_second_attempt(setup,monkeypatch,change):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id']);await flow.plan(owner,task['id'])
    with repo.connection() as db:intent=db.execute('SELECT * FROM ea_appointment_intent').fetchone()
    native=flow.scheduler.raw_schedule(intent['schedule_id']);invocation=flow.scheduler.native.fire(native['provider_id'],5)
    launch=flow.scheduler.materialize(native,invocation,{'title':native['title'],'instruction':native['instruction'],'revision':1})
    calltask=launch['task_id'];actor=repo.owner_actor('scheduled-test')
    context=flow.appointment_context(actor,calltask);ready(setup,context['preparation'])
    call=flow.calls.queue(calltask,person,'Confirm the appointment.','call')
    monkeypatch.setattr('adapters.people_workflow.now',lambda:intent['due_at'])
    if change=='calendar':google.events[0]['etag']='new'
    elif change=='mail':google.ids.append('new-message')
    elif change=='sms':
        new=repo.begin_conversation('+18435550101','new-sms','sms')['conversation']['id']
        repo.save_transcript(str(new),[{'role':'user','text':'Please do not call.'}],finalized=True)
    elif change=='disabled':await flow.configure(owner,task['id'],enabled=False)
    elif change=='prep_binding':
        google.ids.append('other')
        replacement=flow.request_preparation(owner,task['id'],person,event={**event,'id':'other'})
        ready(setup,replacement)
        google.ids.pop()
        with repo.connection() as db:db.execute('UPDATE ea_appointment_intent SET preparation_id=%s WHERE id=%s',(replacement['id'],intent['id']))
    else:
        with repo.connection() as db:db.execute("UPDATE ea_endpoint SET address='+18435550102' WHERE person_id=%s AND kind='phone'",(person,))
    assert flow.pre_dial(call) is False
    with repo.connection() as db:assert db.execute('SELECT state FROM ea_appointment_intent').fetchone()['state']=='held'


@pytest.mark.asyncio
async def test_one_atomic_attempt_and_correct_bound_preparation_for_voice(setup,monkeypatch):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id']);await flow.plan(owner,task['id'])
    with repo.connection() as db:intent=db.execute('SELECT * FROM ea_appointment_intent').fetchone()
    native=flow.scheduler.raw_schedule(intent['schedule_id']);invocation=flow.scheduler.native.fire(native['provider_id'],9)
    launch=flow.scheduler.materialize(native,invocation,{'title':native['title'],'instruction':native['instruction'],'revision':1})
    context=flow.appointment_context(owner,launch['task_id']);ready(setup,context['preparation'],brief='Bound appointment A at Jessen.')
    call=flow.calls.queue(launch['task_id'],person,'Confirm appointment.','one')
    google.ids.append('other-message')
    other=flow.request_preparation(owner,task['id'],person,event={**event,'id':'other'})
    ready(setup,other,brief='Different appointment B elsewhere.')
    # Restore correspondence, preserving original preparation A's bound evidence.
    google.ids.pop()
    full=flow.calls.get(call['id'])
    brief=await Relationships(repo).voice_brief(full)
    assert brief['prepared_logistics']=='Bound appointment A at Jessen.'
    assert 'Different appointment' not in str(brief)
    monkeypatch.setattr('adapters.people_workflow.now',lambda:intent['due_at'])
    assert flow.pre_dial(call) is True
    assert flow.pre_dial(call) is False
    with repo.connection() as db:assert db.execute('SELECT state FROM ea_appointment_intent').fetchone()['state']=='attempted'


@pytest.mark.asyncio
async def test_exclusions_unknown_contact_and_stale_coverage_are_visible(setup):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id'])
    declined=copy.deepcopy(event);declined['id']='declined';declined['attendees'][1]['responseStatus']='declined'
    unknown=copy.deepcopy(event);unknown['id']='unknown';unknown['attendees'][1]['email']='unknown@example.com'
    hachiya=copy.deepcopy(event);hachiya['id']='hachiya';hachiya['summary']='Hachiya meeting'
    google.events=[declined,unknown,hachiya]
    await flow.plan(owner,task['id'])
    data=Oversight(repo).coverage(owner)
    assert [e['state'] for e in data['events']]==['excluded','blocked','excluded']
    with repo.connection() as db:assert db.execute('SELECT count(*) AS n FROM ea_appointment_intent').fetchone()['n']==0
    flow.save_coverage([],datetime.now(timezone.utc),datetime.now(timezone.utc)+timedelta(days=8),state='unavailable',reason='Provider unavailable')
    assert len(Oversight(repo).coverage(owner)['events'])==3
    assert 'Coverage is stale or unavailable' in dashboard_views.coverage(Oversight(repo).coverage(owner),'America/New_York')


@pytest.mark.asyncio
async def test_unfamiliar_research_requires_public_sources_and_nickname_provenance(setup):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id'])
    with repo.connection() as db:db.execute('DELETE FROM ea_conversation WHERE person_id=%s',(person,))
    prep=flow.request_preparation(owner,task['id'],person)
    with pytest.raises(ValueError):ready(setup,prep)
    with pytest.raises(ValueError):flow.save_preparation(owner,prep['task_id'],state='ready',safe_brief='Hello',facts=[],sources=[{'reference':'https://example.com/professional','observed_at':'2026-10-31'}],preferred_name='K')
    saved=flow.save_preparation(owner,prep['task_id'],state='blocked',safe_brief='',facts=[],sources=[],reason='Identity could not be disambiguated')
    assert saved['state']=='blocked'


@pytest.mark.asyncio
async def test_relationship_maintenance_uses_latest_conversation_and_does_not_starve(setup):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id'])
    for label in ['A','B']:
        row=repo.begin_conversation('+18435550101','new-'+label,'sms')['conversation']
        repo.save_transcript(str(row['id']),[{'role':'user','text':'Actual message '+label}],finalized=True)
    flow.refresh_conversations();flow.refresh_conversations()
    with repo.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM ea_person_preparation').fetchone()['n']==1
        assert db.execute("SELECT count(*) AS n FROM ea_task WHERE source_id LIKE 'relationship-maintenance:%%'").fetchone()['n']==1


@pytest.mark.asyncio
async def test_prior_manual_attempt_and_conditional_work_prevent_duplicate_schedule(setup):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id'])
    manual=repo.submit(owner,source_id='manual',title='Manual call',instruction='Confirm appointment')
    call=flow.calls.queue(manual['id'],person,'Confirm this appointment.','manual')
    with repo.connection() as db:db.execute("UPDATE ea_phone_call SET state='finished' WHERE id=%s",(call['id'],))
    repo.record_outcome(manual['id'],'completed','Attempted',turn_id='done',notify=False)
    assert 'prior confirmation' in flow.conflict(flow.person(person),event['id'])
    await flow.plan(owner,task['id'])
    with repo.connection() as db:assert db.execute('SELECT state FROM ea_appointment_intent').fetchone()['state']=='held'


@pytest.mark.asyncio
async def test_authenticated_mcp_configure_and_pending_booking_resume_without_cached_rejection(setup):
    from adapters.executive_mcp import create_tool_server,tool_http_app
    from test_executive_mcp import connected
    repo,owner,person,task,flow,google,event=setup
    app=tool_http_app(create_tool_server(repo,'http://testserver',google=google,calls=flow.calls,
        scheduler=flow.scheduler,people_workflow=flow),'http://testserver')
    token=repo.issue_tool_grant(task['id'])
    async with app.router.lifespan_context(app):
        async with connected(app,token) as client:
            configured=await client.call_tool('configure_appointment_policy',{'enabled':True})
            assert configured.structured_content['enabled'] is True
            args=dict(title='Meet',start=event['start']['dateTime'],end=event['end']['dateTime'],invitation_email='karen@example.com')
            pending=await client.call_tool('book_meeting',args)
            assert pending.structured_content['pending'] and not google.booked
            ready(setup,pending.structured_content['preparation'])
            booked=await client.call_tool('book_meeting',args)
            assert booked.structured_content['ok'] and len(google.booked)==1
            assert (await client.call_tool('book_meeting',args)).structured_content['ok']
            assert len(google.booked)==1
        friend=Actor('alli',person,'phone',False,'')
        job=repo.submit(friend,source_id='spoofed',title='Request',instruction='Prepare me')
        async with connected(app,repo.issue_tool_grant(job['id'])) as client:
            result=await client.call_tool('prepare_interaction',{'person_id':person})
            assert result.is_error or not result.structured_content.get('ok')
            denied=await client.call_tool('preparation_evidence',{})
            assert denied.is_error


@pytest.mark.asyncio
async def test_disabling_and_resuming_reuses_same_native_planner(setup):
    repo,owner,person,task,flow,google,event=setup
    first=await flow.configure(owner,task['id'])
    await flow.configure(owner,task['id'],enabled=False)
    assert not Oversight(repo).appointment_policy(owner)['automated']
    await flow.configure(owner,task['id'])
    assert Oversight(repo).appointment_policy(owner)['automated']
    assert flow.scheduler.native.creates==1


@pytest.mark.asyncio
async def test_failed_research_is_visible_blocked_and_does_not_strand_call(setup):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id'])
    call=flow.calls.queue(task['id'],person,'Ask a question.','pending-failure')
    with repo.connection() as db:
        prep=db.execute('SELECT * FROM ea_person_preparation WHERE id=%s',(call['preparation_id'],)).fetchone()
    repo.record_outcome(prep['task_id'],'failed','Research unavailable',turn_id='failed',notify=False)
    flow.deliver_preparation_waiters()
    assert flow.pre_dial(call) is False
    assert Relationships(repo).read(owner,person)['preparation']['state']=='blocked'


@pytest.mark.asyncio
async def test_calendar_cancellation_stops_native_schedule_and_retains_reason(setup):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id']);await flow.plan(owner,task['id'])
    event['status']='cancelled'
    await flow.plan(owner,task['id'])
    with repo.connection() as db:intent=db.execute('SELECT * FROM ea_appointment_intent').fetchone()
    assert intent['state']=='cancelled'
    assert flow.scheduler.raw_schedule(intent['schedule_id'])['state']=='cancelled'
    assert Oversight(repo).coverage(owner)['events'][0]['reason']=='Calendar appointment cancelled'


@pytest.mark.asyncio
async def test_preparation_works_with_single_connection_pool_and_restart(setup,database):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id'])
    pooled=PostgresRepository(database,repo.profile);pooled.open(max_size=1)
    try:
        other=PeopleWorkflow(pooled,google,flow.scheduler,flow.calls)
        prep=other.request_preparation(owner,task['id'],person)
        assert other.preparation_evidence(owner,prep['task_id'])['person']['name']=='Karen Example'
        other.save_preparation(owner,prep['task_id'],state='ready',safe_brief='Hi Karen.',facts=[],
            sources=[{'reference':prep['task_id'],'observed_at':'2026-10-31'}])
        assert other.request_preparation(owner,task['id'],person)['state']=='ready'
        repo.initialize() # repeatable additive migration retains the same source/intent.
        assert other.request_preparation(owner,task['id'],person)['id']==prep['id']
    finally:pooled.close()


@pytest.mark.asyncio
async def test_nickname_requires_this_person_self_statement_and_is_cached_before_greeting(setup):
    repo,owner,person,task,flow,google,event=setup
    await flow.configure(owner,task['id'])
    conversation=repo.begin_conversation('+18435550101','nickname','sms')['conversation']
    repo.save_transcript(str(conversation['id']),[{'role':'user','text':'Please call me Kay.'}],finalized=True)
    prep=flow.request_preparation(owner,task['id'],person)
    reference='conversation:'+str(conversation['id'])
    flow.save_preparation(owner,prep['task_id'],state='ready',safe_brief='Greet Kay naturally.',facts=[],
        sources=[{'reference':reference,'observed_at':'2026-10-31'}],preferred_name='Kay',name_source=reference)
    call=flow.calls.queue(task['id'],person,'Ask the authorized question.','nickname-call')
    assert flow.calls.get(call['id'])['name']=='Kay'
    google.ids.append('another')
    other=flow.request_preparation(owner,task['id'],person)
    with pytest.raises(ValueError):
        flow.save_preparation(owner,other['task_id'],state='ready',safe_brief='Hello',facts=[],
            sources=[{'reference':reference,'observed_at':'2026-10-31'}],preferred_name='Invented',name_source=reference)
