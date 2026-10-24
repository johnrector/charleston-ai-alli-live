"""Owner-only dashboard: durable SMS login, revocable sessions and CSRF protection."""
import asyncio
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from psycopg.rows import dict_row
import communication_store as store
import promises
import scheduled_sms as sms

COOKIE='__Host-alli_session'
SESSION_SECONDS=12*60*60
STATIC=Path(__file__).parent/'dashboard_assets'


def digest(value): return hashlib.sha256(value.encode()).hexdigest()
def otp_digest(challenge, code):
    return hmac.new(os.environ['DEMO_KEY'].encode(),(challenge+':'+code).encode(),hashlib.sha256).hexdigest()


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS alli_dashboard_login (
        id text PRIMARY KEY, code_digest text NOT NULL, attempts integer NOT NULL DEFAULT 0,
        consumed boolean NOT NULL DEFAULT false, expires_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())''')
    db.execute('''CREATE TABLE IF NOT EXISTS alli_dashboard_session (
        digest text PRIMARY KEY, expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now())''')
    db.execute('''CREATE TABLE IF NOT EXISTS alli_dashboard_ack (
        update_id bigint PRIMARY KEY REFERENCES alli_communication_update(id), created_at timestamptz NOT NULL DEFAULT now())''')


def request_code():
    store.initialize()
    owner=promises.owner_phone()
    if not owner or not os.getenv('DEMO_KEY'): raise ValueError('Owner login is not configured')
    challenge=secrets.token_hex(16); code=f'{secrets.randbelow(1000000):06}'
    with store.connection() as db:
        db.execute('SELECT pg_advisory_xact_lock(301849630167)')
        db.execute("UPDATE alli_outbound_sms SET body='Dashboard sign-in code [expired]' WHERE request_id LIKE 'dashboard-login:%%' AND expires_at<now()")
        count,last=db.execute("SELECT count(*),max(created_at) FROM alli_dashboard_login WHERE created_at>now()-interval '15 minutes'").fetchone()
        if count>=3 or (last and datetime.now(timezone.utc)-last<timedelta(seconds=60)):
            raise ValueError('A code was recently requested. Wait a minute, or 15 minutes after three requests.')
        db.execute("DELETE FROM alli_dashboard_login WHERE created_at<now()-interval '1 day'")
        db.execute('INSERT INTO alli_dashboard_login(id,code_digest,expires_at) VALUES(%s,%s,now()+interval \'10 minutes\')',(challenge,otp_digest(challenge,code)))
        sms.enqueue_in_db(db,sms.TextMessage(request_id='dashboard-login:'+challenge,phone=owner,recipient_name='John Rector',
            message=f'Alli dashboard sign-in code: {code}. Expires in 10 minutes. Use only on your Alli dashboard. If you did not request this, ignore it.'))
    return dict(ok=True,challenge=challenge,message='A one-time code is queued for your configured phone. It expires in 10 minutes.')


def verify_code(challenge,code):
    store.initialize()
    token=secrets.token_urlsafe(32)
    valid=False
    with store.connection() as db:
        if not challenge:
            latest=db.execute("SELECT id FROM alli_dashboard_login WHERE NOT consumed AND expires_at>now() ORDER BY created_at DESC LIMIT 1 FOR UPDATE").fetchone()
            challenge=latest[0] if latest else ''
        row=db.execute('SELECT code_digest,attempts,consumed,expires_at FROM alli_dashboard_login WHERE id=%s FOR UPDATE',(challenge,)).fetchone()
        if row and not row[2] and row[1]<5 and row[3]>datetime.now(timezone.utc):
            db.execute('UPDATE alli_dashboard_login SET attempts=attempts+1 WHERE id=%s',(challenge,))
            valid=hmac.compare_digest(row[0],otp_digest(challenge,code))
            if valid:
                db.execute('UPDATE alli_dashboard_login SET consumed=true WHERE id=%s',(challenge,))
                db.execute('DELETE FROM alli_dashboard_session WHERE expires_at<now()')
                db.execute("INSERT INTO alli_dashboard_session(digest,expires_at) VALUES(%s,now()+interval '12 hours')",(digest(token),))
                from owner_access import grant_sms
                grant_sms(db)
    # Raise after the transaction commits, so invalid-attempt counters persist.
    if not valid: raise ValueError('Code invalid, expired or already used. Request a new code if needed.')
    return token


def authenticate(request):
    key=request.headers.get('x-demo-key',''); expected=os.getenv('DEMO_KEY','')
    if expected and key and hmac.compare_digest(key,expected): return dict(method='key')
    token=request.cookies.get(COOKIE,'')
    if len(token)>100 or not token: raise HTTPException(401,'Sign in to your private dashboard')
    store.initialize()
    with store.connection() as db:
        valid=db.execute('SELECT 1 FROM alli_dashboard_session WHERE digest=%s AND expires_at>now()',(digest(token),)).fetchone()
    if not valid: raise HTTPException(401,'Session expired. Sign in again.')
    return dict(method='cookie',token=token,csrf=digest(token+':csrf'))


def write_guard(request,auth):
    if auth['method']=='key': return
    origin=request.headers.get('origin','')
    allowed='https://'+os.environ.get('TWILIO_VOICE_PUBLIC_DOMAIN','charleston-ai-alli-gpt-live.onrender.com')
    if origin!=allowed or not hmac.compare_digest(request.headers.get('x-csrf-token',''),auth['csrf']):
        raise HTTPException(403,'Refresh this dashboard before making changes')


class Input(BaseModel):
    model_config=ConfigDict(extra='forbid')
class Verify(Input):
    challenge: str=Field(default='',max_length=32,pattern=r'^(?:[a-f0-9]{32})?$')
    code: str=Field(min_length=6,max_length=6,pattern=r'^\d+$')
class NewPromise(Input):
    title: str=Field(min_length=1,max_length=180)
    details: str=Field(default='',max_length=3000)
    phone: str=Field(default='',max_length=30)
    contact_name: str=Field(default='',max_length=200)
    kind: str=Field(default='task',pattern='^(task|ask_john|text)$')
    due_at: str|None=None
    response: str=Field(default='',max_length=1500)
    intent_id: str=Field(min_length=16,max_length=64,pattern='^[A-Za-z0-9-]+$')
class Action(Input):
    action: str=Field(pattern='^(answer|complete|cancel)$')
    note: str=Field(default='',max_length=1500)
    revision: int=Field(ge=1)
class Contact(Input):
    phone: str=Field(min_length=7,max_length=30)
    name: str=Field(min_length=1,max_length=100)
    email: str=Field(default='',max_length=320)
class Decision(Input):
    approve: bool
    name: str=Field(default='',max_length=100)
class Policy(Input):
    enabled: bool


def data():
    store.initialize()
    with store.connection() as db, db.cursor(row_factory=dict_row) as cur:
        cur.execute('''WITH contacts AS (
          SELECT phone,created_at AS contacted_at,'inbound' AS channel FROM alli_inbound
          UNION ALL SELECT phone,send_at,'outbound_sms' FROM alli_outbound_sms
            WHERE send_at<=now() AND status IN ('submitted','sent','delivered','undelivered','failed')
              AND request_id NOT LIKE 'dashboard-login:%%' AND request_id NOT LIKE 'owner-auth:%%'
          UNION ALL SELECT context->>'phone',created_at,'outbound_call' FROM alli_appointment_dispatch WHERE call_sid IS NOT NULL
        ), activity AS (SELECT phone,max(contacted_at) AS last_activity,count(*) AS conversation_count FROM contacts GROUP BY phone)
        SELECT c.phone,c.name,c.source,c.email,c.updated_at,c.deleted_at,a.last_activity,coalesce(a.conversation_count,0) AS conversation_count,
          (SELECT count(*) FROM alli_work w WHERE w.phone=c.phone AND NOT w.internal) AS work_count
        FROM alli_contact_name c LEFT JOIN activity a ON a.phone=c.phone
        ORDER BY a.last_activity DESC NULLS LAST,lower(c.name),c.phone''')
        people=[dict(promises.serial(row),is_owner=store.normalize_phone(row['phone'])==store.normalize_phone(os.getenv('ALLI_OWNER_PHONE',''))) for row in cur.fetchall()]
        cur.execute("SELECT a.phone,a.code,a.created_at,c.name FROM alli_contact_approval a LEFT JOIN alli_contact_name c ON c.phone=a.phone WHERE a.decision='pending' ORDER BY a.created_at")
        approvals=[promises.serial(row) for row in cur.fetchall()]
        cur.execute('''WITH latest AS (
            SELECT DISTINCT ON (CASE WHEN payload->>'source'='business_tool_result' THEN 'action:'||id::text
                WHEN payload->>'source'='conversation_report' THEN 'report:'||coalesce(payload->>'sid',id::text)
                ELSE coalesce('sid:'||(payload->>'sid'),'update:'||id::text) END) id,payload,created_at
            FROM alli_communication_update WHERE created_at>now()-interval '7 days'
            ORDER BY CASE WHEN payload->>'source'='business_tool_result' THEN 'action:'||id::text
                WHEN payload->>'source'='conversation_report' THEN 'report:'||coalesce(payload->>'sid',id::text)
                ELSE coalesce('sid:'||(payload->>'sid'),'update:'||id::text) END,id DESC)
            SELECT u.id,u.payload,u.created_at FROM latest u
            WHERE u.payload->>'follow_up_needed'='true' AND NOT EXISTS (SELECT 1 FROM alli_dashboard_ack a WHERE a.update_id=u.id)
            ORDER BY u.id DESC LIMIT 40''')
        attention=[promises.serial(row) for row in cur.fetchall()]
        cur.execute('SELECT id,payload,created_at FROM alli_communication_update ORDER BY id DESC LIMIT 30')
        activity=[promises.serial(row) for row in cur.fetchall()]
        cur.execute(f'SELECT {sms.FIELDS} FROM alli_outbound_sms WHERE send_at>now()-interval \'1 day\' AND request_id NOT LIKE \'dashboard-login:%%\' AND request_id NOT LIKE \'owner-auth:%%\' ORDER BY send_at DESC LIMIT 60')
        outbound=[]
        for row in cur.fetchall():
            row=dict(row); row.pop('error_code',None)
            outbound.append(promises.serial(row))
    from appointment_reminders import settings
    from google_integration import CONNECTION_STATUS
    from reasoning_config import MODEL,EFFORT
    return dict(ok=True,checked_at=datetime.now(timezone.utc).isoformat(),timezone='America/New_York',reasoning=dict(model=MODEL,effort=EFFORT),
        promises=promises.listing(include_closed=True),people=people,approvals=approvals,attention=attention,activity=activity,
        messages=outbound,reminder_policy=settings(),google_connection=dict(CONNECTION_STATUS))


def install_dashboard(app,communications,reminders,worker):
    from admin_activity import install as install_admin_activity
    install_admin_activity(app)
    @app.middleware('http')
    async def private_headers(request,call_next):
        response=await call_next(request)
        if request.url.path.startswith('/dashboard'):
            response.headers.update({'Cache-Control':'no-store','X-Robots-Tag':'noindex, nofollow',
                'Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff'})
            response.headers.setdefault('X-Frame-Options','DENY')
            response.headers.setdefault('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        return response

    @app.get('/dashboard',include_in_schema=False)
    async def page(): return FileResponse(STATIC/'index.html')
    @app.get('/dashboard/assets/{filename}',include_in_schema=False)
    async def assets(filename:str):
        if filename not in ('dashboard.js','dashboard.css','alli-portrait.png','alli-welcome.mp4','work.js','admin.js'): raise HTTPException(404)
        return FileResponse(STATIC/filename)
    @app.post('/dashboard/api/login')
    async def login():
        try: return await asyncio.to_thread(request_code)
        except ValueError as exc: raise HTTPException(429,str(exc))
    @app.post('/dashboard/api/verify')
    async def verify(body:Verify,response:Response):
        try: token=await asyncio.to_thread(verify_code,body.challenge,body.code)
        except ValueError as exc: raise HTTPException(401,str(exc))
        response.set_cookie(COOKIE,token,max_age=SESSION_SECONDS,secure=True,httponly=True,samesite='lax',path='/')
        return dict(ok=True,csrf=digest(token+':csrf'))

    async def auth(request:Request): return await asyncio.to_thread(authenticate,request)
    async def write(request:Request,identity=Depends(auth)):
        write_guard(request,identity); return identity
    @app.get('/dashboard/api/session')
    async def session(response:Response,identity=Depends(auth)):
        if identity.get('method')=='cookie':
            # Refresh browser attributes without extending the server's 12-hour session.
            response.set_cookie(COOKIE,identity['token'],max_age=SESSION_SECONDS,secure=True,httponly=True,samesite='lax',path='/')
        return dict(ok=True,csrf=identity.get('csrf',''))
    @app.post('/dashboard/api/logout')
    async def logout(response:Response,identity=Depends(write)):
        if identity['method']=='cookie':
            def revoke():
                with store.connection() as db: db.execute('DELETE FROM alli_dashboard_session WHERE digest=%s',(digest(identity['token']),))
            await asyncio.to_thread(revoke)
        response.delete_cookie(COOKIE,path='/',secure=True,httponly=True,samesite='lax')
        return dict(ok=True)
    @app.get('/dashboard/api/data',dependencies=[Depends(auth)])
    async def snapshot():
        result=await asyncio.to_thread(data)
        result['cloud_worker']=dict(ready=worker.ready,last_scan=worker.last_scan,last_error=worker.last_error)
        return result
    @app.get('/dashboard/api/meetings',dependencies=[Depends(auth)])
    async def meetings():
        from owner_sms import calendar_lookup
        try: return await asyncio.to_thread(calendar_lookup)
        except Exception: return dict(ok=False,error='Calendar unavailable. Your other dashboard information is still available.')
    @app.post('/dashboard/api/promises',dependencies=[Depends(write)])
    async def add(body:NewPromise):
        args=body.model_dump(); intent=args.pop('intent_id')
        try: return await asyncio.to_thread(promises.create,**args,dedupe='dashboard:'+intent)
        except ValueError as exc: raise HTTPException(409,str(exc))
    @app.post('/dashboard/api/promises/{pid}',dependencies=[Depends(write)])
    async def change(pid:str,body:Action):
        try: return await asyncio.to_thread(promises.change,pid,body.action,body.note,body.revision)
        except ValueError as exc: raise HTTPException(409,str(exc))
    @app.get('/dashboard/api/promises/{pid}/history',dependencies=[Depends(auth)])
    async def history(pid:str):
        def read():
            with store.connection() as db,db.cursor(row_factory=dict_row) as cur:
                cur.execute('SELECT action,actor,note,created_at FROM alli_promise_event WHERE promise_id=%s ORDER BY id',(pid,))
                return dict(events=[promises.serial(r) for r in cur.fetchall()])
        return await asyncio.to_thread(read)
    @app.post('/dashboard/api/contacts',dependencies=[Depends(write)])
    async def contact(body:Contact):
        try: return await asyncio.to_thread(store.save_contact,body.phone,body.name,body.email)
        except ValueError as exc: raise HTTPException(409,str(exc))
    @app.post('/dashboard/api/approvals/{code}',dependencies=[Depends(write)])
    async def approval(code:str,body:Decision):
        from contact_approvals import command
        if not re_code(code) or any(c in body.name for c in '<>\r\n'): raise HTTPException(422,'Invalid approval')
        return dict(ok=True,message=await asyncio.to_thread(command,promises.owner_phone(),('APPROVE ' if body.approve else 'DENY ')+code+(' '+body.name if body.approve and body.name else '')))
    @app.post('/dashboard/api/messages/{rid}/cancel',dependencies=[Depends(write)])
    async def cancel(rid:str):
        # Promise-owned messages must update their promise atomically, too.
        if rid.startswith('promise:'): raise HTTPException(409,'Cancel this item from Promises')
        return await asyncio.to_thread(sms.cancel,rid)
    @app.post('/dashboard/api/reminders',dependencies=[Depends(write)])
    async def policy(body:Policy): return await reminders.configure(body.enabled)
    @app.post('/dashboard/api/attention/{update_id}/ack',dependencies=[Depends(write)])
    async def acknowledge(update_id:int):
        def ack():
            with store.connection() as db: db.execute('INSERT INTO alli_dashboard_ack(update_id) SELECT id FROM alli_communication_update WHERE id=%s ON CONFLICT DO NOTHING',(update_id,))
        await asyncio.to_thread(ack); return dict(ok=True)


def re_code(value):
    import re
    return bool(re.fullmatch('[A-F0-9]{8}',value))
