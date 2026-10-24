"""Owner-authorized cloud connections. Credentials never enter model context."""
import base64
from datetime import datetime,timedelta,timezone
import hashlib
import secrets
from urllib.parse import urlencode

from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb


SHOPIFY_MCP='https://setup.shopify.com/mcp'
SHOPIFY_AUTHORIZE='https://setup.shopify.com/oauth/authorize'
SHOPIFY_TOKEN='https://setup.shopify.com/oauth/token'
SHOPIFY_READ_TOOLS=('get-shop-info','list-orders','get-order','list-customers',
    'search_products','get-product','get-inventory-levels','search_collections',
    'get-collection','graphql_schema','graphql_query','validate_graphql_codeblocks','search_docs_chunks')
GOOGLE_SCOPES=('openid','https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/calendar.freebusy',
    'https://www.googleapis.com/auth/calendar.events.owned',
    'https://www.googleapis.com/auth/gmail.send','https://www.googleapis.com/auth/gmail.readonly')


class AccountConnections:
    def __init__(self,repository,key,base_url):
        self.repository,self.cipher,self.base_url=repository,Fernet(key),base_url.rstrip('/')

    @property
    def shopify_client_id(self):return self.base_url+'/connections/shopify/client.json'

    def client_metadata(self):
        return {'client_id':self.shopify_client_id,
            'client_name':self.repository.profile.name+' — personal executive assistant',
            'client_uri':self.base_url,'redirect_uris':[self.base_url+'/connections/shopify/callback'],
            'grant_types':['authorization_code','refresh_token'],'response_types':['code'],
            'token_endpoint_auth_method':'none','scope':'read_all_orders'}

    def get(self,provider):
        with self.repository.connection() as db:
            row=db.execute('SELECT details FROM ea_account_connection WHERE assistant_id=%s AND provider=%s',
                (self.repository.profile.id,provider)).fetchone()
        return row['details'] if row else None

    def failed(self,actor,provider,stage,error):
        with self.repository.connection() as db:
            self.repository._event(db,'account-failure:'+secrets.token_hex(12),'account.connection_failed',
                {'provider':provider,'stage':stage,'error_type':type(error).__name__,
                 'http_status':getattr(getattr(error,'response',None),'status_code',None)},
                person_id=actor.person_id)

    def save(self,actor,provider,details,*,google_refresh=None):
        if provider not in {'google','shopify'}:raise ValueError('Unknown provider')
        # Only explicitly safe connection metadata is persisted in this table.
        allowed={'account','scopes','vault_id','credential_id','server_url','store','domain','verified_at'}
        if set(details)-allowed:raise ValueError('Unsupported connection metadata')
        with self.repository.connection() as db:
            self.repository._person(db,actor,owner=True)
            if google_refresh is not None:
                if provider!='google' or not google_refresh:raise ValueError('Invalid Google grant')
                encrypted=self.cipher.encrypt(google_refresh.encode()).decode()
                db.execute('''INSERT INTO alli_google_token(id,encrypted_token,updated_at) VALUES(1,%s,now())
                    ON CONFLICT(id) DO UPDATE SET encrypted_token=EXCLUDED.encrypted_token,updated_at=now()''',(encrypted,))
            db.execute('''INSERT INTO ea_account_connection(assistant_id,provider,details) VALUES(%s,%s,%s)
                ON CONFLICT(assistant_id,provider) DO UPDATE SET details=EXCLUDED.details,updated_at=now()''',
                (self.repository.profile.id,provider,Jsonb(details)))
            self.repository._event(db,'account:'+provider+':'+secrets.token_hex(12),'account.connected',
                {'provider':provider,'account':details.get('account') or details.get('store'),
                 'scopes':details.get('scopes',[])},person_id=actor.person_id)

    def begin(self,actor,provider,session_digest):
        if provider not in {'google','shopify'}:raise ValueError('Unknown provider')
        state,verifier=secrets.token_urlsafe(32),secrets.token_urlsafe(64)
        with self.repository.connection() as db:
            self.repository._person(db,actor,owner=True)
            db.execute('DELETE FROM ea_account_oauth_flow WHERE expires_at<now()')
            db.execute('''INSERT INTO ea_account_oauth_flow
                (digest,assistant_id,provider,owner_session_digest,verifier_sealed,expires_at)
                VALUES(%s,%s,%s,%s,%s,%s)''',(hashlib.sha256(state.encode()).hexdigest(),
                self.repository.profile.id,provider,session_digest,self.cipher.encrypt(verifier.encode()).decode(),
                datetime.now(timezone.utc)+timedelta(minutes=10)))
        return state,verifier

    def consume(self,provider,state,session_digest):
        with self.repository.connection() as db:
            row=db.execute('''DELETE FROM ea_account_oauth_flow WHERE digest=%s AND assistant_id=%s
                AND provider=%s AND owner_session_digest=%s AND expires_at>now() RETURNING verifier_sealed''',
                (hashlib.sha256(state.encode()).hexdigest(),self.repository.profile.id,provider,session_digest)).fetchone()
        if not row:raise ValueError('This connection request expired or was already used. Start again from Connections.')
        return self.cipher.decrypt(row['verifier_sealed'].encode()).decode()

    def shopify_authorization(self,state,verifier):
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        return SHOPIFY_AUTHORIZE+'?'+urlencode({'client_id':self.shopify_client_id,
            'redirect_uri':self.base_url+'/connections/shopify/callback','response_type':'code',
            'scope':'read_all_orders','resource':SHOPIFY_MCP,'state':state,
            'code_challenge':challenge,'code_challenge_method':'S256'})

    def for_task(self,task_id):
        # A friend's request, or a limited calling mission, never gets this vault.
        with self.repository.connection() as db:
            task=self.repository._task(db,task_id)
            if (not task or task['scope']!='owner' or task['authority'].get('allowed_tools') is not None
                    or not self.repository._permitted(db,task_id,writes=False)):
                return []
        details=self.get('shopify')
        if not details:return []
        return [{'vault_id':details['vault_id'],'tool':{
            'type':'mcp','server_label':'shopify','transport':{'type':'http','server_url':SHOPIFY_MCP},
            'credential_id':details['credential_id'],'allowed_tools':list(SHOPIFY_READ_TOOLS),
            'required':False,'connection_origin':'service'}}]
