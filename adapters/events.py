"""Authenticated MCP Events and persistent, signed callback delivery."""
import asyncio
from datetime import datetime,timedelta,timezone
import hashlib
import hmac
import json
import secrets

from cryptography.fernet import Fernet
from mcp import types
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.extension import Extension,MethodBinding
from mcp.shared.exceptions import MCPError
from pydantic import Field,ValidationError
from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb

from .webhooks import CallbackError,PublicWebhook,signing_key


NAME='alli.work.updated'
STATES={'completed','partial','failed','waiting','needs_owner'}
KINDS={'task.completed','task.partial','task.failed','task.waiting','task.approval_required'}
FILTER_SCHEMA={'type':'object','properties':{'task_id':{'type':'string','maxLength':128},
    'states':{'type':'array','items':{'type':'string','enum':sorted(STATES)},'maxItems':5}},'additionalProperties':False}


class Subscribe(types.RequestParams):
    name:str
    arguments:dict=Field(default_factory=dict)
    delivery:dict
    cursor:str|None=None
    ttlMs:int|None=86400000


class Unsubscribe(types.RequestParams):
    name:str
    arguments:dict=Field(default_factory=dict)
    delivery:dict


class Events(Extension):
    identifier='com.openai/events'

    def __init__(self,repository,key,base_url,*,webhook=None):
        self.repository,self.cipher,self.base=repository,Fernet(key),base_url
        self.webhook=webhook or PublicWebhook()
        self.stopping=asyncio.Event()

    def methods(self):
        return [MethodBinding('events/list',types.RequestParams,self.list, frozenset({'2026-07-28'})),
                MethodBinding('events/subscribe',Subscribe,self.subscribe,frozenset({'2026-07-28'})),
                MethodBinding('events/unsubscribe',Unsubscribe,self.unsubscribe,frozenset({'2026-07-28'}))]

    async def advertise(self,ctx,call_next):
        try:result=await call_next(ctx)
        except ValidationError as error:
            if not ctx.method.startswith('events/'):raise
            raise MCPError(-32602,'Invalid event parameters',error.errors(include_input=False,include_url=False)) from error
        if ctx.method=='server/discover' and ctx.protocol_version=='2026-07-28':
            result=dict(result)
            result['capabilities']={**result.get('capabilities',{}),'events':{}}
        return result

    def principal(self):
        token=get_access_token()
        if not token or token.subject!='alli-owner' or not token.claims:
            raise MCPError(-32001,'Owner authentication is required')
        self.repository.owner_actor(token.claims['proof_id'])
        return json.dumps([token.client_id,token.claims['iss'],token.subject],separators=(',',':'))

    async def list(self,ctx,params):
        await asyncio.to_thread(self.principal)
        return {'events':[{'name':NAME,'description':'Actual saved executive-work status changed. Read the authenticated work record for details.',
            'delivery':['webhook'],'inputSchema':FILTER_SCHEMA,
            'payloadSchema':{'type':'object','properties':{'activity_id':{'type':'integer'},'task_id':{'type':'string'},
                'state':{'type':'string'},'url':{'type':'string'}},'required':['activity_id','task_id','state','url'],'additionalProperties':False}}]}

    async def subscribe(self,ctx,params):
        principal=await asyncio.to_thread(self.principal)
        if params.name!=NAME or params.cursor is not None:
            raise MCPError(-32602,'Unknown event or unsupported replay cursor')
        filters=params.arguments
        if not Draft202012Validator(FILTER_SCHEMA).is_valid(filters):
            raise MCPError(-32602,'Invalid event filters')
        filters={**filters,'states':sorted(set(filters.get('states',STATES)))}
        ttl=86400000 if params.ttlMs is None else params.ttlMs
        if ttl<1:raise MCPError(-32602,'TTL must be positive')
        ttl=max(60000,min(ttl,604800000))
        destination=params.delivery
        if destination.get('mode')!='webhook':raise MCPError(-32602,'Use webhook delivery')
        url,secret=destination.get('url'),destination.get('secret')
        identity=json.dumps([principal,url,params.name,filters],sort_keys=True,separators=(',',':'))
        identifier=hashlib.sha256(identity.encode()).hexdigest()
        try:
            signing_key(secret)
            if not await asyncio.to_thread(self.verified,principal,url,secret):
                challenge=secrets.token_urlsafe(32)
                response=await asyncio.to_thread(self.webhook.send,url,identifier,[secret],secrets.token_hex(16),
                    {'type':'verification','challenge':challenge})
                if not 200<=response['status']<300:raise CallbackError('http_error')
                echo=(response.get('body') or {}).get('challenge')
                if not isinstance(echo,str) or not hmac.compare_digest(echo,challenge):raise CallbackError('challenge_mismatch')
        except CallbackError as error:
            raise MCPError(-32015,'CallbackEndpointError',{'reason':error.reason}) from error
        expires=datetime.now(timezone.utc)+timedelta(milliseconds=ttl)
        await asyncio.to_thread(self.save,identifier,principal,filters,url,secret,expires)
        return {'id':identifier,'refreshBefore':expires.isoformat(),'cursor':None,'truncated':False}

    def verified(self,principal,url,secret):
        with self.repository.connection() as db:
            rows=db.execute('SELECT sealed FROM ea_subscription WHERE assistant_id=%s AND principal=%s AND active AND expires_at>now() LIMIT 100',
                (self.repository.profile.id,principal)).fetchall()
        for row in rows:
            data=json.loads(self.cipher.decrypt(row['sealed'].encode()))
            if (data['url']==url and hmac.compare_digest(data['secret'],secret)
                and data.get('verified_at',0)>datetime.now(timezone.utc).timestamp()-300):return True
        return False

    def save(self,identifier,principal,filters,url,secret,expires):
        with self.repository.connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(%s)',(int(identifier[:15],16),))
            prior=db.execute('SELECT * FROM ea_subscription WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
            config={'url':url,'secret':secret,'verified_at':datetime.now(timezone.utc).timestamp()}
            if prior:
                old=json.loads(self.cipher.decrypt(prior['sealed'].encode()))
                if old['secret']==secret:config['verified_at']=old.get('verified_at',config['verified_at'])
                if old['secret']!=secret:
                    config.update(old_secret=old['secret'],rotate_until=(datetime.now(timezone.utc)+timedelta(minutes=2)).timestamp())
            cursor=db.execute('SELECT coalesce(max(id),0) AS id FROM ea_ledger WHERE assistant_id=%s',
                              (self.repository.profile.id,)).fetchone()['id']
            db.execute('''INSERT INTO ea_subscription(id,assistant_id,principal,sealed,filters,expires_at,cursor)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET sealed=EXCLUDED.sealed,
                cursor=CASE WHEN ea_subscription.expires_at<now() OR NOT ea_subscription.active THEN EXCLUDED.cursor ELSE ea_subscription.cursor END,
                expires_at=EXCLUDED.expires_at,active=true''',
                (identifier,self.repository.profile.id,principal,self.cipher.encrypt(json.dumps(config).encode()).decode(),Jsonb(filters),expires,cursor))

    async def unsubscribe(self,ctx,params):
        principal=await asyncio.to_thread(self.principal)
        if not Draft202012Validator(FILTER_SCHEMA).is_valid(params.arguments):raise MCPError(-32602,'Invalid event filters')
        filters={**params.arguments,'states':sorted(set(params.arguments.get('states',STATES)))}
        identity=json.dumps([principal,params.delivery.get('url'),params.name,filters],sort_keys=True,separators=(',',':'))
        identifier=hashlib.sha256(identity.encode()).hexdigest()
        def remove():
            with self.repository.connection() as db:
                db.execute('UPDATE ea_subscription SET active=false WHERE id=%s AND principal=%s AND assistant_id=%s',
                    (identifier,principal,self.repository.profile.id))
        await asyncio.to_thread(remove)
        return {}

    def claim(self):
        with self.repository.connection() as db:
            subscriptions=db.execute('''SELECT * FROM ea_subscription WHERE assistant_id=%s AND active
                AND expires_at>now() ORDER BY id FOR UPDATE''',(self.repository.profile.id,)).fetchall()
            for sub in subscriptions:
                rows=db.execute('''SELECT * FROM ea_ledger WHERE assistant_id=%s AND id>%s ORDER BY id LIMIT 200''',
                    (self.repository.profile.id,sub['cursor'])).fetchall()
                for row in rows:
                    state='needs_owner' if row['kind']=='task.approval_required' else row['kind'].removeprefix('task.')
                    if (row['facts'].get('conversation_reply') or row['kind'] not in KINDS or state not in sub['filters']['states']
                        or sub['filters'].get('task_id',row['task_id'])!=row['task_id']):continue
                    event_id='ea_'+hashlib.sha256((self.repository.profile.id+':'+str(row['id'])).encode()).hexdigest()
                    payload={'eventId':event_id,'name':NAME,'timestamp':row['created_at'].isoformat(),'cursor':None,
                        'data':{'activity_id':row['id'],'task_id':row['task_id'],'state':state,
                                'url':self.base+'/dashboard/work/'+row['task_id']}}
                    db.execute('''INSERT INTO ea_event_delivery(subscription_id,ledger_id,event_id,payload)
                        VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(sub['id'],row['id'],event_id,Jsonb(payload)))
                if rows:db.execute('UPDATE ea_subscription SET cursor=%s WHERE id=%s',(rows[-1]['id'],sub['id']))
            # A lease permits callback retries after process loss; event IDs stay stable.
            row=db.execute('''SELECT d.*,s.sealed FROM ea_event_delivery d JOIN ea_subscription s ON s.id=d.subscription_id
                WHERE s.assistant_id=%s AND s.active AND s.expires_at>now() AND d.due_at<=now()
                AND d.state IN ('queued','attempting') ORDER BY d.due_at LIMIT 1 FOR UPDATE OF d SKIP LOCKED''',
                (self.repository.profile.id,)).fetchone()
            if not row:return None
            db.execute("UPDATE ea_event_delivery SET state='attempting',attempts=attempts+1,due_at=now()+interval '60 seconds' WHERE subscription_id=%s AND ledger_id=%s",
                (row['subscription_id'],row['ledger_id']))
            row['attempts']+=1
            return row

    async def tick(self):
        row=await asyncio.to_thread(self.claim)
        if not row:return False
        config=json.loads(self.cipher.decrypt(row['sealed'].encode()))
        keys=[config['secret']]
        if config.get('rotate_until',0)>datetime.now(timezone.utc).timestamp():keys.append(config['old_secret'])
        try:
            response=await asyncio.to_thread(self.webhook.send,config['url'],row['subscription_id'],keys,row['event_id'],row['payload'])
            code=response['status']
        except CallbackError:code=0
        state='delivered' if 200<=code<300 else 'failed' if code in {410,413} or row['attempts']>=8 else 'queued'
        def update():
            with self.repository.connection() as db:
                db.execute('''UPDATE ea_event_delivery SET state=%s,due_at=now()+%s*interval '1 second'
                    WHERE subscription_id=%s AND ledger_id=%s''',
                    (state,min(3600,2**row['attempts']*5),row['subscription_id'],row['ledger_id']))
                if code==410:db.execute('UPDATE ea_subscription SET active=false WHERE id=%s',(row['subscription_id'],))
        await asyncio.to_thread(update)
        return True

    async def run(self):
        while not self.stopping.is_set():
            try:await self.tick()
            except Exception as error:
                import logging
                logging.getLogger(__name__).warning('Event delivery supervisor error=%s',type(error).__name__)
            try:await asyncio.wait_for(self.stopping.wait(),timeout=2)
            except asyncio.TimeoutError:pass
