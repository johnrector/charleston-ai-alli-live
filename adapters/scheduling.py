"""OpenAI owns the clock. Each native invocation gets fresh bounded work authority.

The native scheduled session has only a launch capability, not a long-lived
owner/account credential. It delegates the occurrence to the ordinary managed
Agent with a fresh task grant. The application never calculates due occurrences.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import secrets
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb
from pydantic_core import to_jsonable_python

from alli_core.authority import Actor, Conflict, PermissionDenied,friend_tools
from .errors import InvalidInput


class NativeAutomations:
    """Documented REST API; the installed SDK predates this resource wrapper."""
    def __init__(self,api):
        self.api=api.with_options(max_retries=0)

    async def request(self,method,path='',body=None):
        options={'headers':{'OpenAI-Beta':'agents=v1'}}
        arguments={'cast_to':dict,'options':options}
        if body is not None:arguments['body']=body
        return await getattr(self.api,method)('/agents/automations'+path,**arguments)

    async def pages(self,path=''):
        after=None
        while True:
            result=await self.request('get',path+('?' if '?' not in path else '&')+
                'limit=100'+('&after='+quote(after) if after else ''))
            for item in result.get('data',[]):yield item
            if not result.get('has_more'):break
            after=result.get('last_id')
            if not after:raise ValueError('Native automation pagination has no cursor')


def timing(run_at,expression,zone):
    try:ZoneInfo(zone)
    except (KeyError,ValueError) as error:raise InvalidInput('Supply an IANA time zone') from error
    if bool(run_at)==bool(expression):raise ValueError('Supply one future time OR a recurring cron expression')
    if expression:
        if len(expression.split())!=5:raise ValueError('Use a five-field cron expression')
        return {'type':'periodic','expression':expression,'timezone':zone}
    value=datetime.fromisoformat(run_at.replace('Z','+00:00'))
    if value.tzinfo is None or value.utcoffset() is None:raise ValueError('A one-time schedule needs an explicit UTC offset')
    seconds=int(value.timestamp())
    if not 10<=seconds-datetime.now(timezone.utc).timestamp()<=366*86400:
        raise ValueError('Choose a future time within the next year')
    return {'type':'once','run_at':seconds}


class Scheduler:
    def __init__(self,repository,api,key,base_url):
        self.repository,self.api=repository,api
        self.native=NativeAutomations(api)
        self.cipher=Fernet(key.encode() if isinstance(key,str) else key)
        self.base=base_url.rstrip('/')

    @staticmethod
    def public(row):
        return to_jsonable_python({k:row[k] for k in ('id','title','instruction','timing','state','provider_id','revision','control_state','created_at','updated_at')})

    def get(self,actor,identifier):
        with self.repository.connection() as db:
            person=self.repository._person(db,actor)
            owner=person['kind']=='owner' and actor.verified_owner and bool(actor.proof_id)
            row=db.execute('''SELECT * FROM ea_schedule WHERE assistant_id=%s AND id=%s
                AND (%s OR person_id=%s AND NOT authority @> '{"verified_owner":true}'::jsonb)''',
                (self.repository.profile.id,identifier,owner,actor.person_id)).fetchone()
            if not row:raise PermissionDenied('Unknown schedule')
            return row

    def by_source(self,actor,source_id):
        with self.repository.connection() as db:
            self.repository._person(db,actor,owner=True)
            return db.execute('SELECT * FROM ea_schedule WHERE assistant_id=%s AND source_id=%s',
                (self.repository.profile.id,source_id)).fetchone()

    def list(self,actor):
        with self.repository.connection() as db:
            person=self.repository._person(db,actor)
            owner=person['kind']=='owner' and actor.verified_owner and bool(actor.proof_id)
            rows=db.execute('''SELECT * FROM ea_schedule WHERE assistant_id=%s
                AND (%s OR person_id=%s AND NOT authority @> '{"verified_owner":true}'::jsonb)
                ORDER BY CASE WHEN state IN ('creating','active') THEN 0 WHEN state='paused' THEN 1 ELSE 2 END,
                    CASE WHEN state IN ('creating','active','paused') AND timing->>'type'='once'
                        THEN (timing->>'run_at')::bigint END ASC NULLS LAST,created_at DESC LIMIT 200''',
                (self.repository.profile.id,owner,actor.person_id)).fetchall()
            runs=db.execute('''SELECT * FROM (SELECT r.schedule_id,r.invocation_id,r.task_id,r.scheduled_at,
                t.state,t.result,row_number() OVER(PARTITION BY r.schedule_id ORDER BY r.created_at DESC) AS n
                FROM ea_schedule_run r JOIN ea_schedule s ON s.id=r.schedule_id JOIN ea_task t ON t.id=r.task_id
                WHERE s.assistant_id=%s) latest WHERE n<=3''',(self.repository.profile.id,)).fetchall()
            return [{**self.public(r),'recent_runs':[{k:v for k,v in run.items() if k not in {'schedule_id','n'}}
                for run in runs if run['schedule_id']==r['id']]} for r in rows]

    def prepare(self,actor,task_id,source_id,title,instruction,when):
        if not title.strip() or len(title)>180 or not instruction.strip() or len(instruction)>20000:
            raise InvalidInput('Supply a concise title and bounded work instruction')
        token=secrets.token_urlsafe(32)
        with self.repository.connection() as db:
            person=self.repository._person(db,actor)
            db.execute('SELECT id FROM ea_person WHERE id=%s FOR UPDATE',(actor.person_id,))
            task=self.repository._task(db,task_id,lock=True)
            if not task or str(task['person_id'])!=actor.person_id or not self.repository._permitted(db,task_id,writes=False):
                raise PermissionDenied('The source assignment is unavailable')
            owner=person['kind']=='owner' and actor.verified_owner and bool(actor.proof_id)
            if task['scope']=='owner' and not owner:raise PermissionDenied('Owner authority is required')
            prior=db.execute('SELECT * FROM ea_schedule WHERE assistant_id=%s AND source_id=%s',
                (self.repository.profile.id,source_id)).fetchone()
            if prior:
                if (prior['title'],prior['instruction'],prior['timing'])!=(title,instruction,when):
                    raise Conflict('This schedule intent already has different contents')
                return prior,False
            if not owner:
                if actor.channel=='scheduled':raise PermissionDenied('Standing friend work cannot create further schedules')
                if when['type']!='once':raise PermissionDenied('Only John can authorize recurring schedules')
                counts=db.execute('''SELECT count(*) FILTER(WHERE state IN ('creating','active','paused')
                    AND NOT EXISTS(SELECT 1 FROM ea_schedule_run r WHERE r.schedule_id=s.id)) AS outstanding,
                    count(*) FILTER(WHERE created_at>now()-interval '24 hours') AS recent
                    FROM ea_schedule s WHERE assistant_id=%s AND person_id=%s''',
                    (self.repository.profile.id,actor.person_id)).fetchone()
                if counts['outstanding']>=self.repository.friend_active or counts['recent']>=self.repository.friend_daily:
                    raise PermissionDenied('The contact’s scheduled-work limit has been reached')
            row=db.execute('''INSERT INTO ea_schedule(id,assistant_id,person_id,source_task_id,source_id,
                title,instruction,timing,authority,launch_digest,launch_sealed)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                (uuid4().hex,self.repository.profile.id,actor.person_id,task_id,source_id,title,instruction,
                 Jsonb(when),Jsonb(task['authority']),hashlib.sha256(token.encode()).hexdigest(),
                 self.cipher.encrypt(token.encode()).decode())).fetchone()
            self.repository._event(db,'schedule:'+row['id']+':intent','schedule.creating',
                {'schedule_id':row['id'],'title':title,'timing':when},person_id=actor.person_id,task_id=task_id)
            return row,True

    def target(self,row):
        token=self.cipher.decrypt(row['launch_sealed'].encode()).decode()
        profile=self.repository.profile
        snapshot={'type':'alli_scheduled_assignment','schedule_id':row['id'],'revision':row['revision'],
            'title':row['title'],'instruction':row['instruction']}
        return {'type':'new_session','environment':{'type':'none'},
            'agent':{'model':profile.work_model,'reasoning':{'effort':profile.reasoning_effort},
                'instructions':"You coordinate one native scheduled invocation for Alli. Call dispatch_scheduled_work once. "
                "It validates the native invocation and saves a fresh authorized assignment. Return that receipt only. "
                "Do not claim the requested call, text or email has completed. Do not contact anyone yourself.",
                'tools':[{'type':'mcp','server_label':'scheduled_launch','connection_origin':'service','required':True,
                    'allowed_tools':['dispatch_scheduled_work'],
                    'transport':{'type':'http','server_url':self.base+'/executive-tools','authorization':'Bearer '+token}}]},
            'input':[{'role':'user','content':[{'type':'input_text','text':json.dumps(snapshot)}]}]}

    def bind(self,identifier,provider):
        with self.repository.connection() as db:
            row=db.execute('''UPDATE ea_schedule SET provider_id=%s,state=%s,updated_at=now()
                WHERE assistant_id=%s AND id=%s RETURNING *''',
                (provider['id'],provider['status'],self.repository.profile.id,identifier)).fetchone()
            self.repository._event(db,'schedule:'+identifier+':saved:'+str(row['revision']),'schedule.saved',
                {'schedule_id':identifier,'provider_id':provider['id'],'state':row['state'],
                 'timing':row['timing'],'revision':row['revision']},
                person_id=str(row['person_id']),task_id=row['source_task_id'])
            result=self.public(row)
        source=self.repository.task_result(row['source_task_id'])
        if source['context'].get('native_schedule_setup'):
            self.repository.record_outcome(source['id'],'completed','Native schedule saved: '+identifier,
                turn_id='schedule-saved',notify=False)
        return result

    async def create(self,actor,task_id,source_id,title,instruction,run_at=None,expression=None,timezone_name=None):
        try:when=timing(run_at,expression,timezone_name or self.repository.profile.timezone)
        except ValueError as error:raise InvalidInput(str(error)) from error
        row,fresh=await asyncio.to_thread(self.prepare,actor,task_id,source_id,title,instruction,when)
        if row['provider_id']:return {'ok':True,'schedule':self.public(row),'scheduled':True}
        if not fresh:
            # A lost create reply is recovered by identity, never blindly retried.
            async for found in self.native.pages():
                if found.get('name')==self.name(row):
                    return {'ok':True,'schedule':await asyncio.to_thread(self.bind,row['id'],found),'scheduled':True}
            return {'ok':False,'uncertain':True,'schedule_id':row['id'],
                'error':'The native schedule create is unconfirmed. Do not create a replacement.'}
        try:
            provider=await self.native.request('post',body={**when,'name':self.name(row),'target':self.target(row)})
        except Exception as error:
            status=getattr(error,'status_code',None)
            if status and 400<=status<500 and status not in {408,409,429}:
                await asyncio.to_thread(self.set_state,row['id'],'rejected')
                return {'ok':False,'not_attempted':True,'error':'OpenAI rejected the schedule ('+str(status)+': '+str(getattr(error,'code',None) or 'invalid_request')+').'}
            return {'ok':False,'uncertain':True,'schedule_id':row['id'],'error':'Native schedule creation is unconfirmed; inspect before retrying.'}
        return {'ok':True,'schedule':await asyncio.to_thread(self.bind,row['id'],provider),'scheduled':True,
            'note':'OpenAI saved the schedule. The future work is not completed yet.'}

    @staticmethod
    def name(row):return row['title']+' [alli:'+row['id']+']'

    def set_state(self,identifier,state):
        with self.repository.connection() as db:
            db.execute('UPDATE ea_schedule SET state=%s,updated_at=now() WHERE assistant_id=%s AND id=%s',
                (state,self.repository.profile.id,identifier))

    def launch_grant(self,token):
        with self.repository.connection() as db:
            row=db.execute('''SELECT s.id,s.person_id FROM ea_schedule s JOIN ea_person p ON p.id=s.person_id
                AND p.assistant_id=s.assistant_id WHERE s.assistant_id=%s AND s.launch_digest=%s
                AND s.state IN ('active','completed') AND p.status='approved' ''',
                (self.repository.profile.id,hashlib.sha256(token.encode()).hexdigest())).fetchone()
            return {'schedule_id':row['id']} if row else None

    async def dispatch(self,identifier):
        # The launch token can materialize only actual provider invocations of
        # this schedule. It cannot contact anyone or invent another occurrence.
        row=await asyncio.to_thread(self.raw_schedule,identifier)
        if not row or row['state'] not in {'active','completed'}:raise PermissionDenied('Schedule is not active')
        launched=[]
        async for invocation in self.native.pages('/'+quote(row['provider_id'])+'/invocations'):
            if invocation.get('automation_id',row['provider_id'])!=row['provider_id']:
                raise PermissionDenied('Invocation belongs to another native schedule')
            if (invocation['status']!='completed' or not invocation.get('session_id')
                    or invocation.get('source',{}).get('type')!='automation'):continue
            prior=await asyncio.to_thread(self.run,invocation['id'])
            if prior:
                launched.append(prior);continue
            # The provider-retained initial input is the versioned snapshot for
            # this invocation; an edit never rewrites already queued work.
            snapshot=None
            messages=((invocation.get('source',{}).get('automation') or {}).get('target') or {}).get('input',[])
            if not messages:
                page=await self.api.beta.agents.sessions.items.list(invocation['session_id'],order='asc',limit=20)
                messages=[item.model_dump() for item in page.data]
            for item in messages:
                if item.get('role')=='user':
                    for content in item.get('content',[]):
                        if content.get('type')=='input_text':
                            try:
                                candidate=json.loads(content['text'])
                                if candidate.get('type')=='alli_scheduled_assignment':snapshot=candidate;break
                            except (ValueError,AttributeError):pass
                if snapshot:break
            if not snapshot or snapshot.get('schedule_id')!=identifier:raise PermissionDenied('Native invocation snapshot does not match')
            launched.append(await asyncio.to_thread(self.materialize,row,invocation,snapshot))
        return {'ok':True,'runs':launched,'note':'Each run is delegated once; inspect its work and provider receipts for completion.'}

    def raw_schedule(self,identifier):
        with self.repository.connection() as db:
            return db.execute('SELECT * FROM ea_schedule WHERE assistant_id=%s AND id=%s',
                (self.repository.profile.id,identifier)).fetchone()

    def run(self,invocation_id):
        with self.repository.connection() as db:
            return db.execute('SELECT invocation_id,task_id FROM ea_schedule_run WHERE invocation_id=%s',
                (invocation_id,)).fetchone()

    def materialize(self,row,invocation,snapshot,*,failed=False):
        owner=bool(row['authority'].get('verified_owner'))
        allowed=tuple(row['authority']['allowed_tools']) if row['authority'].get('allowed_tools') is not None else None
        if not owner:
            allowed=tuple(t for t in friend_tools(scheduled=True) if allowed is None or t in allowed)
        actor=Actor(self.repository.profile.id,str(row['person_id']),'scheduled',owner,
            'native-schedule:'+row['id']+':'+invocation['id'] if owner else row['authority'].get('proof_id',''),
            allowed,
            row['authority'].get('invitation_email',''))
        with self.repository.connection() as db:
            db.execute('SELECT id FROM ea_person WHERE id=%s FOR UPDATE',(row['person_id'],))
            current=db.execute('SELECT state FROM ea_schedule WHERE id=%s AND assistant_id=%s FOR UPDATE',
                (row['id'],self.repository.profile.id)).fetchone()
            if not current or current['state'] not in {'active','completed'}:raise PermissionDenied('Schedule was stopped')
            task=self.repository.submit(actor,source_id='native-invocation:'+invocation['id'],reservation_id=row['id'],transaction=db,
                title=snapshot['title'],instruction=snapshot['instruction'],
                context={'native_schedule_id':row['id'],'native_invocation_id':invocation['id'],
                         'schedule_revision':snapshot['revision'],'scheduled_at':invocation.get('scheduled_at'),
                         **({'native_schedule_setup':True} if failed else {})})
            scheduled=invocation.get('scheduled_at')
            db.execute('''INSERT INTO ea_schedule_run(invocation_id,schedule_id,native_session_id,task_id,scheduled_at)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(invocation_id) DO NOTHING''',
                (invocation['id'],row['id'],invocation['session_id'],task['id'],
                 datetime.fromtimestamp(scheduled,timezone.utc) if scheduled else None))
            self.repository._event(db,'schedule-run:'+invocation['id'],'schedule.run_started',
                {'schedule_id':row['id'],'provider_id':row['provider_id'],'invocation_id':invocation['id'],
                 'task_id':task['id'],'revision':snapshot['revision']},person_id=actor.person_id,task_id=task['id'])
            if row['timing']['type']=='once':
                db.execute("UPDATE ea_schedule SET state='completed',updated_at=now() WHERE id=%s",(row['id'],))
            return {'invocation_id':invocation['id'],'task_id':task['id']}

    async def manage(self,actor,identifier,revision,action,*,title=None,instruction=None,run_at=None,expression=None,timezone_name=None):
        if not actor.verified_owner and actor.channel=='scheduled':
            raise PermissionDenied('Standing friend work cannot modify schedules')
        row=await asyncio.to_thread(self.get,actor,identifier)
        if row['revision']!=revision:raise Conflict('Schedule changed; read its current revision')
        if row['control_state']:raise Conflict('An earlier schedule change needs reconciliation')
        if not row['provider_id']:raise Conflict('Native schedule creation is not confirmed')
        if action not in {'update','pause','resume','cancel'}:raise ValueError('Use update, pause, resume or cancel')
        updated=dict(row)
        if action=='update':
            if title is not None:updated['title']=title
            if instruction is not None:updated['instruction']=instruction
            if not updated['title'].strip() or len(updated['title'])>180 or not updated['instruction'].strip() or len(updated['instruction'])>20000:
                raise ValueError('Supply bounded schedule content')
            if run_at or expression:
                updated['timing']=timing(run_at,expression,timezone_name or self.repository.profile.timezone)
                if updated['timing']['type']!=row['timing']['type']:raise ValueError('A timing-type change requires a new schedule')
            elif timezone_name and row['timing']['type']=='periodic':
                updated['timing']=timing(None,row['timing']['expression'],timezone_name)
        updated['revision']=await asyncio.to_thread(self.reserve_control,identifier,revision,action)
        path='/'+quote(row['provider_id'])
        if action in {'pause','cancel'}:
            await asyncio.to_thread(self.set_state,identifier,'paused' if action=='pause' else 'cancelled')
        try:
            if action in {'pause','cancel'}:
                await self.native.request('post' if action=='pause' else 'delete',path+('/pause' if action=='pause' else ''))
            elif action=='resume':
                result=await self.native.request('post',path+'/resume')
                await asyncio.to_thread(self.set_state,identifier,result['status'])
            elif action=='update':
                changes={k:v for k,v in updated['timing'].items() if k!='type'}
                result=await self.native.request('post',path,body={**changes,'name':self.name(updated),'target':self.target(updated)})
                await asyncio.to_thread(self.set_state,identifier,result['status'])
        except Exception as error:
            status=getattr(error,'status_code',None)
            if status and 400<=status<500 and status not in {408,409,429}:
                await asyncio.to_thread(self.reject_control,row,updated['revision'])
                raise InvalidInput('OpenAI rejected this schedule change; its previous state is retained.') from error
            raise
        await asyncio.to_thread(self.save_control,actor,updated,action,revision)
        return {'ok':True,'schedule':self.public(await asyncio.to_thread(self.get,actor,identifier)),
                'note':'Changes apply to future invocations. Already accepted work and attempted communications are separate.'}

    def reserve_control(self,identifier,revision,action):
        with self.repository.connection() as db:
            reserved=db.execute('''UPDATE ea_schedule SET control_state=%s,revision=revision+1,updated_at=now()
                WHERE assistant_id=%s AND id=%s AND revision=%s AND control_state='' RETURNING revision''',
                (action,self.repository.profile.id,identifier,revision)).fetchone()
            if not reserved:raise Conflict('Another schedule change is already in progress')
            return reserved['revision']

    def reject_control(self,previous,revision):
        with self.repository.connection() as db:
            db.execute("UPDATE ea_schedule SET state=%s,control_state='',updated_at=now() WHERE id=%s AND revision=%s",
                (previous['state'],previous['id'],revision))

    def save_control(self,actor,updated,action,revision):
        identifier=updated['id']
        with self.repository.connection() as db:
            if action=='update':
                changed=db.execute('''UPDATE ea_schedule SET title=%s,instruction=%s,timing=%s,updated_at=now()
                    WHERE assistant_id=%s AND id=%s AND revision=%s RETURNING id''',
                    (updated['title'],updated['instruction'],Jsonb(updated['timing']),self.repository.profile.id,identifier,updated['revision'])).fetchone()
                if not changed:raise Conflict('Schedule changed during native update; inspect provider state')
            db.execute("UPDATE ea_schedule SET control_state='' WHERE assistant_id=%s AND id=%s AND revision=%s",
                (self.repository.profile.id,identifier,updated['revision']))
            self.repository._event(db,'schedule:'+identifier+':'+action+':'+str(revision),'schedule.'+action,
                {'schedule_id':identifier,'action':action,'previous_revision':revision},person_id=actor.person_id)

    def observed_rows(self):
        with self.repository.connection() as db:
            return db.execute('''SELECT * FROM ea_schedule WHERE assistant_id=%s
                AND (state NOT IN ('cancelled','rejected') OR control_state<>'') ORDER BY updated_at LIMIT 20''',
                (self.repository.profile.id,)).fetchall()

    def missing_provider(self,identifier):
        with self.repository.connection() as db:
            db.execute("UPDATE ea_schedule SET state='cancelled',control_state='',updated_at=now() WHERE assistant_id=%s AND id=%s",
                (self.repository.profile.id,identifier))

    def reconcile_receipt(self,row):
        """Resolve only the exact saved action after native creation is proven."""
        with self.repository.connection() as db:
            actions=db.execute('''SELECT call_id,tool,arguments,state FROM ea_action WHERE task_id=%s
                AND tool IN ('schedule_work','text_person')
                AND (receipt->>'uncertain'='true' OR receipt->>'reconciled'='true')
                AND NOT EXISTS(SELECT 1 FROM ea_agent_input WHERE id=%s)''',
                (row['source_task_id'],'schedule:'+row['id']+':saved')).fetchall()
        for action in actions:
            source=hashlib.sha256(json.dumps([row['source_task_id'],action['tool'],action['arguments']],
                sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if source!=row['source_id']:continue
            with self.repository.connection() as db:
                if action['state']=='uncertain':
                    changed=db.execute('''UPDATE ea_action SET state='done',receipt=%s,updated_at=now()
                        WHERE task_id=%s AND call_id=%s AND state='uncertain' RETURNING call_id''',
                        (Jsonb({'ok':True,'scheduled':True,'schedule':self.public(row),'reconciled':True}),
                         row['source_task_id'],action['call_id'])).fetchone()
                    if not changed:continue
                self.repository._event(db,'schedule:'+row['id']+':reconciled','schedule.reconciled',
                    {'schedule_id':row['id'],'provider_id':row['provider_id']},
                    person_id=str(row['person_id']),task_id=row['source_task_id'])
            task=self.repository.task_result(row['source_task_id'])
            if task['state']!='cancelled':
                self.repository.stage_input(row['source_task_id'],'schedule:'+row['id']+':saved',
                    'Application evidence, not a new request: the original native schedule create is now verified. '
                    'Read get_context and scheduled_work. Its receipt is reconciled. Do not create another schedule. '
                    'Report the saved schedule honestly; its future action is not completed.',role='developer')

    def observe_provider(self,row,provider):
        with self.repository.connection() as db:
            if row['control_state']=='update':
                snapshot=self.snapshot(provider.get('target',{}).get('input',[]))
                if snapshot and snapshot['schedule_id']==row['id'] and snapshot['revision']==row['revision']:
                    when={k:provider[k] for k in ('type','run_at','expression','timezone') if k in provider}
                    db.execute('''UPDATE ea_schedule SET title=%s,instruction=%s,timing=%s,state=%s,
                        control_state='',updated_at=now() WHERE id=%s AND revision=%s''',
                        (snapshot['title'],snapshot['instruction'],Jsonb(when),provider['status'],row['id'],row['revision']))
            elif row['control_state'] in {'pause','resume','cancel'}:
                desired={'pause':'paused','resume':'active','cancel':'cancelled'}[row['control_state']]
                if provider['status']==desired:
                    db.execute("UPDATE ea_schedule SET state=%s,control_state='',updated_at=now() WHERE id=%s AND revision=%s",
                        (desired,row['id'],row['revision']))
            else:
                state=provider['status']
                if (row['timing']['type']=='once' and state=='active' and
                        db.execute('SELECT 1 FROM ea_schedule_run WHERE schedule_id=%s LIMIT 1',(row['id'],)).fetchone()):
                    state='completed'
                db.execute('UPDATE ea_schedule SET state=%s,updated_at=now() WHERE id=%s AND revision=%s',
                    (state,row['id'],row['revision']))
            db.execute('UPDATE ea_schedule SET updated_at=now() WHERE id=%s',(row['id'],))

    async def reconcile(self):
        """Observe provider schedules and failed invocations, never trigger time.

        Completion receipts continue through the ordinary work/call/message
        ledger and the existing Dot event subscription.
        """
        rows=await asyncio.to_thread(self.observed_rows)
        providers={p['name']:p async for p in self.native.pages()} if rows else {}
        for row in rows:
            provider=providers.get(self.name(row))
            if not provider and row['provider_id']:
                try:provider=await self.native.request('get','/'+quote(row['provider_id']))
                except Exception as error:
                    if getattr(error,'status_code',None)!=404:raise
                    await asyncio.to_thread(self.missing_provider,row['id'])
                    continue
            if not provider:continue
            if row['control_state']=='contact_cancel':
                await self.native.request('delete','/'+quote(row['provider_id']))
                await asyncio.to_thread(self.missing_provider,row['id'])
                continue
            if not row['provider_id']:
                await asyncio.to_thread(self.bind,row['id'],provider)
                row['provider_id']=provider['id']
            await asyncio.to_thread(self.observe_provider,row,provider)
            confirmed=await asyncio.to_thread(self.raw_schedule,row['id'])
            await asyncio.to_thread(self.reconcile_receipt,confirmed)
            missed_launch=False
            async for invocation in self.native.pages('/'+quote(row['provider_id'])+'/invocations'):
                if invocation.get('source',{}).get('type')!='automation':continue
                if (row['state'] in {'active','completed'} and invocation['status']=='completed'
                        and not await asyncio.to_thread(self.run,invocation['id'])):missed_launch=True
                if row['state'] in {'active','completed'} and invocation['status'] in {'failed','skipped'} and not await asyncio.to_thread(self.run,invocation['id']):
                    await asyncio.to_thread(self.failed_invocation,row,invocation)
            # Recover a missed launch callback from the same authoritative
            # native occurrence. No local clock or replacement occurrence.
            if missed_launch:await self.dispatch(row['id'])

    @staticmethod
    def snapshot(messages):
        for item in messages:
            if item.get('role')=='user':
                for part in item.get('content',[]):
                    if part.get('type')=='input_text':
                        try:
                            value=json.loads(part['text'])
                            if value.get('type')=='alli_scheduled_assignment':return value
                        except (ValueError,AttributeError):pass
        return None

    def failed_invocation(self,row,invocation):
        snapshot=self.snapshot((invocation.get('source',{}).get('automation') or {}).get('target',{}).get('input',[]))
        snapshot=snapshot or {'title':row['title'],'instruction':row['instruction'],'revision':row['revision']}
        result=self.materialize(row,{**invocation,'session_id':invocation.get('session_id') or 'not-started'},snapshot,failed=True)
        code=(invocation.get('error') or {}).get('code') or invocation.get('skip_reason') or invocation['status']
        self.repository.record_outcome(result['task_id'],'failed',
            'Scheduled work could not start: '+row['title']+'. Native OpenAI invocation: '+str(code)+
            '. No call, text or email completion is confirmed; no uncertain action was repeated.',
            turn_id='native-invocation-failed:'+invocation['id'],notify=True)
