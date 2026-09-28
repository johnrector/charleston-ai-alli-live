"""Private, single-owner OAuth for ChatGPT. MCP SDK handles PKCE and client auth.

Only the one-time owner consent form accepts DEMO_KEY. ChatGPT receives a
short-lived, audience-bound token with calls:write, never the administrator key.
"""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode

import anyio
import jwt
from fastapi import APIRouter, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from google_store import cipher, connection

SCOPE = 'calls:write'
BASE = 'https://' + os.environ.get('TWILIO_VOICE_PUBLIC_DOMAIN', 'charleston-ai-alli-gpt-live.onrender.com').rstrip('/')
RESOURCE = BASE + '/mcp'
ISSUER = BASE + '/'
router = APIRouter()
SAFE_HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://chatgpt.com/connector/oauth/ https://chatgpt.com/connector_platform_oauth_redirect; frame-ancestors 'none'",
                'X-Content-Type-Options': 'nosniff'}


def signing_key():
    secret = os.environ.get('DEMO_KEY', '')
    if not secret:
        raise RuntimeError('Owner authentication is not configured')
    return hmac.new(secret.encode(), b'alli-mcp-oauth-access-v1', hashlib.sha256).digest()


class Store:
    """Separate table; secret values encrypted, opaque lookup keys hashed."""
    def initialize(self):
        with connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS alli_mcp_auth (kind text NOT NULL, digest text NOT NULL, sealed text NOT NULL, expires_at double precision NOT NULL, PRIMARY KEY(kind,digest))')

    def put(self, kind, key, data, ttl):
        sealed = cipher().encrypt(json.dumps(data).encode()).decode()
        with connection() as db:
            db.execute('DELETE FROM alli_mcp_auth WHERE expires_at<%s', (time.time(),))
            db.execute('INSERT INTO alli_mcp_auth VALUES(%s,%s,%s,%s) ON CONFLICT(kind,digest) DO UPDATE SET sealed=EXCLUDED.sealed,expires_at=EXCLUDED.expires_at',
                       (kind, hashlib.sha256(key.encode()).hexdigest(), sealed, time.time()+ttl))

    def get(self, kind, key, consume=False):
        with connection() as db:
            args = (kind, hashlib.sha256(key.encode()).hexdigest(), time.time())
            if consume:
                row = db.execute('DELETE FROM alli_mcp_auth WHERE kind=%s AND digest=%s AND expires_at>%s RETURNING sealed', args).fetchone()
            else:
                row = db.execute('SELECT sealed FROM alli_mcp_auth WHERE kind=%s AND digest=%s AND expires_at>%s', args).fetchone()
        return json.loads(cipher().decrypt(row[0].encode())) if row else None


store = Store()


async def put(kind, key, data, ttl):
    await anyio.to_thread.run_sync(store.put, kind, key, data, ttl)


async def get(kind, key, consume=False):
    return await anyio.to_thread.run_sync(store.get, kind, key, consume)


class OwnerOAuth:
    async def get_client(self, client_id):
        data = await get('client', client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        # No arbitrary OAuth redirects, localhost callbacks, wildcard hosts, or SSRF.
        allowed = r'https://chatgpt\.com/(?:connector/oauth/[A-Za-z0-9_-]+|connector_platform_oauth_redirect)'
        if not client_info.redirect_uris or any(not re.fullmatch(allowed, str(u)) for u in client_info.redirect_uris):
            raise RegistrationError('invalid_redirect_uri', 'Only ChatGPT OAuth callbacks are supported')
        if client_info.token_endpoint_auth_method not in ('client_secret_post', 'client_secret_basic'):
            raise RegistrationError('invalid_client_metadata', 'Use confidential client authentication')
        await put('client', client_info.client_id, client_info.model_dump(mode='json'), 365*86400)

    async def authorize(self, client, params):
        if params.resource not in (None, RESOURCE) or params.scopes != [SCOPE]:
            raise AuthorizeError('invalid_scope', 'Only calls:write for this MCP resource is allowed')
        params.resource = RESOURCE
        nonce = secrets.token_urlsafe(32)
        await put('pending', nonce, {'client_id': client.client_id, 'params': params.model_dump(mode='json')}, 600)
        return BASE + '/mcp-owner/consent?' + urlencode({'request': nonce})

    async def load_authorization_code(self, client, authorization_code):
        data = await get('code', authorization_code)
        if not data or data['client_id'] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **data)

    async def exchange_authorization_code(self, client, authorization_code):
        # Atomic consume prevents two simultaneous exchanges succeeding.
        data = await get('code', authorization_code.code, True)
        if not data or data['client_id'] != client.client_id:
            raise TokenError('invalid_grant', 'Code expired or already used')
        return await self.issue(client.client_id)

    async def issue(self, client_id):
        now = int(time.time())
        refresh = secrets.token_urlsafe(48)
        await put('refresh', refresh, {'client_id': client_id, 'expires_at': now+30*86400}, 30*86400)
        token = jwt.encode({'iss': ISSUER, 'aud': RESOURCE, 'sub': 'alli-owner', 'client_id': client_id,
                            'scope': SCOPE, 'iat': now, 'nbf': now, 'exp': now+300,
                            'jti': secrets.token_hex(16)}, signing_key(), algorithm='HS256')
        return OAuthToken(access_token=token, token_type='Bearer', expires_in=300, refresh_token=refresh, scope=SCOPE)

    async def load_refresh_token(self, client, refresh_token):
        data = await get('refresh', refresh_token)
        if not data or data['client_id'] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=[SCOPE],
                            expires_at=data['expires_at'], resource=RESOURCE, subject='alli-owner')

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if scopes != [SCOPE]:
            raise TokenError('invalid_scope', 'Only calls:write is allowed')
        data = await get('refresh', refresh_token.token, True)
        if not data or data['client_id'] != client.client_id:
            raise TokenError('invalid_grant', 'Refresh token expired or already used')
        return await self.issue(client.client_id)

    async def load_access_token(self, token):
        # Offline verification keeps database and other services off the dial path.
        try:
            claims = jwt.decode(token, signing_key(), algorithms=['HS256'], audience=RESOURCE,
                                issuer=ISSUER, options={'require': ['exp','iat','nbf','sub','client_id','scope','jti']})
            if claims['scope'] != SCOPE or claims['sub'] != 'alli-owner':
                return None
            return AccessToken(token=token, client_id=claims['client_id'], scopes=[SCOPE],
                               expires_at=claims['exp'], resource=RESOURCE, subject='alli-owner')
        except (jwt.InvalidTokenError, KeyError, ValueError):
            return None

    async def revoke_token(self, token):
        # Access tokens expire in at most five minutes; refresh revocation is immediate.
        if isinstance(token, RefreshToken):
            await get('refresh', token.token, True)


