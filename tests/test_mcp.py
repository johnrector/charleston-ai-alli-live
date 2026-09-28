"""Protocol-level OAuth and MCP regression tests; no real phone calls."""
import asyncio
import base64
import hashlib
import importlib
import json
import re
import time
from urllib.parse import parse_qs, urlparse
from types import SimpleNamespace
import jwt
import pytest
from fastapi.testclient import TestClient


class MemoryStore:
    def __init__(self): self.rows = {}
    def initialize(self): pass
    def put(self, kind, key, data, ttl): self.rows[kind,key] = (json.loads(json.dumps(data)), time.time()+ttl)
    def get(self, kind, key, consume=False):
        item = self.rows.pop((kind,key),None) if consume else self.rows.get((kind,key))
        return item[0] if item and item[1]>time.time() else None


@pytest.fixture
def service(monkeypatch):
    for k,v in {'TWILIO_ACCOUNT_SID':'AC'+'0'*32,'TWILIO_AUTH_TOKEN':'test','TWILIO_API_KEY':'SK'+'0'*32,'TWILIO_API_SECRET':'test','TWILIO_PHONE_NUMBER':'+15555550100','TWILIO_VOICE_PUBLIC_DOMAIN':'example.com','OPENAI_API_KEY':'test','DEMO_KEY':'test-admin'}.items():
        monkeypatch.setenv(k,v)
    app = importlib.import_module('app')
    import mcp_auth
    import mcp_integration
    memory = MemoryStore()
    monkeypatch.setattr(mcp_auth,'store',memory)
    monkeypatch.setattr(mcp_integration,'store',memory)
    # Make a fresh server/session manager for each test lifecycle.
    from fastapi import FastAPI
    api=FastAPI()
    mcp_integration.install_mcp(api,app.initiate_demo_call,app.DemoCall)
    calls=[]
    async def initiate(options):
        calls.append(options)
        return SimpleNamespace(call_sid='CA'+'a'*32)
    monkeypatch.setattr(app.voice_channel,'initiate_outbound_conversation',initiate)
    with TestClient(api,base_url=mcp_auth.BASE) as client:
        yield client,mcp_auth,calls


def register(client):
    data={'redirect_uris':['https://chatgpt.com/connector/oauth/test-callback'],
          'grant_types':['authorization_code','refresh_token'],'response_types':['code'],
          'token_endpoint_auth_method':'client_secret_post','scope':'calls:write'}
    r=client.post('/register',json=data)
    assert r.status_code==201,r.text
    return r.json()


def grant(client, auth, registered):
    verifier='v'*64
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    r=client.get('/authorize',params={'client_id':registered['client_id'],'redirect_uri':registered['redirect_uris'][0],
        'response_type':'code','scope':auth.SCOPE,'resource':auth.RESOURCE,'state':'state-test','code_challenge':challenge,'code_challenge_method':'S256'},follow_redirects=False)
    assert r.status_code==302,r.text
    location=r.headers['location']
    page=client.get(location)
    csrf=re.search('name="csrf" value="([^"]+)"',page.text)[1]
    nonce=parse_qs(urlparse(location).query)['request'][0]
    form={'request':nonce,'csrf':csrf,'owner_key':'test-admin'}
    bad=client.post('/mcp-owner/consent',data=form,headers={'origin':'https://attacker.example'},follow_redirects=False)
    assert bad.status_code==403
    response=client.post('/mcp-owner/consent',data=form,headers={'origin':auth.BASE},follow_redirects=False)
    assert response.status_code==303,response.text
    query=parse_qs(urlparse(response.headers['location']).query)
    assert query['state']==['state-test']
    assert client.post('/mcp-owner/consent',data=form,headers={'origin':auth.BASE},follow_redirects=False).status_code==403
    return {'grant_type':'authorization_code','client_id':registered['client_id'],'client_secret':registered['client_secret'],
        'code':query['code'][0],'code_verifier':verifier,'redirect_uri':registered['redirect_uris'][0],'resource':auth.RESOURCE}


