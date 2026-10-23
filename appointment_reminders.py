"""Calendar-driven reminders for uniquely matched, owner-approved contacts."""
import asyncio
import contextlib
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Header
from pydantic import BaseModel
import communication_store as store
import google_integration as google
import scheduled_sms as sms

TZ = ZoneInfo('America/New_York')
log = logging.getLogger('alli.reminders')
POLICY = dict(lead_minutes=120, quiet_start_hour=8, quiet_end_hour=20,
              scan_seconds=300, channel='sms', contacts='approved_invitation_email_or_verified_booking')


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS alli_assistant_policy (
        key text PRIMARY KEY, enabled boolean NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())''')
    db.execute('''CREATE TABLE IF NOT EXISTS alli_contact_preference (
        phone text PRIMARY KEY, reminders_enabled boolean NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now())''')
    db.execute("""CREATE INDEX IF NOT EXISTS alli_booking_receipt_event ON alli_communication_update
        ((payload->'result'->>'event_id')) WHERE payload->>'source'='business_tool_result'
        AND payload->>'action'='create_meeting'""")


def settings(enabled=None):
    store.initialize()
    with store.connection() as db:
        if enabled is not None:
            db.execute("INSERT INTO alli_assistant_policy(key,enabled) VALUES('appointment_reminders',%s) ON CONFLICT(key) DO UPDATE SET enabled=EXCLUDED.enabled,updated_at=now()", (enabled,))
        row = db.execute("SELECT enabled FROM alli_assistant_policy WHERE key='appointment_reminders'").fetchone()
    return dict(enabled=bool(row and row[0]), **POLICY)


def preference_command(phone, body):
    command = body.strip().upper()
    if command not in {'STOP REMINDERS', 'REMINDERS OFF', 'START REMINDERS', 'REMINDERS ON'}: return None
    phone = store.normalize_phone(phone)
    if not phone: return None
    enabled = command in {'START REMINDERS', 'REMINDERS ON'}
    store.initialize()
    with store.connection() as db:
        db.execute('''INSERT INTO alli_contact_preference(phone,reminders_enabled) VALUES(%s,%s)
            ON CONFLICT(phone) DO UPDATE SET reminders_enabled=EXCLUDED.reminders_enabled,updated_at=now()''', (phone,enabled))
    return ('Appointment reminders are on for your number if you have an approved meeting with John.' if enabled
            else "Appointment reminders are off for your number. You can still text me whenever you need help. Reply START REMINDERS to turn them back on.")


def approved_people():
    store.initialize()
    with store.connection() as db:
        rows = db.execute('''SELECT c.phone,c.name,lower(trim(c.email)) FROM alli_contact_name c
            LEFT JOIN alli_contact_preference p ON p.phone=c.phone
            WHERE c.source IN ('owner_saved_contact','owner_supplied_mission','apple_favorite')
                AND coalesce(p.reminders_enabled,true)
                AND (c.source<>'owner_supplied_mission' OR
                    (SELECT count(DISTINCT lower(trim(d.context->>'recipient_name')))
                     FROM alli_appointment_dispatch d WHERE d.context->>'phone'=c.phone
                         AND d.status<>'blocked')<=1)''').fetchall()
    return [dict(phone=phone,name=name,email=email) for phone,name,email in rows]


def contacts():
    by_email = {}
    for person in approved_people():
        if person['email']: by_email.setdefault(person['email'], []).append(person)
    # Multiple numbers for one email require an owner choice; never blast them all.
    return {email: people[0] for email, people in by_email.items() if len(people)==1}


def booked_contacts(event_ids):
    """A verified Alli booking binds this event only, never a person's wider email identity."""
    people = {person['phone']:person for person in approved_people()}
    with store.connection() as db:
        rows = db.execute("""SELECT payload->'result'->>'event_id',payload->>'phone',
                lower(payload->'result'->>'attendee_email') FROM alli_communication_update
            WHERE payload->>'source'='business_tool_result' AND payload->>'action'='create_meeting'
                AND payload->'result'->>'ok'='true' AND payload->'result'->>'attendee_email'<>''
                AND payload->'result'->>'event_id'=ANY(%s)
            ORDER BY id DESC""", (event_ids,)).fetchall()
    bindings = {}
    for event_id,phone,email in rows:
        if event_id and phone in people:
            bindings.setdefault((event_id,email),{})[phone] = dict(people[phone],email=email,booking_receipt=True)
    result = {}
    for (event_id,_), people_by_phone in bindings.items():
        if len(people_by_phone)==1: result.setdefault(event_id,[]).extend(people_by_phone.values())
    return result


def event_start(event):
    value = event.get('start',{}).get('dateTime')
    if not value: return None
    return google.local_time(value).astimezone(TZ)


def due_time(start):
    due = start - timedelta(minutes=POLICY['lead_minutes'])
    if due.hour < POLICY['quiet_start_hour']:
        due = (due-timedelta(days=1)).replace(hour=18,minute=0,second=0,microsecond=0)
    elif due.hour >= POLICY['quiet_end_hour']:
        due = due.replace(hour=19,minute=0,second=0,microsecond=0)
    return due


def eligible(event, email):
    if event.get('status') != 'confirmed' or event.get('attendeesOmitted') or event.get('transparency') == 'transparent': return False
    people = event.get('attendees',[])
    # Remind only for John's own events or invitations he has accepted.
    owner_present = event.get('organizer',{}).get('self') or any(p.get('self') and p.get('responseStatus')=='accepted' for p in people)
    return bool(owner_present and any(str(p.get('email','')).casefold()==email and not p.get('self') and p.get('responseStatus')!='declined' for p in people))


def snapshot(event, person):
    return hashlib.sha256(json.dumps(dict(event_id=event['id'], start=event.get('start'),
        end=event.get('end'), location=event.get('location',''),
        email=person['email'], phone=person['phone']),sort_keys=True).encode()).hexdigest()


def reminder_message(event, person):
    start = event_start(event)
    name = person['name'].strip().split()[0] if person['name'].strip() else 'there'
    when = start.strftime('%A, %B %d at %I:%M %p').replace(' 0',' ')
    location = ' '.join(str(event.get('location','')).split())[:220]
    return (f"Hi {name}, it's Alli, John's AI assistant. A reminder that you're meeting John {when} Eastern. "
            + (f'Location: {location}. ' if location else '')
            + 'Still good for you? Need directions or have any questions? Reply here. Text STOP REMINDERS to turn off appointment reminders.')


def upcoming(now):
    params = dict(timeMin=now.isoformat(), timeMax=(now+timedelta(hours=36)).isoformat(),
                  singleEvents=True, orderBy='startTime', maxResults=250)
    events = []
    for _ in range(5):
        page = google.api('GET','calendar/v3/calendars/primary/events',params=params)
        events.extend(page.get('items',[]))
        token = page.get('nextPageToken')
        if not token: return events
        params['pageToken'] = token
    raise ValueError('Calendar scan incomplete; no reminders dispatched')


def candidates(now):
    events = upcoming(now)
    directory = contacts()
    bookings = booked_contacts([event['id'] for event in events])
    result = []
    for event in events:
        start = event_start(event)
        if not start or start <= now: continue
        due = due_time(start)
        if not due <= now <= due + timedelta(minutes=15): continue
        participants = {person['phone']:person for person in directory.values() if eligible(event,person['email'])}
        for person in bookings.get(event['id'],[]): participants[person['phone']] = person
        for person in participants.values():
            email = person['email']
            if not eligible(event,email): continue
            identity = hashlib.sha256(json.dumps([event['id'],person['phone']]).encode()).hexdigest()
            result.append(dict(request_id='appointment-reminder-'+identity, phone=person['phone'],
                recipient_name=person['name'] or 'Approved contact', message=reminder_message(event,person),
                send_at=due.isoformat(), event_id=event['id'], email=email, snapshot=snapshot(event,person),
                booking_receipt=person.get('booking_receipt',False)))
    return result


def revalidate(context, phone):
    if not settings()['enabled']: return False
    if context.get('booking_receipt'):
        person = next((p for p in booked_contacts([context['event_id']]).get(context['event_id'],[])
                       if p['phone']==phone and p['email']==context.get('email')),None)
    else: person = contacts().get(context.get('email'))
    if not person or person['phone']!=phone: return False
    event = google.api('GET','calendar/v3/calendars/primary/events/'+quote(context['event_id'],safe=''))
    start = event_start(event)
    now = datetime.now(TZ)
    return bool(start and start>now and eligible(event,person['email'])
                and snapshot(event,person)==context.get('snapshot')
                and due_time(start)<=now<=due_time(start)+timedelta(minutes=15))


class AppointmentReminders:
    def __init__(self):
        self.wake = asyncio.Event()
        self.ready = False
        self.last_scan = None
        self.last_error = None

    async def preview(self):
        now = datetime.now(TZ)
        items = await asyncio.to_thread(candidates,now)
        return dict(policy=await asyncio.to_thread(settings), checked_at=now.isoformat(),
                    candidates=items, last_scan=self.last_scan, last_error=self.last_error, no_messages_sent=True)

    async def run_once(self):
        policy = await asyncio.to_thread(settings)
        if not policy['enabled']: return dict(status='disabled',scheduled=[])
        now = datetime.now(TZ)
        items = await asyncio.to_thread(candidates,now)
        scheduled = []
        for item in items:
            prior = await asyncio.to_thread(sms.get,item['request_id'])
            if prior['status']!='not_found': continue
            context = dict(kind='appointment_reminder',event_id=item['event_id'],email=item['email'],snapshot=item['snapshot'])
            if item.get('booking_receipt'): context['booking_receipt'] = True
            if not await asyncio.to_thread(revalidate,context,item['phone']): continue
            body = sms.TextMessage(**{key:item[key] for key in ('request_id','phone','recipient_name','message')})
            scheduled.append(await asyncio.to_thread(sms.enqueue,body,context=context))
        self.last_scan = now.isoformat()
        self.last_error = None
        return dict(status='checked',scheduled=scheduled)

    async def configure(self, enabled):
        result = await asyncio.to_thread(settings,enabled)
        self.wake.set()
        return result

    async def loop(self):
        while True:
            self.wake.clear()
            try: await self.run_once()
            except asyncio.CancelledError: raise
            except Exception:
                self.last_error = 'Calendar reminder scan could not be verified; no unverified reminders were sent.'
                log.warning('appointment_reminder_scan_failed')
                try: await asyncio.to_thread(scan_error, self.last_error)
                except Exception: pass
            try: await asyncio.wait_for(self.wake.wait(),timeout=POLICY['scan_seconds'])
            except asyncio.TimeoutError: pass

    def install(self, app):
        class Setting(BaseModel):
            enabled: bool

        @app.get('/appointment-reminders')
        async def preview(x_demo_key: str=Header(default='')):
            google.require_admin(x_demo_key)
            return await self.preview()

        @app.post('/appointment-reminders')
        async def configure(body: Setting, x_demo_key: str=Header(default='')):
            google.require_admin(x_demo_key)
            return await self.configure(body.enabled)

        original = app.router.lifespan_context
        @asynccontextmanager
        async def lifespan(app):
            async with original(app):
                self.ready = True
                worker = asyncio.create_task(self.loop())
                try: yield
                finally:
                    self.ready = False
                    worker.cancel()
                    with contextlib.suppress(asyncio.CancelledError): await worker
        app.router.lifespan_context = lifespan


def scan_error(message):
    with store.connection() as db:
        store.notify(db,'appointment-reminder-scan:'+datetime.now(TZ).date().isoformat(),
            dict(channel='system',status='reminder_scan_failed',follow_up_needed=True,summary=message))
