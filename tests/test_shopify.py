from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import json

from cryptography.fernet import Fernet
import httpx
import pytest

from adapters.account_connections import AccountConnections
from adapters.errors import InvalidInput
from adapters.postgres import PostgresRepository
from adapters.shopify import ShopifyAccount,read_query,shop_domain
from alli_core.authority import Actor
from alli_core.profile import Profile


@pytest.fixture
def account(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'));repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'dot',True,'owner-proof')
    links=AccountConnections(repo,Fernet.generate_key(),'https://testserver')
    secret={'shop':'red604.myshopify.com','client_id':'private-app-client-id',
        'client_secret':'private-app-client-secret','access_token':'private-access-token',
        'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
    links.save(owner,'shopify',{'store':'Original store'},secret=secret)
    return repo,owner,links,ShopifyAccount(links),secret


@pytest.mark.parametrize('hostname',['evil.example','red604.myshopify.com.evil.example',
    'https://red604.myshopify.com','red604.myshopify.com:443','evil@red604.myshopify.com',
    'red604.myshopify.com/path','127.0.0.1','-bad.myshopify.com'])
def test_shopify_credentials_cannot_be_sent_to_arbitrary_hosts(hostname):
    with pytest.raises(InvalidInput):shop_domain(hostname)


@pytest.mark.parametrize('query',[
    'mutation { orderCancel(orderId:"1") { job { id } } }',
    'query { shop { name } } mutation Hidden { productDelete(input:{id:"1"}) { deletedProductId } }',
    'subscription { shop { name } }','query A { shop { name } } query B { shop { name } }',
    'fragment Only on Shop { name }','query { shop { name }'])
def test_writes_and_ambiguous_operations_rejected_before_loading_any_credentials(account,monkeypatch,query):
    _,_,_,shopify,_=account
    monkeypatch.setattr(shopify,'credentials',lambda:pytest.fail('Invalid query touched credentials'))
    with pytest.raises(InvalidInput):shopify.query(query)


def test_graphql_read_parser_does_not_reject_words_in_data_or_aliases():
    read_query('query { mutation: shop { name } }',{})
    read_query('query($q:String!) { orders(first:1,query:$q) { nodes { id } } }',{'q':'mutation'})


def test_encrypted_cloud_token_survives_restart_and_renews_once_under_concurrent_use(account,monkeypatch):
    repo,owner,links,shopify,secret=account
    count=[];real=httpx.Client
    def provider(request):
        count.append(str(request.url))
        assert request.url.host=='red604.myshopify.com'
        assert b'grant_type=client_credentials' in request.content
        return httpx.Response(200,json={'access_token':'renewed-private-token','expires_in':86399})
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:real(transport=httpx.MockTransport(provider),**kwargs))
    assert ShopifyAccount(links).credentials()['access_token']=='private-access-token'
    assert not count
    secret['expires_at']=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    links.save(owner,'shopify',{'store':'Original store'},secret=secret)
    with ThreadPoolExecutor(max_workers=2) as executor:
        values=list(executor.map(lambda _:ShopifyAccount(links).credentials(),range(2)))
    assert len(count)==1 and all(v['access_token']=='renewed-private-token' for v in values)
    assert 'renewed-private-token' not in json.dumps(repo.ledger(owner),default=str)


def test_wrong_store_does_not_replace_working_cloud_credentials(account,monkeypatch):
    _,owner,links,shopify,_=account
    real=httpx.Client
    def provider(request):
        if request.url.path=='/admin/oauth/access_token':
            return httpx.Response(200,json={'access_token':'new-token','expires_in':86399})
        return httpx.Response(200,json={'data':{'shop':{'name':'Wrong store',
            'myshopifyDomain':'red604.myshopify.com','primaryDomain':{'host':'wrong.example'}},
            'orders':{'nodes':[]},'currentAppInstallation':{'accessScopes':[]}}})
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:real(transport=httpx.MockTransport(provider),**kwargs))
    with pytest.raises(InvalidInput,match='does not match'):
        shopify.connect(owner,'red604.myshopify.com','new-client-id-123456','new-client-secret-123456')
    assert links.get('shopify')['store']=='Original store'
    assert shopify.credentials()['access_token']=='private-access-token'


def test_partial_shopify_errors_and_pagination_remain_visible_to_the_model(account,monkeypatch):
    _,_,_,shopify,_=account;real=httpx.Client
    def provider(request):
        assert request.headers['X-Shopify-Access-Token']=='private-access-token'
        assert request.url.path=='/admin/api/2026-10/graphql.json'
        return httpx.Response(200,json={'data':{'orders':{'nodes':[],
            'pageInfo':{'hasNextPage':True,'endCursor':'next'}}},
            'errors':[{'message':'Address field needs protected customer data access'}],
            'extensions':{'cost':{'actualQueryCost':2}}})
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:real(transport=httpx.MockTransport(provider),**kwargs))
    result=shopify.query('query { orders(first:1) { nodes { id } pageInfo { hasNextPage endCursor } } }')
    assert not result['ok'] and result['errors'] and result['extensions']['cost']['actualQueryCost']==2
    assert result['data']['orders']['pageInfo']['hasNextPage']


def test_trusted_operator_import_reuses_matching_credentials_without_network_calls(account,monkeypatch):
    _,owner,links,shopify,secret=account
    values={key:secret[key] for key in ('shop','client_id','client_secret')}
    monkeypatch.setattr(shopify,'connect',lambda *args,**kwargs:pytest.fail('Matching setup unexpectedly reconnected'))
    assert shopify.configure(owner,values) is False
    assert links.get('shopify')['store']=='Original store'


def test_operator_import_rejects_friend_and_malformed_configuration(account,monkeypatch):
    from alli_core.authority import PermissionDenied
    repo,owner,_,shopify,secret=account
    person=repo.save_person(owner,name='Friend',phone='+18435550111')
    repo.set_person_status(owner,str(person['id']),'approved',person['revision'])
    friend=Actor('alli',str(person['id']),'sms')
    values={key:secret[key] for key in ('shop','client_id','client_secret')}
    monkeypatch.setattr(shopify,'connect',lambda *args,**kwargs:pytest.fail('Rejected setup reached provider'))
    with pytest.raises(PermissionDenied):shopify.configure(friend,values)
    for invalid in ({'client_secret':'secret'},'not an object',{**values,'access_token':'injected'}):
        with pytest.raises(InvalidInput):shopify.configure(owner,invalid)
