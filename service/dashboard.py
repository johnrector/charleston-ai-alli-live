"""A small owner-only oversight surface; work executes through the agent tools."""
import asyncio
import hmac
import json
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from fastapi import APIRouter,HTTPException,Request
from starlette.responses import RedirectResponse

from adapters.owner_sessions import RateLimited
from .web_helpers import escape as e,form,page,safe_return
from . import dashboard_views as views
from adapters.oversight import Oversight
from adapters.relationships import Relationships


SESSION='alli_owner_session'
CHALLENGE='alli_owner_challenge'


def dashboard_router(settings,repository,sessions,carrier,outbox,*,scheduler=None,calls=None):
    router=APIRouter()
    oversight,relationships=Oversight(repository),Relationships(repository)
    zone=repository.profile.timezone

    async def owner(request: Request):
        actor=await asyncio.to_thread(sessions.actor,request.cookies.get(SESSION,''))
        if not actor:
            destination=request.url.path+('?' + request.url.query if request.url.query else '')
            raise HTTPException(303,headers={'Location':'/dashboard/login?'+urlencode({'next':safe_return(destination)})})
        return actor

    def origin(request: Request):
        if request.headers.get('origin')!=settings.base_url:
            raise HTTPException(403,'Use the dashboard form from this site')

    def csrf(request: Request,data):
        origin(request)
        if not hmac.compare_digest(data.get('csrf',''),sessions.csrf(request.cookies.get(SESSION,''))):
            raise HTTPException(403,'Reload the dashboard and try again')

    def hidden(request: Request):
        return '<input type="hidden" name="csrf" value="'+e(sessions.csrf(request.cookies.get(SESSION,'')))+'">'

    def login(destination,message='',error=False):
        destination=safe_return(destination)
        content='<h1>Good to have you here.</h1><section><p>Sign in with a one-time code sent to your configured phone.</p>'
        if message:content+='<p class="'+('error' if error else 'muted')+'">'+e(message)+'</p>'
        content+='<form method="post" action="/dashboard/sign-in"><input type="hidden" name="next" value="'+e(destination)+'">'
        content+='<label>Six-digit code<input name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required></label><button>Sign in</button></form>'
        content+='<form method="post" action="/dashboard/code"><input type="hidden" name="next" value="'+e(destination)+'"><p><button>Text me a sign-in code</button></p></form>'
        content+='<p class="muted">Your sign-in lasts 12 hours in this browser, including across service restarts.</p></section>'
        return page('Sign in',content)

    @router.get('/dashboard/login')
    async def sign_in_page(request: Request):
        destination=safe_return(request.query_params.get('next','/dashboard'))
        if await asyncio.to_thread(sessions.actor,request.cookies.get(SESSION,'')):
            return RedirectResponse(destination,303)
        return login(destination)

    @router.post('/dashboard/code')
    async def send_code(request: Request):
        origin(request);data=await form(request)
        destination=safe_return(data.get('next'))
        if await asyncio.to_thread(sessions.actor,request.cookies.get(SESSION,'')):
            return RedirectResponse(destination,303)
        try:
            challenge=await asyncio.to_thread(sessions.start)
        except RateLimited as error:
            # Keep the code input visible and retain this browser's challenge.
            return login(destination,str(error),True)
        message='A code was sent. Enter it above.'
        try:
            await carrier.send(settings.owner_phone,'Your '+repository.profile.name+' sign-in code is '+challenge.code+
                '. It expires in 10 minutes.',status_path='/transport/auth-status/'+sessions.digest(challenge.token))
        except Exception:
            message='Code delivery could not be confirmed. If a code arrives, you can still enter it above.'
        response=login(destination,message)
        response.set_cookie(CHALLENGE,challenge.token,max_age=600,secure=True,httponly=True,samesite='lax',path='/')
        return response

    @router.post('/dashboard/sign-in')
    async def sign_in(request: Request):
        origin(request);data=await form(request)
        destination=safe_return(data.get('next'))
        token=await asyncio.to_thread(sessions.verify,request.cookies.get(CHALLENGE,''),data.get('code',''))
        if not token:return login(destination,'The code is incorrect or expired. You can try an existing code or request another.',True)
        response=RedirectResponse(destination,303)
        response.set_cookie(SESSION,token,max_age=sessions.lifetime,secure=True,httponly=True,samesite='lax',path='/')
        response.delete_cookie(CHALLENGE,path='/')
        return response

    @router.post('/dashboard/sign-out')
    async def sign_out(request: Request):
        await owner(request);data=await form(request);csrf(request,data)
        await asyncio.to_thread(sessions.revoke,request.cookies.get(SESSION,''))
        response=RedirectResponse('/dashboard/login',303);response.delete_cookie(SESSION,path='/')
        return response

    def events(rows):
        content=''
        for row in reversed(rows):
            facts=row['facts']
            label=facts.get('summary') or facts.get('title') or row['kind'].replace('.',' · ')
            content+='<article class="panel activity-item"><p class="eyebrow">'+e(views.when(row['created_at'],zone))+' · '+e(row['kind'])+'</p><p class="outcome">'+e(str(label)[:1200])+'</p>'+views.evidence('Read saved receipt',facts)+'</article>'
        return content or '<p class="small">No activity yet.</p>'

    @router.get('/dashboard')
    async def overview(request: Request):
        actor=await owner(request)
        summary,schedules,commitments=await asyncio.gather(
            asyncio.to_thread(oversight.summary,actor),
            asyncio.to_thread(scheduler.list,actor) if scheduler else asyncio.sleep(0,result=[]),
            asyncio.to_thread(repository.owner_commitments,actor))
        return page('Today',views.overview(summary,schedules,commitments,zone,settings.release))

    @router.get('/dashboard/activity')
    async def activity(request: Request):
        actor=await owner(request)
        rows=await asyncio.to_thread(repository.recent_ledger,actor,limit=60)
        return page('Activity',views.title('The record of the work.','Actual receipts and reported outcomes, with their source evidence.',zone)+events(rows))

    @router.get('/dashboard/policies')
    async def policies(request: Request):
        actor=await owner(request)
        return page('Rules',views.policies(await asyncio.to_thread(oversight.policies,actor),zone))

    @router.get('/dashboard/health')
    async def health(request: Request):
        actor=await owner(request)
        return page('Health',views.health(await asyncio.to_thread(oversight.summary,actor),settings.release,zone))

    @router.get('/dashboard/people')
    async def people(request: Request):
        actor=await owner(request)
        rows=await asyncio.to_thread(repository.list_people,actor,limit=500)
        query=request.query_params.get('q','').casefold().strip()
        if query:rows=[row for row in rows if query in (row['name']+' '+(row.get('phone') or '')).casefold()]
        content=views.title('Your people, remembered.','A relationship history, not just an address book. Most recent conversations first.',zone)+'<form><label>Find a person or number<input name="q" value="'+e(query)+'"></label><button>Find</button></form><div class="grid">'
        for row in rows:
            content+='<article><small>'+e(row['status'])+'</small><h2><a href="/dashboard/people/'+e(row['id'])+'">'+e(row['name'] or 'Name not supplied')+'</a></h2><p>'+e(row.get('phone') or '')+'</p><p>'+e(row['email'])+'</p><small>Last conversation: '+e(row['last_activity'] or 'None yet')+'</small></article>'
        content+='</div><section><h2>Add a person</h2><form method="post" action="/dashboard/people">'+hidden(request)
        content+='<label>Name<input name="name" required maxlength="120"></label><label>Phone<input name="phone" required></label><label>Invitation email<input name="email" type="email"></label><button>Save and approve contact</button></form></section>'
        return page('People',content)

    @router.post('/dashboard/people')
    async def add_person(request: Request):
        actor=await owner(request);data=await form(request);csrf(request,data)
        row=await asyncio.to_thread(repository.save_person,actor,name=data.get('name',''),phone=data.get('phone',''),email=data.get('email',''))
        await asyncio.to_thread(repository.set_person_status,actor,str(row['id']),'approved',row['revision'])
        return RedirectResponse('/dashboard/people/'+str(row['id']),303)

    @router.get('/dashboard/people/{person_id}')
    async def person(request: Request,person_id):
        actor=await owner(request)
        rows=await asyncio.to_thread(repository.list_people,actor,limit=500)
        row=next((row for row in rows if str(row['id'])==person_id),None)
        if not row:raise HTTPException(404,'Person not found')
        content='<h1>'+e(row['name'] or 'New contact')+'</h1><p>'+e(row['status'])+'</p>'
        if row['kind']!='owner':
            content+='<section><form method="post" action="/dashboard/people/'+e(person_id)+'/edit">'+hidden(request)+'<input type="hidden" name="revision" value="'+e(row['revision'])+'">'
            content+='<label>Name<input name="name" value="'+e(row['name'])+'" required></label><label>Phone<input name="phone" value="'+e(row.get('phone') or '')+'" required></label><label>Email<input name="email" type="email" value="'+e(row['email'])+'"></label><button>Save changes</button></form></section>'
            content+='<section><form method="post" action="/dashboard/people/'+e(person_id)+'/status">'+hidden(request)+'<input type="hidden" name="revision" value="'+e(row['revision'])+'"><div class="row">'
            for status,label in [('approved','Approve / reactivate'),('suspended','Suspend'),('deleted','Delete')]:
                content+='<button name="status" value="'+status+'">'+label+'</button>'
            content+='</div><p class="muted">Deletion retains the activity history and blocks access. Reactivation does not replay cancelled work.</p></form></section>'
        context=await asyncio.to_thread(relationships.read,actor,person_id)
        content+=views.relationship(context,zone)
        activity=await asyncio.to_thread(repository.recent_ledger,actor,person_id=person_id,limit=60)
        return page(row['name'] or 'Contact',content+'<h2>Activity with Alli</h2>'+events(activity[-60:]))

    @router.post('/dashboard/people/{person_id}/edit')
    async def edit_person(request: Request,person_id):
        actor=await owner(request);data=await form(request);csrf(request,data)
        await asyncio.to_thread(repository.save_person,actor,person_id=person_id,revision=int(data['revision']),
            name=data.get('name',''),phone=data.get('phone',''),email=data.get('email',''))
        return RedirectResponse('/dashboard/people/'+person_id,303)

    @router.post('/dashboard/people/{person_id}/status')
    async def person_status(request: Request,person_id):
        actor=await owner(request);data=await form(request);csrf(request,data)
        await asyncio.to_thread(repository.set_person_status,actor,person_id,data.get('status',''),int(data['revision']))
        return RedirectResponse('/dashboard/people/'+person_id,303)

    @router.get('/dashboard/work')
    async def work(request: Request):
        actor=await owner(request)
        rows=await asyncio.to_thread(repository.owner_tasks,actor)
        content='<h1>Delegated work.</h1>'
        for row in rows:
            content+='<article><small>'+e(row['state'])+'</small><h2><a href="/dashboard/work/'+e(row['id'])+'">'+e(row['title'])+'</a></h2><p>'+e(row['result'])+'</p></article>'
        return page('Work',content)

    @router.get('/dashboard/work/{task_id}')
    async def task(request: Request,task_id):
        actor=await owner(request)
        row=await asyncio.to_thread(repository.owner_task,actor,task_id)
        if not row:raise HTTPException(404,'Work not found')
        activity=await asyncio.to_thread(repository.recent_ledger,actor,task_id=task_id,limit=100)
        return page(row['title'],'<h1>'+e(row['title'])+'</h1><p>'+e(row['state'])+'</p><section><pre>'+e(row['result'] or 'Work is pending.')+'</pre></section>'+events([event for event in activity if event['task_id']==task_id]))

    @router.get('/dashboard/calls/{identifier}')
    async def call_evidence(request:Request,identifier):
        await owner(request)
        row=await asyncio.to_thread(calls.lookup,identifier) if calls is not None else None
        if not row:raise HTTPException(404,'Call not found')
        errors=await asyncio.to_thread(calls.errors,row['id'])
        updates=await asyncio.to_thread(calls.context_updates,row['id'])
        content=views.title('Saved call evidence.','Transcript, transport and context updates describe different parts of this call.',zone)
        content+='<section class="panel"><h2>'+e(row['name'] or 'Saved recipient')+'</h2>'+views.evidence('Call identity and transport receipt',
            {k:row.get(k) for k in ('id','conversation_id','parent_task_id','direction','state','provider_id','carrier_status','duration_seconds','finalized','native_finalized')})
        content+='<p class="small">An incoming transcript may be a voicemail system. Transcript generation and context acknowledgment do not prove listener playback or human confirmation.</p>'
        if row['parent_task_id']:content+='<a href="/dashboard/work/'+e(row['parent_task_id'])+'">Read mission outcome</a>'
        content+='</section><section class="panel"><h2>Voicemail playback</h2><p>Finite application speech is separate from Live transcript. A playback mark confirms carrier playback, not mailbox storage or a human answer.</p>'+views.evidence('Saved finite voicemail receipt',await asyncio.to_thread(calls.voicemail_evidence,row['id']))+'</section><section class="panel"><h2>Captured transcript</h2>'
        groups=[]
        for part in row['transcript']:
            role=part.get('role','unknown')
            if groups and groups[-1]['role']==role and groups[-1].get('delivery')==part.get('delivery'):
                groups[-1]['text']+=str(part.get('text',''));groups[-1]['end_ms']=part.get('end_ms')
            else:groups.append({'role':role,'text':str(part.get('text','')),'start_ms':part.get('start_ms'),'end_ms':part.get('end_ms'),'delivery':part.get('delivery')})
        for part in groups:
            content+='<article><h3>'+e('Incoming audio' if part['role']=='user' else 'Alli transcript' if part['role']=='assistant' else part['role'])+'</h3><p class="small">'+e(part['start_ms'])+'–'+e(part['end_ms'])+' ms · '+e(part.get('delivery') or 'captured transcript; playback unverified')+'</p><p>'+e(part['text'])+'</p></article>'
        if not groups:content+='<p>No captured transcript.</p>'
        content+=views.evidence('Raw saved transcript',row['transcript'])+'</section><section class="panel"><h2>Context diagnostics</h2><p>Pending context at session close does not establish whether an earlier message was delivered. Command IDs link future append errors to their acknowledgments; historical errors may lack attribution.</p>'+views.evidence('Saved Live errors',errors)+views.evidence('Context append receipts',updates)+views.evidence('Transport and Live event counts',await asyncio.to_thread(calls.media_evidence,row['id']))+'</section>'
        return page('Call evidence',content)

    @router.get('/dashboard/promises')
    async def promises(request: Request):
        actor=await owner(request)
        rows=await asyncio.to_thread(repository.owner_commitments,actor)
        content=views.title('Calls, commitments & work.','A saved schedule, work in progress and a delivered result are different things.',zone)
        content+='<p class="small">Showing up to 200 saved schedules and 100 commitments, with pending items first. Reload to refresh saved state.</p>'
        content+='<a class="text-link" href="/dashboard/work">All delegated work →</a>'
        if scheduler is not None:
            for item in await asyncio.to_thread(scheduler.list,actor):
                content+='<article class="panel"><small>OpenAI native schedule · '+e(item['state'])+'</small><h2>'+e(item['title'])+'</h2>'
                when=item['timing']
                if when['type']=='once':
                    label=datetime.fromtimestamp(when['run_at'],timezone.utc).astimezone(ZoneInfo(repository.profile.timezone)).strftime('%A, %B %-d at %-I:%M %p %Z')
                else:label='Recurring: '+when['expression']+' · '+when['timezone']
                content+='<p class="small">'+e(label)+'</p>'+views.evidence('Read assignment and schedule identity',{'id':item['id'],'provider_id':item['provider_id'],'revision':item['revision'],'instruction':item['instruction'],'updated_at':item['updated_at']})
                content+='<p class="small">Schedule state: '+e(item['state'])+'. Each run below has its own work outcome. A completed schedule does not establish communication delivery.</p>'
                if item['control_state']:content+='<p>Schedule change awaiting confirmation: '+e(item['control_state'])+'</p>'
                for run in item['recent_runs']:
                    content+='<p>Run: <a href="/dashboard/work/'+e(run['task_id'])+'">'+e(run['state'])+'</a> · '+e(run['scheduled_at'])+'</p>'
                for action in (['pause','cancel'] if item['state']=='active' else ['resume','cancel'] if item['state']=='paused' else []):
                    content+='<form method="post" action="/dashboard/schedules/'+e(item['id'])+'/'+action+'">'+hidden(request)+'<input type="hidden" name="revision" value="'+e(item['revision'])+'"><button>'+action.title()+' schedule</button></form>'
                content+='</article>'
        for row in rows:
            content+='<article class="panel"><div class="panel-head"><h2>'+e(row['kind'].upper())+' · '+e(row.get('person_name') or 'Saved recipient')+'</h2>'+views.badge(row['state'])+'</div><p class="small">'+e(views.when(row['due_at'],zone))+'</p>'+views.evidence('Read saved commitment',{'id':row['id'],'instruction':row['instruction'],'state':row['state'],'due_at':row['due_at']})
            if row['state']=='planned':
                content+='<form method="post" action="/dashboard/promises/'+e(row['id'])+'/cancel">'+hidden(request)+'<input type="hidden" name="revision" value="'+e(row['revision'])+'"><button>Cancel scheduled text</button></form>'
            content+='</article>'
        summary=await asyncio.to_thread(oversight.summary,actor)
        content+=views.call_history(summary['calls'],zone)
        return page('Promises',content)

    @router.post('/dashboard/promises/{commitment_id}/cancel')
    async def cancel(request: Request,commitment_id):
        actor=await owner(request);data=await form(request);csrf(request,data)
        await asyncio.to_thread(outbox.cancel,actor,commitment_id,int(data['revision']))
        return RedirectResponse('/dashboard/promises',303)

    @router.post('/dashboard/schedules/{identifier}/{action}')
    async def change_schedule(request:Request,identifier:str,action:str):
        actor=await owner(request);data=await form(request);csrf(request,data)
        if scheduler is None or action not in {'pause','resume','cancel'}:raise HTTPException(404)
        await scheduler.manage(actor,identifier,int(data['revision']),action)
        return RedirectResponse('/dashboard/promises',303)

    return router
