import asyncio
import copy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace as NS

from cryptography.fernet import Fernet
import pytest

from adapters.calls import CallRepository
from adapters.postgres import PostgresRepository
from adapters.scheduling import Scheduler,timing
from adapters.executive_mcp import TaskGrantVerifier
from alli_core.authority import Actor,Conflict,PermissionDenied
from alli_core.profile import Profile


class Native:
    def __init__(self):
        self.saved={};self.invocations=[];self.creates=0;self.lose_reply=False
    async def request(self,method,path='',body=None):
        if method=='post' and not path:
            self.creates+=1
            row={**copy.deepcopy(body),'id':'auto_'+str(self.creates),'status':'active'}
            self.saved[row['id']]=row
            if self.lose_reply:raise TimeoutError('reply lost after provider save')
            return row
        identifier=path.strip('/').split('/')[0]
        row=self.saved[identifier]
        if method=='get':return copy.deepcopy(row)
        if path.endswith('/pause'):row['status']='paused'
        elif path.endswith('/resume'):row['status']='active'
        elif method=='delete':row['status']='cancelled'
        else:row.update(copy.deepcopy(body))
        return row
    async def pages(self,path=''):
        for row in list(self.invocations if path else self.saved.values()):yield copy.deepcopy(row)
    def fire(self,identifier,number=1,status='completed'):
        invocation={'id':'ainv_'+str(number),'automation_id':identifier,'status':status,
            'session_id':'scheduled-session-'+str(number),'scheduled_at':int(datetime.now(timezone.utc).timestamp()),
            'source':{'type':'automation','automation':copy.deepcopy(self.saved[identifier])}}
        self.invocations.append(invocation)
        return invocation


