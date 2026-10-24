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
