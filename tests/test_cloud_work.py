import asyncio
from datetime import datetime,timedelta,timezone
import io,json,zipfile
from types import SimpleNamespace as NS
from unittest.mock import Mock,AsyncMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import work_agent as agent
import work_store as jobs
import work_routes as routes
import cloud_skills
import cloud_runtime
from test_inbound import BASE,Memory,SID,CALLER
import inbound_communications as inbound

OWNER='+18433276008'
FRIEND='+18435550100'
OTHER='+18435550101'

def test_final_json_is_parsed_separately_from_hosted_tool_progress():
    final={'outcome':'completed','result':'Created the requested files'}
    response=NS(output=[NS(type='message',content=[NS(type='output_text',text='I am using the PDF skill.')]),
      NS(type='shell_call'),NS(type='message',content=[NS(type='output_text',text=json.dumps(final))])],
      output_text='I am using the PDF skill.\n'+json.dumps(final))
    assert agent.final_output(response)==final
    response.output.append(NS(type='message',content=[NS(type='output_text',text='Invalid final output')]))
    with pytest.raises(ValueError,match='verified'):agent.final_output(response)

def test_pinned_linux_runtime_extracts_the_actual_official_package_layout(tmp_path,monkeypatch):
    import tarfile,hashlib,base64
    content=b'official-runtime-placeholder'
    package=io.BytesIO()
    with tarfile.open(fileobj=package,mode='w:gz') as archive:
        for name,data in ((cloud_runtime.RUNTIME_MEMBER,content),('package/vendor/x86_64-unknown-linux-musl/bin/codex-code-mode-host',b'other-executable')):
            info=tarfile.TarInfo(name);info.size=len(data);archive.addfile(info,io.BytesIO(data))
    data=package.getvalue()
    monkeypatch.delenv('ALLI_CODEX_BINARY',raising=False)
    monkeypatch.setattr(cloud_runtime.os,'uname',lambda:NS(sysname='Linux'))
    monkeypatch.setattr(cloud_runtime,'Path',lambda _:tmp_path)
    monkeypatch.setattr(cloud_runtime.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(data))
    monkeypatch.setattr(cloud_runtime,'INTEGRITY',base64.b64encode(hashlib.sha512(data).digest()).decode())
    result=cloud_runtime.binary()
    assert result==str(tmp_path/'codex')
    assert (tmp_path/'codex').read_bytes()==content
    assert (tmp_path/'codex').stat().st_mode&0o777==0o700
    assert not (tmp_path/'codex-code-mode-host').exists()

