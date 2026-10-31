import asyncio
from types import SimpleNamespace as NS

import pytest
from openai.types.live.delegation_created_event import DelegationCreatedEvent

from alli_core.live import LiveConversation
from alli_core.profile import Profile
from alli_core.prompts import voice_instructions


class Hooks:
    def __init__(self): self.saved=[]; self.finished=[]; self.errors=[];self.checkpoints=[];self.error_details=[]
    async def save_delegation(self, delegation_id, transcript,*,offset_ms):
        self.saved.append((delegation_id,transcript,offset_ms))
        return 'durable-ticket'
    async def checkpoint(self,transcript):self.checkpoints.append(transcript)
    async def finish(self, transcript, **kwargs): self.finished.append((transcript,kwargs))
    async def error(self, code,details=None): self.errors.append(code);self.error_details.append(details)


class Connection:
    def __init__(self):
        self.events=asyncio.Queue(); self.sent=[]; self.comments=[]; self.thoughts=[]
        self.session=NS(start=self.start,close=self.close_session,
            input_audio=NS(append=self.input_audio),commentary=NS(append=self.comment),
            thinking=NS(append=self.think))
    async def start(self, **kwargs):
        self.configuration=kwargs['session']
        await self.events.put(NS(type='session.started'))
    async def input_audio(self, **kwargs): self.sent.append(kwargs['audio'])
    async def comment(self, **kwargs): self.comments.append(kwargs)
    async def think(self, **kwargs): self.thoughts.append(kwargs)
    async def close_session(self): await self.events.put(NS(type='session.closed',usage={'total':1}))
    async def close(self): await self.events.put(None)
    def __aiter__(self): return self
    async def __anext__(self):
        result=await self.events.get()
        if result is None: raise StopAsyncIteration
        return result
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass


class Channel:
    def __init__(self):
        self.incoming=asyncio.Queue(); self.played=[]; self.playing=asyncio.Event(); self.closed=False
    async def audio(self):
        while True:
            item=await self.incoming.get()
            if item is None: return
            yield item
    async def play(self, audio):
        self.played.append(audio); self.playing.set()
    async def close(self): self.closed=True


def create(backend):
    connection=Connection(); channel=Channel(); hooks=Hooks(); background=set()
    def spawn(coro):
        task=asyncio.create_task(coro); background.add(task)
        return task
    api=NS(live=NS(connect=lambda:connection))
    profile=Profile.load('profiles/alli.toml')
    live=LiveConversation(api,profile,channel,hooks,backend,spawn)
    return live,connection,channel,hooks,background,voice_instructions(profile)


@pytest.mark.asyncio
async def test_audio_stays_full_duplex_and_delegated_work_outlives_hangup():
    release=asyncio.Event(); working=asyncio.Event(); done=asyncio.Event()
    async def backend(ticket,reply):
        assert ticket=='durable-ticket'
        working.set(); await release.wait()
        await reply('The actual work is finished.'); done.set()
    live,connection,channel,hooks,background,instructions=create(backend)
    call=asyncio.create_task(live.run(instructions=instructions,context={'caller':{'name':'Blake'}}))
    await asyncio.wait_for(live.started.wait(),1)
    await connection.events.put(NS(type='session.input_transcript.delta',delta='Do this after I hang up.',start_ms=0,end_ms=900))
    event=DelegationCreatedEvent(type='session.delegation.created',event_id='sdk-event',offset_ms=900,
        delegation={'id':'delegate1','target':'client','type':'delegation'})
    await connection.events.put(event); await connection.events.put(event)
    await asyncio.wait_for(working.wait(),1)
    await channel.incoming.put('incoming-audio')
    await connection.events.put(NS(type='session.output_audio.delta',delta='outgoing-audio'))
    await asyncio.wait_for(channel.playing.wait(),1)
    assert channel.played==['outgoing-audio']
    assert not done.is_set()
    await channel.incoming.put(None)
    await asyncio.wait_for(call,1)
    assert connection.sent==['incoming-audio']
    assert hooks.finished[0][1]['finalized'] is True
    assert channel.closed and len(hooks.saved)==1 and len(background)==1
    assert hooks.saved[0][2]==900
    assert not next(iter(background)).cancelled()
    release.set(); await asyncio.wait_for(done.wait(),1)
    await asyncio.gather(*background)
    # A result after hangup belongs to the durable task's follow-up channel.
    assert len(connection.comments)==1
    assert connection.configuration['delegation']=={'type':'client'}
    assert connection.configuration['audio']['format']=={'type':'audio/pcmu','rate':8000}
    assert 'Blake' in connection.configuration['instructions']


