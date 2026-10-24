from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from adapters.deliveries import DeliveryRepository
from adapters.postgres import PostgresRepository
from alli_core.authority import Actor
from alli_core.profile import Profile
from service.worker import Worker


@pytest.fixture
def worker_setup(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'))
    repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'sms',True,'owner-proof')
    task=repo.submit(owner,source_id='work',title='Task',instruction='Complete this work')
    outbox=DeliveryRepository(repo)
    carrier=NS(send=AsyncMock(return_value={'ok':True,'message_sid':'SM-test','status':'queued'}))
    runtime=NS(recover=AsyncMock(),inspect=AsyncMock(),follow_up=AsyncMock(),dispatch=AsyncMock())
    def worker():return Worker(repo,runtime,outbox,carrier,'https://example.com')
    return repo,owner,task,outbox,carrier,runtime,worker


@pytest.mark.asyncio
async def test_completion_report_survives_process_loss_before_queue_and_send(worker_setup):
    repo,owner,task,outbox,carrier,_,worker=worker_setup
    repo.record_outcome(task['id'],'completed','The requested work is finished.',turn_id='turn1')
    # The process that recorded the outcome is gone before a notification exists.
    assert outbox.read(owner)==[]
    await worker().tick()
    assert outbox.read(owner)[0]['state']=='queued'
    await worker().tick()
    assert carrier.send.await_count==1
    assert carrier.send.call_args.args==('+18435550100','The requested work is finished.')
    await worker().tick()
    assert carrier.send.await_count==1
    assert outbox.read(owner)[0]['state']=='accepted'


@pytest.mark.asyncio
async def test_unknown_send_is_reported_to_native_session_without_repeating_it(worker_setup):
    repo,owner,task,outbox,carrier,runtime,worker=worker_setup
    repo.claim_dispatch(task['id']);repo.bind_session(task['id'],'native-session')
    row=outbox.queue_text(task['id'],owner.person_id,'One authorized text','one-text')
    carrier.send.side_effect=TimeoutError('A carrier reply was lost')
    await worker().tick()
    assert outbox.read(owner)[0]['state']=='uncertain'
    assert repo.pending_inputs(task['id'])[0]['id']=='delivery:'+row['id']+':uncertain'
    await worker().tick()
    assert carrier.send.await_count==1
    assert runtime.follow_up.await_count==1
    assert runtime.follow_up.call_args.kwargs['role']=='developer'


@pytest.mark.asyncio
async def test_saved_receipt_is_forwarded_after_a_transient_database_failure(worker_setup,monkeypatch):
    repo,owner,task,outbox,_,_,worker=worker_setup
    repo.claim_dispatch(task['id']);repo.bind_session(task['id'],'native-session')
    row=outbox.queue_text(task['id'],owner.person_id,'An authorized text','one-text')
    outbox.claim_due();outbox.receipt(row['id'],'SM-test','accepted',{'status':'queued'})
    original=repo.stage_input
    def unavailable(*args,**kwargs):raise RuntimeError('Database temporarily unavailable')
    monkeypatch.setattr(repo,'stage_input',unavailable)
    await worker().forward_receipts()
    assert len(outbox.unforwarded())==1
    monkeypatch.setattr(repo,'stage_input',original)
    await worker().forward_receipts()
    assert outbox.unforwarded()==[]
    assert len(repo.pending_inputs(task['id']))==1


def test_input_saved_before_first_dispatch_does_not_strand_the_task(worker_setup):
    repo,_,task,_,_,_,_=worker_setup
    repo.stage_input(task['id'],'final-caller-words','The completed caller request',role='user')
    assert repo.task_result(task['id'])['state']=='queued'
    assert [item['id'] for item in repo.ready_tasks()]==[task['id']]
    assert repo.task_context(task['id'])['saved_updates'][0]['text']=='The completed caller request'


@pytest.mark.asyncio
async def test_after_hangup_work_result_returns_to_existing_call_mission_once(worker_setup):
    from adapters.calls import CallRepository
    repo,owner,task,_,_,_,make_worker=worker_setup
    repo.claim_dispatch(task['id']);repo.bind_session(task['id'],'existing-calling-agent')
    calls=CallRepository(repo,'+18435550999')
    call=calls.queue(task['id'],owner.person_id,'Ask what needs doing','one-call')
    calls.claim()
    calls.bind(call['id'],'CA-test',to='+18435550100',from_='+18435550999',status='completed',duration=40)
    repo.save_transcript(str(call['conversation_id']),[{'role':'user','text':'Do the work after I hang up.'}],finalized=True)
    conversation=repo.submit(owner,source_id='conversation:'+str(call['conversation_id']),
        title='Caller work',instruction='Do the requested work',conversation_id=call['conversation_id'])
    repo.claim_action(conversation['id'],'actual-send','send_email',{},True)
    repo.save_action(conversation['id'],'actual-send',{'ok':True,'message_id':'actual-provider-id'})
    repo.record_outcome(task['id'],'waiting','Waiting for delegated work.',turn_id='call1')
    repo.record_outcome(conversation['id'],'partial','Email sent; Shopify requires authentication.',turn_id='work1')
    await make_worker().forward_conversation_work()
    await make_worker().forward_conversation_work()
    inputs=repo.pending_inputs(task['id'])
    assert len(inputs)==1 and 'Email sent' in inputs[0]['body']
    assert 'not a new authorization' in inputs[0]['body']
    assert repo.task_result(task['id'])['state']=='working'
    assert repo.task_context(task['id'])['action_receipts'][0]['receipt']['message_id']=='actual-provider-id'
    assert calls.claim() is None


@pytest.mark.asyncio
async def test_scheduled_delivery_continues_while_native_observation_is_blocked(worker_setup):
    import asyncio
    repo,owner,task,outbox,carrier,runtime,make_worker=worker_setup
    repo.claim_dispatch(task['id']);repo.bind_session(task['id'],'native-session')
    outbox.queue_text(task['id'],owner.person_id,'Already authorized delivery','delivery')
    observing,release,sent=asyncio.Event(),asyncio.Event(),asyncio.Event()
    async def observe(*args):
        observing.set()
        await release.wait()
    async def send(*args,**kwargs):
        await observing.wait()
        sent.set()
        return {'ok':True,'message_sid':'SM-test','status':'queued'}
    runtime.inspect.side_effect=observe;carrier.send.side_effect=send
    worker=make_worker();running=asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(sent.wait(),3)
        assert not release.is_set() and carrier.send.await_count==1
    finally:
        worker.stopping.set();release.set()
        await asyncio.wait_for(running,3)


@pytest.mark.asyncio
async def test_replacement_worker_waits_for_its_public_mcp_release(worker_setup,monkeypatch):
    import asyncio,httpx
    *_,make_worker=worker_setup
    worker=make_worker();worker.release='new-commit'
    old_seen,new_public,started=asyncio.Event(),asyncio.Event(),asyncio.Event()
    def health(request):
        old_seen.set()
        return httpx.Response(200,json={'release':'new-commit' if new_public.is_set() else 'old-commit'})
    real_client=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:
        real_client(transport=httpx.MockTransport(health),**kwargs))
    async def work():started.set();worker.stopping.set()
    worker.work_tick=work;worker.transport_tick=AsyncMock()
    running=asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(old_seen.wait(),2)
        assert not started.is_set() and worker.transport_tick.await_count==0
        new_public.set()
        await asyncio.wait_for(started.wait(),3)
    finally:
        worker.stopping.set()
        await asyncio.wait_for(running,3)
