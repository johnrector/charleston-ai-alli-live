"""Protocol and persistence tests; all speech, classification and carrier APIs mocked."""
import asyncio
import base64
import math
from array import array
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from adapters.calls import CallRepository
from adapters.postgres import PostgresRepository
from adapters.telephony import MediaChannel
from adapters.voicemail import Voicemail,VoicemailControl
from alli_core.authority import Actor
from alli_core.profile import Profile
from alli_core.voice_audio import pcm24_to_ulaw8,ulaw_encode,ulaw_decode,nonquiet_audio
from service.worker import Worker


class Socket:
    def __init__(self):self.incoming=asyncio.Queue();self.sent=[]
    async def receive_json(self):return await self.incoming.get()
    async def send_json(self,value):self.sent.append(value)
    async def close(self):pass


async def channel_setup():
    socket=Socket();channel=MediaChannel(socket,account_sid='AC-test',call_sid='CA-test')
    channel.stream_sid='MZ-test'
    async def consume():
        async for _ in channel.audio():pass
    reader=asyncio.create_task(consume())
    return socket,channel,reader


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_finite_audio_waits_for_exact_played_mark_and_clear_invalidates_old_marks():
    socket,channel,reader=await channel_setup()
    first=asyncio.create_task(channel.play_finite(b'\xff'*8000))
    await until(lambda:any(e['event']=='mark' for e in socket.sent))
    mark=socket.sent[-1]['mark']['name']
    assert not first.done()
    await channel.clear() # Twilio returns marks for cleared, unplayed audio too.
    assert await first is False
    second=asyncio.create_task(channel.play_finite(b'\xff'*16000))
    await until(lambda:len([e for e in socket.sent if e['event']=='mark'])==2)
    new=socket.sent[-1]['mark']['name']
    await socket.incoming.put({'event':'mark','streamSid':'MZ-test','mark':{'name':mark}})
    await socket.incoming.put({'event':'mark','streamSid':'MZ-test','mark':{'name':'unrelated'}})
    await asyncio.sleep(0)
    assert not second.done()
    await socket.incoming.put({'event':'mark','streamSid':'MZ-test','mark':{'name':new}})
    assert await second is True
    evidence=channel.evidence()
    assert evidence['last_finite_playback']['mark_name']==new and evidence['last_finite_playback']['played']
    assert evidence['marks_acknowledged']==1 and evidence['marks_invalidated']==1
    assert b''.join(base64.b64decode(e['media']['payload']) for e in socket.sent if e['event']=='media')==b'\xff'*24000
    await socket.incoming.put({'event':'stop'});await reader;await channel.close()


@pytest.mark.asyncio
async def test_disconnect_cannot_be_mistaken_for_played_and_live_output_guard_is_inside_lock():
    socket,channel,reader=await channel_setup()
    guard=True
    channel.allow_live_output=lambda:guard
    async with channel.output_lock:
        waiting=asyncio.create_task(channel.play('normal'))
        await asyncio.sleep(0);guard=False
    await waiting
    assert socket.sent==[]
    playing=asyncio.create_task(channel.play_finite(b'\xff'*80))
    await until(lambda:bool(channel.marks))
    await socket.incoming.put({'event':'stop'});await reader
    assert await playing is False
    await channel.close()


def test_finite_pcm_conversion_duration_ulaw_and_antialiasing():
    assert ulaw_encode(0)==255 and ulaw_decode(255)==0 and ulaw_decode(127)==0
    assert abs(ulaw_decode(ulaw_encode(12000))-12000)<512
    def tone(f):return array('h',(int(10000*math.sin(2*math.pi*f*n/24000)) for n in range(2400))).tobytes()
    low,high=pcm24_to_ulaw8(tone(1000)),pcm24_to_ulaw8(tone(10000))
    assert len(low)==800 and nonquiet_audio(low) and not nonquiet_audio(b'\xff'*160)
    energy=lambda b:sum(ulaw_decode(x)**2 for x in b[20:-20])
    assert energy(high)<energy(low)/100
    assert len(pcm24_to_ulaw8(b'\0\0'))==1
    with pytest.raises(ValueError):pcm24_to_ulaw8(b'\0')


