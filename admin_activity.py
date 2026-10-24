"""Owner-only history and contact lifecycle controls."""
import asyncio
import json
import base64
from datetime import datetime
from psycopg.rows import dict_row
from fastapi import Depends,HTTPException,Request
from pydantic import BaseModel,ConfigDict,Field
import communication_store as store
import dashboard
import security_controls as security

class ContactAction(BaseModel):
    model_config=ConfigDict(extra='forbid')
    phone:str=Field(min_length=7,max_length=30)
    action:str=Field(pattern='^(suspend|reactivate|delete)$')
    reason:str=Field(default='',max_length=300)
class SMSAccess(BaseModel):
    enabled:bool
class MeetingLink(BaseModel):
    phone:str=Field(min_length=7,max_length=30)
    event_id:str=Field(min_length=1,max_length=300)
class PublicProfile(BaseModel):
    body:str=Field(min_length=30,max_length=6000)

def history(before=0,query='',limit=40,phone=''):
    store.initialize()
    query=query.strip()[:160]
    with store.connection() as db,db.cursor(row_factory=dict_row) as cur:
        cur.execute('''SELECT id,payload,created_at FROM alli_communication_update
          WHERE (%s=0 OR id<%s) AND (%s='' OR payload::text ILIKE %s)
          AND (%s='' OR payload->>'phone'=%s OR payload->>'work_id' IN (SELECT id FROM alli_work WHERE phone=%s))
          ORDER BY id DESC LIMIT %s''',(before,before,query,'%'+query+'%',phone,phone,phone,limit+1))
        rows=cur.fetchall()
    more=len(rows)>limit;rows=rows[:limit]
    return dict(ok=True,activities=[__import__('promises').serial(r) for r in rows],next_cursor=rows[-1]['id'] if more else None)

def conversation(sid):
    with store.connection() as db,db.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT sid,phone,channel,status,body,reply,report,transcript,created_at,updated_at,owner_verified FROM alli_inbound WHERE sid=%s',(sid,));row=cur.fetchone()
        if row:
            cur.execute('SELECT status,participants,transcript,work_id FROM alli_group_notes WHERE sid=%s',(sid,));group=cur.fetchone()
            if group:row=dict(row,group_notes=group)
    if not row:
        if sid.startswith(('owner-auth:','dashboard-login:')):return dict(status='private_verification',body='Verification codes are not displayed in activity history')
        import scheduled_sms
        message=scheduled_sms.get(sid)
        if message:return message
        with store.connection() as db,db.cursor(row_factory=dict_row) as cur:
            cur.execute('SELECT id,call_sid,status,context,outcome,disposition,voicemail,reason,created_at,updated_at FROM alli_appointment_dispatch WHERE id=%s OR call_sid=%s',(sid,sid));row=cur.fetchone()
            return __import__('promises').serial(row) if row else None
    return __import__('promises').serial(row)

def conversations(phone,cursor=''):
    before=None;key=''
    if cursor:
        try:
            stamp,key=json.loads(base64.urlsafe_b64decode(cursor[:500]+'='*((-len(cursor))%4)))
            before=datetime.fromisoformat(stamp)
            if not before.tzinfo or not isinstance(key,str):raise ValueError()
        except Exception:raise ValueError('Invalid conversation page')
    with store.connection() as db,db.cursor(row_factory=dict_row) as cur:
        cur.execute('''WITH conversations AS (
          SELECT 'in:'||sid AS key,sid AS identifier,channel AS kind,status,created_at,left(body,220) AS preview FROM alli_inbound WHERE phone=%s
          UNION ALL SELECT 'sms:'||request_id,request_id,'outbound text',status,send_at,left(body,220) FROM alli_outbound_sms
            WHERE phone=%s AND send_at<=now() AND status<>'scheduled' AND request_id NOT LIKE 'dashboard-login:%%' AND request_id NOT LIKE 'owner-auth:%%'
          UNION ALL SELECT 'call:'||id,coalesce(call_sid,id),'outbound call',coalesce(disposition,status),created_at,
            left(coalesce(outcome->>'summary',context->>'mission','Saved call receipt'),220) FROM alli_appointment_dispatch WHERE context->>'phone'=%s
          ) SELECT * FROM conversations WHERE (%s::timestamptz IS NULL OR (created_at,key)<(%s::timestamptz,%s))
          ORDER BY created_at DESC,key DESC LIMIT 26''',(phone,phone,phone,before,before,key))
        rows=cur.fetchall()
    more=len(rows)>25;rows=rows[:25]
    next_cursor=base64.urlsafe_b64encode(json.dumps([rows[-1]['created_at'].isoformat(),rows[-1]['key']]).encode()).decode().rstrip('=') if more else None
    return dict(ok=True,conversations=[__import__('promises').serial(r) for r in rows],next_cursor=next_cursor)

