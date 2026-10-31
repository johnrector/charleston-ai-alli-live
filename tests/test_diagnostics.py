import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from adapters.diagnostics import DiagnosticStore, ServiceDiagnostics
from adapters.postgres import PostgresRepository
from alli_core.authority import Actor, PermissionDenied
from alli_core.profile import Profile


@pytest.fixture
def diagnostic_setup(database):
    repository = PostgresRepository(database, Profile.load('profiles/alli.toml'))
    repository.initialize()
    owner = Actor('alli', repository.bootstrap_owner('+18435550100'), 'dot', True, 'test-proof')
    return repository, owner


class Live:
    def __init__(self):
        self.events = asyncio.Queue()
        self.session = NS(start=AsyncMock(side_effect=self.start),
                          close=AsyncMock(side_effect=self.close),
                          input_audio=NS(append=AsyncMock()))
    async def start(self, **kwargs):
        await self.events.put(NS(type='session.started'))
    async def close(self):
        await self.events.put(NS(type='session.closed', reason='close_requested'))
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def __aiter__(self): return self
    async def __anext__(self): return await self.events.get()


def configured(repository, *, reference=None, now=None):
    live = Live()
    connects = []
    def connect(**kwargs): connects.append(kwargs); return live
    api = NS(live=NS(connect=connect), responses=NS(create=AsyncMock(return_value=NS(
        status='completed', model='gpt-6.1-sol'))), beta=NS(agents=NS(sessions=NS(
        retrieve=AsyncMock(return_value=NS(id='existing', metadata={'assistant_id':'alli','task_id':'task'}))))))
    api.with_options = lambda **kwargs: api
    diagnostics = ServiceDiagnostics(repository, api, 'release', clock=lambda: now or datetime.now(timezone.utc))
    diagnostics.store.existing_session = lambda: reference
    return diagnostics, api, live, connects


def daily_id(repository):
    from zoneinfo import ZoneInfo
    return 'daily-health:' + datetime.now(ZoneInfo(repository.profile.timezone)).date().isoformat()


@pytest.mark.asyncio
async def test_daily_check_is_cached_and_cannot_create_communication_or_normal_work(diagnostic_setup):
    repository, owner = diagnostic_setup
    diagnostics, api, live, connects = configured(repository, reference={'session_id':'existing','task_id':'task'})
    before = repository.ledger(owner)
    first = await diagnostics.run(owner, daily_id(repository))
    again = await diagnostics.run(owner, daily_id(repository))
    assert first['ok'] and again['ok'] and again['reused']
    assert first['checks']['voice']['finalized']
    assert api.responses.create.await_count == 1 and api.beta.agents.sessions.retrieve.await_count == 1
    assert connects == [{'max_retries':0,'websocket_connection_options':{'open_timeout':5,'close_timeout':1}}]
    args = api.responses.create.call_args.kwargs
    assert args['tools']==[] and args['store'] is False and args['max_output_tokens']==256
    assert live.session.start.call_args.kwargs['session']['delegation']=={'type':'client'}
    assert repository.ready_tasks()==[] and repository.ledger(owner)==before
    with repository.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM ea_delivery').fetchone()['n']==0


@pytest.mark.asyncio
async def test_failures_are_sanitized_and_never_retried(diagnostic_setup):
    repository, owner = diagnostic_setup
    diagnostics, api, _, _ = configured(repository)
    class Rejected(Exception):
        status_code=401
        code='invalid_api_key'
    api.responses.create.side_effect = Rejected('SECRET API KEY AND PRIVATE PROVIDER BODY')
    result = await diagnostics.run(owner, daily_id(repository))
    assert not result['ok']
    assert result['checks']['text']['http_status']==401
    assert result['checks']['text']['code']=='invalid_api_key'
    assert result['checks']['agents']['tested'] is False
    assert 'SECRET' not in json.dumps(result)
    assert (await diagnostics.run(owner,daily_id(repository)))['reused']
    assert api.responses.create.await_count==1