@pytest.fixture
def setup(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'));repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'sms',True,'proof')
    person=repo.save_person(owner,name='Test Person',phone='+18435550101')
    repo.set_person_status(owner,str(person['id']),'approved',person['revision'])
    task=repo.submit(owner,source_id='vm-test',title='Call',instruction='Call this person')
    calls=CallRepository(repo,'+18435550999')
    call=calls.queue(task['id'],str(person['id']),'Mission','vm-source');calls.claim()
    carrier=NS(end=AsyncMock(return_value={'status':'completed'}),dial=AsyncMock())
    api=NS(audio=NS(speech=NS(create=AsyncMock(return_value=NS(content=b'\0\0'*2400)))))
    api.with_options=lambda **kwargs:api
    manager=Voicemail(repo,calls,carrier,api)
    return repo,task,calls,call,manager


class Channel:
    def __init__(self):self.clear=AsyncMock();self.play_finite=AsyncMock(return_value=True)


async def ready(setup,*,bind=True):
    repo,task,calls,call,manager=setup
    await manager.prepare(call)
    if bind:calls.bind(call['id'],'CA-test',to='+18435550101',from_='+18435550999',status='in-progress')
    return calls.get(call['id'])


async def control(setup,*,detection='machine_end_beep',live_spoken=False):
    repo,task,calls,_,manager=setup
    call=await ready(setup)
    await manager.detected(call['id'],{'CallSid':'CA-test','AnsweredBy':detection})
    channel=Channel()
    jobs=[]
    def spawn(coro):
        job=asyncio.create_task(coro);jobs.append(job);return job
    value=manager.attach(call,channel,spawn)
    if live_spoken:value.live_output()
    await value.start()
    if value.job:await value.job
    return value,channel,jobs


@pytest.mark.asyncio
async def test_one_finite_message_then_one_hangup_with_separate_durable_receipt(setup):
    _,_,calls,call,manager=setup
    value,channel,_=await control(setup)
    assert value.state=='hangup_accepted'
    channel.play_finite.assert_awaited_once();manager.carrier.end.assert_awaited_once_with('CA-test')
    row=calls.voicemail_evidence(call['id'])
    assert row['playback_state']=='played' and row['played_at'] and row['hangup_state']=='hangup_accepted'
    assert 'Please call me back' in row['body'] and 'Mission' not in row['body']
    await manager.detected(call['id'],{'CallSid':'CA-test','AnsweredBy':'machine_end_beep'})
    value.transcript_event('user','Are you still there?')
    assert manager.carrier.end.await_count==1 and channel.play_finite.await_count==1
    await value.close();assert not manager.active


@pytest.mark.asyncio
@pytest.mark.parametrize('detection,live_spoken',[
    ('human',False),('unknown',False),('fax',False),('machine_end_beep',True)])
async def test_human_unknown_fax_or_earlier_live_audio_never_gets_clip_or_auto_hangup(setup,detection,live_spoken):
    value,channel,_=await control(setup,detection=detection,live_spoken=live_spoken)
    channel.play_finite.assert_not_awaited();setup[-1].carrier.end.assert_not_awaited()
    assert not value.suppress_live
    await value.close()


@pytest.mark.asyncio
async def test_missing_playback_ack_never_hangs_up_and_never_replays(setup):
    manager=setup[-1]
    # Set up explicitly so the transport result is false rather than successful.
    call=await ready(setup)
    channel=Channel();channel.play_finite.return_value=False
    value=manager.attach(call,channel,asyncio.create_task)
    await value.start();value.transcript=[{'role':'user','text':'record a message'}];value.detector='machine_end_beep'
    await value.play_and_end()
    assert value.state=='playback_unconfirmed'
    manager.carrier.end.assert_not_awaited()
    value.transcript_event('user','menu')
    assert channel.play_finite.await_count==1
    await value.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['during_audio','after_ack','during_claim'])