def install(app):
    async def auth(request:Request):return await asyncio.to_thread(dashboard.authenticate,request)
    async def write(request:Request,identity=Depends(auth)):dashboard.write_guard(request,identity)
    @app.get('/dashboard/api/activity',dependencies=[Depends(auth)])
    async def activities(before:int=0,q:str=''):return await asyncio.to_thread(history,max(0,before),q)
    @app.get('/dashboard/api/people/{phone}/conversations',dependencies=[Depends(auth)])
    async def person_conversations(phone:str,cursor:str=''):
        phone=store.normalize_phone(phone)
        if not phone:raise HTTPException(422,'Invalid contact number')
        try:return await asyncio.to_thread(conversations,phone,cursor)
        except ValueError as exc:raise HTTPException(422,str(exc))
    @app.get('/dashboard/api/people/{phone}',dependencies=[Depends(auth)])
    async def person(phone:str,before:int=0):
        phone=store.normalize_phone(phone)
        if not phone:raise HTTPException(422,'Invalid contact number')
        with store.connection() as db,db.cursor(row_factory=dict_row) as cur:
            cur.execute('SELECT phone,name,source,email,updated_at,deleted_at FROM alli_contact_name WHERE phone=%s',(phone,));row=cur.fetchone()
            if not row:raise HTTPException(404,'Contact not found')
        return dict(ok=True,person=__import__('promises').serial(row),suspended=await asyncio.to_thread(security.blocked,phone),**{k:v for k,v in (await asyncio.to_thread(history,max(0,before),'',40,phone)).items() if k!='ok'})
    @app.get('/dashboard/api/activity/conversation/{sid}',dependencies=[Depends(auth)])
    async def read_conversation(sid:str):
        row=await asyncio.to_thread(conversation,sid)
        if not row:raise HTTPException(404,'Conversation unavailable')
        return dict(ok=True,conversation=row)
    @app.get('/dashboard/api/activity/work/{wid}',dependencies=[Depends(auth)])
    async def work(wid:str):
        import work_store
        row=await asyncio.to_thread(work_store.read,wid,internal=True)
        if not row:raise HTTPException(404,'Work unavailable')
        with store.connection() as db,db.cursor(row_factory=dict_row) as cur:
            cur.execute('SELECT id,kind,details,created_at FROM alli_work_event WHERE work_id=%s ORDER BY id',(wid,))
            events=[__import__('promises').serial(r) for r in cur.fetchall()]
        # Internal context may contain imported account content. It is accessible
        # only through this authenticated owner view, never the public result.
        return dict(ok=True,work=row,events=events)
    @app.get('/dashboard/api/security',dependencies=[Depends(auth)])
    async def controls():return dict(ok=True,**await asyncio.to_thread(security.snapshot))
    @app.get('/dashboard/api/profile',dependencies=[Depends(auth)])
    async def profile():return dict(ok=True,**await asyncio.to_thread(__import__('public_profile').read))
    @app.post('/dashboard/api/profile',dependencies=[Depends(write)])
    async def save_profile(body:PublicProfile):return await asyncio.to_thread(__import__('public_profile').save,body.body)
    @app.post('/dashboard/api/security/contacts',dependencies=[Depends(write)])
    async def contact(body:ContactAction):
        try:
            if body.action=='delete':await asyncio.to_thread(security.delete_contact,body.phone)
            else:await asyncio.to_thread(security.set_block,body.phone,body.action=='suspend',body.reason)
            return dict(ok=True)
        except ValueError as exc:raise HTTPException(409,str(exc))
    @app.post('/dashboard/api/security/sms-session',dependencies=[Depends(write)])
    async def sms_session(body:SMSAccess):
        import owner_access
        with store.connection() as db:
            if body.enabled:owner_access.grant_sms(db)
            else:db.execute("UPDATE alli_owner_access SET expires_at='epoch' WHERE channel='sms'")
        return dict(ok=True,enabled=body.enabled)
    @app.post('/dashboard/api/security/meeting-link',dependencies=[Depends(write)])
    async def meeting_link(body:MeetingLink):
        import contact_events,google_integration as g
        from urllib.parse import quote
        phone=store.normalize_phone(body.phone)
        with store.connection() as db:
            if not phone or security.tier(db,phone)!='friend':raise HTTPException(409,'Choose an approved contact')
        event=await asyncio.to_thread(g.api,'GET','calendar/v3/calendars/primary/events/'+quote(body.event_id,safe=''))
        if event.get('status')!='confirmed':raise HTTPException(409,'Choose an active event')
        await asyncio.to_thread(contact_events.bind,phone,event['id'],'owner_dashboard_link')
        return dict(ok=True)
