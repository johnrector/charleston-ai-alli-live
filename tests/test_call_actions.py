import asyncio
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
import call_actions as a
import google_integration as g
import outbound_profiles as p
from mcp_integration import manual_capabilities


def body(**kw):
    return p.OutboundCall(request_id='full-test', phone='+12025550123', recipient_name='Jim Reilly',
        mission='Move our meeting to 6:30 and email confirmation', capabilities=['check_calendar', 'create_meeting', 'manage_calendar', 'send_email'], **kw)


def event():
    return {'id':'meeting1','etag':'version1','status':'confirmed','summary':'John Rector and Jim Reilly meeting',
        'start':{'dateTime':'2030-01-07T17:30:00-05:00'},'end':{'dateTime':'2030-01-07T18:00:00-05:00'},
        'attendees':[{'email':'jim@example.com','displayName':'Jim Reilly'}], 'location':'Office'}


@contextmanager
def lock(): yield Mock()


def test_matching_does_not_expose_unrelated_people():
    assert a.associated(event(),'Jim Reilly','')
    assert a.associated(event(),'James','jim@example.com')
    assert not a.associated(event(),'Jim','')
    assert not a.associated(event(),'Jim Reilly','wrong@example.com')


def test_reschedule_preserves_event_and_notifies_after_version_check(monkeypatch):
    monkeypatch.setattr(g.store,'action_lock',lock)
    saved=event(); calls=[]
    def api(method,path,**kw):
        calls.append((method,path,kw))
        if method=='PATCH': saved.update(kw['body']); return deepcopy(saved)
        if path.endswith('/events'): return {'items':[deepcopy(saved)]}
        return deepcopy(saved)
    monkeypatch.setattr(g,'api',api)
    result=a.reschedule('meeting1','Jim Reilly','','2030-01-07T18:30:00-05:00',30)
    assert result['ok'] and saved['start']['dateTime'].endswith('18:30:00-05:00')
    patch=next(c for c in calls if c[0]=='PATCH')
    assert set(patch[2]['body'])=={'start','end'}
    assert patch[2]['extra_headers']=={'If-Match':'version1'}
    assert patch[2]['params']=={'sendUpdates':'all'}
    assert saved['attendees']==event()['attendees'] and saved['location']=='Office'
    # Repeating the same agreed change only reads, without another notification.
    a.reschedule('meeting1','Jim Reilly','','2030-01-07T18:30:00-05:00',30)
    assert sum(c[0]=='PATCH' for c in calls)==1


def test_conflict_blocks_reschedule(monkeypatch):
    monkeypatch.setattr(g.store,'action_lock',lock)
    def api(method,path,**kw):
        assert method=='GET'
        return {'items':[{'id':'other','status':'confirmed'}]} if path.endswith('/events') else event()
    monkeypatch.setattr(g,'api',api)
    with pytest.raises(ValueError,match='another event'):
        a.reschedule('meeting1','Jim Reilly','','2030-01-07T18:30:00-05:00',30)


def test_runtime_requires_read_and_consent_and_preserves_shared_registry(monkeypatch):
    monkeypatch.setattr(a,'events_in_window',lambda *args:[event(),{**event(),'id':'unrelated','summary':'Other','attendees':[]}])
    move=Mock(return_value={'ok':True,'event_id':'meeting1'}); monkeypatch.setattr(a,'reschedule',move)
    report=SimpleNamespace(name='report',to_realtime_format=lambda:{'name':'report'})
    tools=p.tools_for(body(),report)
    session=p.session_for(body(),{'model':'gpt-live-1'},report,executable_tools=tools)
    assert 'cannot move' not in session['instructions']
    registry={t.name:t for t in tools}
    async def run():
        args=dict(event_id='meeting1',start_datetime='2030-01-07T18:30:00-05:00',duration_minutes=30,identity_confirmed=True,confirmed=True)
        assert not (await registry['update_meeting'](**args))['ok']
        found=await registry['find_meetings'](start_datetime='2030-01-07T00:00:00-05:00',end_datetime='2030-01-08T00:00:00-05:00')
        assert len(found['meetings'])==1
        assert not (await registry['update_meeting'](**{**args,'confirmed':'true'}))['ok']
        assert (await registry['update_meeting'](**args))['ok']
    asyncio.run(run()); move.assert_called_once()


def test_email_can_use_verified_address_when_missing_but_cannot_override_bound_address(monkeypatch):
    send=AsyncMock(return_value={'ok':True,'message_id':'sent1'}); monkeypatch.setattr(g,'send_email',send)
    async def run():
        tool=next(t for t in a.tools_for_call(body(email='jim@example.com')) if t.name=='send_email')
        assert not (await tool(recipient_email_address='other@example.com',subject='Confirmation',message='Agreed',identity_confirmed=True,confirmed=True))['ok']
        assert not (await tool(recipient_email_address='jim@example.com',subject='Confirmation',message='Agreed',identity_confirmed=False,confirmed=True))['ok']
        assert (await tool(recipient_email_address='jim@example.com',subject='Confirmation',message='Agreed',identity_confirmed=True,confirmed=True))['message_id']=='sent1'
        tool=next(t for t in a.tools_for_call(body()) if t.name=='send_email')
        assert (await tool(recipient_email_address='jim@example.com',subject='Confirmation',message='Agreed',identity_confirmed=True,confirmed=True))['ok']
    asyncio.run(run()); assert send.await_count==2


