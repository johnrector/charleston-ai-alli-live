"""Durable inbound queue, shared history and append-only owner update feed."""
import json
from threading import Lock
from psycopg.types.json import Jsonb
from google_store import connection
import appointment_store

_lock = Lock()
_ready = False

def initialize():
    global _ready
    if _ready: return
    with _lock:
        if _ready: return
        appointment_store.initialize()
        with connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(301849630153)')
            db.execute('''CREATE TABLE IF NOT EXISTS alli_inbound (
                sid text PRIMARY KEY, phone text NOT NULL, channel text NOT NULL,
                status text NOT NULL, context jsonb NOT NULL, body text NOT NULL DEFAULT '',
                reply text, delivery_sid text, report jsonb, transcript jsonb,
                created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())''')
            db.execute('CREATE INDEX IF NOT EXISTS alli_inbound_phone ON alli_inbound(phone, created_at DESC)')
            db.execute('''CREATE TABLE IF NOT EXISTS alli_communication_update (
                id bigserial PRIMARY KEY, dedupe text UNIQUE NOT NULL, payload jsonb NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now())''')
            db.execute('''CREATE TABLE IF NOT EXISTS alli_return_identity (
                phone text NOT NULL, request_id text NOT NULL, verified_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY(phone,request_id))''')
        _ready = True

def notify(db, key, payload):
    # Serialize feed commits so cursor consumers cannot skip a late lower ID.
    db.execute('SELECT pg_advisory_xact_lock(301849630154)')
    db.execute('INSERT INTO alli_communication_update(dedupe,payload) VALUES(%s,%s) ON CONFLICT(dedupe) DO NOTHING', (key, Jsonb(payload)))

def updates(after_id=0, limit=30):
    initialize()
    with connection() as db:
        rows = db.execute('SELECT id,payload,created_at FROM alli_communication_update WHERE id>%s ORDER BY id LIMIT %s', (after_id, limit)).fetchall()
    return {'updates':[dict(id=r[0], **r[1], created_at=r[2].isoformat()) for r in rows], 'next_cursor':rows[-1][0] if rows else after_id}

def context_for(phone):
    initialize()
    with connection() as db:
        rows = db.execute("""SELECT context,outcome FROM alli_appointment_dispatch
            WHERE context->>'phone'=%s AND status<>'blocked' AND created_at>now()-interval '30 days'
            ORDER BY created_at DESC LIMIT 3""", (phone,)).fetchall()
        # Ambiguous identities on a shared number must not reveal either person's record.
        names = {r[0].get('recipient_name','').strip().casefold() for r in rows}
        matched = rows[0][0] if rows and len(names)==1 else None
        history = db.execute("""SELECT channel,body,reply,report,transcript FROM alli_inbound
            WHERE phone=%s AND created_at>now()-interval '7 days' ORDER BY created_at DESC LIMIT 12""", (phone,)).fetchall() if matched else []
    return {'mission':matched, 'previous_outcome': rows[0][1] if matched else None,
            'history':[dict(zip(('channel','body','reply','report','transcript'), r)) for r in reversed(history)]}

def receive(sid, phone, channel, body='', context=None):
    initialize()
    with connection() as db:
        row = db.execute('''INSERT INTO alli_inbound(sid,phone,channel,status,body,context)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(sid) DO NOTHING RETURNING sid''',
            (sid,phone,channel,'queued' if channel=='sms' else 'active',body,Jsonb(context or {}))).fetchone()
    return bool(row)

def get(sid):
    initialize()
    with connection() as db:
        row = db.execute('SELECT sid,phone,channel,status,body,context,reply,report FROM alli_inbound WHERE sid=%s',(sid,)).fetchone()
    return dict(zip(('sid','phone','channel','status','body','context','reply','report'),row)) if row else None

def claim_sms():
    initialize()
    with connection() as db:
        db.execute('SELECT pg_advisory_xact_lock(301849630155)')
        row = db.execute("""SELECT sid FROM alli_inbound queued WHERE channel='sms' AND status='queued'
            AND NOT EXISTS (SELECT 1 FROM alli_inbound busy WHERE busy.phone=queued.phone
                AND busy.status IN ('processing','sending'))
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not row:return None
        db.execute("UPDATE alli_inbound SET status='processing',updated_at=now() WHERE sid=%s",row)
    return get(row[0])

def recover_stale():
    initialize()
    with connection() as db:
        rows = db.execute("""UPDATE alli_inbound SET status='uncertain',updated_at=now()
            WHERE status IN ('processing','sending') AND updated_at<now()-interval '10 minutes'
            RETURNING sid,phone""").fetchall()
        for sid,phone in rows:
            notify(db,sid+':uncertain',dict(sid=sid,phone=phone,channel='sms',status='uncertain',follow_up_needed=True,
                summary='Processing was interrupted. Review before retrying; a calendar/email action or reply may already have completed.'))

def identity(phone, request_id, save=False):
    initialize()
    with connection() as db:
        if save:
            db.execute('''INSERT INTO alli_return_identity(phone,request_id) VALUES(%s,%s)
                ON CONFLICT(phone,request_id) DO UPDATE SET verified_at=now()''',(phone,request_id))
            return True
        return db.execute("SELECT 1 FROM alli_return_identity WHERE phone=%s AND request_id=%s AND verified_at>now()-interval '24 hours'",(phone,request_id)).fetchone() is not None

def report(sid, value):
    initialize()
    with connection() as db:
        row=db.execute('UPDATE alli_inbound SET report=%s,updated_at=now() WHERE sid=%s RETURNING phone,channel',(Jsonb(value),sid)).fetchone()
        if row:notify(db,sid+':report',dict(sid=sid,phone=row[0],channel=row[1],**value))
    return bool(row)

def finish(sid,status,reply=None,delivery_sid=None,transcript=None):
    initialize()
    with connection() as db:
        row=db.execute('''UPDATE alli_inbound SET status=CASE WHEN status IN ('delivered','undelivered','failed') AND %s='submitted' THEN status ELSE %s END,reply=COALESCE(%s,reply),delivery_sid=COALESCE(%s,delivery_sid),
            transcript=COALESCE(%s,transcript),updated_at=now() WHERE sid=%s RETURNING phone,channel,report,body,reply''',
            (status,status,reply,delivery_sid,Jsonb(transcript) if transcript is not None else None,sid)).fetchone()
        if row and status not in ('sending','active'):
            notify(db,sid+':'+status,dict(sid=sid,phone=row[0],channel=row[1],status=status,
                report=row[2],received_text=row[3],reply=row[4],follow_up_needed=status in ('failed','uncertain','fallback') or not row[2],
                summary='Inbound interaction '+status))

def delivery(sid, message_sid, status):
    initialize()
    with connection() as db:
        row=db.execute('''UPDATE alli_inbound SET status=%s,delivery_sid=COALESCE(delivery_sid,%s),updated_at=now() WHERE sid=%s AND (delivery_sid=%s OR delivery_sid IS NULL)
            AND status IN ('submitted','sending') RETURNING phone''',(status,message_sid,sid,message_sid)).fetchone()
        if row and status in ('undelivered','failed'):
            notify(db,sid+':delivery',dict(sid=sid,phone=row[0],channel='sms',status=status,follow_up_needed=True,
                summary='The SMS reply could not be delivered.'))
