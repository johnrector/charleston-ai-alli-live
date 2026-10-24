"""Provider consent is performed by the owner; cloud credentials stay private."""
import asyncio
from datetime import datetime,timedelta,timezone
import hmac

import httpx
from fastapi import APIRouter,HTTPException,Request
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
from starlette.responses import JSONResponse,RedirectResponse

from adapters.account_connections import GOOGLE_SCOPES,SHOPIFY_MCP,SHOPIFY_TOKEN
from .dashboard import SESSION
from .web_helpers import escape as e,form,page


def account_router(settings,repository,sessions,connections,google,api):
    router=APIRouter()

    async def owner(request):
        actor=await asyncio.to_thread(sessions.actor,request.cookies.get(SESSION,''))
        if not actor:raise HTTPException(303,headers={'Location':'/dashboard/login?next=/dashboard/connections'})
        return actor

    def digest(request):return sessions.digest(request.cookies.get(SESSION,''))

    @router.get('/dashboard/connections')
    async def account_page(request:Request):
        await owner(request)
        content='<h1>Alli’s cloud connections.</h1><p>These accounts work when your Mac is off. Account access belongs to John; approved friends do not receive private mailbox or store access.</p>'
        for provider,label,description in (
            ('google','Google Calendar & Gmail','Calendar access and email sending, plus mailbox search and reading.'),
            ('shopify','Shopify','Read store information, orders, shipping addresses and inventory through Shopify’s MCP server.')):
            linked=await asyncio.to_thread(connections.get,provider)
            content+='<section><h2>'+label+'</h2><p>'+description+'</p><p>'
            content+=('Authorized: '+e(linked.get('account') or linked.get('store') or '')+
                      ' · '+e(linked.get('verified_at',''))) if linked else 'Additional cloud authorization needed.'
            content+='</p><form method="post" action="/dashboard/connections/'+provider+'/connect">'
            content+='<input type="hidden" name="csrf" value="'+e(sessions.csrf(request.cookies.get(SESSION,'')))+'">'
            content+='<button>'+('Reconnect ' if linked else 'Connect ')+label+'</button></form></section>'
        return page('Connections',content)

    def google_flow(state,verifier):
        tokens=google.tokens
        return Flow.from_client_config({'web':{'client_id':tokens.client_id,'client_secret':tokens.client_secret,
            'auth_uri':'https://accounts.google.com/o/oauth2/v2/auth','token_uri':'https://oauth2.googleapis.com/token'}},
            scopes=list(GOOGLE_SCOPES),state=state,code_verifier=verifier,
            redirect_uri=settings.base_url+'/google/callback')

    @router.post('/dashboard/connections/{provider}/connect')
    async def start(provider:str,request:Request):
        actor=await owner(request);data=await form(request)
        if (request.headers.get('origin')!=settings.base_url or not hmac.compare_digest(
            data.get('csrf',''),sessions.csrf(request.cookies.get(SESSION,'')))):
            raise HTTPException(403,'Use the Connections page to authorize this account')
        if provider not in {'google','shopify'} or provider=='google' and google is None:
            raise HTTPException(404)
        state,verifier=await asyncio.to_thread(connections.begin,actor,provider,digest(request))
        if provider=='google':
            url,_=google_flow(state,verifier).authorization_url(access_type='offline',prompt='consent',
                include_granted_scopes='true',login_hint=google.owner_email,nonce=state)
        else:url=connections.shopify_authorization(state,verifier)
        return RedirectResponse(url,303,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

    @router.get('/connections/shopify/client.json')
    async def client_metadata():
        return JSONResponse(connections.client_metadata(),headers={'Cache-Control':'public, max-age=300'})

    def verify_google(tokens,state):
        identity=id_token.verify_oauth2_token(tokens.get('id_token',''),GoogleRequest(),google.tokens.client_id)
        if (not identity.get('email_verified') or identity.get('email','').casefold()!=google.owner_email.casefold()
                or identity.get('nonce')!=state):raise ValueError('Wrong Google account or authorization')
        scopes=set(tokens.get('scope','').split())
        if 'email' in scopes:scopes.add('https://www.googleapis.com/auth/userinfo.email')
        if not set(GOOGLE_SCOPES)<=scopes or not tokens.get('refresh_token'):
            raise ValueError('Calendar, email sending, inbox reading and offline access must be granted')
        # Confirm the newly granted mailbox before replacing the working token.
        with httpx.Client(timeout=15,follow_redirects=False) as http:
            response=http.get('https://gmail.googleapis.com/gmail/v1/users/me/profile',
                headers={'Authorization':'Bearer '+tokens['access_token']})
            response.raise_for_status()
            if response.json().get('emailAddress','').casefold()!=google.owner_email.casefold():
                raise ValueError('Wrong mailbox')
        return scopes

    @router.get('/google/callback')
    async def google_callback(request:Request):
        actor=await owner(request)
        if google is None:raise HTTPException(404)
        stage='state'
        try:
            state=request.query_params.get('state','')
            verifier=await asyncio.to_thread(connections.consume,'google',state,digest(request))
            if request.query_params.get('error') or not request.query_params.get('code'):
                raise ValueError('Authorization was not completed')
            # Validate granted scopes ourselves, including Google's OIDC aliases,
            # so incremental authorization can safely return extra prior scopes.
            async with httpx.AsyncClient(timeout=20,follow_redirects=False) as http:
                stage='token_exchange'
                result=await http.post('https://oauth2.googleapis.com/token',data={
                    'grant_type':'authorization_code','code':request.query_params['code'],
                    'client_id':google.tokens.client_id,'client_secret':google.tokens.client_secret,
                    'redirect_uri':settings.base_url+'/google/callback','code_verifier':verifier})
                result.raise_for_status();tokens=result.json()
            stage='account_and_scopes'
            scopes=await asyncio.to_thread(verify_google,tokens,state)
            stage='save'
            await asyncio.to_thread(connections.save,actor,'google',{
                'account':google.owner_email,'scopes':sorted(scopes),'verified_at':datetime.now(timezone.utc).isoformat()},
                google_refresh=tokens['refresh_token'])
        except Exception as error:
            await asyncio.to_thread(connections.failed,actor,'google',stage,error)
            return page('Connection incomplete','<h1>Google connection incomplete.</h1><p>The account, required permissions or authorization could not be verified. Your earlier connection is retained. Start again from Connections and grant the listed permissions.</p><p><a href="/dashboard/connections">Connections</a></p>',status=400)
        return RedirectResponse('/dashboard/connections',303,headers={'Cache-Control':'no-store'})

    async def shopify_call(token,name,arguments):
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as http:
            result=await http.post(SHOPIFY_MCP,headers={'Authorization':'Bearer '+token,
                'Accept':'application/json, text/event-stream'},json={
                'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':arguments}})
            result.raise_for_status();body=result.json()
        payload=body.get('result',{})
        if body.get('error') or payload.get('isError'):raise ValueError('Shopify account check failed')
        return payload

    @router.get('/connections/shopify/callback')
    async def shopify_callback(request:Request):
        import json
        actor=await owner(request)
        stage='state'
        try:
            state=request.query_params.get('state','')
            verifier=await asyncio.to_thread(connections.consume,'shopify',state,digest(request))
            if request.query_params.get('error') or not request.query_params.get('code'):
                raise ValueError('Authorization was not completed')
            async with httpx.AsyncClient(timeout=20,follow_redirects=False) as http:
                stage='token_exchange'
                response=await http.post(SHOPIFY_TOKEN,data={'grant_type':'authorization_code',
                    'code':request.query_params['code'],'code_verifier':verifier,
                    'client_id':connections.shopify_client_id,'resource':SHOPIFY_MCP,
                    'redirect_uri':settings.base_url+'/connections/shopify/callback'})
                response.raise_for_status();grant=response.json()
            if not grant.get('refresh_token') or not grant.get('access_token'):
                raise ValueError('Offline Shopify authorization is required')
            stage='store_verification'
            payload=await shopify_call(grant['access_token'],'get-shop-info',{})
            info=payload.get('structuredContent')
            if not info:
                info=next((json.loads(item['text']) for item in payload.get('content',[])
                    if item.get('type')=='text' and item.get('text','').lstrip().startswith('{')),None)
            if not info or (repository.profile.shop_domain and
                info.get('domain','').casefold()!=repository.profile.shop_domain.casefold()):
                raise ValueError('The chosen Shopify store does not match the configured store')
            await shopify_call(grant['access_token'],'list-orders',{'first':1,'query':'fulfillment_status:unfulfilled'})
            stage='cloud_vault'
            prior=await asyncio.to_thread(connections.get,'shopify')
            if prior:vault_id=prior['vault_id']
            else:
                vault=await api.beta.agents.vaults.create(name=repository.profile.name+' Shopify',
                    metadata={'assistant_id':repository.profile.id,'provider':'shopify'})
                vault_id=vault.id
            expiry=({'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=int(grant['expires_in']))).isoformat()}
                    if grant.get('expires_in') else {})
            credential=await api.beta.agents.vaults.credentials.create(vault_id,name='Shopify cloud OAuth',
                auth={'type':'mcp_oauth','mcp_server_url':SHOPIFY_MCP,'access_token':grant['access_token'],**expiry,
                    'refresh':{'token_endpoint':SHOPIFY_TOKEN,'client_id':connections.shopify_client_id,
                        'refresh_token':grant['refresh_token'],'token_endpoint_auth':{'type':'none'}}})
            stage='save'
            await asyncio.to_thread(connections.save,actor,'shopify',{'vault_id':vault_id,
                'credential_id':credential.id,'server_url':SHOPIFY_MCP,'store':info.get('name',''),
                'domain':info.get('domain',''),'scopes':grant.get('scope','read_all_orders').split(),
                'verified_at':datetime.now(timezone.utc).isoformat()})
        except Exception as error:
            await asyncio.to_thread(connections.failed,actor,'shopify',stage,error)
            return page('Connection incomplete','<h1>Shopify connection incomplete.</h1><p>The store, offline authorization or cloud credential could not be verified. Your earlier connection is retained. Select your configured store when authorizing.</p><p><a href="/dashboard/connections">Connections</a></p>',status=400)
        return RedirectResponse('/dashboard/connections',303,headers={'Cache-Control':'no-store'})

    return router
