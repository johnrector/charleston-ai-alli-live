"""Owner OAuth using the MCP SDK's PKCE/client-auth implementation.

Existing encrypted connector registrations and refresh grants remain readable;
new writes are namespaced. No ChatGPT token is copied into agent instructions.
"""
import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlencode

from cryptography.fernet import Fernet
import jwt
from mcp.server.auth.provider import AccessToken,AuthorizationCode,AuthorizeError,RefreshToken,RegistrationError,TokenError
from mcp.shared.auth import OAuthClientInformationFull,OAuthToken


SCOPE='calls:write'  # retained identifier for John's existing owner-only connector


class OAuthRecords:
    def __init__(self,repository,key):
        self.repository,self.cipher=repository,Fernet(key)

    def put(self,kind,key,data,ttl):
        sealed=self.cipher.encrypt(json.dumps(data).encode()).decode()
        with self.repository.connection() as db:
            db.execute('''INSERT INTO ea_oauth_record(assistant_id,kind,digest,sealed,expires_at)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(assistant_id,kind,digest) DO UPDATE
                SET sealed=EXCLUDED.sealed,expires_at=EXCLUDED.expires_at''',
                (self.repository.profile.id,kind,hashlib.sha256(key.encode()).hexdigest(),sealed,time.time()+ttl))

    def get(self,kind,key,consume=False):
        digest=hashlib.sha256(key.encode()).hexdigest()
        with self.repository.connection() as db:
            args=(self.repository.profile.id,kind,digest,time.time())
            if consume:
                row=db.execute('''DELETE FROM ea_oauth_record WHERE assistant_id=%s AND kind=%s AND digest=%s
                    AND expires_at>%s RETURNING sealed''',args).fetchone()
            else:
                row=db.execute('''SELECT sealed FROM ea_oauth_record WHERE assistant_id=%s AND kind=%s
                    AND digest=%s AND expires_at>%s''',args).fetchone()
            # This compatibility read is solely for the current service's
            # existing connector, not a migration of its old runtime or work.
            if not row and db.execute("SELECT to_regclass('alli_mcp_auth') AS name").fetchone()['name']:
                old_args=(kind,digest,time.time())
                if consume:
                    row=db.execute('''DELETE FROM alli_mcp_auth WHERE kind=%s AND digest=%s
                        AND expires_at>%s RETURNING sealed''',old_args).fetchone()
                else:
                    row=db.execute('''SELECT sealed FROM alli_mcp_auth WHERE kind=%s AND digest=%s
                        AND expires_at>%s''',old_args).fetchone()
        return json.loads(self.cipher.decrypt(row['sealed'].encode())) if row else None