provider = OwnerOAuth()


@router.get('/mcp-owner/consent')
async def consent(request: Request):
    nonce = request.query_params.get('request', '')
    if not await get('pending', nonce):
        return HTMLResponse('Connection request expired. Restart linking in ChatGPT.', status_code=400, headers=SAFE_HEADERS)
    csrf = secrets.token_urlsafe(32)
    page = '''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Alli to ChatGPT</title></head>
<body style="font:18px system-ui;max-width:540px;margin:60px auto;padding:24px"><h1>Connect Alli to ChatGPT</h1>
<p>Allow your private ChatGPT connection to place outbound calls as Alli, using the contact and mission you supply.</p>
<p>This grants <strong>calls:write</strong> only. Calendar and Gmail remain inside your existing phone assistant.</p>
<form method="post"><input type="hidden" name="request" value="%s"><input type="hidden" name="csrf" value="%s">
<label for="owner_key">Demo administrator key</label><br><input id="owner_key" type="password" name="owner_key" required autocomplete="off" style="width:100%%;padding:10px;margin:12px 0">
<p>The key stays on this service. It is never sent to ChatGPT.</p><button type="submit" style="padding:12px">Authorize ChatGPT calling</button></form></body></html>''' % (html.escape(nonce, quote=True), csrf)
    response = HTMLResponse(page, headers=SAFE_HEADERS)
    response.set_cookie('__Host-alli-link', csrf, secure=True, httponly=True, samesite='strict', max_age=600, path='/')
    return response


@router.post('/mcp-owner/consent')
async def approve(request: Request):
    if request.headers.get('origin') != BASE:
        return JSONResponse({'error':'Invalid origin'}, status_code=403, headers=SAFE_HEADERS)
    form = await request.form()
    csrf = str(form.get('csrf',''))
    if not csrf or not hmac.compare_digest(csrf, request.cookies.get('__Host-alli-link','')):
        return JSONResponse({'error':'Invalid session'}, status_code=403, headers=SAFE_HEADERS)
    # Consume on every attempt: failed password cannot reuse this consent request.
    data = await get('pending', str(form.get('request','')), True)
    expected = os.environ.get('DEMO_KEY','')
    if not data or not expected or not hmac.compare_digest(str(form.get('owner_key','')), expected):
        return HTMLResponse('Not authorized. Restart linking in ChatGPT.', status_code=403, headers=SAFE_HEADERS)
    params = AuthorizationParams.model_validate(data['params'])
    code = secrets.token_urlsafe(32)
    auth_code = AuthorizationCode(code=code, client_id=data['client_id'], scopes=[SCOPE],
        expires_at=time.time()+120, code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
        redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=RESOURCE, subject='alli-owner')
    await put('code', code, auth_code.model_dump(mode='json', exclude={'code'}), 120)
    query = {'code':code}
    if params.state is not None:
        query['state'] = params.state
    response = RedirectResponse(str(params.redirect_uri) + '?' + urlencode(query), status_code=303, headers=SAFE_HEADERS)
    response.delete_cookie('__Host-alli-link', secure=True, httponly=True, samesite='strict', path='/')
    return response
