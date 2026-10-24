from urllib.parse import parse_qs,urlsplit
import json
import base64

from cryptography.fernet import Fernet
from fastapi import FastAPI
import httpx
import pytest

from adapters.account_connections import AccountConnections,GOOGLE_SCOPES
from adapters.google import GoogleAccount,GoogleTokenStore,mail_text
from adapters.shopify import ShopifyAccount
from adapters.owner_sessions import OwnerSessions
from adapters.postgres import PostgresRepository
from alli_core.authority import Actor,PermissionDenied
from alli_core.profile import Profile
from service.account_connections import account_router
from service.settings import Settings


@pytest.fixture
def setup(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'));repo.initialize()
    owner_id=repo.bootstrap_owner('+18435550100')
    actor=Actor('alli',owner_id,'dot',True,'owner-proof')
    key=base64.urlsafe_b64encode(b't'*32);cipher=Fernet(key)
    with repo.connection() as db:
        db.execute('CREATE TABLE alli_google_token(id integer PRIMARY KEY,encrypted_token text,updated_at timestamptz)')
        db.execute('INSERT INTO alli_google_token VALUES(1,%s,now())',(cipher.encrypt(b'old-refresh').decode(),))
    sessions=OwnerSessions(repo,'session-secret')
    challenge=sessions.start();cookie=sessions.verify(challenge.token,challenge.code)
    connections=AccountConnections(repo,key,'https://testserver')
    google=GoogleAccount(GoogleTokenStore(repo.connection,key,client_id='client',client_secret='client-secret'),
        repo.profile,sender_email='owner@example.com',owner_email='owner@example.com')
    settings=Settings('https://testserver','+18435550100','+18435550999','AC-test',database,'carrier',
        'session-secret',key.decode(),background=False)
    app=FastAPI();app.include_router(account_router(settings,repo,sessions,connections,google,ShopifyAccount(connections)))
    return repo,actor,connections,google,sessions,cookie,app


def test_oauth_state_is_owner_bound_single_use_and_survives_restart(setup):
    repo,owner,links,_,sessions,cookie,_=setup
    digest=sessions.digest(cookie)
    state,verifier=links.begin(owner,'shopify',digest)
    other=AccountConnections(repo,base64.urlsafe_b64encode(b't'*32),'https://testserver')
    with pytest.raises(ValueError):links.consume('shopify',state,'wrong-browser')
    assert other.consume('shopify',state,digest)==verifier
    with pytest.raises(ValueError):links.consume('shopify',state,digest)
    state,_=links.begin(owner,'google',digest)
    with repo.connection() as db:db.execute("UPDATE ea_account_oauth_flow SET expires_at=now()-interval '1 second'")
    with pytest.raises(ValueError):links.consume('google',state,digest)


def test_only_owner_can_replace_cloud_connections_or_private_credentials(setup):
    repo,owner,links,*_=setup
    links.save(owner,'shopify',{'store':'Store'},secret={'client_secret':'private-store-secret'})
    person=repo.save_person(owner,name='Friend',phone='+18435550101')
    repo.set_person_status(owner,str(person['id']),'approved',person['revision'])
    friend=Actor('alli',str(person['id']),'sms')
    with pytest.raises(PermissionDenied):links.save(friend,'shopify',{'store':'Wrong'},secret={'client_secret':'stolen'})
    assert links.get('shopify')=={'store':'Store'}
    with repo.connection() as db:
        sealed=db.execute("SELECT sealed FROM ea_account_secret WHERE provider='shopify'").fetchone()['sealed']
    assert 'private-store-secret' not in sealed
    assert 'private-store-secret' not in json.dumps(repo.ledger(owner),default=str)
    assert json.loads(links.cipher.decrypt(sealed.encode()))['client_secret']=='private-store-secret'


@pytest.mark.asyncio
async def test_connection_forms_require_owner_origin_csrf_and_use_pkce(setup):
    _,_,links,_,sessions,cookie,app=setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://testserver') as http:
        assert (await http.get('/dashboard/connections')).status_code==303
        assert (await http.get('/connections/shopify/client.json')).status_code==404
        http.cookies.set('alli_owner_session',cookie)
        account_page=await http.get('/dashboard/connections')
        policy=account_page.headers['content-security-policy']
        assert "form-action 'self' https://accounts.google.com;" in policy
        assert 'name="client_secret"' in account_page.text and 'read_orders' in account_page.text
        assert (await http.post('/dashboard/connections/google/connect',data={'csrf':sessions.csrf(cookie)})).status_code==403
        for provider in ('google',):
            response=await http.post('/dashboard/connections/'+provider+'/connect',
                data={'csrf':sessions.csrf(cookie)},headers={'Origin':'https://testserver'})
            params=parse_qs(urlsplit(response.headers['location']).query)
            assert response.status_code==303 and params['code_challenge_method']==['S256'] and params['state']
            assert 'refresh_token' not in response.headers['location']
            if provider=='google':
                assert params['access_type']==['offline'] and 'gmail.readonly' in params['scope'][0]
                assert params['redirect_uri']==['https://testserver/google/callback']


@pytest.mark.asyncio
async def test_verified_google_grant_is_atomic_and_wrong_account_keeps_old_token(setup,monkeypatch):
    repo,owner,links,google,sessions,cookie,app=setup
    real_async,real_sync=httpx.AsyncClient,httpx.Client
    state,verifier=links.begin(owner,'google',sessions.digest(cookie))
    email=['wrong@example.com']
    def provider(request):
        if request.url.host=='oauth2.googleapis.com':
            assert verifier in request.content.decode()
            return httpx.Response(200,json={'access_token':'new-access','refresh_token':'new-refresh',
                'id_token':'signed-provider-token','scope':' '.join(GOOGLE_SCOPES)})
        return httpx.Response(200,json={'emailAddress':'owner@example.com'})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:real_async(transport=httpx.MockTransport(provider),**kwargs))
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:real_sync(transport=httpx.MockTransport(provider),**kwargs))
    monkeypatch.setattr('service.account_connections.id_token.verify_oauth2_token',
        lambda *args,**kwargs:{'email':email[0],'email_verified':True,'nonce':state})
    async with real_async(transport=httpx.ASGITransport(app),base_url='https://testserver',
                          cookies={'alli_owner_session':cookie}) as http:
        failed=await http.get('/google/callback',params={'state':state,'code':'private-code'})
        assert failed.status_code==400 and google.tokens.credentials().refresh_token=='old-refresh'
        state,verifier=links.begin(owner,'google',sessions.digest(cookie));email[0]='owner@example.com'
        response=await http.get('/google/callback',params={'state':state,'code':'private-code'})
        assert response.status_code==303 and google.tokens.credentials().refresh_token=='new-refresh'
    assert 'gmail.readonly' in ' '.join(links.get('google')['scopes'])
    assert 'new-refresh' not in json.dumps(repo.ledger(owner),default=str)


