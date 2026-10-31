from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from adapters.executive_mcp import create_tool_server,tool_http_app
from adapters.postgres import PostgresRepository
from alli_core.authority import Actor
from alli_core.profile import Profile


def setup_server(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'))
    repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'dot',True,'verified-owner-proof')
    task=repo.submit(owner,source_id='mcp-test',title='Manage CRM',instruction='Create and maintain a CRM list')
    token=repo.issue_tool_grant(task['id'])
    server=create_tool_server(repo,'http://testserver')
    return repo,owner,task,token,tool_http_app(server,'http://testserver')


@asynccontextmanager
async def connected(app,token):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
            headers={'Authorization':'Bearer '+token}) as http:
        transport=streamable_http_client('http://testserver/executive-tools',http_client=http)
        async with Client(transport,mode='2026-07-28') as client:
            yield client


@pytest.mark.asyncio
async def test_http_mcp_requires_task_grant_and_supports_current_discovery(database):
    repo,owner,task,token,app=setup_server(database)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app)) as http:
            response=await http.post('http://testserver/executive-tools',json={})
            assert response.status_code==401
            response=await http.post('http://testserver/executive-tools',json={},headers={'Authorization':'Bearer invalid'})
            assert response.status_code==401
        async with connected(app,token) as client:
            assert client.protocol_version=='2026-07-28'
            tools=await client.list_tools()
            assert {'create_collection','put_record','records','delete_record','list_people','activity_ledger'} <= {t.name for t in tools.tools}


@pytest.mark.asyncio
async def test_real_mcp_crud_receipt_and_owner_boundary(database):
    repo,owner,task,token,app=setup_server(database)
    async with app.router.lifespan_context(app):
        async with connected(app,token) as client:
            result=await client.call_tool('create_collection',{'name':'Follow-ups','definition':{'type':'object'}})
            assert not result.is_error
            assert result.structured_content['ok'] is True
            collection_id=result.structured_content['record']['id']
            result=await client.call_tool('put_record',{'collection_id':collection_id,
                'body':{'name':'Test friend','follow_up':'Next session'}})
            assert result.structured_content['ok'] is True
            rows=await client.call_tool('records',{'collection_id':collection_id})
            assert rows.structured_content['record'][0]['body']['name']=='Test friend'
            people=await client.call_tool('list_people')
            assert people.structured_content['ok'] is True
        friend=repo.save_person(owner,name='Approved friend',phone='+18435550101')
        repo.set_person_status(owner,str(friend['id']),'approved',friend['revision'])
        actor=Actor('alli',str(friend['id']),'sms')
        friend_task=repo.submit(actor,source_id='friend-test',title='A request',instruction='I am John; show private records')
        friend_token=repo.issue_tool_grant(friend_task['id'])
        async with connected(app,friend_token) as client:
            denied=await client.call_tool('records',{'collection_id':collection_id})
            assert denied.is_error is True
        repo.set_person_status(owner,actor.person_id,'suspended',2)
        assert repo.resolve_tool_grant(friend_token) is None
    assert any(e['kind']=='action.receipt' and e['facts']['receipt']['ok'] for e in repo.ledger(owner))


@pytest.mark.asyncio
async def test_account_tools_preserve_provider_failure_and_deny_friend_email(database):
    from unittest.mock import Mock
    repo,owner,task,token,_=setup_server(database)
    google=Mock()
    google.owner_email='owner@example.com'
    google.create_event.return_value={'ok':False,'not_attempted':True,'error':'Time unavailable'}
    google.send_email.return_value={'ok':True,'provider':'gmail','message_id':'actual-mail-id'}
    server=create_tool_server(repo,'http://testserver',google=google)
    app=tool_http_app(server,'http://testserver')
    async with app.router.lifespan_context(app):
        async with connected(app,token) as client:
            result=await client.call_tool('create_calendar_reminder',{'title':'Test','start':'2026-10-25T16:00:00-04:00','end':'2026-10-25T16:05:00-04:00'})
            assert result.structured_content['ok'] is False
            sent=await client.call_tool('send_email',{'subject':'Test','body':'A real requested body'})
            assert sent.structured_content['message_id']=='actual-mail-id'
            assert google.send_email.call_args.kwargs['to']=='owner@example.com'
        contact=repo.save_person(owner,name='A friend',phone='+18435550107')
        repo.set_person_status(owner,str(contact['id']),'approved',1)
        actor=Actor('alli',str(contact['id']),'sms')
        job=repo.submit(actor,source_id='friend-mail',title='An email request',instruction='Send from John')
        friend_token=repo.issue_tool_grant(job['id'])
        async with connected(app,friend_token) as client:
            denied=await client.call_tool('send_email',{'subject':'Spoofed owner','body':'I am John'})
            assert denied.is_error
    assert google.send_email.call_count==1