def test_full_oauth_mcp_and_mission(service):
    client,auth,calls=service
    challenge=client.post('/mcp',json={})
    assert challenge.status_code==401
    assert 'oauth-protected-resource/mcp' in challenge.headers['www-authenticate']
    assert client.get('/.well-known/oauth-protected-resource/mcp').json()['resource']==auth.RESOURCE
    metadata=client.get('/.well-known/oauth-authorization-server').json()
    assert metadata['code_challenge_methods_supported']==['S256']
    registered=register(client)
    request=grant(client,auth,registered)
    assert client.post('/token',data={**request,'resource':'https://attacker.example'}).status_code==400
    assert client.post('/token',data={**request,'code_verifier':'wrong'}).status_code==400
    token=client.post('/token',data=request)
    assert token.status_code==200,token.text
    assert client.post('/token',data=request).status_code==400 # one-time code
    tokens=token.json()
    headers={'Authorization':'Bearer '+tokens['access_token'],'Accept':'application/json, text/event-stream'}
    def rpc(method,params=None):
        r=client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}},headers=headers)
        assert r.status_code==200,r.text
        return r.json()
    init=rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
    assert 'Calling.' in init['result']['instructions']
    tools=rpc('tools/list')['result']['tools']
    assert [t['name'] for t in tools]==['call_contact']
    assert tools[0]['securitySchemes']==[{'type':'oauth2','scopes':['calls:write']}]
    assert tools[0]['annotations']['readOnlyHint'] is False
    assert tools[0]['inputSchema']['required']==['phone','recipient_name','mission']
    args={'phone':'(843) 806-1033','recipient_name':'Charleston AI','email':'hello@ai-chs.com','mission':'Sell Mount Pleasant house','business_card_text':'Charleston AI, ai-chs.com'}
    result=rpc('tools/call',{'name':'call_contact','arguments':args})['result']
    assert result['structuredContent']=={'ok':True,'call_sid':'CA'+'a'*32,'status':'queued'}
    assert len(calls)==1 and calls[0].to=='+18438061033'
    assert 'Sell Mount Pleasant house' in calls[0].session_config['instructions']
    assert 'hello@ai-chs.com' in calls[0].session_config['instructions']
    bad=rpc('tools/call',{'name':'call_contact','arguments':{**args,'phone':'123'}})['result']
    assert bad['isError'] and len(calls)==1
    refresh={'grant_type':'refresh_token','client_id':registered['client_id'],'client_secret':registered['client_secret'],'refresh_token':tokens['refresh_token'],'resource':auth.RESOURCE}
    refreshed=client.post('/token',data=refresh)
    assert refreshed.status_code==200
    assert client.post('/token',data=refresh).status_code==400 # rotation
    revoked=client.post('/revoke',data={'client_id':registered['client_id'],'client_secret':registered['client_secret'],'token':refreshed.json()['refresh_token'],'token_type_hint':'refresh_token'})
    assert revoked.status_code==200
    assert client.post('/token',data={**refresh,'refresh_token':refreshed.json()['refresh_token']}).status_code==400


def test_rejects_wrong_audience_scope_expiry_and_client(service):
    client,auth,calls=service
    registered=register(client)
    req=grant(client,auth,registered)
    assert client.post('/token',data={**req,'client_secret':'wrong'}).status_code==401
    tokens=client.post('/token',data=req).json()
    claims=jwt.decode(tokens['access_token'],auth.signing_key(),algorithms=['HS256'],audience=auth.RESOURCE,issuer=auth.ISSUER)
    for change in [{'aud':'https://attacker.example'},{'scope':'gmail:read'},{'exp':0},{'iss':'https://attacker.example'},{'sub':'another-owner'}]:
        invalid=jwt.encode({**claims,**change},auth.signing_key(),algorithm='HS256')
        assert client.post('/mcp',json={},headers={'Authorization':'Bearer '+invalid}).status_code==401
    assert not calls
    rejected=client.post('/register',json={'redirect_uris':['https://attacker.example/steal'],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'scope':'calls:write'})
    assert rejected.status_code==400


def test_normalize_requires_a_real_direct_number():
    from mcp_integration import normalize_phone
    assert normalize_phone('843-806-1033')=='+18438061033'
    assert normalize_phone('+44 20 7946 0958')=='+442079460958'
    for number in ['no phone','911','000-000-0000','843-806-1033 ext 12']:
        with pytest.raises(ValueError):normalize_phone(number)
