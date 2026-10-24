import asyncio
from types import SimpleNamespace as NS

import pytest
from openai.types.live.delegation_created_event import DelegationCreatedEvent

from alli_core.live import LiveConversation
from alli_core.profile import Profile
from alli_core.prompts import voice_instructions


class Hooks:
    def __init__(self): self.saved=[]; self.finished=[]; self.errors=[];self.checkpoints=[]
    async def save_delegation(self, delegation_id, transcript,*,offset_ms):
        self.saved.append((delegation_id,transcript,offset_ms))
        return 'durable-ticket'
    async def checkpoint(self,transcript):self.checkpoints.append(transcript)
    async def finish(self, transcript, **kwargs): self.finished.append((transcript,kwargs))
    async def error(self, code): self.errors.append(code)


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