@pytest.fixture
def setup(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'));repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'sms',True,'signed-owner')
    task=repo.submit(owner,source_id='owner-schedule-request',title='Future work',instruction='Do future work')
    api=NS(with_options=lambda **kw:api)
    key=Fernet.generate_key()
    scheduler=Scheduler(repo,api,key,'https://example.com')
    native=Native();scheduler.native=native
    return repo,owner,task,scheduler,native,key


def future():return (datetime.now(timezone.utc)+timedelta(days=30)).isoformat()


def approved_friend(repo,owner):
    person=repo.save_person(owner,name='Blake Miller',phone='+18435550101')
    repo.set_person_status(owner,str(person['id']),'approved',person['revision'])
    actor=Actor('alli',str(person['id']),'sms',False,'signed-sms:blake')
    task=repo.submit(actor,source_id='friend-request',title='Remind me',instruction='Remind me tomorrow')
    return actor,task


@pytest.mark.asyncio
async def test_friend_schedule_keeps_identity_permissions_and_uses_existing_reserved_work_queue(setup):
    repo,owner,_,scheduler,native,_=setup
    friend,source=approved_friend(repo,owner)
    repo.friend_active=1;repo.friend_daily=1
    saved=await scheduler.create(friend,source['id'],'friend-intent','Reminder','Remind Blake to call John',future())
    repo.record_outcome(source['id'],'completed','Schedule saved',turn_id='saved',notify=False)
    # Capacity queues the reserved occurrence without losing its reminder.
    repo.friend_daily=10
    busy=repo.submit(friend,source_id='busy',title='Other work',instruction='Other work')
    assert repo.claim_dispatch(busy['id'])
    repo.friend_daily=1
    native.fire(saved['schedule']['provider_id'])
    await scheduler.reconcile()
    run=scheduler.run('ainv_1');work=repo.task_result(run['task_id'])
    assert work['scope']=='friend' and str(work['person_id'])==friend.person_id
    assert not work['authority']['verified_owner']
    assert work['authority']['proof_id']=='signed-sms:blake'
    assert 'send_email' not in work['authority']['allowed_tools']
    assert 'schedule_work' not in work['authority']['allowed_tools']
    assert 'manage_scheduled_work' not in work['authority']['allowed_tools']
    assert repo.task_context(work['id'])['saved_record_collections']==[]
    assert repo.task_context(work['id'])['requester']['name']=='Blake Miller'
    assert not repo.claim_dispatch(work['id'])
    repo.record_outcome(busy['id'],'completed','Done',turn_id='done',notify=False)
    assert repo.claim_dispatch(work['id'])
    token=repo.issue_tool_grant(work['id'])
    actor=repo.resolve_tool_grant(token)['actor']
    assert not actor.verified_owner
    with pytest.raises(PermissionDenied):
        await scheduler.create(actor,work['id'],'chain','Another reminder','Keep scheduling more work',future())


@pytest.mark.asyncio
async def test_friends_see_and_control_only_their_own_one_time_schedules(setup):
    repo,owner,owner_task,scheduler,native,_=setup
    private=await scheduler.create(owner,owner_task['id'],'private','Private','Private owner work',future())
    friend,task=approved_friend(repo,owner)
    with pytest.raises(PermissionDenied):scheduler.get(friend,private['schedule']['id'])
    with pytest.raises(PermissionDenied):
        repo.submit(friend,source_id='stolen-reservation',title='No',instruction='No',reservation_id=private['schedule']['id'])
    with pytest.raises(PermissionDenied):
        await scheduler.create(friend,task['id'],'recurring','No','Every minute',expression='* * * * *')
    saved=[]
    for number in range(repo.friend_active):
        saved.append(await scheduler.create(friend,task['id'],'one:'+str(number),'Reminder','Own reminder',future()))
    with pytest.raises(PermissionDenied):
        await scheduler.create(friend,task['id'],'over-limit','No','Too many',future())
    assert len(scheduler.list(friend))==repo.friend_active
    assert len(scheduler.list(owner))==repo.friend_active+1
    item=saved[0]['schedule']
    updated=await scheduler.manage(friend,item['id'],item['revision'],'update',instruction='Updated own reminder')
    assert updated['schedule']['instruction']=='Updated own reminder'
    cancelled=await scheduler.manage(friend,item['id'],updated['schedule']['revision'],'cancel')
    assert cancelled['schedule']['state']=='cancelled'


@pytest.mark.asyncio
async def test_suspension_cancels_friend_schedules_and_reactivation_never_replays_them(setup):
    repo,owner,_,scheduler,native,_=setup
    friend,task=approved_friend(repo,owner)
    saved=await scheduler.create(friend,task['id'],'friend-intent','Reminder','Own reminder',future())
    token=native.saved[saved['schedule']['provider_id']]['target']['agent']['tools'][0]['transport']['authorization'].split()[1]
    assert scheduler.launch_grant(token)
    with repo.connection() as db:
        revision=db.execute('SELECT revision FROM ea_person WHERE id=%s',(friend.person_id,)).fetchone()['revision']
    suspended=repo.set_person_status(owner,friend.person_id,'suspended',revision)
    assert scheduler.launch_grant(token) is None
    repo.set_person_status(owner,friend.person_id,'approved',suspended['revision'])
    assert scheduler.launch_grant(token) is None
    await scheduler.reconcile()
    assert scheduler.get(owner,saved['schedule']['id'])['state']=='cancelled'
    assert native.saved[saved['schedule']['provider_id']]['status']=='cancelled'


@pytest.mark.asyncio
async def test_friend_can_schedule_through_same_authenticated_mcp_service_but_cannot_read_owner_contacts(setup):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from adapters.executive_mcp import create_tool_server,tool_http_app
    repo,owner,_,scheduler,native,_=setup
    friend,task=approved_friend(repo,owner)
    token=repo.issue_tool_grant(task['id'])
    app=tool_http_app(create_tool_server(repo,'https://example.com',scheduler=scheduler),'https://example.com')
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),headers={'Authorization':'Bearer '+token}) as http:
            async with Client(streamable_http_client('https://example.com/executive-tools',http_client=http),mode='2026-07-28') as client:
                result=await client.call_tool('schedule_work',{'title':'Own reminder',
                    'instruction':'Remind Blake to call John','run_at':future()})
                assert result.structured_content['ok'] and native.creates==1
                listed=await client.call_tool('scheduled_work',{})
                assert len(listed.structured_content['record'])==1
                denied=await client.call_tool('list_people',{})
                assert denied.is_error