@pytest.mark.parametrize('channel',['voice','sms'])
def test_approved_friends_can_delegate_but_cannot_dial_or_access_accounts(channel,monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',OWNER)
    context={'contact':{'name':'Jane Doe','source':'owner_saved_contact','email':'jane@example.com'}}
    session,tools=asyncio.run(inbound.build_session(BASE,context,SID,FRIEND,channel,Memory()))
    names={t.name for t in tools}
    assert {'start_cloud_work','get_my_work','continue_cloud_work'}<=names
    assert not {'call_person','find_call_contact','lookup_my_calendar','run_connected_tool'}&names
    assert session['delegation']['responses']['tools'][-1]=={'type':'web_search'}


def test_job_phone_and_authority_are_bound_outside_model_arguments(monkeypatch):
    monkeypatch.setattr(agent,'recent_context',lambda phone:{})
    create=Mock(return_value={'ok':True,'state':'queued'});monkeypatch.setattr(jobs,'create',create)
    tool=next(t for t in agent.phone_tools(FRIEND,'Jane',SID) if t.name=='start_cloud_work')
    assert set(tool.params_json_schema['properties'])=={'title','instruction'}
    result=asyncio.run(tool(title='Research',instruction='Compare two options'))
    assert result['state']=='queued'
    assert create.call_args.args[:2]==(FRIEND,'Jane')
    assert create.call_args.kwargs['owner_scope'] is False

@pytest.mark.parametrize('channel',['voice','sms'])
def test_owner_can_delegate_account_work_and_calls_from_both_phone_channels(channel,monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',OWNER)
    monkeypatch.setattr(agent,'recent_context',lambda phone:{})
    create=Mock(return_value={'ok':True,'state':'queued'});monkeypatch.setattr(jobs,'create',create)
    session,tools=asyncio.run(inbound.build_session(BASE,{},SID,OWNER,channel,Memory()))
    assert {'find_call_contact','call_person','get_requested_call'}<={t.name for t in tools}
    delegate=next(t for t in tools if t.name=='start_cloud_work')
    asyncio.run(delegate(title='Requested work',instruction='Use my connected calendar to prepare a brief'))
    assert create.call_args.args[0]==OWNER
    assert create.call_args.kwargs['owner_scope'] is True


def test_unknown_number_cannot_request_private_owner_scope(monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',OWNER)
    initialize=Mock();monkeypatch.setattr(jobs.store,'initialize',initialize)
    with pytest.raises(ValueError,match='restricted'):
        jobs.create(FRIEND,'Jane','Test','Read all email',SID,owner_scope=True)
    initialize.assert_not_called()


def test_friend_work_rejects_account_tool_even_if_model_attempts_it(monkeypatch):
    monkeypatch.setattr(jobs,'read',lambda wid:{'state':'running'})
    invoke=AsyncMock();monkeypatch.setattr(agent.runtime,'invoke',invoke)
    job={'id':'A','scope':'friend'}
    for name in ('run_connected_tool','lookup_calendar'):
        with pytest.raises(ValueError,match='unavailable'):
            asyncio.run(agent.execute(job,NS(name=name,arguments='{}',call_id='C')))
    invoke.assert_not_called()


def test_account_write_pauses_before_any_provider_call(monkeypatch):
    monkeypatch.setattr(jobs,'read',lambda wid:{'state':'running'})
    monkeypatch.setattr(agent.runtime,'tools',{'mail.send':{'name':'mail.send','annotations':{'readOnlyHint':False}}})
    invoke=AsyncMock();monkeypatch.setattr(agent.runtime,'invoke',invoke)
    args={'tool_name':'mail.send','arguments_json':'{"to":"someone@example.com"}'}
    with pytest.raises(agent.Waiting) as pending:
        asyncio.run(agent.execute({'id':'A','scope':'owner'},NS(name='run_connected_tool',arguments=json.dumps(args),call_id='C')))
    assert pending.value.approval['arguments']==args
    invoke.assert_not_called()


def test_denied_action_does_not_reach_the_provider(monkeypatch):
    monkeypatch.setattr(jobs,'read',lambda wid:{'state':'running'})
    invoke=AsyncMock();monkeypatch.setattr(agent.runtime,'invoke',invoke)
    job={'id':'A','scope':'owner','approval':{'call_id':'C','decision':'denied'}}
    result=asyncio.run(agent.execute(job,NS(name='run_connected_tool',arguments='{}',call_id='C')))
    assert result['ok'] is False
    invoke.assert_not_called()


def test_cancellation_stops_execution_not_the_entire_worker(monkeypatch):
    worker=agent.WorkWorker()
    monkeypatch.setattr(jobs,'claim',lambda:{'id':'A'})
    monkeypatch.setattr(agent,'run_job',AsyncMock(side_effect=agent.WorkCancelled()))
    assert asyncio.run(worker.process_one()) is True


def test_call_tool_needs_actual_owner_and_explicit_request(monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',OWNER)
    calls=NS(initiate=AsyncMock());monkeypatch.setattr(agent,'call_executor',calls)
    for phone,requested in ((FRIEND,True),(OWNER,False)):
        tool=next(t for t in agent.owner_call_tools(phone,SID) if t.name=='call_person')
        result=asyncio.run(tool(phone_number=OTHER,recipient_name='Other',mission='Call',owner_requested=requested))
        assert result['ok'] is False
    calls.initiate.assert_not_called()


def test_explicit_owner_call_has_stable_intent_and_honest_status(monkeypatch):
    monkeypatch.setenv('ALLI_OWNER_PHONE',OWNER)
    calls=NS(initiate=AsyncMock(return_value={'ok':True,'status':'queued'}));monkeypatch.setattr(agent,'call_executor',calls)
    tool=next(t for t in agent.owner_call_tools(OWNER,SID) if t.name=='call_person')
    async def run():
        for _ in range(2):
            result=await tool(phone_number=FRIEND,recipient_name='Jane',mission='Ask about meeting focus',owner_requested=True)
            assert result['status']=='queued'
    asyncio.run(run())
    bodies=[call.args[0] for call in calls.initiate.call_args_list]
    assert bodies[0].request_id==bodies[1].request_id
    assert bodies[0].phone==FRIEND


def test_private_result_link_is_bound_to_phone_job_and_expiry(monkeypatch):
    monkeypatch.setenv('DEMO_KEY','test-secret')
    job={'id':'ABC','phone':FRIEND,'created_at':datetime.now(timezone.utc).isoformat()}
    token=jobs.result_token(job)
    assert jobs.valid_token(job,token)
    assert not jobs.valid_token(dict(job,phone=OTHER),token)
    assert not jobs.valid_token(dict(job,id='OTHER'),token)
    assert not jobs.valid_token(dict(job,created_at=(datetime.now(timezone.utc)-timedelta(days=15)).isoformat()),token)


def test_new_owner_routes_are_locked_and_cookie_writes_keep_csrf(monkeypatch):
    monkeypatch.setenv('TWILIO_VOICE_PUBLIC_DOMAIN','example.com')
    app=FastAPI();routes.install_work(app);client=TestClient(app,base_url='https://example.com')
    for method,path,body in [('GET','work',None),('POST','cloud/login',{}),('POST','cloud/refresh',{}),('POST','skills/import',{}),('POST','work/ABC',{'action':'approve'})]:
        assert client.request(method,'/dashboard/api/'+path,json=body).status_code==401
    monkeypatch.setattr(routes.dashboard,'authenticate',lambda req:{'method':'cookie','csrf':'right'})
    assert client.post('/dashboard/api/cloud/login',json={}).status_code==403
    assert client.post('/dashboard/api/work/ABC',json={'action':'approve'},headers={'Origin':'https://evil.example','X-CSRF-Token':'right'}).status_code==403


def test_result_page_escapes_requester_content_and_exposes_only_bound_files(monkeypatch):
    monkeypatch.setenv('DEMO_KEY','test-secret')
    job={'id':'ABC','phone':FRIEND,'state':'completed','created_at':datetime.now(timezone.utc).isoformat(),
         'title':'<script>bad()</script>','result':'<img src=x onerror=bad()> https://example.com/a','sources':[]}
    monkeypatch.setattr(jobs,'read',lambda wid:job)
    monkeypatch.setattr(routes,'files',lambda wid,content=False:[{'id':'FILE','filename':'result.pdf','content_type':'application/pdf','content':b'%PDF'}])
    app=FastAPI();routes.install_work(app);client=TestClient(app)
    token=jobs.result_token(job)
    response=client.get('/work/ABC/'+token)
    assert response.status_code==200 and '<script>bad()' not in response.text
    assert '&lt;script&gt;' in response.text and '&lt;img' in response.text
    assert response.headers['cache-control']=='no-store'
    assert client.get('/work/ABC/wrong').status_code==404
    assert client.get('/work/ABC/'+token+'/files/WRONG').status_code==404
    response=client.get('/work/ABC/'+token+'/files/FILE')
    assert response.content==b'%PDF' and response.headers['content-disposition'].startswith('attachment;')


def bundle(extra=None):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        z.writestr('john-skills/SKILL.md','---\nname: test\ndescription: Test\n---')
        z.writestr('john-skills/catalog.json',json.dumps([dict(name='test',description='Test',path='SKILL.md')]))
        if extra:z.writestr(*extra)
    return b.getvalue()


def test_skill_import_checks_paths_and_entrypoints():
    assert cloud_skills.validate_bundle(bundle())[0]['name']=='test'
    for path in ('../secret','john-skills/../../auth.json','/etc/passwd','john-skills/..\\auth.json'):
        with pytest.raises(ValueError):cloud_skills.validate_bundle(bundle((path,'secret')))


def test_skill_symlink_cannot_point_at_cloud_credentials():
    b=io.BytesIO(bundle())
    with zipfile.ZipFile(b,'a') as z:
        info=zipfile.ZipInfo('john-skills/secret');info.create_system=3;info.external_attr=(0o120777<<16)
        z.writestr(info,'/tmp/runtime/auth.json')
    with pytest.raises(ValueError,match='Symlinks'):cloud_skills.validate_bundle(b.getvalue())


def test_cancelled_job_cannot_call_account_tools(monkeypatch):
    monkeypatch.setattr(jobs,'read',lambda wid:{'state':'cancelled'})
    invoke=AsyncMock();monkeypatch.setattr(agent.runtime,'invoke',invoke)
    with pytest.raises(agent.WorkCancelled):asyncio.run(agent.execute({'id':'A','scope':'owner'},NS(arguments='{}',call_id='C',name='run_connected_tool')))
    invoke.assert_not_called()


def test_routine_delegated_write_executes_without_reconfirmation(monkeypatch):
    from contextlib import contextmanager
    monkeypatch.setattr(jobs,'read',lambda wid:{'state':'running'})
    monkeypatch.setattr(agent.runtime,'tools',{'mail.send':{'name':'mail.send','annotations':{'readOnlyHint':False}}})
    monkeypatch.setattr(agent,'delegated_authority',AsyncMock(return_value={'allowed':True,'reason':'Complete original send instruction'}))
    invoke=AsyncMock(return_value={'ok':True,'message_id':'sent-receipt'});monkeypatch.setattr(agent.runtime,'invoke',invoke)
    db=Mock();db.execute.return_value.fetchone.return_value=None
    @contextmanager
    def connection():yield db
    monkeypatch.setattr(store:=agent.store,'connection',connection)
    monkeypatch.setattr(jobs,'event',Mock())
    args={'tool_name':'mail.send','arguments_json':'{"to":"jane@example.com","body":"Requested message"}'}
    result=asyncio.run(agent.execute({'id':'A','scope':'owner','instruction':'Send Jane the requested message'},NS(name='run_connected_tool',arguments=json.dumps(args),call_id='C'),object()))
    assert result['message_id']=='sent-receipt'
    invoke.assert_awaited_once()


def test_changed_arguments_do_not_reuse_an_old_action_approval(monkeypatch):
    monkeypatch.setattr(jobs,'read',lambda wid:{'state':'running'})
    monkeypatch.setattr(agent.runtime,'tools',{'mail.send':{'name':'mail.send','annotations':{'readOnlyHint':False}}})
    policy=AsyncMock(return_value={'allowed':False,'reason':'Different recipient'});monkeypatch.setattr(agent,'delegated_authority',policy)
    job={'id':'A','scope':'owner','instruction':'Send Jane a message','approval':{'call_id':'C','decision':'approved','arguments':{'tool_name':'mail.send','arguments_json':'{"to":"jane@example.com"}'}}}
    with pytest.raises(agent.Waiting):asyncio.run(agent.execute(job,NS(name='run_connected_tool',arguments=json.dumps({'tool_name':'mail.send','arguments_json':'{"to":"other@example.com"}'}),call_id='C'),object()))
    policy.assert_awaited_once()