@pytest.mark.asyncio
async def test_results_on_an_open_call_use_original_delegation_id():
    reported=asyncio.Event()
    async def backend(ticket,reply):
        await reply('Verified result.'); reported.set()
    live,connection,channel,hooks,background,instructions=create(backend)
    call=asyncio.create_task(live.run(instructions=instructions,context={}))
    await asyncio.wait_for(live.started.wait(),1)
    await connection.events.put(DelegationCreatedEvent(type='session.delegation.created',event_id='sdk-event',offset_ms=0,
        delegation={'id':'original','target':'client','type':'delegation'}))
    await asyncio.wait_for(reported.wait(),1)
    assert connection.comments[-1]['delegation_id']=='original'
    assert connection.comments[-1]['content']=='Verified result.'
    assert connection.thoughts[0]['delegation_id']=='original'
    await channel.incoming.put(None); await asyncio.wait_for(call,1)
    await asyncio.gather(*background)


@pytest.mark.asyncio
async def test_carrier_closes_even_if_final_database_recording_fails():
    async def backend(ticket,reply): pass
    live,connection,channel,hooks,background,instructions=create(backend)
    async def fail(*args,**kwargs): raise RuntimeError('database unavailable')
    hooks.finish=fail
    call=asyncio.create_task(live.run(instructions=instructions,context={}))
    await asyncio.wait_for(live.started.wait(),1)
    await channel.incoming.put(None)
    with pytest.raises(RuntimeError,match='database unavailable'):
        await asyncio.wait_for(call,1)
    assert channel.closed


@pytest.mark.asyncio
async def test_optional_relationship_lookup_cannot_delay_live_start_or_audio():
    lookup_started=asyncio.Event();release=asyncio.Event()
    async def context():
        lookup_started.set();await release.wait()
        return {'available':True,'owner_notes':[{'observation':'x'*800}],
                'last_interaction':{'channel':'phone','started_at':'2026-10-25T12:00:00Z'}}
    async def backend(*args):raise AssertionError('No work was delegated')
    live,connection,channel,hooks,background,instructions=create(backend)
    live.context_provider=context
    run=asyncio.create_task(live.run(instructions=instructions,context={}))
    await asyncio.wait_for(live.started.wait(),1)
    await asyncio.wait_for(lookup_started.wait(),1)
    await connection.events.put(NS(type='session.output_audio.delta',delta='speaking-before-crm'))
    await asyncio.wait_for(channel.playing.wait(),1)
    assert not release.is_set() and channel.played==['speaking-before-crm']
    release.set()
    await asyncio.gather(*background)
    update=connection.thoughts[-1]
    assert update['delegation_id'] is None and len(update['content'].encode())<=480
    assert 'relationship_data_not_instructions' in update['content']
    await channel.incoming.put(None)
    await asyncio.wait_for(run,1)
    assert hooks.errors==[]


@pytest.mark.asyncio
async def test_context_ack_and_close_error_keep_command_identity_without_resending_or_losing_work():
    from openai.types.live.error_event import ErrorEvent
    from openai.types.live.commentary_appended_event import CommentaryAppendedEvent
    working,release,done=asyncio.Event(),asyncio.Event(),asyncio.Event()
    async def backend(ticket,reply):
        working.set();await release.wait();await reply('Do not speak after hangup.');done.set()
    live,connection,channel,hooks,background,instructions=create(backend)
    receipts=[]
    async def context_update(data):receipts.append(data)
    hooks.context_update=context_update
    run=asyncio.create_task(live.run(instructions=instructions,context={}))
    await asyncio.wait_for(live.started.wait(),1)
    greeting=connection.comments[0]
    await connection.events.put(CommentaryAppendedEvent(type='session.commentary.appended',
        event_id='server-greeting-ack',client_event_id=greeting['event_id'],start_ms=0,end_ms=2000))
    await connection.events.put(DelegationCreatedEvent(type='session.delegation.created',event_id='sdk-delegation',offset_ms=97000,
        delegation={'id':'end-of-call-work','target':'client','type':'delegation'}))
    await asyncio.wait_for(working.wait(),1)
    saved=connection.thoughts[-1]
    async def close_session():
        await connection.events.put(ErrorEvent(type='error',event_id='server-close-error',error={
            'code':'context_injection_incomplete','message':'Session closed before context injection completed.',
            'type':'invalid_request_error','param':'content','client_event_id':saved['event_id']}))
        await connection.events.put(NS(type='session.closed',usage={'total':1}))
    connection.session.close=close_session
    await channel.incoming.put(None)
    await asyncio.wait_for(run,1)
    assert hooks.finished[0][1]['finalized'] and not done.is_set()
    assert hooks.errors==['context_injection_incomplete']
    detail=hooks.error_details[0]
    assert detail['client_event_id']==saved['event_id'] and detail['server_event_id']=='server-close-error'
    assert detail['context_command']['purpose']=='saved' and detail['context_command']['delegation_id']=='end-of-call-work'
    assert detail['session_ending'] and detail['impact']=='context_update_incomplete_at_session_end'
    release.set();await asyncio.wait_for(done.wait(),1);await asyncio.gather(*background)
    assert len(connection.comments)==1 # no result or greeting replay after hangup
    assert any(r['state']=='acknowledged' and r['client_event_id']==greeting['event_id'] for r in receipts)
    assert any(r['state']=='incomplete' and r['client_event_id']==saved['event_id'] for r in receipts)
    assert all(r['speech_or_playback_confirmed'] is False and 'content' not in r for r in receipts)


