import asyncio
import importlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator


class MemoryLedger:
    def __init__(self): self.rows = {}
    def claim(self,key,context):
        if key in self.rows: return False
        self.rows[key] = {'status':'dispatching','context':deepcopy(context),'call_sid':None,'outcome':None,'disposition':None}
        return True
    def get(self,key): return self.rows.get(key)
    def queued(self,key,sid): self.rows[key].update(status='queued',call_sid=sid); return True
    def uncertain(self,key): self.rows[key]['status']='uncertain'; return True
    def blocked(self,key,reason): self.rows[key].update(status='blocked',reason=reason); return True
    def record_outcome(self,key,result):
        if self.rows[key]['outcome'] not in (None,result): return False
        self.rows[key]['outcome']=result
        return True
    def claim_voicemail(self,key,sid,message):
        row=self.rows[key]
        if row.get('voicemail') or row['call_sid'] not in (None,sid): return False
        row['call_sid']=sid
        row['voicemail']={'status':'pending','message':message}
        return True
    def finish_voicemail(self,key,status):
        self.rows[key]['voicemail']['status']=status
        return True
    def record_disposition(self,key,sid,status):
        if self.rows[key]['call_sid'] not in (None,sid): return False
        self.rows[key].update(call_sid=sid,disposition=status)
        return True


@pytest.fixture
def service(monkeypatch):
    for k,v in {'TWILIO_ACCOUNT_SID':'AC'+'0'*32,'TWILIO_AUTH_TOKEN':'test','TWILIO_API_KEY':'SK'+'0'*32,'TWILIO_API_SECRET':'test','TWILIO_PHONE_NUMBER':'+15555550100','TWILIO_VOICE_PUBLIC_DOMAIN':'example.com','OPENAI_API_KEY':'test','DEMO_KEY':'test-admin'}.items():
        monkeypatch.setenv(k,v)
    from tac import TAC,TACConfig
    import appointment_voice as voice
    from outbound_profiles import OutboundCall
    store=MemoryLedger()
    adapter=voice.OutboundVoice(TAC(config=TACConfig.from_env()),{
        'model':'gpt-live-1','audio':{'format':'audio/pcmu'},
        'instructions':'PROPERTY DEMO SECRET','delegation':{'responses':{'model':'gpt-5.6-sol'}}},store)
    body=OutboundCall(request_id='test-1', purpose='generic',phone='+12025550123',recipient_name='Pat',mission='Discuss the workshop')
    return voice,adapter,store,body


def test_disabled_never_claims_or_dials(service,monkeypatch):
    _,adapter,store,body=service
    monkeypatch.delenv('OUTBOUND_CALLS_ENABLED',raising=False)
    with pytest.raises(HTTPException) as error: asyncio.run(adapter.initiate(body))
    assert error.value.status_code==503 and store.rows=={}


def test_isolated_channel_claim_and_outcome(service,monkeypatch):
    voice,adapter,store,body=service
    monkeypatch.setenv('OUTBOUND_CALLS_ENABLED','true')
    channels=[]
    async def initiate(channel,options):
        channels.append(channel)
        assert store.rows[voice.request_key(body.request_id)]['status']=='dispatching'
        assert 'PROPERTY DEMO SECRET' not in options.session_config['instructions']
        assert channel._provider.config.default_session_config is None
        assert set(channel._provider._tools_by_name)=={'report_call_outcome','end_call'}
        assert options.call_options.machine_detection=='DetectMessageEnd'
        assert options.call_options.time_limit==900
        # Even a malicious model request cannot execute a demo action.
        denied=await channel._provider._run_tool_call('test','send_email','{}')
        assert 'error' in denied
        assert channel._alli_ledger_key==voice.request_key(body.request_id)
        return SimpleNamespace(call_sid='CA-test')
    monkeypatch.setattr(voice.VoiceChannel,'initiate_outbound_conversation',initiate)
    async def run():
        one=await adapter.initiate(body)
        two=await adapter.initiate(body)
        outcome=channels[0]._provider._tools_by_name['report_call_outcome']
        assert (await outcome(attendance='not_applicable',questions=['When is the next workshop?'],follow_up_needed=True,summary='Asked about the next workshop.'))['ok']
        read=await adapter.read_result(body.request_id)
        channels[0].end_call=AsyncMock()
        assert not (await channels[0]._provider._tools_by_name['end_call']())['ok']
        channels[0]._alli_answered_by='human'
        assert (await channels[0]._provider._tools_by_name['end_call']())['ok']
        channels[0].end_call.assert_awaited_once_with('CA-test')
        return one,two,read
    first,second,read=asyncio.run(run())
    assert len(channels)==1 and first['status']==second['status']=='queued'
    assert read['outcome']['source']=='conversation_report'
    assert read['outcome']['questions']==['When is the next workshop?']
    assert 'context' not in read