async def test_interruptions_prevent_hangup_even_during_persistence_windows(setup,phase):
    manager=setup[-1];call=await ready(setup)
    channel=Channel();value=manager.attach(call,channel,asyncio.create_task)
    await value.start();value.detector='machine_end_beep';value.transcript=[{'role':'user','text':'record a message'}]
    if phase=='during_audio':
        async def play(audio,**kwargs):
            await value.input_audio(base64.b64encode(b'\x80'*160).decode());return True
        channel.play_finite.side_effect=play
    else:
        original=manager.state if phase=='after_ack' else manager.claim_hangup
        def persistence(*args):
            result=original(*args)
            if phase=='during_claim' or args[1]=='played':value.cancelled=True;value.revision+=1
            return result
        if phase=='after_ack':manager.state=persistence
        else:manager.claim_hangup=persistence
    await value.play_and_end()
    manager.carrier.end.assert_not_awaited()
    await value.close()


@pytest.mark.asyncio
async def test_early_signed_detector_is_bound_before_use_wrong_sid_and_human_override(setup):
    _,_,calls,_,manager=setup
    call=await ready(setup,bind=False)
    assert await manager.detected(call['id'],{'CallSid':'CA-test','AnsweredBy':'machine_end_beep'})
    assert not await manager.detected(call['id'],{'CallSid':'CA-other','AnsweredBy':'machine_end_beep'})
    calls.bind(call['id'],'CA-test',to='+18435550101',from_='+18435550999',status='in-progress')
    channel=Channel();value=manager.attach(calls.get(call['id']),channel,asyncio.create_task)
    await value.start();assert value.detector=='machine_end_beep'
    assert await manager.detected(call['id'],{'CallSid':'CA-test','AnsweredBy':'human'})
    assert await manager.detected(call['id'],{'CallSid':'CA-test','AnsweredBy':'machine_end_beep'})
    assert value.detector=='human' and manager.row(call['id'])['detector_state']=='human'
    await value.close()


@pytest.mark.asyncio
async def test_renderer_and_classifier_and_hangup_uncertainty_never_retry(setup):
    repo,task,calls,call,manager=setup
    manager.api.audio.speech.create.side_effect=PermissionError('unavailable')
    assert (await manager.prepare(call))['asset_state']=='unavailable'
    await manager.prepare(call)
    assert manager.api.audio.speech.create.await_count==1
    worker=Worker(repo,NS(),NS(),manager.carrier,'https://test',calls=calls,voicemail=manager)
    # Claim already exists here: release only this known never-dialed test call.
    calls.defer_before_dial(call['id']);assert await worker.call_one()
    assert calls.get(call['id'])['state']=='cancelled';manager.carrier.dial.assert_not_awaited()


@pytest.mark.asyncio
async def test_hangup_provider_failure_is_uncertain_and_not_retried(setup):
    setup[-1].carrier.end.side_effect=TimeoutError('unknown outcome')
    value,channel,_=await control(setup)
    assert value.state=='hangup_uncertain'
    value.transcript_event('user','menu')
    assert setup[-1].carrier.end.await_count==1
    assert setup[2].voicemail_evidence(setup[3]['id'])['hangup_state']=='hangup_uncertain'
    await value.close()


@pytest.mark.asyncio
async def test_interrupt_while_waiting_for_output_lock_sends_no_voicemail_bytes():
    socket,channel,reader=await channel_setup()
    allowed=True
    async with channel.output_lock:
        playing=asyncio.create_task(channel.play_finite(b'\xff'*8000,permitted=lambda:allowed))
        await asyncio.sleep(0);allowed=False
        clearing=asyncio.create_task(channel.clear())
    assert await playing is False
    await clearing
    assert all(e['event']=='clear' for e in socket.sent)
    await socket.incoming.put({'event':'stop'});await reader;await channel.close()


@pytest.mark.asyncio
async def test_live_output_during_asset_fetch_blocks_duplicate_reminder(setup):
    from threading import Event
    manager=setup[-1];call=await ready(setup)
    waiting=Event();release=Event()
    channel=Channel();value=manager.attach(call,channel,asyncio.create_task)
    await value.start();value.detector='machine_end_beep'
    original=manager.row
    def row(*args,**kwargs):
        if kwargs.get('audio'):
            waiting.set();assert release.wait(2)
        return original(*args,**kwargs)
    manager.row=row
    job=asyncio.create_task(value.play_and_end())
    assert await asyncio.to_thread(waiting.wait,2)
    value.live_output();release.set();await job
    channel.play_finite.assert_not_awaited();manager.carrier.end.assert_not_awaited()
    assert not value.suppress_live
    await value.close()