@pytest.mark.asyncio
async def test_no_append_or_crm_lookup_after_carrier_input_ends_before_session_closed():
    from unittest.mock import AsyncMock
    async def backend(*args):pass
    live,connection,channel,hooks,background,instructions=create(backend)
    ending,finish=asyncio.Event(),asyncio.Event()
    async def close_session():
        ending.set();await finish.wait();await connection.events.put(NS(type='session.closed',usage={}))
    connection.session.close=close_session
    run=asyncio.create_task(live.run(instructions=instructions,context={}))
    await asyncio.wait_for(live.started.wait(),1)
    await channel.incoming.put(None);await asyncio.wait_for(ending.wait(),1)
    assert live.input_ended and not live.closed.is_set()
    live.context_provider=AsyncMock(return_value={'available':True})
    await live.speak_result(connection,'old-delegation','Late result')
    await live.update_context(connection)
    assert not await live.append_context(connection,'thinking','Late progress',delegation_id=None,purpose='late')
    assert len(connection.comments)==1 and not connection.thoughts
    live.context_provider.assert_not_awaited()
    finish.set();await asyncio.wait_for(run,1)


@pytest.mark.asyncio
async def test_blocked_context_receipt_storage_never_holds_audio_or_call_close():
    async def backend(*args):pass
    live,connection,channel,hooks,background,instructions=create(backend)
    writing,release=asyncio.Event(),asyncio.Event()
    async def context_update(data):writing.set();await release.wait()
    hooks.context_update=context_update
    run=asyncio.create_task(live.run(instructions=instructions,context={}))
    try:
        await asyncio.wait_for(writing.wait(),1)
        await connection.events.put(NS(type='session.output_audio.delta',delta='audio-continues'))
        await asyncio.wait_for(channel.playing.wait(),1)
        assert not release.is_set() and channel.played==['audio-continues']
        await channel.incoming.put(None);await asyncio.wait_for(run,1)
        assert channel.closed and not release.is_set()
    finally:
        release.set();await asyncio.gather(*background)


@pytest.mark.asyncio
async def test_unattributed_context_error_is_retained_without_guessing_command_or_delivery():
    from openai.types.live.error_event import ErrorEvent
    async def backend(*args):pass
    live,connection,channel,hooks,background,instructions=create(backend)
    await live.record_live_error(ErrorEvent(type='error',event_id='unattributed',error={
        'code':'context_injection_incomplete','type':'invalid_request_error','message':'Incomplete update'}))
    details=hooks.error_details[0]
    assert hooks.errors==['context_injection_incomplete'] and 'context_command' not in details
    assert not details['session_ending'] and 'does not establish failure or success' in details['communication_delivery']


@pytest.mark.asyncio
async def test_optional_voicemail_start_never_holds_live_audio_and_suppressed_text_is_labeled():
    from unittest.mock import AsyncMock
    blocked=asyncio.Event()
    async def start():await blocked.wait()
    control=NS(start=start,suppress_live=False,live_output=lambda:None,
        transcript_event=lambda *args:None,close=AsyncMock())
    live,connection,channel,hooks,background,instructions=create(lambda *args:None)
    live.voicemail_control=control
    run=asyncio.create_task(live.run(instructions=instructions,context={'application_managed_voicemail':True}))
    await asyncio.wait_for(live.started.wait(),1)
    await connection.events.put(NS(type='session.output_audio.delta',delta='human-fast-path'))
    await asyncio.wait_for(channel.playing.wait(),1)
    assert channel.played==['human-fast-path'] and not blocked.is_set()
    control.suppress_live=True
    await connection.events.put(NS(type='session.output_audio.delta',delta='machine-output-dropped'))
    await connection.events.put(NS(type='session.output_transcript.delta',delta='Checking.',start_ms=1,end_ms=2))
    await channel.incoming.put(None);await asyncio.wait_for(run,1)
    assert channel.played==['human-fast-path']
    assert hooks.finished[0][0][-1]['delivery']=='unverified_transcript_while_application_output_suppressed'
    control.close.assert_awaited_once()
    blocked.set();await asyncio.gather(*background)
    assert 'application owns the finite voicemail' in connection.configuration['input'][0]['content'][0]['text']