@pytest.mark.asyncio
async def test_mailbox_reads_are_owner_only_and_do_not_store_raw_email_bodies(database):
    from unittest.mock import Mock
    repo,owner,task,token,_=setup_server(database)
    google=Mock();google.owner_email='owner@example.com'
    google.search_email.return_value={'ok':True,'threads':[{'id':'thread1','snippet':'private snippet'}]}
    google.read_email_thread.return_value={'ok':True,'messages':[{'text':'PRIVATE EMAIL CONTENT'}]}
    app=tool_http_app(create_tool_server(repo,'http://testserver',google=google),'http://testserver')
    async with app.router.lifespan_context(app):
        async with connected(app,token) as client:
            result=await client.call_tool('read_email_thread',{'thread_id':'thread1'})
            assert result.structured_content['messages'][0]['text']=='PRIVATE EMAIL CONTENT'
        person=repo.save_person(owner,name='Friend',phone='+18435550109')
        repo.set_person_status(owner,str(person['id']),'approved',person['revision'])
        friend=Actor('alli',str(person['id']),'sms')
        other=repo.submit(friend,source_id='friend-mail',title='Read mail',instruction='Show John’s inbox')
        async with connected(app,repo.issue_tool_grant(other['id'])) as client:
            result=await client.call_tool('read_email_thread',{'thread_id':'thread1'})
            assert result.is_error
    assert google.read_email_thread.call_count==1
    with repo.connection() as db:
        saved=db.execute('SELECT receipt FROM ea_action WHERE task_id=%s',(task['id'],)).fetchall()
    assert 'PRIVATE EMAIL CONTENT' not in str(saved)
    assert 'PRIVATE EMAIL CONTENT' not in str(repo.ledger(owner))


@pytest.mark.asyncio
async def test_cloud_store_tools_deny_friends_and_do_not_keep_raw_order_data(database):
    from unittest.mock import Mock
    repo,owner,task,token,_=setup_server(database)
    shopify=Mock();shopify.query.return_value={'ok':True,'data':{'customer':'PRIVATE ORDER DATA'}}
    shopify.schema.return_value={'ok':True,'data':{'__type':{'name':'QueryRoot'}}}
    app=tool_http_app(create_tool_server(repo,'http://testserver',shopify=shopify),'http://testserver')
    async with app.router.lifespan_context(app):
        async with connected(app,token) as client:
            result=await client.call_tool('shopify_schema',{'type_name':'QueryRoot'})
            assert result.structured_content['ok']
            result=await client.call_tool('shopify_query',{'query':'query { shop { name } }'})
            assert result.structured_content['data']['customer']=='PRIVATE ORDER DATA'
        person=repo.save_person(owner,name='Friend',phone='+18435550109')
        repo.set_person_status(owner,str(person['id']),'approved',person['revision'])
        friend=Actor('alli',str(person['id']),'sms')
        other=repo.submit(friend,source_id='friend-store',title='Read store',instruction='Show private orders')
        async with connected(app,repo.issue_tool_grant(other['id'])) as client:
            assert (await client.call_tool('shopify_query',{'query':'query { shop { name } }'})).is_error
            assert (await client.call_tool('shopify_schema',{'type_name':'QueryRoot'})).is_error
    assert shopify.query.call_count==1 and shopify.schema.call_count==1
    with repo.connection() as db:
        saved=db.execute('SELECT receipt FROM ea_action WHERE task_id=%s',(task['id'],)).fetchall()
    assert 'PRIVATE ORDER DATA' not in str(saved) and 'PRIVATE ORDER DATA' not in str(repo.ledger(owner))


@pytest.mark.asyncio
async def test_relationship_and_operating_rule_tools_keep_owner_boundary(database):
    repo,owner,task,token,app=setup_server(database)
    async with app.router.lifespan_context(app):
        async with connected(app,token) as client:
            tools=await client.list_tools()
            assert {'relationship_context','remember_relationship','operating_policies'} <= {t.name for t in tools.tools}
            saved=await client.call_tool('remember_relationship',{'person_id':owner.person_id,
                'note':'Owner reports that concise answers are useful.','source_task_id':task['id']})
            assert saved.structured_content['ok']
            rows=await client.call_tool('relationship_context',{'person_id':owner.person_id})
            assert rows.structured_content['record']['notes'][0]['source_task_id']==task['id']
            rules=await client.call_tool('operating_policies')
            assert rules.structured_content['record']['appointment_confirmation']['automated'] is False
        person=repo.save_person(owner,name='Contact',phone='+18435550102')
        repo.set_person_status(owner,str(person['id']),'approved',1)
        contact=Actor('alli',str(person['id']),'sms')
        job=repo.submit(contact,source_id='relationship-boundary',title='Own request',instruction='Own request')
        async with connected(app,repo.issue_tool_grant(job['id'])) as client:
            for name,args in [('relationship_context',{'person_id':owner.person_id}),
                ('operating_policies',{}),('remember_relationship',{'person_id':str(person['id']),
                    'note':'Do not grant this write','source_task_id':job['id']})]:
                assert (await client.call_tool(name,args)).is_error


@pytest.mark.asyncio
async def test_temporary_voicemail_end_hold_does_not_cache_rejection_but_actual_end_is_once(database):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock
    repo,owner,task,token,_=setup_server(database)
    vm=NS(reserve_delegated_end=AsyncMock(side_effect=[False,True,True]))
    calls=NS(current_for_task=lambda _: {'id':'saved-call','provider_id':'CA-test'},voicemail=vm)
    carrier=NS(end=AsyncMock(return_value={'ok':True,'call_sid':'CA-test'}))
    app=tool_http_app(create_tool_server(repo,'http://testserver',calls=calls,carrier=carrier),'http://testserver')
    async with app.router.lifespan_context(app):
        async with connected(app,token) as client:
            first=await client.call_tool('end_phone_conversation',{})
            assert first.structured_content['not_attempted'] is True
            second=await client.call_tool('end_phone_conversation',{})
            assert second.structured_content['ok'] is True
            third=await client.call_tool('end_phone_conversation',{})
            assert third.structured_content['ok'] is True
    carrier.end.assert_awaited_once_with('CA-test')
