import base64
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import httpx2
from mcp import Client,types
from mcp.client.streamable_http import streamable_http_client
from pydantic import TypeAdapter
import pytest
from standardwebhooks.webhooks import Webhook

from adapters.postgres import PostgresRepository
from adapters.webhooks import PublicWebhook,CallbackError
from alli_core.profile import Profile
from service.application import create_application
from service.settings import Settings


@pytest.mark.asyncio
async def test_native_event_protocol_persists_filters_and_stops_delivery(database):
    settings=Settings('https://testserver','+18435550100','+18435550999','AC-test',database,
        'test-secret','test-session-secret',Fernet.generate_key().decode(),background=False)
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'))
    api=NS(close=AsyncMock());api.with_options=lambda **kwargs:api
    app=create_application(settings,repo,NS(),None,api)
    received=[]
    def send(url,subscription,keys,event_id,payload):
        received.append((event_id,payload))
        return {'status':200,'body':{'challenge':payload.get('challenge')}}
    app.state.events.webhook=NS(send=send)
    async with app.router.lifespan_context(app):
        token=await app.state.oauth.issue('test-dot')
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),headers={'Authorization':'Bearer '+token.access_token}) as http:
            async with Client(streamable_http_client('https://testserver/mcp',http_client=http),mode='2026-07-28') as client:
                async def request(method,params):
                    return await client.session.send_request(types.Request[dict,str](method=method,params=params),TypeAdapter(dict))
                catalog=await request('events/list',{})
                assert catalog['events'][0]['name']=='alli.work.updated'
                params={'name':'alli.work.updated','arguments':{'states':['completed']},'delivery':{
                    'mode':'webhook','url':'https://receiver.example/callback','secret':'whsec_'+base64.b64encode(b'x'*32).decode()}}
                subscription=await request('events/subscribe',params)
                assert subscription['id'] and subscription['refreshBefore'] and subscription['cursor'] is None
                assert (await request('events/subscribe',params))['id']==subscription['id']
                owner=repo.owner_actor('test')
                task=repo.submit(owner,source_id='test-event',title='A test',instruction='Do the work')
                repo.record_outcome(task['id'],'completed','Actually finished',turn_id='turn-test')
                assert await app.state.events.tick()
                delivered=[payload for _,payload in received if payload.get('name')]
                assert len(delivered)==1 and delivered[0]['data']['task_id']==task['id']
                assert 'Actually finished' not in json.dumps(delivered)  # no private work body in notification
                assert not await app.state.events.tick()
                await request('events/unsubscribe',{key:value for key,value in params.items() if key!='ttlMs'})
                with repo.connection() as db:
                    assert db.execute('SELECT active FROM ea_subscription').fetchone()['active'] is False


def test_public_callback_pins_address_preserves_tls_hostname_and_signs_exact_bytes():
    sent={}
    class Pool:
        def __init__(self,address,**kwargs):sent.update(address=address,options=kwargs)
        def request(self,method,path,**kwargs):
            sent.update(method=method,path=path,**kwargs)
            return NS(status=204,read=lambda size:b'')
        def close(self):pass
    callback=PublicWebhook(resolver=lambda *a,**k:[(2,1,6,'',('8.8.8.8',443))],pool_factory=Pool)
    key='whsec_'+base64.b64encode(b'x'*32).decode()
    callback.send('https://receiver.example/cb?token=test','sub',[key],'event',{'eventId':'event','data':{'name':'Alli'}})
    assert sent['address']=='8.8.8.8' and sent['options']['assert_hostname']=='receiver.example'
    assert sent['options']['server_hostname']=='receiver.example' and sent['redirect'] is False
    assert Webhook(key).verify(sent['body'],sent['headers'])['eventId']=='event'
    callback.resolver=lambda *a,**k:[(2,1,6,'',('127.0.0.1',443))]
    with pytest.raises(CallbackError):callback.send('https://receiver.example/cb','s',[key],'e',{})
    for url in ['http://receiver.example','https://user:password@receiver.example','https://receiver.example:8443']:
        with pytest.raises(CallbackError):callback.destination(url)