@pytest.mark.asyncio
async def test_timeout_and_overlapping_retry_cannot_double_spend(diagnostic_setup):
    repository, owner = diagnostic_setup
    diagnostics, api, _, _ = configured(repository)
    entered = asyncio.Event()
    async def blocked(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    api.responses.create.side_effect=blocked
    diagnostics.TIMEOUT=.05
    initial=asyncio.create_task(diagnostics.run(owner,daily_id(repository)))
    await asyncio.wait_for(entered.wait(),2)
    concurrent=await diagnostics.run(owner,daily_id(repository))
    assert concurrent['state']=='running' and concurrent['reused']
    finished=await initial
    assert finished['checks']['text']['error']=='timeout'
    assert (await diagnostics.run(owner,daily_id(repository)))['reused']
    assert api.responses.create.await_count==1


def test_database_claim_survives_restart_and_is_unique_under_concurrency(diagnostic_setup):
    repository, owner = diagnostic_setup
    store=DiagnosticStore(repository)
    from zoneinfo import ZoneInfo
    today=datetime.now(ZoneInfo(repository.profile.timezone)).date()
    identity='daily-health:'+today.isoformat()
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims=list(pool.map(lambda _:store.claim(owner,identity,today,'release')[1],range(6)))
    assert sum(claims)==1
    assert DiagnosticStore(repository).claim(owner,identity,today,'new-release')[1] is False
    with pytest.raises(ValueError):store.claim(owner,'different-id',today,'release')
    with pytest.raises(ValueError):store.claim(owner,'daily-health:'+(today+timedelta(days=1)).isoformat(),today,'release')


@pytest.mark.asyncio
async def test_abandoned_claim_stays_uncertain_instead_of_restarting(diagnostic_setup):
    repository, owner = diagnostic_setup
    diagnostics, api, _, connects = configured(repository)
    identity=daily_id(repository)
    today=datetime.now(__import__('zoneinfo').ZoneInfo(repository.profile.timezone)).date()
    DiagnosticStore(repository).claim(owner,identity,today,'older-release')
    with repository.connection() as db:
        db.execute("UPDATE ea_diagnostic_run SET started_at=now()-interval '2 minutes'")
    result=await diagnostics.run(owner,identity)
    assert result['state']=='uncertain' and result['automatic_retry'] is False
    api.responses.create.assert_not_awaited()
    assert connects==[]


@pytest.mark.asyncio
async def test_friend_and_unverified_owner_cannot_start_a_probe(diagnostic_setup):
    repository, owner = diagnostic_setup
    person=repository.save_person(owner,name='Friend',phone='+18435550101')
    person=repository.set_person_status(owner,str(person['id']),'approved',person['revision'])
    diagnostics, api, _, connects = configured(repository)
    for actor in [replace(owner,verified_owner=False),Actor('alli',str(person['id']),'sms')]:
        with pytest.raises(PermissionDenied):await diagnostics.run(actor,daily_id(repository))
    api.responses.create.assert_not_awaited()
    assert connects==[]


@pytest.mark.asyncio
async def test_date_is_eastern_not_utc_and_previous_results_remain_readable(diagnostic_setup):
    repository, owner = diagnostic_setup
    now=datetime(2026,11,1,2,30,tzinfo=timezone.utc)
    diagnostics, _, _, _ = configured(repository,now=now)
    result=await diagnostics.run(owner,'daily-health:2026-10-31')
    diagnostics.clock=lambda:now+timedelta(days=1)
    assert (await diagnostics.run(owner,'daily-health:2026-10-31'))['reused']
    assert result['timezone']=='America/New_York'


@pytest.mark.asyncio
async def test_live_rejection_and_wrong_agent_identity_are_not_healthy(diagnostic_setup):
    repository, owner = diagnostic_setup
    diagnostics, api, live, _ = configured(repository,reference={'session_id':'existing','task_id':'task'})
    async def rejected(**kwargs):
        await live.events.put(NS(type='error',error=NS(code='PRIVATE_PROVIDER_VALUE')))
    live.session.start.side_effect=rejected
    api.beta.agents.sessions.retrieve.return_value=NS(id='existing',metadata={'assistant_id':'other'})
    result=await diagnostics.run(owner,daily_id(repository))
    assert not result['checks']['voice']['ok'] and result['checks']['voice']['code'] is None
    assert not result['checks']['agents']['ok']
    assert 'PRIVATE' not in json.dumps(result)


@pytest.mark.asyncio
async def test_lost_result_commit_cannot_repeat_provider_calls(diagnostic_setup):
    repository, owner = diagnostic_setup
    diagnostics, api, _, connects = configured(repository)
    def lost(*args):raise RuntimeError('PRIVATE DATABASE DETAILS')
    diagnostics.store.finish=lost
    result=await diagnostics.run(owner,daily_id(repository))
    assert result['state']=='uncertain' and 'PRIVATE' not in json.dumps(result)
    assert (await diagnostics.run(owner,daily_id(repository)))['reused']
    assert api.responses.create.await_count==1 and len(connects)==1


@pytest.mark.asyncio
async def test_cancelled_invocation_retains_its_daily_claim(diagnostic_setup):
    repository, owner = diagnostic_setup
    diagnostics, api, _, _ = configured(repository)
    entered=asyncio.Event()
    async def blocked(**kwargs):entered.set();await asyncio.Event().wait()
    api.responses.create.side_effect=blocked
    attempt=asyncio.create_task(diagnostics.run(owner,daily_id(repository)))
    await asyncio.wait_for(entered.wait(),2)
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):await attempt
    result=await diagnostics.run(owner,daily_id(repository))
    assert result['reused'] and result['state']=='running'
    assert api.responses.create.await_count==1