@pytest.mark.asyncio
async def test_unknown_detection_preserves_normal_end_and_never_rearms_after_end_reservation(setup):
    value,channel,_=await control(setup,detection='unknown')
    assert setup[-1].allow_delegated_end(value.call['id'])
    assert await setup[-1].reserve_delegated_end(value.call['id'])
    value.notify_detection('machine_end_beep')
    value.transcript_event('user','Yes, this is Test, I can hear you.')
    channel.play_finite.assert_not_awaited()
    assert value.cancelled
    await value.close()


@pytest.mark.asyncio
async def test_no_message_policy_hangs_up_without_claiming_audio_played(setup):
    repo,_,calls,call,manager=setup
    with repo.connection() as db:db.execute("UPDATE ea_phone_call SET voicemail_policy='hang_up' WHERE id=%s",(call['id'],))
    value,channel,_=await control(setup)
    manager.api.audio.speech.create.assert_not_awaited();channel.play_finite.assert_not_awaited()
    manager.carrier.end.assert_awaited_once()
    row=calls.voicemail_evidence(call['id'])
    assert row['body']=='' and row['played_at'] is None and row['playback_state']=='not_started'
    await value.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('detection',['machine_end_beep','machine_end_silence','machine_end_other'])
async def test_signed_ready_without_any_transcript_or_second_model_plays_once_and_ends(setup,detection):
    value,channel,_=await control(setup,detection=detection)
    assert value.transcript==[] and value.state=='hangup_accepted'
    assert not hasattr(setup[-1].api,'responses') # The protocol cannot depend on another model.
    channel.play_finite.assert_awaited_once();setup[-1].carrier.end.assert_awaited_once()
    await value.close()


@pytest.mark.asyncio
async def test_human_during_no_message_asset_lookup_never_suppresses_live(setup):
    from threading import Event
    repo,_,calls,call,manager=setup
    with repo.connection() as db:db.execute("UPDATE ea_phone_call SET voicemail_policy='hang_up' WHERE id=%s",(call['id'],))
    call=await ready(setup)
    waiting=Event();release=Event();jobs=[]
    def spawn(coro):
        task=asyncio.create_task(coro);jobs.append(task);return task
    channel=Channel();value=manager.attach(call,channel,spawn)
    await value.start();value.detector='machine_end_beep'
    original=manager.row
    def row(*args,**kwargs):
        if kwargs.get('audio'):
            waiting.set();assert release.wait(2)
        return original(*args,**kwargs)
    manager.row=row
    job=asyncio.create_task(value.play_and_end())
    assert await asyncio.to_thread(waiting.wait,2)
    value.notify_detection('human');release.set();await job
    await asyncio.gather(*jobs)
    assert not value.suppress_live and value.detector=='human'
    channel.play_finite.assert_not_awaited();manager.carrier.end.assert_not_awaited()
    await value.close()


@pytest.mark.asyncio
async def test_delegated_end_reserves_before_finite_playback_and_pending_end_does_not_replay(setup):
    manager=setup[-1];call=await ready(setup)
    assert await manager.reserve_delegated_end(call['id'])
    assert not manager.claim_playback(call['id'])
    assert await manager.reserve_delegated_end(call['id']) # Actual end gate supplies cached receipt.
    assert manager.row(call['id'])['state']=='delegated_end_pending'


@pytest.mark.asyncio
async def test_interruption_receipt_cannot_be_overwritten_by_late_played_write(setup):
    manager=setup[-1];call=await ready(setup)
    manager.state(call['id'],'interrupted','Human interruption')
    manager.state(call['id'],'played','Late thread completion')
    row=manager.row(call['id'])
    assert row['state']=='interrupted' and row['played_at'] is None


