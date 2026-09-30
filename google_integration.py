import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
from psycopg.types.json import Jsonb
from tac.tools import function_tool
import google_store as store

TZ = ZoneInfo('America/New_York')
SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email',
          'https://www.googleapis.com/auth/calendar.freebusy',
          'https://www.googleapis.com/auth/calendar.events.owned',
          'https://www.googleapis.com/auth/gmail.send']
REDIRECT = 'https://charleston-ai-alli-gpt-live.onrender.com/google/callback'
router = APIRouter()


def require_admin(key):
    expected = os.getenv('DEMO_KEY', '')
    if not expected or not hmac.compare_digest(key, expected):
        raise HTTPException(401, 'Unauthorized')


def oauth_flow(state=None, verifier=None):
    config = {'web': {'client_id': os.environ['GOOGLE_CLIENT_ID'],
                      'client_secret': os.environ['GOOGLE_CLIENT_SECRET'],
                      'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                      'token_uri': 'https://oauth2.googleapis.com/token'}}
    return Flow.from_client_config(config, scopes=SCOPES, state=state,
                                  redirect_uri=REDIRECT, code_verifier=verifier,
                                  autogenerate_code_verifier=verifier is None)


@router.get('/google/authorize')
def authorize(x_demo_key: str = Header(default='')):
    if not x_demo_key:
        return HTMLResponse('<!doctype html><title>Connect Alli to Google</title><h1>Connect Alli to Google</h1><p>Administrator access required. This connects John’s Calendar and Gmail to Alli.</p><form method="post"><label>Demo administrator key <input name="admin_key" type="password" required autocomplete="off"></label><button>Connect Google</button></form>', headers={'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer'})
    require_admin(x_demo_key)
    return start_authorization()


@router.post('/google/authorize')
def authorize_form(admin_key: str = Form()):
    require_admin(admin_key)
    return start_authorization()


def start_authorization():
    store.initialize()
    flow = oauth_flow()
    url, state = flow.authorization_url(access_type='offline', prompt='consent',
        login_hint=os.environ.get('GOOGLE_OWNER_EMAIL', 'jsrector@gmail.com'),
        include_granted_scopes='false')
    store.register_state(state)
    cookie = store.cipher().encrypt(json.dumps({'state':state,'verifier':flow.code_verifier}).encode()).decode()
    response = RedirectResponse(url, status_code=303)
    response.set_cookie('alli_oauth', cookie, max_age=600, httponly=True, secure=True, samesite='lax', path='/google')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@router.get('/google/callback')
def callback(request: Request):
    try:
        saved = json.loads(store.cipher().decrypt(request.cookies.get('alli_oauth','').encode(), ttl=600))
    except (InvalidToken, ValueError, KeyError):
        raise HTTPException(400, 'OAuth session expired. Restart authorization.')
    state = request.query_params.get('state','')
    if not state or not hmac.compare_digest(state, saved['state']) or not store.consume_state(state):
        raise HTTPException(400, 'Invalid or already used OAuth state')
    if request.query_params.get('error') or not request.query_params.get('code'):
        raise HTTPException(400, 'Google authorization was not completed')
    try:
        flow = oauth_flow(state, saved['verifier'])
        flow.fetch_token(code=request.query_params['code'])
        credentials = flow.credentials
        identity = id_token.verify_oauth2_token(credentials.id_token, GoogleRequest(), os.environ['GOOGLE_CLIENT_ID'])
        if not identity.get('email_verified') or identity.get('email','').lower() != os.environ.get('GOOGLE_OWNER_EMAIL','jsrector@gmail.com').lower():
            raise ValueError('Wrong account')
        granted = set(credentials.granted_scopes or credentials.scopes or [])
        if not set(SCOPES).issubset(granted):
            raise ValueError('Required permissions missing')
        if not credentials.refresh_token:
            raise ValueError('Offline token missing; reconnect with consent')
        store.save_token(credentials.refresh_token)
        # Read back the committed encrypted record, then force a refresh.
        fresh_credentials()
    except Exception:
        # Never include authorization codes, token payloads or Google exceptions in logs/HTML.
        raise HTTPException(400, 'Google connection could not be verified. Check account, permissions and configuration; then reconnect.') from None
    response = HTMLResponse('<!doctype html><title>Alli connected</title><h1>Google connected</h1><p>Alli’s refresh token is encrypted in durable storage and has been verified with Google.</p>', headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
    response.delete_cookie('alli_oauth', path='/google')
    return response


def fresh_credentials():
    credentials = Credentials(None, refresh_token=store.load_token(),
        token_uri='https://oauth2.googleapis.com/token', client_id=os.environ['GOOGLE_CLIENT_ID'],
        client_secret=os.environ['GOOGLE_CLIENT_SECRET'], scopes=SCOPES)
    credentials.refresh(GoogleRequest())
    return credentials


class GoogleError(Exception):
    pass


def api(method, path, *, body=None, params=None, credentials=None):
    credentials = credentials or fresh_credentials()
    with httpx.Client(timeout=25) as client:
        r = client.request(method, 'https://www.googleapis.com/' + path,
            headers={'Authorization': 'Bearer ' + credentials.token}, json=body, params=params)
    if not r.is_success:
        raise GoogleError(f'Google rejected the operation (HTTP {r.status_code}); success is not confirmed.')
    return r.json() if r.content else {}


def local_time(value):
    parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
        if parsed.astimezone(timezone.utc).astimezone(TZ).replace(tzinfo=None) != parsed.replace(tzinfo=None):
            raise ValueError('That local time does not exist due to daylight saving time')
        if parsed.replace(fold=0).utcoffset() != parsed.replace(fold=1).utcoffset():
            raise ValueError('That local time is ambiguous; supply its Eastern UTC offset')
    return parsed.astimezone(TZ)


def email_address(value):
    if not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', value) or '\r' in value or '\n' in value:
        raise ValueError('A single valid email address is required')
    return value


def availability(start_datetime, end_datetime, duration_minutes=30):
    start, end = local_time(start_datetime), local_time(end_datetime)
    elapsed = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    if elapsed <= timedelta(0) or elapsed > timedelta(days=31) or not 5 <= duration_minutes <= 480:
        raise ValueError('Use an ordered window up to 31 days and a duration of 5–480 minutes')
    data = api('POST','calendar/v3/freeBusy',body={'timeMin':start.isoformat(),'timeMax':end.isoformat(),'timeZone':str(TZ),'items':[{'id':'primary'}]})
    calendar = data.get('calendars',{}).get('primary',{})
    if calendar.get('errors') or 'busy' not in calendar:
        raise GoogleError('Google did not confirm calendar availability')
    busy = sorted([(local_time(x['start']),local_time(x['end'])) for x in calendar['busy']])
    now = datetime.now(TZ)
    cursor = max(start,now).replace(second=0,microsecond=0)
    if cursor < max(start,now): cursor += timedelta(minutes=1)
    cursor += timedelta(minutes=(-cursor.minute)%15)
    suggestions=[]
    while cursor+timedelta(minutes=duration_minutes)<=end and len(suggestions)<12:
        finish=cursor+timedelta(minutes=duration_minutes)
        if cursor.weekday()<5 and cursor.hour>=9 and (finish.hour<17 or (finish.hour==17 and finish.minute==0)) and cursor.date()==finish.date() and not any(cursor<b and finish>a for a,b in busy):
            suggestions.append({'start':cursor.isoformat(),'end':finish.isoformat()})
        cursor+=timedelta(minutes=15)
    return {'ok':True,'timezone':str(TZ),'now':now.isoformat(),'window_start':start.isoformat(),'window_end':end.isoformat(),'busy':[{'start':a.isoformat(),'end':b.isoformat()} for a,b in busy],'suggested_slots':suggestions,'suggestion_policy':'Weekdays 9am–5pm Eastern; busy intervals are authoritative for the entire requested window.'}


def meeting(attendee_name, attendee_email, start_datetime, duration_minutes, title, context='', confirmed=False):
    if not confirmed: raise ValueError('Obtain agreement on the meeting time before creating it')
    if not isinstance(attendee_name, str) or not attendee_name.strip() or len(attendee_name) > 200 or any(ord(c) < 32 for c in attendee_name):
        raise ValueError('A nonempty attendee name of at most 200 characters is required')
    if not isinstance(attendee_email, str) or len(attendee_email) > 320:
        raise ValueError('A valid attendee email of at most 320 characters is required')
    email_address(attendee_email)
    start = local_time(start_datetime)
    if start <= datetime.now(TZ) or not 5 <= duration_minutes <= 480 or not title.strip():
        raise ValueError('Meeting must have a title, a future time and a duration of 5–480 minutes')
    end=(start.astimezone(timezone.utc)+timedelta(minutes=duration_minutes)).astimezone(TZ)
    event_id=hashlib.sha256(json.dumps([attendee_email.lower(),start.isoformat(),duration_minutes,title],ensure_ascii=True).encode()).hexdigest()
    event_path='calendar/v3/calendars/primary/events/'+event_id
    with store.action_lock() as db:
        existing=db.execute('SELECT result FROM alli_google_action WHERE id=%s AND status=%s',(event_id,'done')).fetchone()
        # Cached success is historical; always re-read the current event.
        # Deterministic Google event ID makes retries safe after a lost HTTP response.
        try:
            event=api('GET',event_path)
        except GoogleError as e:
            if 'HTTP 404' not in str(e): raise
            if existing:
                raise GoogleError('Previously booked event is missing; do not recreate it automatically') from None
            current=availability(start.isoformat(),end.isoformat(),duration_minutes)
            if current['busy']: raise ValueError('John is busy during that time. Choose another slot.')
            payload={'id':event_id,'summary':title,'description':context,'start':{'dateTime':start.isoformat(),'timeZone':str(TZ)},'end':{'dateTime':end.isoformat(),'timeZone':str(TZ)},'attendees':[{'email':attendee_email,'displayName':attendee_name}]}
            event=api('POST','calendar/v3/calendars/primary/events',body=payload,params={'sendUpdates':'all'})
        try:
            attendees = event.get('attendees', [])
            matches = (
                event.get('id') == event_id and event.get('status') == 'confirmed'
                and local_time(event['start']['dateTime']).astimezone(timezone.utc) == start.astimezone(timezone.utc)
                and local_time(event['end']['dateTime']).astimezone(timezone.utc) == end.astimezone(timezone.utc)
                and {a.get('email', '').lower() for a in attendees} == {attendee_email.lower()}
                and all(a.get('responseStatus') != 'declined' for a in attendees)
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            matches = False
        if not matches:
            raise GoogleError('Calendar event is missing, changed, cancelled or declined; booking is not confirmed')
        result={'ok':True,'event_id':event['id'],'event_link':event.get('htmlLink'),'start':event['start'],'end':event['end'],'timezone':str(TZ),'invitation_requested':True,'attendee_email':attendee_email}
        db.execute("INSERT INTO alli_google_action(id,status,result) VALUES(%s,'done',%s) ON CONFLICT(id) DO UPDATE SET status='done',result=EXCLUDED.result",(event_id,Jsonb(result)))
        return result


def mail(recipient_email, subject, body, confirmed=False):
    if not confirmed: raise ValueError('Confirm the recipient and intent before sending email')
    email_address(recipient_email)
    if not subject.strip() or not body.strip() or '\n' in subject or '\r' in subject: raise ValueError('Valid subject and nonempty body required')
    key='mail-'+hashlib.sha256(json.dumps([recipient_email.lower(),subject,body]).encode()).hexdigest()
    with store.action_lock() as db:
        existing=db.execute('SELECT status,result FROM alli_google_action WHERE id=%s',(key,)).fetchone()
        if existing:
            if existing[0]=='done': return existing[1]
            return {'ok':False,'status':'unknown','error':'An identical send was already attempted; delivery is uncertain. Do not resend or claim success.'}
        db.execute("INSERT INTO alli_google_action(id,status) VALUES(%s,'pending')",(key,))
        db.commit() # Retain pending if process or network fails after Gmail accepts the send.
        msg=EmailMessage()
        msg['To']=recipient_email
        msg['Subject']=subject
        msg.set_content(body)
        result=api('POST','gmail/v1/users/me/messages/send',body={'raw':base64.urlsafe_b64encode(msg.as_bytes()).decode()})
        if not result.get('id'): raise GoogleError('Gmail did not confirm sending')
        answer={'ok':True,'message_id':result['id'],'recipient_email':recipient_email}
        db.execute("UPDATE alli_google_action SET status='done',result=%s WHERE id=%s",(Jsonb(answer),key))
        return answer


async def safely(fn,*args):
    try:
        return await asyncio.to_thread(fn,*args)
    except (ValueError,GoogleError) as e:
        return {'ok':False,'error':str(e)}
    except Exception:
        return {'ok':False,'error':'Google action could not be verified. Do not claim success or blindly retry a send.'}


@function_tool()
async def check_calendar(start_datetime: str, end_datetime: str, duration_minutes: int=30) -> dict:
    """Check John's real primary-calendar busy intervals and suggest meeting slots. ISO datetime inputs; naive values are Eastern. Always America/New_York."""
    return await safely(availability,start_datetime,end_datetime,duration_minutes)


@function_tool()
async def create_meeting(attendee_name: str, attendee_email: str, start_datetime: str, duration_minutes: int, title: str, context: str='', confirmed: bool=False) -> dict:
    """Create a real meeting and send the attendee a Calendar invitation ONLY after they agree to the time. Set confirmed only after agreement. Claim success only when ok=true with event_id."""
    return await safely(meeting,attendee_name,attendee_email,start_datetime,duration_minutes,title,context,confirmed)


@function_tool()
async def send_email(recipient_email: str, subject: str, body: str, confirmed: bool=False) -> dict:
    """Send a real Gmail message after recipient and purpose are confirmed. Use business-card email when provided. Claim sent only when ok=true with message_id. Do not retry uncertain sends."""
    return await safely(mail,recipient_email,subject,body,confirmed)


@router.get('/google/status')
def status(x_demo_key: str=Header(default='')):
    require_admin(x_demo_key)
    try:
        store.initialize()
        creds=fresh_credentials()
        now=datetime.now(TZ)
        calendar=availability(now.isoformat(),(now+timedelta(hours=24)).isoformat())
        # Gmail send-only scope cannot read the inbox/profile. Verify granted scope
        # via Google's token metadata, without sending any message.
        with httpx.Client(timeout=15) as client:
            tokeninfo=client.get('https://oauth2.googleapis.com/tokeninfo',params={'access_token':creds.token})
        info=tokeninfo.json() if tokeninfo.is_success else {}
        gmail_ok='https://www.googleapis.com/auth/gmail.send' in info.get('scope','').split()
        return {'ok':calendar['ok'] and gmail_ok,'durable_token':True,'refresh_verified':True,'calendar_read_verified':calendar['ok'],'gmail_send_scope_verified':gmail_ok,'email_sent_during_check':False,'timezone':str(TZ)}
    except Exception:
        raise HTTPException(503,'Google connection not ready; check configuration and reconnect') from None


@router.post('/google/self-test')
def calendar_self_test(x_demo_key: str=Header(default='')):
    """Admin-only, transparent test event with no attendees; always attempt cleanup."""
    require_admin(x_demo_key)
    event_id = 'allitest' + secrets.token_hex(16)
    path = 'calendar/v3/calendars/primary/events/' + event_id
    start = (datetime.now(TZ) + timedelta(days=7)).replace(hour=3,minute=0,second=0,microsecond=0)
    attempted = False
    try:
        attempted = True
        created = api('POST','calendar/v3/calendars/primary/events',body={
            'id':event_id,'summary':'Alli integration check — automatic cleanup',
            'description':'Private integration test. No attendees and no invitations.',
            'transparency':'transparent','reminders':{'useDefault':False},
            'start':{'dateTime':start.isoformat(),'timeZone':str(TZ)},
            'end':{'dateTime':(start+timedelta(minutes=5)).isoformat(),'timeZone':str(TZ)}},params={'sendUpdates':'none'})
        verified = api('GET',path)
        if created.get('id') != event_id or verified.get('id') != event_id:
            raise GoogleError('Test creation not verified')
    except Exception:
        raise HTTPException(503,'Calendar self-test failed; cleanup was attempted') from None
    finally:
        if attempted:
            try:
                api('DELETE',path,params={'sendUpdates':'none'})
            except GoogleError as e:
                if 'HTTP 404' not in str(e) and 'HTTP 410' not in str(e):
                    raise HTTPException(503, 'Test cleanup needs attention; event ID: '+event_id) from None
    try:
        deleted = api('GET',path)
        cleanup_ok = deleted.get('status') == 'cancelled'
    except GoogleError as e:
        cleanup_ok = 'HTTP 404' in str(e) or 'HTTP 410' in str(e)
    if not cleanup_ok:
        raise HTTPException(503,'Test event cleanup could not be verified; event ID: '+event_id)
    return {'ok':True,'create_verified':True,'read_verified':True,'cleanup_verified':True,'attendees':0,'emails_sent':0,'event_id':event_id}