def test_manual_mode_is_owner_controlled(monkeypatch):
    monkeypatch.delenv('MANUAL_CALL_ACTIONS_ENABLED',raising=False); assert manual_capabilities()==[]
    monkeypatch.setenv('MANUAL_CALL_ACTIONS_ENABLED','true'); assert set(manual_capabilities())=={'check_calendar','create_meeting','manage_calendar','send_email'}


def test_call_end_captures_transcript_without_redial(monkeypatch):
    from appointment_voice import OutboundVoice
    store=SimpleNamespace(find_by_call_sid=Mock(return_value={'key':'k','outcome':None}),record_outcome=Mock(return_value=True))
    service=OutboundVoice(SimpleNamespace(on_conversation_ended=Mock()),{},store)
    session=SimpleNamespace(call_sid='CA1',conversation_id='CA1',metadata={'transcript':[{'role':'user','text':'Yes, 6:30 works.'}]})
    asyncio.run(service.conversation_ended(session))
    saved=store.record_outcome.call_args.args[1]
    assert saved['transcript'][0]['text']=='Yes, 6:30 works.' and saved['source']=='call_end_transcript'
    store.find_by_call_sid.return_value={'key':'k','outcome':{'summary':'Already saved'}}
    asyncio.run(service.conversation_ended(session)); assert store.record_outcome.call_count==1


def test_recent_recipient_guard_serializes_before_check(monkeypatch):
    import appointment_store as s
    monkeypatch.setattr(s,'initialize',lambda:None)
    db=Mock(); db.execute.return_value.fetchone.side_effect=[None,({'request_id':'other-session'},)]
    @contextmanager
    def connection(): yield db
    monkeypatch.setattr(s,'connection',connection)
    with pytest.raises(s.RecentRecipientCall) as exc:
        s.claim_with_recipient_guard('key',{'phone':'+12025550123'})
    assert exc.value.request_id=='other-session'
    assert 'pg_advisory_xact_lock' in db.execute.call_args_list[0].args[0]
    assert not any('INSERT' in c.args[0] for c in db.execute.call_args_list)


def test_creation_and_email_use_real_tool_adapters(monkeypatch):
    create=Mock(return_value={'ok':True,'event_id':'new1'})
    send=Mock(return_value={'ok':True,'message_id':'mail1'})
    monkeypatch.setattr(g,'meeting',create); monkeypatch.setattr(g,'mail',send)
    registry={t.name:t for t in a.tools_for_call(body())}
    async def run():
        assert (await registry['create_meeting'](start_datetime='2030-01-07T18:30:00-05:00',duration_minutes=30,title='Meet',recipient_email_address='jim@example.com',identity_confirmed=True,confirmed=True))['event_id']=='new1'
        assert (await registry['send_email'](recipient_email_address='jim@example.com',subject='Meeting',message='Confirmed for 6:30',identity_confirmed=True,confirmed=True))['message_id']=='mail1'
    asyncio.run(run()); create.assert_called_once(); send.assert_called_once()


def test_cancel_preserves_recipient_binding_and_uses_version(monkeypatch):
    monkeypatch.setattr(g.store,'action_lock',lock)
    saved=event(); calls=[]
    def api(method,path,**kw):
        calls.append((method,kw))
        if method=='DELETE': saved['status']='cancelled'; return {}
        return deepcopy(saved)
    monkeypatch.setattr(g,'api',api)
    assert a.cancel('meeting1','Jim Reilly','')['cancelled']
    assert calls[1]==('DELETE',{'params':{'sendUpdates':'all'},'extra_headers':{'If-Match':'version1'}})


def test_mcp_full_mode_and_existing_intent_survive_mode_change(monkeypatch):
    from fastapi import FastAPI
    from pydantic import BaseModel, ConfigDict
    from mcp_integration import install_mcp
    class Legacy(BaseModel): model_config=ConfigDict(extra='allow')
    calls=[]; saved={}
    async def dial(call):
        calls.append(call)
        value={'ok':True,'request_id':call.request_id,'status':'queued','call_sid':'CA1'}
        saved[call.request_id]=value
        return value
    async def read(request_id): return saved.get(request_id,{'ok':False,'status':'not_found'})
    mcp=install_mcp(FastAPI(),dial,Legacy,initiate_outbound=dial,outbound_model=p.OutboundCall,read_outcome=read)
    args={'phone':'+12025550123','recipient_name':'Jim Reilly','mission':'Move our meeting to 6:30'}
    async def run():
        monkeypatch.setenv('MANUAL_CALL_ACTIONS_ENABLED','true')
        await mcp.call_tool('call_contact',args)
        assert set(calls[0].capabilities)==set(manual_capabilities())
        monkeypatch.setenv('MANUAL_CALL_ACTIONS_ENABLED','false')
        await mcp.call_tool('call_contact',args)
        assert len(calls)==1
    asyncio.run(run())
