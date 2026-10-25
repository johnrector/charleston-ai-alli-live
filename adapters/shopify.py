"""Shopify account I/O only. OpenAI does the reasoning; no Mac is required."""
from datetime import datetime,timedelta,timezone
import json
import re

import httpx
from graphql import parse,OperationType
from graphql.language.ast import OperationDefinitionNode,FragmentDefinitionNode

from .errors import InvalidInput,ProviderRejected


API_VERSION='2026-10'
CHECK_QUERY='''query AlliStoreCheck {
  shop { name myshopifyDomain primaryDomain { host } }
  orders(first: 1) { nodes { id name } }
  currentAppInstallation { accessScopes { handle } }
}'''
_TYPE='name kind ofType { name kind ofType { name kind ofType { name kind ofType { name kind } } } }'
SCHEMA_QUERY='''query AlliShopifyType($name: String!) { __type(name: $name) {
  name kind fields { name type { TYPE } args { name defaultValue type { TYPE } } }
  inputFields { name defaultValue type { TYPE } } enumValues { name } possibleTypes { name }
} }'''.replace('TYPE',_TYPE)


def shop_domain(value):
    value=value.strip().lower()
    if not re.fullmatch(r'(?:[a-z0-9]|[a-z0-9][a-z0-9-]{0,61}[a-z0-9])\.myshopify\.com',value):
        raise InvalidInput('Use the store’s exact myshopify.com hostname, without https:// or a path')
    return value


def read_query(query,variables):
    if not isinstance(query,str) or not 1<=len(query)<=16000:
        raise InvalidInput('Use one GraphQL read query within 16,000 characters')
    if not isinstance(variables,dict) or len(json.dumps(variables))>16000:
        raise InvalidInput('Use a variables object within 16,000 characters')
    try:document=parse(query,max_tokens=6000)
    except Exception as error:raise InvalidInput('Invalid or excessive GraphQL query') from error
    operations=[node for node in document.definitions if isinstance(node,OperationDefinitionNode)]
    if (len(operations)!=1 or operations[0].operation!=OperationType.QUERY or
        any(not isinstance(node,(OperationDefinitionNode,FragmentDefinitionNode)) for node in document.definitions)):
        raise InvalidInput('Shopify access supports one read query; mutations and subscriptions are unavailable')


