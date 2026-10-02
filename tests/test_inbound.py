import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator
from tac.tools import function_tool
import inbound_communications as inbound

PHONE='+18544447852'
CALLER='+18435550100'
SID='CA'+'a'*32
SMS='SM'+'b'*32
ACCOUNT='AC'+'c'*32
BASE={'model':'gpt-live-1','audio':{'format':{'type':'audio/pcmu'},'output':{'voice':'marin'}},
      'delegation':{'type':'responses','responses':{'model':'gpt-5.6-sol','tools':[]}}}
CONTEXT={'mission':{'request_id':'prior','phone':CALLER,'recipient_name':'Jane Doe',
    'mission':'Confirm or reschedule our appointment','email':'jane@example.com',
    'capabilities':['check_calendar','manage_calendar','send_email']},'previous_outcome':{'summary':'Asked to call back'},'history':[]}
class Memory:
    def __init__(self):self.rows={};self.identities=set();self.events=[]
    def initialize(self):pass
    def context_for(self,phone):return deepcopy(CONTEXT) if phone==CALLER else {}
    def receive(self,sid,phone,channel,body='',context=None):
        if sid in self.rows:return False
        self.rows[sid]=dict(sid=sid,phone=phone,channel=channel,body=body,context=context or {},status='queued',report=None);return True
    def identity(self,phone,request,save=False):
        if save:self.identities.add((phone,request))
        return (phone,request) in self.identities
    def report(self,sid,value):self.events.append(value);return True
    def get(self,sid):return self.rows.get(sid)
    def finish(self,sid,status,**kwargs):self.rows[sid].update(status=status,**kwargs)
    def delivery(self,*args):self.events.append(args)
    def recover_stale(self):pass
    def claim_sms(self):return None

@pytest.fixture
def service(monkeypatch):
    monkeypatch.setenv('TWILIO_VOICE_PUBLIC_DOMAIN','example.com')
    tac=SimpleNamespace(config=SimpleNamespace(account_sid=ACCOUNT,auth_token='secret',phone_number=PHONE))
    service=inbound.InboundCommunications(tac,BASE,Memory())
    service.ready=True
    return service

def test_identity_blocks_every_action_and_preserves_architecture(monkeypatch):
    calls=[]
    @function_tool()
    async def check_calendar() -> dict:
        """Check calendar."""
        calls.append(True);return {'ok':True}
    monkeypatch.setattr(inbound,'tools_for',lambda b,o:[check_calendar,o])
    async def run():
        memory=Memory()
        session,tools=await inbound.build_session(BASE,CONTEXT,SID,CALLER,'voice',memory)
        reg={t.name:t for t in tools}
        assert session['model']=='gpt-live-1' and session['audio']==BASE['audio']
        assert session['delegation']['responses']['model']=='gpt-5.6-sol'
        assert session['delegation']['type']=='responses'
        assert not (await reg['check_calendar']())['ok']
        assert not (await reg['confirm_identity'](name='John Rector',returning_call=True))['ok']
        assert not (await reg['confirm_identity'](name='Jane Doe',returning_call=False))['ok']
        assert not calls
        assert (await reg['confirm_identity'](name='Jane Doe',returning_call=True))['ok']
        assert (await reg['check_calendar']())['ok'] and calls==[True]
        assert not memory.identities # Voice does not authorize future SMS solely through caller ID
    asyncio.run(run())

def test_unknown_sender_has_no_calendar_or_email_tools():
    async def run():
        session,tools=await inbound.build_session(BASE,{},SID,CALLER,'sms',Memory())
        assert {t.name for t in tools}=={'confirm_identity','report_call_outcome'}
        assert 'jane@example.com' not in session['instructions']
    asyncio.run(run())

def test_signed_webhooks_deduplicate_and_reject_wrong_account(service):
    app=FastAPI();service.install(app)
    with TestClient(app,base_url='https://example.com') as client:
        data={'AccountSid':ACCOUNT,'To':PHONE,'From':CALLER,'MessageSid':SMS,'Body':'Tuesday works'}
        def post(data,signature=True):
            headers={'X-Twilio-Signature':RequestValidator('secret').compute_signature('https://example.com/inbound/sms',data)} if signature else {}
            return client.post('/inbound/sms',data=data,headers=headers)
        assert post(data,False).status_code==403
        assert not service.store.rows
        assert post(data).status_code==200
        assert post(data).status_code==200
        assert len(service.store.rows)==1
        assert post({**data,'AccountSid':'AC'+'d'*32}).status_code==403
        assert post({**data,'To':'+18435550199'}).status_code==403
        assert post({**data,'MessageSid':'bad'}).status_code==400
        assert post({**data,'MessageSid':'SM'+'d'*32,'OptOutType':'STOP'}).status_code==200
        assert len(service.store.rows)==1

