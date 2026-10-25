import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from adapters.deliveries import DeliveryRepository
from adapters.postgres import PostgresRepository
from adapters.text_conversation import TextConversation
from alli_core.authority import Actor,PermissionDenied
from alli_core.profile import Profile
from service.worker import Worker


@pytest.fixture
def setup(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'));repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'sms',True,'signed-sms:owner')
    person=repo.save_person(owner,name='Blake Miller',phone='+18435550103')
    repo.set_person_status(owner,str(person['id']),'approved',person['revision'])
    blake=Actor('alli',str(person['id']),'sms',False,'signed-sms:blake',('get_context',))
    api=NS(responses=NS(create=AsyncMock(return_value=NS(id='resp_one',output=[],output_text='Hi, Blake!'))))
    api.with_options=lambda **kw:api
    front=TextConversation(repo,api)
    def message(actor,text,identifier='one',phone=None):
        found=repo.begin_conversation(phone or ('8435550103' if actor==blake else '+18435550100'),
            'sms:'+identifier,'sms',owner_verified=True)
        repo.save_transcript(str(found['conversation']['id']),[{'role':'user','text':text}],finalized=True)
        return repo.submit(actor,source_id='sms:'+identifier,title=text[:100],instruction=text,
            conversation_id=str(found['conversation']['id']),context={'text_frontend':True})
    return repo,owner,blake,api,front,message


def delegated(api,title='Do the requested work',ack='Checking—I’ll text you when it’s done.'):
    api.responses.create.return_value=NS(id='resp_delegation',output_text='',output=[
        NS(type='function_call',name='delegate_work',arguments=json.dumps({'title':title,'acknowledgment':ack}))])


@pytest.mark.asyncio
async def test_phone_identity_precedes_greeting_and_no_work_agent_is_created(setup):
    repo,_,blake,api,front,message=setup
    task=message(blake,'hi',phone='(843) 555-0103')
    await front.handle(task)
    supplied=json.loads(api.responses.create.call_args.kwargs['input'])
    assert supplied['requester']['name']=='Blake Miller' and supplied['scope']=='friend'
    assert api.responses.create.call_args.kwargs['model']=='gpt-6.1-sol'
    assert api.responses.create.call_args.kwargs['reasoning']=={'effort':'high'}
    assert [t['name'] for t in api.responses.create.call_args.kwargs['tools']]==['delegate_work','no_reply']
    assert not repo.ready_tasks() and not repo.active_runs()
    assert repo.task_result(task['id'])['result']=='Hi, Blake!'
    outbox=DeliveryRepository(repo)
    worker=Worker(repo,NS(),outbox,NS(),'https://example.com',text_conversation=front)
    await worker.report(repo.task_result(task['id']))
    row=outbox.claim_due();assert row['destination']=='+18435550103'
    outbox.receipt(row['id'],'SM-greeting','delivered',{'status':'delivered'})
    with repo.connection() as db:
        parts=db.execute('SELECT transcript FROM ea_conversation WHERE id=%s',(task['conversation_id'],)).fetchone()['transcript']
    assert parts[-1]['delivery']=='delivered'


@pytest.mark.asyncio
async def test_delegation_preserves_original_request_and_friend_authority_across_recovery(setup):
    repo,owner,blake,api,front,message=setup
    task=message(blake,'Check my meeting with John')
    delegated(api)
    await front.handle(task)
    # Replaying the original message and recovering another process never
    # creates a second assignment, role promotion, or acknowledgment turn.
    await TextConversation(repo,api).handle(task)
    await TextConversation(repo,api).recover()
    work=repo.ready_tasks(exclude_sms=True)
    assert len(work)==1 and work[0]['instruction']==task['instruction']
    assert work[0]['scope']=='friend' and str(work[0]['person_id'])==blake.person_id
    assert work[0]['authority']['verified_owner'] is False
    assert work[0]['authority']['allowed_tools']==['get_context']
    assert work[0]['parent_id']==task['id'] and work[0]['context']['backend_only']
    assert repo.task_context(work[0]['id'])['received_at']==task['created_at'].isoformat()
    assert repo.ready_text_tasks()==[] and api.responses.create.await_count==1


@pytest.mark.asyncio
async def test_saved_response_survives_crash_before_outcome_without_repeating_inference_or_work(setup,monkeypatch):
    repo,_,_,api,front,message=setup
    owner=repo.owner_actor('signed-sms:owner','sms')
    task=message(owner,'Email me my delivery route')
    delegated(api)
    original=repo.record_outcome
    def unavailable(*args,**kwargs):raise RuntimeError('Process lost before result save')
    monkeypatch.setattr(repo,'record_outcome',unavailable)
    await front.handle(task)
    assert len(repo.ready_tasks(exclude_sms=True))==1
    monkeypatch.setattr(repo,'record_outcome',original)
    await TextConversation(repo,api).recover()
    assert len(repo.ready_tasks(exclude_sms=True))==1 and api.responses.create.await_count==1
    assert repo.task_result(task['id'])['state']=='completed'
    with repo.connection() as db:
        parts=db.execute('SELECT transcript FROM ea_conversation WHERE id=%s',(task['conversation_id'],)).fetchone()['transcript']
    assert len([p for p in parts if p['role']=='assistant'])==1


@pytest.mark.asyncio
async def test_api_failure_keeps_original_message_for_managed_agent_and_never_acknowledges_unsaved_work(setup):
    repo,owner,_,api,front,message=setup
    task=message(owner,'Text me tomorrow at nine')
    api.responses.create.side_effect=TimeoutError('Inference reply lost')
    await front.handle(task)
    ready=repo.ready_tasks(exclude_sms=True)
    assert len(ready)==1 and ready[0]['id']==task['id'] and ready[0]['instruction']==task['instruction']
    assert ready[0]['context']['backend_only'] and not ready[0]['context'].get('text_frontend')
    assert DeliveryRepository(repo).read(owner)==[]


@pytest.mark.asyncio
async def test_same_person_turns_are_serialized_and_suspension_revokes_unfinished_frontend(setup):
    repo,owner,blake,api,front,message=setup
    first=message(blake,'Hi',identifier='one')
    second=message(blake,'Do that',identifier='two')
    assert front.claim(first['id']) and front.claim(second['id']) is None
    with repo.connection() as db:
        revision=db.execute('SELECT revision FROM ea_person WHERE id=%s',(blake.person_id,)).fetchone()['revision']
    repo.set_person_status(owner,blake.person_id,'suspended',revision)
    front.save(first['id'],'resp_pending',{'kind':'delegate','title':'Work','text':'Checking'})
    front.finish(first['id'])
    assert not repo.ready_tasks() and not repo.active_runs() and DeliveryRepository(repo).read(owner)==[]


@pytest.mark.asyncio
async def test_conversation_does_not_consume_friend_work_quota_but_work_limit_still_applies(setup):
    repo,_,blake,api,front,message=setup
    repo.friend_daily=1
    await front.handle(message(blake,'hi',identifier='hello'))
    delegated(api)
    await front.handle(message(blake,'Check my meeting',identifier='work'))
    assert len(repo.ready_tasks(exclude_sms=True))==1
    await front.handle(message(blake,'Also do more work',identifier='over-limit'))
    assert len(repo.ready_tasks(exclude_sms=True))==1
    with repo.connection() as db:
        result=db.execute("SELECT state,result FROM ea_task WHERE source_id='sms:over-limit'").fetchone()
    assert result['state']=='failed' and 'couldn’t start' in result['result']
