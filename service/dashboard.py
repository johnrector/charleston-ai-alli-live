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


SESSION='alli_owner_session'
CHALLENGE='alli_owner_challenge'


def dashboard_router(settings,repository,sessions,carrier,outbox,*,scheduler=None):
    router=APIRouter()

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
        return ''.join('<article><small>'+e(row['created_at'])+' · '+e(row['kind'])+'</small><pre>'+
            e(json.dumps(row['facts'],ensure_ascii=False,default=str,indent=2))+'</pre></article>' for row in reversed(rows)) or '<p>No activity yet.</p>'

    @router.get('/dashboard')
    async def overview(request: Request):
        actor=await owner(request)
        rows=await asyncio.to_thread(repository.recent_ledger,actor,limit=60)
        return page('Activity','<h1>What Alli is doing.</h1>'+events(rows[-60:]))

    @router.get('/dashboard/people')
    async def people(request: Request):
        actor=await owner(request)
        rows=await asyncio.to_thread(repository.list_people,actor,limit=500)
        query=request.query_params.get('q','').casefold().strip()
        if query:rows=[row for row in rows if query in (row['name']+' '+(row.get('phone') or '')).casefold()]
        content='<h1>Your people, remembered.</h1><p>Most recent conversations first.</p><form><label>Find a person or number<input name="q" value="'+e(query)+'"></label><button>Find</button></form><div class="grid">'
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

    @router.get('/dashboard/promises')
    async def promises(request: Request):
        actor=await owner(request)
        rows=await asyncio.to_thread(repository.owner_commitments,actor)
        content='<h1>Promises and scheduled work.</h1>'
        if scheduler is not None:
            for item in await asyncio.to_thread(scheduler.list,actor):
                content+='<article><small>OpenAI native schedule · '+e(item['state'])+'</small><h2>'+e(item['title'])+'</h2>'
                when=item['timing']
                if when['type']=='once':
                    label=datetime.fromtimestamp(when['run_at'],timezone.utc).astimezone(ZoneInfo(repository.profile.timezone)).strftime('%A, %B %-d at %-I:%M %p %Z')
                else:label='Recurring: '+when['expression']+' · '+when['timezone']
                content+='<p>'+e(label)+'</p><pre>'+e(item['instruction'])+'</pre>'
                if item['control_state']:content+='<p>Schedule change awaiting confirmation: '+e(item['control_state'])+'</p>'
                for run in item['recent_runs']:
                    content+='<p>Run: <a href="/dashboard/work/'+e(run['task_id'])+'">'+e(run['state'])+'</a> · '+e(run['scheduled_at'])+'</p>'
                for action in (['pause','cancel'] if item['state']=='active' else ['resume','cancel'] if item['state']=='paused' else []):
                    content+='<form method="post" action="/dashboard/schedules/'+e(item['id'])+'/'+action+'">'+hidden(request)+'<input type="hidden" name="revision" value="'+e(item['revision'])+'"><button>'+action.title()+' schedule</button></form>'
                content+='</article>'
        for row in rows:
            content+='<article><small>'+e(row['state'])+' · '+e(row['due_at'])+'</small><pre>'+e(row['instruction'])+'</pre>'
            if row['state']=='planned':
                content+='<form method="post" action="/dashboard/promises/'+e(row['id'])+'/cancel">'+hidden(request)+'<input type="hidden" name="revision" value="'+e(row['revision'])+'"><button>Cancel scheduled text</button></form>'
            content+='</article>'
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