@pytest.mark.asyncio
async def test_native_creation_proof_resolves_exact_uncertain_action_without_repeating_it(setup):
    import hashlib,json
    repo,owner,task,scheduler,native,_=setup
    when=future()
    args={'title':'Future work','instruction':'Email me the report','run_at':when,'expression':None,'timezone_name':None}
    source=hashlib.sha256(json.dumps([task['id'],'schedule_work',args],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    repo.claim_action(task['id'],'lost-call','schedule_work',args,True)
    native.lose_reply=True
    result=await scheduler.create(owner,task['id'],source,'Future work','Email me the report',when)
    repo.save_action(task['id'],'lost-call',result)
    assert result['uncertain'] and not repo.permitted(task['id'],writes=True)
    await scheduler.reconcile()
    receipt=repo.task_context(task['id'])['action_receipts'][0]['receipt']
    assert receipt['ok'] and receipt['reconciled'] and repo.permitted(task['id'],writes=True)
    assert native.creates==1 and len(repo.pending_inputs(task['id']))==1


@pytest.mark.asyncio
async def test_known_control_rejection_retains_previous_state_without_an_uncertain_stop(setup):
    from adapters.errors import InvalidInput
    repo,owner,task,scheduler,native,_=setup
    saved=await scheduler.create(owner,task['id'],'intent','Future work','Email me the report',future())
    class Rejected(Exception):status_code=400
    async def reject(*args,**kwargs):raise Rejected()
    native.request=reject
    with pytest.raises(InvalidInput):
        await scheduler.manage(owner,saved['schedule']['id'],1,'pause')
    row=scheduler.get(owner,saved['schedule']['id'])
    assert row['state']=='active' and row['control_state']=='' and row['revision']==2


def test_explicit_timezone_and_one_time_vs_recurring_timing():
    assert timing(None,'0 8 * * MON-FRI','America/New_York')['timezone']=='America/New_York'
    winter=timing('2027-01-01T08:30:00-05:00',None,'America/New_York')
    assert datetime.fromtimestamp(winter['run_at'],timezone.utc).hour==13
    for args in [('2026-01-01T08:30:00',None,'America/New_York'),(future(),'0 8 * * *','UTC'),
                 (None,None,'UTC'),(None,'bad cron','UTC')]:
        with pytest.raises(ValueError):timing(*args)


@pytest.mark.asyncio
async def test_native_receipt_authority_and_launch_only_credential(setup):
    repo,owner,task,scheduler,native,key=setup
    result=await scheduler.create(owner,task['id'],'intent','A future email','Email me the current report',future())
    assert result['ok'] and result['scheduled'] and native.creates==1
    wire=str(result)
    assert 'launch_digest' not in wire and 'launch_sealed' not in wire and 'Bearer' not in wire
    saved=scheduler.get(owner,result['schedule']['id'])
    target=native.saved[saved['provider_id']]['target']
    assert target['environment']=={'type':'none'}
    assert target['agent']['model']=='gpt-6.1-sol'
    assert target['agent']['reasoning']=={'effort':'high'}
    assert target['agent']['tools'][0]['allowed_tools']==['dispatch_scheduled_work']
    token=target['agent']['tools'][0]['transport']['authorization'].split()[1]
    grant=await TaskGrantVerifier(repo,'https://example.com/executive-tools',scheduler).verify_token(token)
    assert grant.claims=={'schedule_id':saved['id']} and 'actor' not in grant.claims
    assert repo.resolve_tool_grant(token) is None
    person=repo.save_person(owner,name='Friend',phone='+18435550101')
    repo.set_person_status(owner,str(person['id']),'approved',person['revision'])
    friend=Actor('alli',str(person['id']),'sms')
    assert scheduler.list(friend)==[]
    with pytest.raises(PermissionDenied):await scheduler.create(friend,task['id'],'friend-intent','No','Not authorized',future())


@pytest.mark.asyncio
async def test_restart_and_native_invocation_duplicate_prevention_with_fresh_grant(setup,database):
    repo,owner,task,scheduler,native,key=setup
    when=future()
    first=await scheduler.create(owner,task['id'],'intent','A future call','Call my saved contact',when)
    again=await scheduler.create(owner,task['id'],'intent','A future call','Call my saved contact',when)
    assert first['schedule']['id']==again['schedule']['id'] and native.creates==1
    row=scheduler.get(owner,first['schedule']['id'])
    # The original request can finish and its task credentials can expire long
    # before the native provider fires; launch authority is not that old grant.
    token=repo.issue_tool_grant(task['id'])
    with repo.connection() as db:db.execute("UPDATE ea_tool_grant SET expires_at=now()-interval '1 day'")
    repo.record_outcome(task['id'],'completed','Future work scheduled',turn_id='created',notify=False)
    assert repo.resolve_tool_grant(token) is None
    restarted=Scheduler(PostgresRepository(database,repo.profile),scheduler.api,key,'https://example.com')
    # The launch path reads only the persisted digest/snapshot, never decrypts
    # the old launch token or uses the original task's expired account grant.
    restarted.native=native
    native.fire(row['provider_id'])
    results=await asyncio.gather(*(restarted.dispatch(row['id']) for _ in range(4)))
    ids={r['runs'][0]['task_id'] for r in results}
    assert len(ids)==1
    child=repo.task_result(ids.pop())
    assert child['state']=='queued' and child['scope']=='owner'
    new_grant=repo.issue_tool_grant(child['id'])
    assert repo.resolve_tool_grant(new_grant)['actor'].verified_owner
    assert repo.task_context(child['id'])['instruction']=='Call my saved contact'


@pytest.mark.asyncio
async def test_recurring_occurrences_are_separate_call_tasks_not_redials(setup):
    repo,owner,task,scheduler,native,key=setup
    result=await scheduler.create(owner,task['id'],'weekly','Weekly check','Call me once',expression='0 8 * * MON')
    row=scheduler.get(owner,result['schedule']['id'])
    native.fire(row['provider_id'],1);native.fire(row['provider_id'],2)
    runs=(await scheduler.dispatch(row['id']))['runs']
    assert len({r['task_id'] for r in runs})==2
    calls=CallRepository(repo,'+18435550999')
    for run in runs:calls.queue(run['task_id'],owner.person_id,'One check','call:'+run['invocation_id'])
    with repo.connection() as db:assert db.execute("SELECT count(*) AS n FROM ea_phone_call WHERE direction='outbound'").fetchone()['n']==2
    await scheduler.dispatch(row['id'])
    with repo.connection() as db:assert db.execute("SELECT count(*) AS n FROM ea_phone_call WHERE direction='outbound'").fetchone()['n']==2


@pytest.mark.asyncio
async def test_uncertain_create_is_recovered_without_duplicate_native_schedule(setup):
    repo,owner,task,scheduler,native,key=setup
    native.lose_reply=True;when=future()
    result=await scheduler.create(owner,task['id'],'lost','Report','Email me report',when)
    assert result['uncertain'] and native.creates==1
    recovered=await scheduler.create(owner,task['id'],'lost','Report','Email me report',when)
    assert recovered['ok'] and native.creates==1


@pytest.mark.asyncio
async def test_updates_retain_provider_queued_snapshot_and_reject_stale_revision(setup):
    repo,owner,task,scheduler,native,key=setup
    result=await scheduler.create(owner,task['id'],'edit','Email','Send the first wording',future())
    identifier=result['schedule']['id'];row=scheduler.get(owner,identifier)
    native.fire(row['provider_id'],1)
    changed=await scheduler.manage(owner,identifier,1,'update',instruction='Send the new wording')
    assert changed['schedule']['revision']==2
    with pytest.raises(Conflict):await scheduler.manage(owner,identifier,1,'update',instruction='Stale change')
    native.fire(row['provider_id'],2)
    runs=(await scheduler.dispatch(identifier))['runs']
    assert [repo.task_result(r['task_id'])['instruction'] for r in runs]==['Send the first wording','Send the new wording']
    await scheduler.manage(owner,identifier,2,'pause')
    assert scheduler.get(owner,identifier)['state']=='paused'
    with pytest.raises(PermissionDenied):await scheduler.dispatch(identifier)
    await scheduler.manage(owner,identifier,3,'resume')
    await scheduler.manage(owner,identifier,4,'cancel')
    assert scheduler.get(owner,identifier)['state']=='cancelled'


@pytest.mark.asyncio
async def test_forged_snapshot_cannot_launch_another_schedule(setup):
    repo,owner,task,scheduler,native,key=setup
    result=await scheduler.create(owner,task['id'],'scope','Email','Email report',future())
    row=scheduler.get(owner,result['schedule']['id']);invocation=native.fire(row['provider_id'])
    invocation['source']['automation']['target']['input'][0]['content'][0]['text']='{"type":"alli_scheduled_assignment","schedule_id":"different","instruction":"No"}'
    with pytest.raises(PermissionDenied):await scheduler.dispatch(row['id'])


@pytest.mark.asyncio
async def test_failed_native_delivery_is_reported_without_executing_the_work(setup):
    repo,owner,task,scheduler,native,key=setup
    result=await scheduler.create(owner,task['id'],'failure','Email','Email report',future())
    row=scheduler.get(owner,result['schedule']['id']);native.fire(row['provider_id'],status='failed')
    await scheduler.reconcile()
    run=scheduler.run('ainv_1');child=repo.task_result(run['task_id'])
    assert child['state']=='failed' and child['context']['native_schedule_setup']
    assert 'No call, text or email completion is confirmed' in child['result']
    assert len(scheduler.list(owner)[0]['recent_runs'])==1
    await scheduler.reconcile()
    with repo.connection() as db:assert db.execute('SELECT count(*) AS n FROM ea_schedule_run').fetchone()['n']==1


@pytest.mark.asyncio
async def test_launch_credential_cannot_use_account_or_contact_tools(setup):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from adapters.executive_mcp import create_tool_server,tool_http_app
    repo,owner,task,scheduler,native,key=setup
    result=await scheduler.create(owner,task['id'],'launch-auth','Work','Do authorized work',future())
    row=scheduler.get(owner,result['schedule']['id']);native.fire(row['provider_id'])
    token=native.saved[row['provider_id']]['target']['agent']['tools'][0]['transport']['authorization'].split()[1]
    server=create_tool_server(repo,'https://example.com',scheduler=scheduler)
    app=tool_http_app(server,'https://example.com')
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),headers={'Authorization':'Bearer '+token}) as http:
            async with Client(streamable_http_client('https://example.com/executive-tools',http_client=http),mode='2026-07-28') as client:
                assert (await client.call_tool('list_people',{})).is_error
                assert (await client.call_tool('get_context',{})).is_error
                result=await client.call_tool('dispatch_scheduled_work',{})
                assert result.structured_content['ok']
                assert len(result.structured_content['runs'])==1