class OwnerOAuth:
    def __init__(self,repository,base_url,secret,encryption_key):
        self.repository=repository
        self.base=base_url.rstrip('/')
        self.resource=self.base+'/mcp'
        self.issuer=self.base+'/'
        self.key=hmac.new(secret.encode(),b'alli-mcp-oauth-access-v1',hashlib.sha256).digest()
        self.records=OAuthRecords(repository,encryption_key)

    async def get(self,kind,key,consume=False):
        return await asyncio.to_thread(self.records.get,kind,key,consume)

    async def put(self,kind,key,data,ttl):
        await asyncio.to_thread(self.records.put,kind,key,data,ttl)

    async def get_client(self,client_id):
        data=await self.get('client',client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self,client_info):
        allowed=r'https://chatgpt\.com/(?:connector/oauth/[A-Za-z0-9_-]+|connector_platform_oauth_redirect)'
        if not client_info.redirect_uris or any(not re.fullmatch(allowed,str(uri)) for uri in client_info.redirect_uris):
            raise RegistrationError('invalid_redirect_uri','Use a supported ChatGPT connector callback')
        if client_info.token_endpoint_auth_method not in {'client_secret_post','client_secret_basic'}:
            raise RegistrationError('invalid_client_metadata','Confidential client authentication is required')
        await self.put('client',client_info.client_id,client_info.model_dump(mode='json'),365*86400)

    async def authorize(self,client,params):
        if params.resource not in {None,self.resource} or set(params.scopes or [SCOPE])!={SCOPE}:
            raise AuthorizeError('invalid_scope','Use the existing owner-only Alli connection')
        params.resource=self.resource
        params.scopes=[SCOPE]
        nonce=secrets.token_urlsafe(32)
        await self.put('pending',nonce,{'client_id':client.client_id,'params':params.model_dump(mode='json')},600)
        return self.base+'/mcp-owner/consent?'+urlencode({'request':nonce})

    async def consent(self,nonce):
        data=await self.get('pending',nonce,True)
        if not data:raise ValueError('Connection request expired')
        params=data['params']
        code=secrets.token_urlsafe(32)
        record=AuthorizationCode(code=code,client_id=data['client_id'],scopes=[SCOPE],
            expires_at=int(time.time())+120,code_challenge=params['code_challenge'],redirect_uri=params['redirect_uri'],
            redirect_uri_provided_explicitly=params['redirect_uri_provided_explicitly'],resource=self.resource,subject='alli-owner')
        await self.put('code',code,record.model_dump(mode='json',exclude={'code'}),120)
        query={'code':code}
        if params.get('state') is not None:query['state']=params['state']
        return str(record.redirect_uri)+'?'+urlencode(query)

    async def load_authorization_code(self,client,authorization_code):
        data=await self.get('code',authorization_code)
        if not data or data['client_id']!=client.client_id:return None
        return AuthorizationCode(code=authorization_code,**data)

    async def exchange_authorization_code(self,client,authorization_code):
        data=await self.get('code',authorization_code.code,True)
        if not data or data['client_id']!=client.client_id:
            raise TokenError('invalid_grant','Authorization code expired or already used')
        return await self.issue(client.client_id)

    async def issue(self,client_id):
        now=int(time.time())
        refresh=secrets.token_urlsafe(48)
        await self.put('refresh',refresh,{'client_id':client_id,'expires_at':now+30*86400},30*86400)
        token=jwt.encode({'iss':self.issuer,'aud':self.resource,'sub':'alli-owner','client_id':client_id,
            'scope':SCOPE,'iat':now,'nbf':now,'exp':now+300,'jti':secrets.token_hex(16)},self.key,algorithm='HS256')
        return OAuthToken(access_token=token,token_type='Bearer',expires_in=300,refresh_token=refresh,scope=SCOPE)

    async def load_refresh_token(self,client,refresh_token):
        data=await self.get('refresh',refresh_token)
        if not data or data['client_id']!=client.client_id:return None
        return RefreshToken(token=refresh_token,client_id=client.client_id,scopes=[SCOPE],
            expires_at=int(data['expires_at']),resource=self.resource,subject='alli-owner')

    async def exchange_refresh_token(self,client,refresh_token,scopes):
        if scopes!=[SCOPE]:raise TokenError('invalid_scope','Unsupported owner scope')
        data=await self.get('refresh',refresh_token.token,True)
        if not data or data['client_id']!=client.client_id:
            raise TokenError('invalid_grant','Refresh token expired or already used')
        return await self.issue(client.client_id)

    async def load_access_token(self,token):
        try:
            if await self.get('revoked_access',token):return None
            claims=jwt.decode(token,self.key,algorithms=['HS256'],audience=self.resource,issuer=self.issuer,
                options={'require':['exp','iat','nbf','sub','client_id','scope','jti']})
            if claims['scope']!=SCOPE or claims['sub']!='alli-owner':return None
            return AccessToken(token=token,client_id=claims['client_id'],subject='alli-owner',scopes=[SCOPE],
                expires_at=claims['exp'],resource=self.resource,claims={'iss':self.issuer,'proof_id':'oauth:'+claims['jti']})
        except (jwt.InvalidTokenError,ValueError,KeyError):return None

    async def revoke_token(self,token):
        if isinstance(token,RefreshToken):await self.get('refresh',token.token,True)
        else:await self.put('revoked_access',token.token,{'revoked':True},600)
        def stop_subscriptions():
            with self.repository.connection() as db:
                db.execute('''UPDATE ea_subscription SET active=false WHERE assistant_id=%s
                    AND principal::jsonb->>0=%s''',(self.repository.profile.id,token.client_id))
        await asyncio.to_thread(stop_subscriptions)