def test_uncertain_no_redial_and_conflicting_request_rejected(service,monkeypatch):
    voice,adapter,store,body=service
    monkeypatch.setenv('OUTBOUND_CALLS_ENABLED','true')
    dial=AsyncMock(side_effect=TimeoutError('Twilio acceptance unknown'))
    monkeypatch.setattr(voice.VoiceChannel,'initiate_outbound_conversation',dial)
    async def run():
        with pytest.raises(TimeoutError): await adapter.initiate(body)
        assert (await adapter.initiate(body))['status']=='uncertain'
        with pytest.raises(HTTPException) as error:
            await adapter.initiate(body.model_copy(update={'mission':'Different call'}))
        assert error.value.status_code==409
    asyncio.run(run())
    assert dial.await_count==1


def test_outcome_closures_cannot_select_another_dispatch(service):
    voice,_,store,_=service
    store.claim('one',{}); store.claim('two',{})
    report=voice.outcome_tool('one',store)
    result=asyncio.run(report(attendance='confirmed',questions=[],follow_up_needed=False,summary='Confirmed'))
    assert result['ok'] and store.rows['one']['outcome']
    assert store.rows['two']['outcome'] is None
    assert 'dispatch_id' not in report.params_json_schema['properties']


def test_http_auth_and_signed_callbacks(service,monkeypatch):
    voice,adapter,store,body=service
    from outbound_profiles import OutboundCall
    api=FastAPI(); adapter.install(api,OutboundCall)
    key=voice.request_key(body.request_id); store.claim(key,{})
    channel=SimpleNamespace(_alli_ledger_key=key,_alli_voicemail_message='Safe generic voicemail',end_call=AsyncMock())
    submitted=[]
    monkeypatch.setattr(adapter,'_submit_voicemail',lambda channel,sid,message:submitted.append((sid,message)))
    adapter.channels['token']=channel
    with TestClient(api,base_url='https://example.com') as client:
        assert client.get('/outbound-calls/test-1').status_code==401
        assert client.get('/outbound-calls/test-1',headers={'X-Demo-Key':'test-admin'}).status_code==200
        form={'CallSid':'CA-test','CallStatus':'completed'}
        url='https://example.com/outbound/status/token'
        assert client.post(url,data=form).status_code==403
        sig=RequestValidator('test').compute_signature(url,form)
        assert client.post(url,data=form,headers={'X-Twilio-Signature':sig}).json()['ok']
        assert store.rows[key]['disposition']=='completed'
        amd={'CallSid':'CA-test','AnsweredBy':'machine_start'}
        url='https://example.com/outbound/amd/token'
        sig=RequestValidator('test').compute_signature(url,amd)
        assert client.post(url,data=amd,headers={'X-Twilio-Signature':sig}).json()['ok']
        channel.end_call.assert_not_awaited()
        assert submitted==[]
        amd['AnsweredBy']='machine_end_beep'
        sig=RequestValidator('test').compute_signature(url,amd)
        assert client.post(url,data=amd,headers={'X-Twilio-Signature':sig}).json()['ok']
        assert client.post(url,data=amd,headers={'X-Twilio-Signature':sig}).json()['ok']
        assert submitted==[('CA-test','Safe generic voicemail')]
        assert store.rows[key]['voicemail']['status']=='submitted'
        assert store.rows[key]['outcome'] is None


def test_owner_test_mode_blocks_other_recipients_and_calendar(service,monkeypatch):
    voice,adapter,store,body=service
    monkeypatch.delenv('OUTBOUND_CALLS_ENABLED',raising=False)
    monkeypatch.setenv('OUTBOUND_TEST_PHONE',body.phone)
    dial=AsyncMock(return_value=SimpleNamespace(call_sid='CA-test'))
    monkeypatch.setattr(voice.VoiceChannel,'initiate_outbound_conversation',dial)
    async def run():
        with pytest.raises(HTTPException):
            await adapter.initiate(body.model_copy(update={'phone':'+12025550124'}))
        with pytest.raises(HTTPException):
            await adapter.initiate(body.model_copy(update={'capabilities':['check_calendar']}))
        assert store.rows=={}
        assert (await adapter.initiate(body))['status']=='queued'
    asyncio.run(run())
    assert dial.await_count==1