@pytest.mark.asyncio
async def test_dot_future_sms_uses_native_schedule_and_no_immediate_transport(database):
    import httpx2
    from unittest.mock import AsyncMock
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from adapters.deliveries import DeliveryRepository
    from adapters.owner_mcp import create_owner_server,owner_http_app
    from adapters.owner_oauth import OwnerOAuth
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'));repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'dot',True,'test-owner')
    api=NS(with_options=lambda **kw:api)
    scheduler=Scheduler(repo,api,Fernet.generate_key(),'https://example.com');scheduler.native=Native()
    oauth=OwnerOAuth(repo,'https://example.com','test-session-key',Fernet.generate_key().decode())
    calls=CallRepository(repo,'+18435550999');outbox=DeliveryRepository(repo)
    app=owner_http_app(create_owner_server(repo,calls,outbox,oauth,scheduler=scheduler),'https://example.com')
    token=await oauth.issue('dot-test-client')
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),headers={'Authorization':'Bearer '+token.access_token}) as http:
            async with Client(streamable_http_client('https://example.com/mcp',http_client=http),mode='2026-07-28') as client:
                result=await client.call_tool('send_text_message',{'request_id':'future-text','phone':'+18435550100',
                    'recipient_name':'John','message':'A future message','send_at':future()})
                assert result.structured_content['scheduled'] and not result.structured_content['delivery_confirmed']
                assert outbox.read(owner)==[] and repo.ready_tasks()==[]
                saved=await client.call_tool('get_text_message',{'request_id':'future-text'})
                assert saved.structured_content['ok'] and saved.structured_content['deliveries']==[]
                cancelled=await client.call_tool('cancel_text_message',{'request_id':'future-text'})
                assert cancelled.structured_content['schedule']['state']=='cancelled'
