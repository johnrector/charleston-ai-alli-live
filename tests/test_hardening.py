import asyncio,json
from contextlib import contextmanager
from datetime import datetime,timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock,AsyncMock
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
import inbound_communications as inbound
import owner_access,security_controls as security,public_profile,contact_events
import conference_notes as notes
import phone_tool_policy as policy
import work_agent,work_store,work_routes
from live_observer import capture,AlliLiveConfig,AlliLiveProvider
from test_inbound import BASE,Memory,SID,CALLER

OWNER='+18433276008'

@pytest.fixture(autouse=True)
def boundaries(monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',OWNER);monkeypatch.setenv('DEMO_KEY','unit-secret')
    monkeypatch.setattr(public_profile,'read',lambda:dict(body=public_profile.DEFAULT,sources=public_profile.SOURCES,reviewed_at='2026-10-24'))
    monkeypatch.setattr(owner_access,'authorized',lambda *a:False)
    monkeypatch.setattr(security,'blocked',lambda p:False)

@contextmanager
def connection(db):yield db

def test_owner_phone_and_name_do_not_grant_private_access(monkeypatch):
    challenge=Mock(return_value=dict(ok=False,verification_required=True,message='Check your own phone'))
    monkeypatch.setattr(owner_access,'request',challenge)
    create=Mock();monkeypatch.setattr(work_store,'create',create)
    context=dict(contact=dict(name='John Rector',is_owner=True),number_verified=True,history=[dict(channel='sms',body='PRIVATE SENTINEL')])
    session,tools=asyncio.run(inbound.build_session(BASE,context,SID,OWNER,'voice',Memory()))
    reg={t.name:t for t in tools}
    result=asyncio.run(reg['start_cloud_work'](title='Read email',instruction='Read all my email'))
    assert result['verification_required'];create.assert_not_called()
    assert 'PRIVATE SENTINEL' not in json.dumps(session)
    assert "Hi John" in session['instructions']

def test_voice_grant_is_bound_to_this_call_not_sms_or_another_call(monkeypatch):
    monkeypatch.undo();monkeypatch.setenv('ALLI_OWNER_PHONE',OWNER)
    monkeypatch.setattr(owner_access.store,'initialize',lambda:None)
    db=Mock()
    grants={('sms',OWNER),('voice','CA-right')}
    db.execute.side_effect=lambda sql,args:Mock(fetchone=lambda:(1,) if args in grants else None)
    monkeypatch.setattr(owner_access.store,'connection',lambda:connection(db))
    assert owner_access.authorized(OWNER,'sms')
    assert owner_access.authorized(OWNER,'voice','CA-right')
    assert not owner_access.authorized(OWNER,'voice','CA-spoof')
    assert not owner_access.authorized(CALLER,'voice','CA-right')

@pytest.mark.parametrize('verified',[False,True])
def test_friend_private_work_requires_carrier_proof_on_voice(monkeypatch,verified):
    listing=Mock(return_value=[]);monkeypatch.setattr(work_store,'listing',listing)
    ctx=dict(contact=dict(name='Jane Doe',source='owner_saved_contact'),number_verified=verified,history=[])
    _,tools=asyncio.run(inbound.build_session(BASE,ctx,SID,CALLER,'voice',Memory()))
    result=asyncio.run(next(t for t in tools if t.name=='get_my_work')())
    assert result['ok'] is verified
    assert listing.called is verified

def test_group_mode_removes_private_tools_and_delegates_public_work(monkeypatch):
    monkeypatch.setattr(owner_access,'authorized',lambda *a:True)
    monkeypatch.setattr(notes,'start',lambda *a:dict(ok=True,notes_active=True))
    monkeypatch.setattr(security,'extend_voice',lambda *a:3600)
    private=Mock();monkeypatch.setattr(work_store,'listing',private)
    create=Mock(return_value=dict(ok=True,work_id='ABC',state='queued'));monkeypatch.setattr(work_store,'create',create)
    live=NS(metadata={'live_transcript_fragments':[]})
    session,tools=asyncio.run(inbound.build_session(BASE,{},SID,OWNER,'voice',Memory(),session_reader=lambda:live))
    reg={t.name:t for t in tools}
    assert asyncio.run(reg['start_group_notes'](participants_json='[]'))['ok']
    assert not asyncio.run(reg['get_my_work']())['ok'];private.assert_not_called()
    assert asyncio.run(reg['start_cloud_work'](title='Party plan',instruction='Create a plan'))['ok']
    assert create.call_args.kwargs['owner_scope'] is False
    assert create.call_args.kwargs['context']['group_delegation'] is True
    assert 'prior_work' not in create.call_args.kwargs['context']
    checked=asyncio.run(reg['check_owner_access']())
    assert checked['owner_verified'] and 'context' not in checked

def test_note_boundary_excludes_private_preamble(monkeypatch):
    live=NS(metadata=dict(live_transcript_fragments=[dict(role='user',text='PRIVATE PREAMBLE',start_ms=0,end_ms=100)]))
    start=Mock(return_value=dict(ok=True));monkeypatch.setattr(notes,'start',start)
    state={'active':False}
    tool=notes.tools(CALLER,SID,lambda:live,state)[0]
    asyncio.run(tool(participants_json='[{"name":"Jane Doe"}]'))
    assert start.call_args.args[2]==1 and state['active']
    live.metadata['live_transcript_fragments'].append(dict(role='user',text='Jane owns the research',start_ms=200,end_ms=300))
    db=Mock();db.execute.return_value.fetchone.return_value=(1,)
    monkeypatch.setattr(notes.store,'connection',lambda:connection(db))
    notes.checkpoint(SID,live)
    saved=db.execute.call_args.args[1][0].obj
    assert len(saved)==1 and saved[0]['text']=='Jane owns the research'
    assert saved[0]['start_ms']==200

def test_pause_clears_persistence_but_never_reopens_owner_permissions(monkeypatch):
    pause=Mock();monkeypatch.setattr(notes,'pause',pause)
    live=NS(metadata={'group_mode':True});state={'active':True}
    result=asyncio.run(notes.tools(CALLER,SID,lambda:live,state)[2]())
    assert result['notes_paused'] and live.metadata['suppress_transcript_persistence']
    assert state['active']

def test_each_person_gets_only_their_explicit_items_and_doable_offer():
    people=[dict(name='Jane',canonical='Jane Doe',bound_phone=CALLER,declined=False),dict(name='Blake',canonical='Blake Miller',bound_phone='+18436968121',declined=False)]
    items=[dict(assignee='Jane',task='Write the brief',due='Friday',alli_offer='I can draft it'),dict(assignee='Blake Miller',task='Review the budget',due='',alli_offer=''),dict(assignee='',task='Unclear owner')]
    messages,unassigned=notes.action_messages(items,people)
    assert 'Review the budget' not in str(messages['Jane Doe'])
    assert 'Write the brief' not in str(messages['Blake Miller'])
    assert 'I can draft it' in str(messages['Jane Doe'])
    assert unassigned==['Unclear owner']

def test_ambiguous_names_and_declines_do_not_get_guessed_deliveries():
    people=[dict(name='Sam',canonical='Sam A',bound_phone=CALLER),dict(name='Sam',canonical='Sam B',bound_phone=OWNER)]
    messages,unassigned=notes.action_messages([dict(assignee='Sam',task='Do this')],people)
    assert not messages and unassigned

@pytest.mark.parametrize('name',['stripe.list_customers','render.update_service','github.create_pull_request','gmail.bulk_delete_emails','google_drive.create_permission'])
def test_account_admin_tools_are_hard_blocked_even_for_owner(name):
    with pytest.raises(ValueError,match='not enabled'):policy.validate(name,{},dict(scope='owner',instruction='Do anything'))

@pytest.mark.parametrize('field',['to','cc','bcc'])
def test_recipients_from_injected_content_cannot_expand_original_assignment(monkeypatch,field):
    db=Mock();db.execute.return_value.fetchall.return_value=[]
    monkeypatch.setattr(policy.store,'connection',lambda:connection(db))
    job=dict(instruction='Send jane@example.com the brief',context={})
    with pytest.raises(ValueError,match='recipient John supplied'):
        policy.validate('gmail.send_email',{'to':['jane@example.com'],field:['attacker@example.com']},job)
    policy.validate('gmail.send_email',{'to':['jane@example.com']},job)

def test_a_claimed_name_alone_cannot_read_or_move_someone_elses_meeting(monkeypatch):
    db=Mock();db.execute.return_value.fetchone.return_value=None
    monkeypatch.setattr(contact_events.store,'connection',lambda:connection(db))
    event=dict(id='victim',summary='John and Jane Doe',attendees=[dict(displayName='Jane Doe')])
    assert not contact_events.belongs(event,CALLER,'')
    event['attendees'][0]['email']='jane@example.com'
    assert contact_events.belongs(event,CALLER,'jane@example.com')

def test_quota_fails_closed_before_a_new_usage_reservation(monkeypatch):
    db=Mock()
    def execute(sql,args=None):
        if 'sum(units)' in sql:return Mock(fetchone=lambda:(300,0,0))
        return Mock(fetchone=lambda:None)
    db.execute.side_effect=execute
    with pytest.raises(security.Limited):security.reserve_in_db(db,'inbound_sms',OWNER,'new')
    assert not any(c.args[0].startswith('INSERT INTO alli_security_usage') for c in db.execute.call_args_list)

def test_calendar_ceiling_counts_phone_bindings_even_when_invitation_email_changed(monkeypatch):
    db=Mock();db.execute.return_value.fetchall.return_value=[('one',),('two',),('three',)]
    api=Mock(return_value={'items':[dict(id=x,status='confirmed',attendees=[]) for x in ('one','two','three')]})
    monkeypatch.setattr(__import__('google_integration'),'api',api)
    with pytest.raises(ValueError,match='3 upcoming'):contact_events.check_capacity(db,CALLER,'new@example.com')

def test_private_result_requires_owner_signin_and_revocation_keeps_admin_access(monkeypatch):
    job=dict(id='ABC',phone=OWNER,scope='owner',state='completed',created_at=datetime.now(timezone.utc).isoformat(),title='Private report',result='PRIVATE CONTENT',sources=[],link_revoked=False)
    monkeypatch.setattr(work_store,'read',lambda *a,**k:job)
    monkeypatch.setattr(work_routes,'files',lambda *a,**k:[])
    def deny(req):raise HTTPException(401,'Sign in')
    monkeypatch.setattr(work_routes.dashboard,'authenticate',deny)
    app=FastAPI();work_routes.install_work(app);client=TestClient(app,follow_redirects=False)
    url='/work/ABC/'+work_store.result_token(job)
    assert client.get(url).status_code==303
    redirect=client.get('/dashboard/work/ABC');assert redirect.status_code==303 and redirect.headers['location']=='/dashboard?result=ABC' and 'PRIVATE CONTENT' not in redirect.text
    job['link_revoked']=True
    assert client.get(url).status_code==404
    monkeypatch.setattr(work_routes.dashboard,'authenticate',lambda req:dict(method='cookie',csrf='x'))
    result=client.get('/dashboard/work/ABC')
    assert result.status_code==200 and 'PRIVATE CONTENT' in result.text

def test_openai_fragments_preserve_text_and_overlap_timing():
    metadata={}
    for role,text,start,end in [('input','We should',100,200),('output','mm-hmm',150,220),('input',' do this',200,400)]:
        capture(metadata,dict(type='session.'+role+'_transcript.delta',delta=text,start_ms=start,end_ms=end))
    assert ''.join(x['text'] for x in metadata['live_transcript_fragments'] if x['role']=='user')=='We should do this'
    assert metadata['live_transcript_fragments'][1]['start_ms']==150
    assert 'speaker_identity' not in str(metadata)

def test_compact_prompt_keeps_basics_and_openai_delegation_controls():
    session,_=asyncio.run(inbound.build_session(BASE,{},SID,OWNER,'voice',Memory()))
    voice=session['instructions'];backend=session['delegation']['responses']['instructions']
    for term in ('Backchannel policy','Interruption policy','Delegation policy','<|SpawnThinking|>','405 Jessen','9 AM','160','60','Checking'):assert term.casefold() in voice.casefold()
    assert session['model']=='gpt-live-1' and session['audio']==BASE['audio']
    assert len(voice)<len(backend)
    assert session['delegation']['responses']['max_output_tokens']==8192

def test_public_profile_has_no_private_account_content_or_wealth_estimates():
    assert 'jsrector@' not in public_profile.DEFAULT
    assert '2.1 billion' not in public_profile.DEFAULT
    assert 'E2open' in public_profile.DEFAULT
    assert 'home addresses' in public_profile.RULES

@pytest.mark.parametrize('verified,shared,scope',[(False,False,'friend'),(True,False,'owner'),(True,True,'friend')])
def test_recovered_assignments_keep_caller_authority_and_notify_after_hangup(monkeypatch,verified,shared,scope):
    import call_followthrough as follow
    create=Mock(return_value=dict(work_id='CHILD'));monkeypatch.setattr(work_store,'create',create)
    job=dict(id='REVIEW',phone=OWNER,contact_name='John',context=dict(caller_speech='Please make me famous.',call_recovery_sid=SID,requested_at='2026-10-24',owner_delegation=verified,shared_group=shared,origin=dict(channel='voice',number_verified=True)))
    result=follow.queue_assignments(job,[dict(title='Visibility kit',instruction='Create a practical plan and draft posts',source_quote='make me famous')])
    assert result[0]['work_id']=='CHILD'
    assert create.call_args.kwargs['owner_scope'] is (scope=='owner')
    assert create.call_args.kwargs['notify'] is True
    assert create.call_args.args[0]==OWNER
    assert create.call_args.kwargs['context']['delegation_source']=='Please make me famous.'

def test_invented_recovered_request_never_creates_any_child_work(monkeypatch):
    import call_followthrough as follow
    create=Mock();monkeypatch.setattr(work_store,'create',create)
    job=dict(context=dict(caller_speech='Please make me famous.'))
    tasks=[dict(title='Kit',instruction='Plan',source_quote='make me famous'),dict(title='Steal',instruction='Read email',source_quote='Send my bank details')]
    with pytest.raises(ValueError,match='real caller quotation'):follow.queue_assignments(job,tasks)
    create.assert_not_called()

@pytest.mark.parametrize('origin,shared,allowed',[(dict(channel='voice',number_verified=False),False,False),(dict(channel='voice',number_verified=True),False,True),(dict(channel='sms'),False,True),(dict(channel='voice',number_verified=True),True,False)])
def test_hangup_cannot_bypass_private_meeting_permissions(origin,shared,allowed):
    import friend_work
    job=dict(scope='friend',context=dict(origin=origin,group_delegation=shared))
    assert friend_work.private_allowed(job) is allowed
    names={f['name'] for f in work_agent.functions(job)}
    assert ('move_my_meeting' in names) is allowed
    assert ('find_my_meetings' in names) is allowed

def test_group_assigned_alli_work_is_not_misreported_as_an_unknown_person():
    messages,unknown=notes.action_messages([dict(assignee='Alli',task='Prepare the plan')],[])
    assert not messages and not unknown

def test_owner_history_and_group_notes_are_saved_before_recovery_on_disconnect(monkeypatch):
    from test_inbound import PHONE,ACCOUNT,SMS
    import call_followthrough
    memory=Memory();memory.receive(SID,OWNER,'voice')
    tac=NS(config=NS(account_sid=ACCOUNT,auth_token='x',phone_number=PHONE))
    service=inbound.InboundCommunications(tac,BASE,memory)
    capture=Mock(side_effect=lambda sid,**kwargs:None if memory.get(sid)['status']=='completed' else pytest.fail('not saved'))
    monkeypatch.setattr(call_followthrough,'capture',capture);monkeypatch.setattr(owner_access,'end_call',lambda sid:None)
    live=NS(call_sid=SID,conversation_id=SID,metadata=dict(live_transcript_fragments=[dict(role='user',text='Make me famous.',start_ms=0,end_ms=1000)]))
    asyncio.run(service.ended(live))
    capture.assert_called_once_with(SID,finalized=True)
    assert memory.get(SID)['transcript'][0]['text']=='Make me famous.'

def test_routine_background_booking_uses_only_the_bound_saved_contact(monkeypatch):
    import friend_work,google_integration as g
    monkeypatch.setattr(friend_work.store,'context_for',lambda p:dict(contact=dict(name='Jane Doe',source='owner_saved_contact',email='jane@example.com')))
    monkeypatch.setattr(work_agent,'delegated_authority',AsyncMock(return_value=dict(allowed=True,reason='Complete original booking request')))
    monkeypatch.setattr(security,'calendar_write',lambda *a:None)
    db=Mock();db.execute.return_value.fetchone.return_value=None
    monkeypatch.setattr(friend_work.store,'connection',lambda:connection(db))
    monkeypatch.setattr(work_store,'event',Mock());monkeypatch.setattr(friend_work.store,'notify',Mock())
    meeting=Mock(return_value=dict(ok=True,event_id='EVENT'));monkeypatch.setattr(g,'meeting',meeting)
    job=dict(id='JOB',phone=CALLER,scope='friend',instruction='Book my meeting',context=dict(delegation_source='Book me Friday at 11 for an hour',origin=dict(channel='sms')))
    args=dict(start_datetime='2026-10-30T11:00:00-04:00',duration_minutes=60,title='Meeting',email='')
    result=asyncio.run(friend_work.execute(job,NS(name='book_my_meeting',call_id='C'),args,object()))
    assert result['event_id']=='EVENT'
    assert meeting.call_args.args[0:2]==('Jane Doe','jane@example.com') and meeting.call_args.args[-1]==CALLER


def test_contact_cannot_change_a_meeting_for_other_guests():
    event=dict(organizer=dict(email='jsrector@gmail.com',self=True),attendees=[dict(email='jsrector@gmail.com',self=True),dict(email='jane@example.com'),dict(email='other@example.com')])
    with pytest.raises(ValueError,match='other people'):contact_events.require_individual_meeting(event)
    event['attendees'].pop()
    contact_events.require_individual_meeting(event)


def test_owner_in_directory_is_not_ambiguous_in_group_delivery():
    db=Mock();db.execute.return_value.fetchall.return_value=[(OWNER,'John Rector'),(CALLER,'Jane Doe')]
    result=notes.resolve(db,[dict(name='John',phone='',declined=False)])
    assert len(result)==1 and result[0]['bound_phone']==OWNER


def test_late_native_job_refreshes_call_review_before_recovery(monkeypatch):
    import call_followthrough as follow
    db=Mock();cur=Mock();cur.fetchall.side_effect=[[dict(id='LATE-JOB',title='Saved by the live backend',instruction='Do the requested work',state='queued')],[]]
    db.cursor.side_effect=lambda **kw:connection(cur)
    monkeypatch.setattr(follow.store,'connection',lambda:connection(db))
    monkeypatch.setattr(follow.jobs,'event',Mock())
    job=dict(id='REVIEW',context=dict(call_recovery_sid=SID,requested_at=datetime.now(timezone.utc).isoformat(),existing_jobs=[],verified_receipts=[]))
    assert follow.refresh_review(job)
    sql,params=db.execute.call_args.args
    assert params[0].obj['existing_jobs'][0]['id']=='LATE-JOB'
    assert params[0].obj['review_revision']==1
    assert "state='queued'" in sql and 'response_id=NULL' in sql


@pytest.mark.parametrize('scope,group,should_mount',[('owner',False,True),('friend',False,False),('owner',True,False)])
def test_private_skill_snapshot_is_not_mounted_for_friends_or_groups(monkeypatch,scope,group,should_mount):
    import cloud_skills
    mount=AsyncMock(return_value={'type':'skill_reference','skill_id':'owner-private','version':'1'})
    monkeypatch.setattr(cloud_skills,'attachment',mount)
    tools=asyncio.run(work_agent.execution_tools(dict(scope=scope,phone=OWNER,context=dict(group_delegation=group)),object()))
    environment=next(t['environment'] for t in tools if t['type']=='shell')
    assert bool(environment.get('skills')) is should_mount
    assert bool(mount.await_count) is should_mount


def test_resuming_notes_does_not_restore_paused_discussion(monkeypatch):
    live=NS(metadata=dict(suppress_transcript_persistence=True,group_fragment_start=1,live_transcript_fragments=[dict(role='user',text='PRIVATE PAUSED DISCUSSION')]*3))
    monkeypatch.setattr(notes,'start',lambda *a:dict(ok=True))
    state={'active':True}
    asyncio.run(notes.tools(CALLER,SID,lambda:live,state)[0](participants_json='[]'))
    assert live.metadata['transcript_persist_start']==3
    assert live.metadata['group_fragment_start']==3
    assert state['active']


def test_native_owner_write_uses_current_actual_request(monkeypatch):
    monkeypatch.setattr(owner_access,'authorized',lambda *a:True)
    checker=AsyncMock(return_value=dict(allowed=False,reason='A schedule lookup does not authorize a text'))
    monkeypatch.setattr(work_agent,'native_authority',checker)
    source='Show my schedule today'
    _,tools=asyncio.run(inbound.build_session(BASE,{},SID,OWNER,'sms',Memory(),owner_text=source,current_request=source))
    result=asyncio.run(next(t for t in tools if t.name=='text_contact')(phone_number=CALLER,recipient_name='Jane',message='Unrequested',send_at='',owner_requested=True))
    assert result['performed'] is False
    assert checker.call_args.args[-2:]==(source,source)


@pytest.fixture
def call_boundary(monkeypatch):
    import cloud_calls
    db=Mock();db.execute.return_value.fetchone.return_value=None
    monkeypatch.setattr(cloud_calls.store,'connection',lambda:connection(db))
    monkeypatch.setattr(cloud_calls.security,'reserve',lambda *a,**k:None)
    monkeypatch.setattr(cloud_calls.security,'reserve_in_db',lambda *a,**k:None)
    monkeypatch.setattr(cloud_calls.jobs,'event',Mock())
    monkeypatch.setattr(cloud_calls.store,'notify',Mock())
    monkeypatch.setattr(cloud_calls.jobs,'read',lambda wid:dict(state='running'))
    monkeypatch.setattr(cloud_calls,'ordinary_call',AsyncMock(return_value=dict(safe=True,reason='Ordinary call')))
    calls=NS(initiate=AsyncMock(return_value=dict(ok=True,status='queued',request_id='accepted')),read_result=AsyncMock(return_value=dict(ok=True,status='queued',disposition='in-progress')))
    monkeypatch.setattr(work_agent,'call_executor',calls)
    monkeypatch.setattr(work_agent,'delegated_authority',AsyncMock(return_value=dict(allowed=True,reason='Original call requested')))
    return cloud_calls,calls,db


def call_args():return dict(phone_number=CALLER,recipient_name='Jane Doe',mission='Ask what to focus on at the meeting',phone_source_url='',not_before='')


def test_background_friend_call_requires_exact_owner_approval(call_boundary):
    module,calls,db=call_boundary
    job=dict(id='JOB',scope='friend',phone='+18435550101',contact_name='Other friend',instruction='Call Jane about meeting focus',context={})
    call=NS(name='phone_call',call_id='C')
    with pytest.raises(work_agent.Waiting) as wait:asyncio.run(module.execute(job,call,call_args(),object()))
    assert wait.value.approval['kind']=='tool'
    assert wait.value.approval['arguments']==call_args()
    calls.initiate.assert_not_awaited()


def test_background_approved_call_does_not_promote_friend_job(call_boundary):
    module,calls,db=call_boundary;args=call_args()
    job=dict(id='JOB',scope='friend',phone='+18435550101',contact_name='Other friend',instruction='Call Jane',context={},approval=dict(kind='tool',call_id='C',decision='approved',arguments=args))
    result=asyncio.run(module.execute(job,NS(name='phone_call',call_id='C'),args,object()))
    assert result['status']=='queued' and job['scope']=='friend'
    body=calls.initiate.call_args.args[0]
    assert body.phone==CALLER and body.capabilities==[]
    assert body.request_id.startswith('work-call:JOB:')


def test_background_owner_call_runs_without_another_confirmation(call_boundary):
    module,calls,db=call_boundary;args=call_args()
    job=dict(id='JOB',scope='owner',phone=OWNER,contact_name='John Rector',instruction='Call Jane at '+CALLER,context={})
    result=asyncio.run(module.execute(job,NS(name='phone_call',call_id='C'),args,object()))
    assert result['status']=='queued'
    calls.initiate.assert_awaited_once()


def test_in_progress_call_defers_without_notifying_or_redial(call_boundary,monkeypatch):
    module,calls,db=call_boundary
    deferred=Mock(side_effect=module.Deferred());monkeypatch.setattr(module,'defer',deferred)
    job=dict(id='JOB',scope='owner',phone=OWNER,instruction='Call Jane',context={})
    with pytest.raises(module.Deferred):asyncio.run(module.execute(job,NS(name='get_call_result'),dict(request_id='work-call:JOB:target'),object()))
    deferred.assert_called_once_with('JOB');calls.initiate.assert_not_awaited()


def test_phone_result_cannot_read_another_job(call_boundary):
    module,calls,db=call_boundary
    with pytest.raises(ValueError,match='belonging'):
        asyncio.run(module.execute(dict(id='JOB'),NS(name='get_call_result'),dict(request_id='work-call:OTHER:target'),object()))
    calls.read_result.assert_not_awaited()


def test_financial_call_is_rejected_before_owner_approval_request(call_boundary,monkeypatch):
    module,calls,db=call_boundary
    monkeypatch.setattr(module,'ordinary_call',AsyncMock(return_value=dict(safe=False,reason='No bill payments')))
    result=asyncio.run(module.execute(dict(id='JOB',scope='friend',phone=CALLER),NS(name='phone_call',call_id='C'),call_args(),object()))
    assert result['status']=='not_permitted' and result['performed'] is False
    calls.initiate.assert_not_awaited()


def test_saved_sender_self_callback_stays_bound_to_verified_phone():
    import cloud_calls
    job=dict(phone=CALLER,context=dict(origin=dict(channel='voice',number_verified=True)))
    assert cloud_calls.self_callback(job,CALLER)
    assert not cloud_calls.self_callback(job,OWNER)
    job['context']['group_delegation']=True
    assert not cloud_calls.self_callback(job,CALLER)


def test_calling_service_cannot_dial_itself(monkeypatch):
    import cloud_calls
    monkeypatch.setenv('TWILIO_PHONE_NUMBER',CALLER)
    with pytest.raises(ValueError,match='own service'):cloud_calls.target_number(CALLER)


def test_result_expiry_starts_when_scheduled_work_finishes(monkeypatch):
    monkeypatch.setattr(work_store,'result_token',lambda job:'token')
    now=datetime.now(timezone.utc)
    job=dict(id='J',phone=CALLER,created_at=now-__import__('datetime').timedelta(days=12),completed_at=now)
    assert work_store.valid_token(job,'token')


def test_dot_feed_carries_results_but_not_bearer_links_or_internal_review():
    import assistant_bridge as bridge
    db=Mock()
    db.execute.return_value.fetchall.return_value=[('J','owner',False,'completed','A practical plan [PDF](sandbox:/mnt/data/private.pdf)'),('R','owner',True,'completed','PRIVATE REVIEW TEXT')]
    events=[dict(source='cloud_work',work_id='J'),dict(source='cloud_work',work_id='R')]
    bridge.enrich(db,events)
    assert events[0]['requester_scope']=='owner' and 'sandbox:' not in events[0]['result_excerpt']
    assert events[0]['result_requires_owner_sign_in'] and '?' not in events[0]['result_page']
    assert events[1]['internal_review'] and 'result_excerpt' not in events[1]


def test_dot_completion_is_a_report_not_a_fabricated_verified_action(monkeypatch):
    import assistant_bridge as bridge
    db=Mock();db.execute.return_value.fetchone.return_value=None
    monkeypatch.setattr(bridge.store,'connection',lambda:connection(db))
    monkeypatch.setattr(bridge.store,'initialize',lambda:None)
    notify=Mock();monkeypatch.setattr(bridge.store,'notify',notify)
    result=bridge.report('demo:route','Prepare route','completed','Route built',['maps-result-1'])
    payload=notify.call_args.args[2]
    assert payload['source']=='dot_work' and payload['status']=='reported_completed'
    assert 'not independently verified' in payload['verification']
    assert result['performed_external_action'] is False
    db.execute.return_value.fetchone.return_value=(payload,)
    assert bridge.report('demo:route','Prepare route','completed','Route built',['maps-result-1'])['duplicate']
    with pytest.raises(ValueError,match='different receipt'):bridge.report('demo:route','Prepare route','completed','Changed',[])


def test_suspension_cancels_future_promises_not_only_queued_sms(monkeypatch):
    db=Mock();db.execute.return_value.fetchall.return_value=[('P',)]
    monkeypatch.setattr(security.store,'connection',lambda:connection(db))
    event=Mock();monkeypatch.setattr(__import__('promises'),'event',event)
    monkeypatch.setattr(security.store,'notify',Mock())
    security.set_block(CALLER,True)
    assert any("UPDATE alli_promise SET state='cancelled'" in c.args[0] for c in db.execute.call_args_list)
    event.assert_called_once_with(db,'P','cancelled','John','Contact suspended; pending promise cancelled')


def test_birthday_schedule_preserves_eastern_time_after_daylight_saving_ends():
    from zoneinfo import ZoneInfo
    when=datetime(2026,11,1,9,0,tzinfo=ZoneInfo('America/New_York'))
    assert when.astimezone(timezone.utc).hour==14


def test_dot_reuses_an_existing_future_text_across_portal_and_plugin(monkeypatch):
    import assistant_bridge as bridge,scheduled_sms as sms,mcp_integration as mcp
    from fastapi import FastAPI
    from pydantic import BaseModel
    class Input(BaseModel):pass
    get=Mock(return_value=dict(ok=False,status='not_found'))
    enqueue=Mock();monkeypatch.setattr(sms,'get',get);monkeypatch.setattr(sms,'enqueue',enqueue)
    monkeypatch.setattr(bridge,'matching_text',lambda body:'promise:ABCDEF123456')
    monkeypatch.setattr(bridge,'scheduled_text',lambda rid:dict(ok=True,request_id=rid,status='scheduled',message='Happy birthday!'))
    server=mcp.install_mcp(FastAPI(),lambda x:None,Input,read_updates=lambda *a:None,sms_service=object())
    result=asyncio.run(server.call_tool('send_text_message',dict(request_id='different-dot-id',phone=CALLER,recipient_name='Jane',message='Happy birthday!',send_at='2026-11-01T09:00:00-05:00')))
    assert enqueue.call_count==0
    assert 'promise:ABCDEF123456' in str(result) and 'Happy birthday!' in str(result)


def test_new_scheduling_helper_preserves_existing_request_id_conflict_check(monkeypatch):
    import assistant_bridge as bridge,scheduled_sms as sms,mcp_integration as mcp
    from fastapi import FastAPI
    from pydantic import BaseModel
    class Input(BaseModel):pass
    monkeypatch.setattr(sms,'get',lambda rid:dict(ok=True,status='scheduled'))
    enqueue=Mock(side_effect=ValueError('different approved content'))
    monkeypatch.setattr(sms,'enqueue',enqueue)
    matching=Mock();monkeypatch.setattr(bridge,'matching_text',matching)
    server=mcp.install_mcp(FastAPI(),lambda x:None,Input,read_updates=lambda *a:None,sms_service=object())
    with pytest.raises(__import__('mcp.server.fastmcp.exceptions',fromlist=['ToolError']).ToolError,match='different approved content'):
        asyncio.run(server.call_tool('send_text_message',dict(request_id='existing',phone=CALLER,recipient_name='Jane',message='Changed',send_at='2026-11-01T09:00:00-05:00')))
    enqueue.assert_called_once();matching.assert_not_called()
