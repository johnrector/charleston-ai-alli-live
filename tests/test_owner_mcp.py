from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import pytest

from adapters.postgres import PostgresRepository
from alli_core.profile import Profile
from service.application import create_application
from service.settings import Settings


@pytest.mark.asyncio
async def test_dot_bridge_records_and_delegates_without_side_effects_or_repeated_tasks(database):
    settings=Settings('https://testserver','+18435550100','+18435550999','AC-test',database,
        'test-carrier-secret','test-session-secret',Fernet.generate_key().decode(),background=False)
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'))
    api=NS(close=AsyncMock());api.with_options=lambda **kwargs:api
    carrier=NS(send=AsyncMock(),dial=AsyncMock())
    app=create_application(settings,repo,carrier,None,api)
    async with app.router.lifespan_context(app):
        token=await app.state.oauth.issue('test-dot-client')
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
                headers={'Authorization':'Bearer '+token.access_token}) as http:
            transport=streamable_http_client('https://testserver/mcp',http_client=http)
            async with Client(transport,mode='2026-07-28') as client:
                tools=await client.list_tools()
                assert {'call_outbound','get_call_result','list_communication_updates','record_dot_activity','delegate_work',
                        'check_service_health'} <= {tool.name for tool in tools.tools}
                result=await client.call_tool('delegate_work',{'request_id':'dot-test','title':'Public research',
                                                              'instruction':'Research one public topic'})
                assert not result.is_error and result.structured_content['state']=='queued'
                duplicate=await client.call_tool('delegate_work',{'request_id':'dot-test','title':'Public research',
                                                              'instruction':'Research one public topic'})
                assert duplicate.structured_content['task_id']==result.structured_content['task_id']
                assert len(repo.ready_tasks())==1
                recorded=await client.call_tool('record_dot_activity',{'request_id':'dot-receipt','title':'Dot test',
                    'status':'completed','details':'Read actual updates','evidence':['unit-test-evidence']})
                assert recorded.structured_content['independently_verified'] is False
                feed=await client.call_tool('list_communication_updates',{'after_id':0,'limit':100})
                updates=feed.structured_content['updates']
                assert any(event['kind']=='dot.reported' for event in updates)
                assert feed.structured_content['next_cursor']==updates[-1]['id']
        carrier.send.assert_not_awaited();carrier.dial.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_mcp_and_task_mcp_tokens_are_not_interchangeable(database):
    settings=Settings('https://testserver','+18435550100','+18435550999','AC-test',database,
        'test-secret','test-session-secret',Fernet.generate_key().decode(),background=False)
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'))
    api=NS(close=AsyncMock());api.with_options=lambda **kwargs:api
    app=create_application(settings,repo,NS(),None,api)
    async with app.router.lifespan_context(app):
        owner=repo.owner_actor('unit-test-owner')
        task=repo.submit(owner,source_id='task',title='A task',instruction='Do this')
        task_token=repo.issue_tool_grant(task['id'])
        owner_token=(await app.state.oauth.issue('test-client')).access_token
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app)) as http:
            assert (await http.post('https://testserver/mcp',json={},headers={'Authorization':'Bearer '+task_token})).status_code==401
            assert (await http.post('https://testserver/executive-tools',json={},headers={'Authorization':'Bearer '+owner_token})).status_code==401
            metadata=await http.get('https://testserver/.well-known/oauth-authorization-server')
            assert metadata.status_code==200


@pytest.mark.asyncio
async def test_owner_diagnostic_tool_exports_schema_and_never_dispatches_work(database,monkeypatch):
    from adapters.diagnostics import ServiceDiagnostics
    from zoneinfo import ZoneInfo
    from datetime import datetime
    settings=Settings('https://testserver','+18435550100','+18435550999','AC-test',database,
        'test-secret','test-session-secret',Fernet.generate_key().decode(),background=False)
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'))
    api=NS(close=AsyncMock());api.with_options=lambda **kwargs:api
    probes=[]
    async def checked(self):probes.append(True);return {'ok':True}
    for name in ('voice','text','agents'):monkeypatch.setattr(ServiceDiagnostics,name,checked)
    carrier=NS(send=AsyncMock(),dial=AsyncMock())
    app=create_application(settings,repo,carrier,None,api)
    async with app.router.lifespan_context(app):
        token=(await app.state.oauth.issue('daily-check-test')).access_token
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
                headers={'Authorization':'Bearer '+token}) as http:
            async with Client(streamable_http_client('https://testserver/mcp',http_client=http),mode='2026-07-28') as client:
                tools=await client.list_tools()
                tool=next(t for t in tools.tools if t.name=='check_service_health')
                assert tool.input_schema['required']==['request_id']
                assert set(tool.input_schema['properties'])=={'request_id'}
                assert tool.annotations.destructive_hint is False
                assert tool.annotations.idempotent_hint is True
                request={'request_id':'daily-health:'+datetime.now(ZoneInfo('America/New_York')).date().isoformat()}
                first=await client.call_tool('check_service_health',request)
                again=await client.call_tool('check_service_health',request)
                assert not first.is_error and first.structured_content['ok']
                assert again.structured_content['reused'] and len(probes)==3
        assert repo.ready_tasks()==[]
        carrier.send.assert_not_awaited();carrier.dial.assert_not_awaited()
