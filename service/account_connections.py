"""Provider consent is performed by the owner; cloud credentials stay private."""
import asyncio
from datetime import datetime,timezone
import hmac

import httpx
from fastapi import APIRouter,HTTPException,Request
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
from starlette.responses import RedirectResponse

from adapters.account_connections import GOOGLE_SCOPES
from adapters.errors import InvalidInput,ProviderRejected
from .dashboard import SESSION
from .web_helpers import escape as e,form,page


def account_router(settings,repository,sessions,connections,google,shopify):
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
            ('shopify','Shopify','Read store information, orders, shipping addresses and inventory in the cloud.')):
            linked=await asyncio.to_thread(connections.get,provider)
            content+='<section><h2>'+label+'</h2><p>'+description+'</p><p>'
            content+=('Authorized: '+e(linked.get('account') or linked.get('store') or '')+
                      ' · '+e(linked.get('verified_at',''))) if linked else 'Additional cloud authorization needed.'
            content+='</p><form method="post" action="/dashboard/connections/'+provider+'/connect">'
            content+='<input type="hidden" name="csrf" value="'+e(sessions.csrf(request.cookies.get(SESSION,'')))+'">'
            if provider=='shopify':
                content+='''<p>One-time setup for your own Shopify store. Alli renews access automatically in the cloud.
                Store operations through this connection are read-only. Credentials are encrypted and never placed in model instructions.</p>
                <details><summary>Set up the Shopify app</summary><ol>
                <li>Open <a href="https://dev.shopify.com/" target="_blank" rel="noopener noreferrer">Shopify’s Dev Dashboard</a>
                and select the organization that owns '''+e(repository.profile.shop_domain or 'your store')+'''.</li>
                <li>Create an app named <strong>'''+e(repository.profile.name)+''' Cloud</strong>. On its version, set the app URL to
                <code>'''+e(settings.base_url)+'''/dashboard/connections</code> and turn off embedding in Shopify admin.</li>
                <li>Choose these read permissions: <code>read_orders, read_products, read_inventory, read_locations</code>.
                Release the version and install it on your store.</li>
                <li>In the app’s Settings, copy its Client ID and Client secret into the fields below.
                Do not send the secret in chat or email.</li></ol>
                <p>The app and store must belong to the same Shopify organization.
                <a href="https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant" target="_blank" rel="noopener noreferrer">Shopify’s own-store connection instructions</a></p></details>'''
                content+='<label>Store hostname<input name="shop" required maxlength="100" autocomplete="off" value="'+e((linked or {}).get('shop') or repository.profile.shop_admin_domain)+'"></label>'
                content+='<label>App Client ID<input name="client_id" required maxlength="1024" autocomplete="off" spellcheck="false"></label>'
                content+='<label>App Client secret<input type="password" name="client_secret" required maxlength="1024" autocomplete="off" spellcheck="false"></label>'
            content+='<button>'+('Reconnect ' if linked else 'Connect ')+label+'</button></form></section>'
        response=page('Connections',content)
        # Browsers apply form-action to the POST's redirect as well. Keep
        # unrelated dashboard forms restricted to self; authorize these exact
        # provider origins only on the connection page.
        response.headers['Content-Security-Policy']=response.headers['Content-Security-Policy'].replace(
            "form-action 'self';","form-action 'self' https://accounts.google.com;")
        return response

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
        if provider=='google':
            state,verifier=await asyncio.to_thread(connections.begin,actor,provider,digest(request))
            url,_=google_flow(state,verifier).authorization_url(access_type='offline',prompt='consent',
                include_granted_scopes='true',login_hint=google.owner_email,nonce=state)
            return RedirectResponse(url,303,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
        try:
            await asyncio.to_thread(shopify.connect,actor,data.get('shop',''),data.get('client_id',''),data.get('client_secret',''))
        except (InvalidInput,ProviderRejected) as error:
            await asyncio.to_thread(connections.failed,actor,'shopify','store_app',error)
            return page('Shopify connection incomplete','<h1>Shopify connection incomplete.</h1><p>'+e(str(error))+
                '</p><p>Confirm the app is released and installed on the store, with the listed read permissions. Existing connections are retained.</p><p><a href="/dashboard/connections">Return to Connections</a></p>',status=400)
        except Exception as error:
            await asyncio.to_thread(connections.failed,actor,'shopify','store_app',error)
            return page('Shopify connection incomplete','<h1>Shopify connection incomplete.</h1><p>The store connection could not be verified. Your earlier connection is retained. Try again from Connections.</p><a href="/dashboard/connections">Connections</a>',status=400)
        return RedirectResponse('/dashboard/connections',303,headers={'Cache-Control':'no-store'})

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

    return router