def test_voice_isolated_sessions_and_repeat_webhook_no_second_channel(service,monkeypatch):
    channels=[]
    def make(tac,config):
        channel=SimpleNamespace(config=config,end_call=AsyncMock());channels.append(channel);return channel
    monkeypatch.setattr(inbound,'VoiceChannel',make)
    async def run():
        data={'AccountSid':ACCOUNT,'To':PHONE,'From':CALLER,'CallSid':SID}
        first=await service.voice(data)
        assert first==await service.voice(data)
        assert len(channels)==1
        second=await service.voice({**data,'CallSid':'CA'+'b'*32,'From':'+18435550200'})
        assert second!=first and len(channels)==2
        assert 'Jane Doe' in channels[0].config.default_session_config['instructions']
        assert 'Jane Doe' not in channels[1].config.default_session_config['instructions']
        assert '/inbound/fallback' in first and '/inbound/ws/' in first
        assert BASE['delegation']['responses']['tools']==[]
    asyncio.run(run())

def test_sms_send_uncertain_not_retried(service,monkeypatch):
    service.store.receive(SMS,CALLER,'sms','Hello')
    service.sms_answer=AsyncMock(return_value='Hello from Alli')
    calls=[]
    def send(**kwargs):calls.append(kwargs);raise TimeoutError('lost response')
    monkeypatch.setattr(service,'client',lambda:SimpleNamespace(messages=SimpleNamespace(create=send)))
    asyncio.run(service.process_sms(service.store.get(SMS)))
    assert len(calls)==1 and service.store.rows[SMS]['status']=='uncertain'

def test_sms_function_results_return_to_same_responses_conversation(service,monkeypatch):
    service.store.receive(SMS,CALLER,'sms','I am Jane Doe returning your call')
    requests=[]
    async def create(**kwargs):
        requests.append(deepcopy(kwargs))
        if len(requests)==1:
            return SimpleNamespace(output=[SimpleNamespace(type='function_call',name='confirm_identity',arguments='{"name":"Jane Doe","returning_call":true}',call_id='call1')])
        return SimpleNamespace(output=[],output_text='Thanks Jane. Which day works?')
    result=asyncio.run(service.sms_answer(service.store.get(SMS),SimpleNamespace(responses=SimpleNamespace(create=create))))
    assert result.startswith('Thanks Jane')
    assert len(requests)==2 and requests[0]['parallel_tool_calls'] is False
    assert json_result(requests[1]['input'][-1])['ok'] is True
    assert requests[0]['model']=='gpt-5.6-sol'

def json_result(value):
    import json
    return json.loads(value['output'])

def test_new_feed_is_optional_and_readonly(monkeypatch):
    import mcp_integration as m
    from pydantic import BaseModel
    async def run():
        server=m.install_mcp(FastAPI(),None,BaseModel,read_updates=lambda after,limit:{'updates':[],'next_cursor':after})
        tools={t.name:t for t in await server.list_tools()}
        assert tools['list_communication_updates'].annotations.readOnlyHint
        assert tools['list_communication_updates'].inputSchema['properties']['after_id']['minimum']==0
    asyncio.run(run())

def test_bound_stream_rejects_other_call_and_replay():
    class Socket:
        def __init__(self,items):self.items=iter(items)
        async def receive_json(self):return next(self.items)
    start={'event':'start','start':{'callSid':SID,'accountSid':ACCOUNT}}
    async def run():
        good=inbound.BoundSocket(Socket([start,start]),SID,ACCOUNT)
        await good.receive_json()
        with pytest.raises(ValueError):await good.receive_json()
        bad=inbound.BoundSocket(Socket([start]),'CA'+'d'*32,ACCOUNT)
        with pytest.raises(ValueError):await bad.receive_json()
    asyncio.run(run())

def test_inbound_unready_returns_503_before_claiming(service):
    service.ready=False
    with pytest.raises(Exception) as exc:
        asyncio.run(service.voice({'AccountSid':ACCOUNT,'To':PHONE,'From':CALLER,'CallSid':SID}))
    assert exc.value.status_code==503
    assert not service.store.rows