class ShopifyAccount:
    def __init__(self,connections):
        self.connections,self.repository=connections,connections.repository

    def configure(self,actor,credentials):
        """Import trusted deployment configuration without exposing a new endpoint.

        This is for an owner-authorized operator using the existing hosting
        administration. Ordinary conversations never receive these credentials.
        Repeated deploys must not replace an already imported matching grant.
        """
        if (not isinstance(credentials,dict) or set(credentials)!={'shop','client_id','client_secret'} or
            any(not isinstance(value,str) for value in credentials.values())):
            raise InvalidInput('Invalid deployment account configuration')
        values={key:value.strip() for key,value in credentials.items()}
        values['shop']=shop_domain(values['shop'])
        with self.repository.connection() as db:
            self.repository._person(db,actor,owner=True)
            row=db.execute('''SELECT sealed FROM ea_account_secret WHERE assistant_id=%s
                AND provider='shopify' ''',(self.repository.profile.id,)).fetchone()
        if row:
            saved=json.loads(self.connections.cipher.decrypt(row['sealed'].encode()))
            if all(saved.get(key)==value for key,value in values.items()):return False
        self.connect(actor,**values)
        return True

    def exchange(self,credentials):
        # The app and store must belong to the same Shopify organization.
        # Shopify grants 24-hour tokens; renew before expiry, never in a prompt.
        shop=shop_domain(credentials['shop'])
        with httpx.Client(timeout=20,follow_redirects=False) as http:
            response=http.post('https://'+shop+'/admin/oauth/access_token',data={
                'grant_type':'client_credentials','client_id':credentials['client_id'],
                'client_secret':credentials['client_secret']})
        if response.status_code!=200:raise ProviderRejected(response.status_code,'app_not_authorized','Shopify')
        grant=response.json()
        if (not isinstance(grant.get('access_token'),str) or not grant['access_token'] or
            not isinstance(grant.get('expires_in'),int) or not 120<grant['expires_in']<=86400):
            raise ProviderRejected(502,'invalid_token_response','Shopify')
        return {**credentials,'access_token':grant['access_token'],
            'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=grant['expires_in'])).isoformat()}

    def request(self,credentials,query,variables):
        read_query(query,variables)
        with httpx.Client(timeout=20,follow_redirects=False) as http:
            # Bounded streaming prevents an accidental huge result being held in memory.
            with http.stream('POST','https://'+shop_domain(credentials['shop'])+
                '/admin/api/'+API_VERSION+'/graphql.json',
                headers={'X-Shopify-Access-Token':credentials['access_token']},
                json={'query':query,'variables':variables}) as response:
                if response.status_code!=200:raise ProviderRejected(response.status_code,'query_rejected','Shopify')
                body=bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body)>2_000_000:raise InvalidInput('Shopify result is too large; narrow the fields or page size')
        result=json.loads(body)
        # Return errors alongside partial data; do not silently report a partial read as complete.
        return {'ok':not bool(result.get('errors')),'provider':'shopify','api_version':API_VERSION,
            'data':result.get('data'),'errors':result.get('errors',[]),
            'extensions':result.get('extensions',{}),'content_is_untrusted_data':True}

    def connect(self,actor,shop,client_id,client_secret):
        with self.repository.connection() as db:self.repository._person(db,actor,owner=True)
        credentials={'shop':shop_domain(shop),'client_id':client_id.strip(),'client_secret':client_secret.strip()}
        if any(not 16<=len(credentials[key])<=1024 or any(c.isspace() for c in credentials[key])
               for key in ('client_id','client_secret')):
            raise InvalidInput('Enter the app’s Client ID and Client secret from its Shopify Settings')
        grant=self.exchange(credentials)
        checked=self.request(grant,CHECK_QUERY,{})
        if not checked['ok']:raise InvalidInput('The app needs read_orders access and installation on this store')
        info=checked['data']['shop']
        expected=self.repository.profile.shop_domain
        if (info['myshopifyDomain'].lower()!=credentials['shop'] or
            expected and info['primaryDomain']['host'].lower()!=expected.lower()):
            raise InvalidInput('The authorized store does not match your configured store')
        scopes=[item['handle'] for item in checked['data']['currentAppInstallation']['accessScopes']]
        # Store details, encrypted credentials and audit event commit together.
        self.connections.save(actor,'shopify',{'store':info['name'],'shop':credentials['shop'],
            'domain':info['primaryDomain']['host'],'scopes':scopes,
            'verified_at':datetime.now(timezone.utc).isoformat()},secret=grant)

    def credentials(self):
        with self.repository.connection() as db:
            # Serialize renewal across requests and service instances.
            row=db.execute('''SELECT sealed FROM ea_account_secret WHERE assistant_id=%s
                AND provider='shopify' FOR UPDATE''',(self.repository.profile.id,)).fetchone()
            if not row:raise InvalidInput('Connect Shopify from your private dashboard’s Connections page first')
            credentials=json.loads(self.connections.cipher.decrypt(row['sealed'].encode()))
            if datetime.fromisoformat(credentials['expires_at'])<=datetime.now(timezone.utc)+timedelta(seconds=60):
                credentials=self.exchange(credentials)
                db.execute('''UPDATE ea_account_secret SET sealed=%s,updated_at=now()
                    WHERE assistant_id=%s AND provider='shopify' ''',
                    (self.connections.cipher.encrypt(json.dumps(credentials).encode()).decode(),self.repository.profile.id))
        return credentials

    def query(self,query,variables=None):
        variables={} if variables is None else variables
        read_query(query,variables) # Reject writes before even requesting a token.
        return self.request(self.credentials(),query,variables)

    def schema(self,type_name='QueryRoot'):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,100}',type_name):raise InvalidInput('Invalid schema type')
        return self.query(SCHEMA_QUERY,{'name':type_name})