@pytest.mark.asyncio
async def test_shopify_store_app_form_verifies_then_encrypts_without_vault_or_redirect(setup,monkeypatch):
    repo,owner,links,_,sessions,cookie,app=setup
    real_async,real_sync=httpx.AsyncClient,httpx.Client
    def provider(request):
        if request.url.path=='/admin/oauth/access_token':return httpx.Response(200,json={
            'access_token':'private-access','expires_in':86399,'scope':'read_orders'})
        assert request.url.path=='/admin/api/2026-10/graphql.json'
        return httpx.Response(200,json={'data':{'shop':{'name':'Configured store',
            'myshopifyDomain':'red604.myshopify.com','primaryDomain':{'host':repo.profile.shop_domain}},
            'orders':{'nodes':[]},'currentAppInstallation':{'accessScopes':[{'handle':'read_orders'}]}}})
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:real_sync(transport=httpx.MockTransport(provider),**kwargs))
    async with real_async(transport=httpx.ASGITransport(app),base_url='https://testserver',
                          cookies={'alli_owner_session':cookie}) as http:
        args={'csrf':sessions.csrf(cookie),'shop':'red604.myshopify.com',
            'client_id':'app-client-id-123456','client_secret':'private-client-secret-123456'}
        assert (await http.post('/dashboard/connections/shopify/connect',data=args)).status_code==403
        response=await http.post('/dashboard/connections/shopify/connect',data=args,
            headers={'Origin':'https://testserver'})
        assert response.status_code==303
    saved=links.get('shopify')
    assert saved['shop']=='red604.myshopify.com' and saved['scopes']==['read_orders']
    assert 'private-access' not in json.dumps(saved) and 'private-client-secret' not in json.dumps(repo.ledger(owner),default=str)
    assert ShopifyAccount(links).credentials()['access_token']=='private-access'