def test_readback_can_reconcile_only_the_bound_call(service, monkeypatch):
    voice,adapter,store,body=service
    key=voice.request_key(body.request_id)
    store.claim(key,{})
    store.queued(key,'CA-bound')
    adapter.read_provider=True
    seen=[]
    def details(sid):
        seen.append(sid)
        return {'provider_lookup':'verified','provider_status':'completed','answered_by':'machine','duration_seconds':'4'}
    monkeypatch.setattr(adapter,'_provider_details',details)
    result=asyncio.run(adapter.read_result(body.request_id))
    assert seen==['CA-bound'] and result['answered_by']=='machine'
    assert result['outcome'] is None
    assert asyncio.run(adapter.read_result('missing'))['status']=='not_found'
    assert seen==['CA-bound']


def test_fresh_validation_after_claim_blocks_without_dial(service,monkeypatch):
    voice,adapter,store,body=service
    monkeypatch.setenv('OUTBOUND_CALLS_ENABLED','true')
    dial=AsyncMock()
    monkeypatch.setattr(voice.VoiceChannel,'initiate_outbound_conversation',dial)
    async def revalidate():
        assert store.rows[voice.request_key(body.request_id)]['status']=='dispatching'
        return False
    result=asyncio.run(adapter.initiate(body,before_dial=revalidate))
    assert result['status']=='blocked' and not result['ok']
    assert store.rows[voice.request_key(body.request_id)]['status']=='blocked'
    dial.assert_not_awaited()


def test_voicemail_contains_no_private_call_context(service):
    voice,_,_,body=service
    body=body.model_copy(update={'mission':'SECRET MISSION','approved_logistics':'SECRET ADDRESS','recipient_name':'PRIVATE RECIPIENT'})
    message=voice.voicemail_message(body)
    assert 'Alli' in message and 'Charleston AI' in message
    assert all(value not in message for value in ['SECRET MISSION','SECRET ADDRESS','PRIVATE RECIPIENT'])
    assert 'call me back' not in message.lower()


def test_voicemail_twiml_replaces_stream_and_hangs_up(service):
    voice,_,_,_=service
    saved=[]
    client=SimpleNamespace(calls=lambda sid:SimpleNamespace(update=lambda **kw:saved.append((sid,kw))))
    channel=SimpleNamespace(_get_twilio_client=lambda:client)
    voice.OutboundVoice._submit_voicemail(channel,'CA-bound','Hello <friend> & goodbye')
    assert saved[0][0]=='CA-bound'
    assert '&lt;friend&gt; &amp;' in saved[0][1]['twiml']
    assert '<Hangup' in saved[0][1]['twiml']
    assert '<Connect' not in saved[0][1]['twiml']


def test_exact_expiring_intent_can_book_without_broad_enable(service,monkeypatch):
    import json
    from datetime import datetime,timedelta,timezone
    voice,adapter,store,body=service
    monkeypatch.setenv('OUTBOUND_CALLS_ENABLED','false')
    monkeypatch.setenv('OUTBOUND_TEST_PHONE','+12025550999')
    body=body.model_copy(update={'email':'pat@example.com'})
    approved=body.model_dump(mode='json') | {'request_id':'one-meeting-intent','preset':'scheduling',
        'capabilities':['check_calendar','create_meeting']}
    monkeypatch.setenv('OUTBOUND_SINGLE_CALL_JSON',json.dumps({'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(),'call':approved}))
    captured=[]
    async def dial(channel,options):
        captured.append(options)
        assert set(channel._provider._tools_by_name)=={'check_calendar','create_meeting','report_call_outcome','end_call'}
        return SimpleNamespace(call_sid='CA-once')
    monkeypatch.setattr(voice.VoiceChannel,'initiate_outbound_conversation',dial)
    async def run():
        with pytest.raises(HTTPException):
            await adapter.initiate(body.model_copy(update={'mission':'Unauthorized change'}))
        one=await adapter.initiate(body)
        two=await adapter.initiate(body.model_copy(update={'request_id':'different-client-id'}))
        assert one['request_id']==two['request_id']=='one-meeting-intent'
    asyncio.run(run())
    assert len(captured)==1