def test_appointment_voicemail_uses_bound_logistics_and_correct_dst_without_private_title():
    from contextlib import contextmanager
    event={'start':{'dateTime':'2026-11-01T14:30:00Z','timeZone':'America/New_York'},
        'location':'405 Test Lane, Suite E','summary':'Private subject that must not be disclosed'}
    class Cursor:
        def fetchone(self):return {'event':event}
    class DB:
        def execute(self,*args):return Cursor()
    @contextmanager
    def connection():yield DB()
    repo=NS(profile=Profile.load('profiles/alli.toml'),connection=connection,
        task_result=lambda _: {'context':{'appointment_id':'bound-event'}})
    body=Voicemail(repo,None,None,None).body({'name':'Verified Nickname','parent_task_id':'bound-task'})
    assert body.startswith('Hi Verified Nickname.')
    assert 'Sunday, November 1 at 9:30 AM EST' in body and event['location'] in body
    assert event['summary'] not in body


@pytest.mark.asyncio
async def test_completion_request_outlives_media_teardown_without_retry(setup):
    manager=setup[-1];call=await ready(setup)
    entered=asyncio.Event();release=asyncio.Event()
    async def end(_):entered.set();await release.wait()
    manager.carrier.end=AsyncMock(side_effect=end)
    value=manager.attach(call,Channel(),asyncio.create_task)
    await value.start();value.detector='machine_end_beep';value.transcript=[{'role':'user','text':'record a message'}]
    value.job=asyncio.create_task(value.play_and_end());await entered.wait()
    await value.close()
    assert manager.row(call['id'])['hangup_state']=='attempting'
    assert not value.job.done() and not manager.active
    release.set();await value.job
    assert manager.row(call['id'])['hangup_state']=='hangup_accepted'
    manager.carrier.end.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_clear_during_teardown_still_detaches_and_preserves_interruption(setup):
    manager=setup[-1];call=await ready(setup)
    channel=Channel();channel.clear.side_effect=RuntimeError('socket already closed')
    value=manager.attach(call,channel,asyncio.create_task);value.state='playing'
    manager.claim_playback(call['id'])
    with pytest.raises(RuntimeError):await value.close()
    assert not manager.active and manager.row(call['id'])['state']=='interrupted'
    manager.carrier.end.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_raw_audio_during_asset_fetch_aborts_without_asr_and_allows_normal_end(setup):
    from threading import Event
    manager=setup[-1];call=await ready(setup)
    waiting=Event();release=Event()
    channel=Channel();value=manager.attach(call,channel,asyncio.create_task)
    await value.start();value.detector='machine_end_beep'
    original=manager.row
    def row(*args,**kwargs):
        if kwargs.get('audio'):
            waiting.set();assert release.wait(2)
        return original(*args,**kwargs)
    manager.row=row
    job=asyncio.create_task(value.play_and_end())
    assert await asyncio.to_thread(waiting.wait,2)
    await value.input_audio(base64.b64encode(b'\x80'*160).decode())
    release.set();await job
    assert value.transcript==[] and value.state=='interrupted' and not value.suppress_live
    assert manager.allow_delegated_end(call['id'])
    channel.play_finite.assert_not_awaited();manager.carrier.end.assert_not_awaited()
    await value.close()


@pytest.mark.asyncio
async def test_late_greeting_transcript_during_clip_is_not_a_speech_clock(setup):
    manager=setup[-1];call=await ready(setup)
    channel=Channel();value=manager.attach(call,channel,asyncio.create_task)
    await value.start();value.detector='machine_end_beep'
    async def play(audio,**kwargs):
        value.transcript_event('user','At the tone, leave a message.') # Delayed old greeting transcript.
        return True
    channel.play_finite.side_effect=play
    await value.play_and_end()
    assert value.state=='hangup_accepted'
    manager.carrier.end.assert_awaited_once();await value.close()


@pytest.mark.asyncio
async def test_armed_teardown_database_failure_still_detaches_and_cancels_job(setup):
    manager=setup[-1];call=await ready(setup)
    value=manager.attach(call,Channel(),asyncio.create_task)
    value.job=asyncio.create_task(asyncio.Event().wait())
    def fail(*args):raise RuntimeError('diagnostic database unavailable')
    manager.state=fail
    with pytest.raises(RuntimeError):await value.close()
    assert not manager.active and value.job.cancelled()
