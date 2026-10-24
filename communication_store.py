"""Durable inbound queue, shared history and append-only owner update feed."""
import json
import os
import phonenumbers
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
            db.execute('''CREATE TABLE IF NOT EXISTS alli_contact_name (phone text PRIMARY KEY, name text NOT NULL, source text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())''')
            db.execute("ALTER TABLE alli_contact_name ADD COLUMN IF NOT EXISTS email text NOT NULL DEFAULT ''")
            from contact_approvals import initialize as initialize_approvals
            initialize_approvals(db)
            from scheduled_sms import initialize as initialize_sms
            initialize_sms(db)
            from appointment_reminders import initialize as initialize_reminders
            initialize_reminders(db)
            from promises import initialize as initialize_promises
            initialize_promises(db)
            from dashboard import initialize as initialize_dashboard
            initialize_dashboard(db)
            from work_store import initialize as initialize_work
            initialize_work(db)
            from cloud_runtime import initialize as initialize_runtime
            initialize_runtime(db)
            # Preserve owner-supplied contacts independently of mission expiry. Shared
            # numbers with conflicting names are deliberately not auto-approved.
            db.execute(r"""INSERT INTO alli_contact_name(phone,name,source,email)
                SELECT context->>'phone', min(context->>'recipient_name'), 'owner_supplied_mission',
                    CASE WHEN count(DISTINCT lower(nullif(context->>'email','')))=1
                         THEN coalesce(min(nullif(context->>'email','')),'') ELSE '' END
                FROM alli_appointment_dispatch WHERE status<>'blocked'
                    AND context->>'phone' ~ '^\+[1-9][0-9]{7,14}$'
                    AND length(trim(context->>'recipient_name')) BETWEEN 1 AND 100
                GROUP BY context->>'phone' HAVING count(DISTINCT lower(trim(context->>'recipient_name')))=1
                ON CONFLICT(phone) DO UPDATE SET name=EXCLUDED.name,source=EXCLUDED.source,email=EXCLUDED.email
                WHERE alli_contact_name.source='self_introduced'""")
        _ready = True

def notify(db, key, payload):
    # Serialize feed commits so cursor consumers cannot skip a late lower ID.
    db.execute('SELECT pg_advisory_xact_lock(301849630154)')
    db.execute('INSERT INTO alli_communication_update(dedupe,payload) VALUES(%s,%s) ON CONFLICT(dedupe) DO NOTHING', (key, Jsonb(payload)))

def updates(after_id=0, limit=30):
    from google_integration import CONNECTION_STATUS
    initialize()
    with connection() as db:
        rows = db.execute('SELECT id,payload,created_at FROM alli_communication_update WHERE id>%s ORDER BY id LIMIT %s', (after_id, limit)).fetchall()
        counts=db.execute('SELECT source,count(*) FROM alli_contact_name GROUP BY source').fetchall()
    return {'updates':[dict(id=r[0], **r[1], created_at=r[2].isoformat()) for r in rows], 'next_cursor':rows[-1][0] if rows else after_id,
            'contact_directory':{'policy':'saved-contact-v1','counts_by_source':dict(counts)},
            'google_connection':dict(CONNECTION_STATUS)}

def normalize_phone(value):
    try:
        parsed=phonenumbers.parse(value, 'US')
        if not phonenumbers.is_possible_number(parsed):return None
        return phonenumbers.format_number(parsed,phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:return None

def remember_name(phone, name):
    """Greeting preference only. Never grants owner status or business permissions."""
    phone=normalize_phone(phone)
    name=' '.join(name.strip().split())
    if not phone or not 1<=len(name)<=100 or any(c in name for c in '\n<>'):
        return False
    initialize()
    with connection() as db:
        db.execute("""INSERT INTO alli_contact_name(phone,name,source) VALUES(%s,%s,'self_introduced')
            ON CONFLICT(phone) DO UPDATE SET name=EXCLUDED.name,
                source=CASE WHEN alli_contact_name.source IN ('apple_favorite','owner_saved_contact') THEN alli_contact_name.source ELSE EXCLUDED.source END,
                updated_at=now()
            WHERE alli_contact_name.source='self_introduced'
                OR (alli_contact_name.source IN ('apple_favorite','owner_saved_contact') AND alli_contact_name.name='')""",(phone,name))
    return True

def context_for(phone):
    initialize()
    phone=normalize_phone(phone)
    if not phone:return {'mission':None,'history':[],'contact':None}
    owner=phone==normalize_phone(os.getenv('ALLI_OWNER_PHONE',''))
    with connection() as db:
        rows = db.execute("""SELECT context,outcome FROM alli_appointment_dispatch
            WHERE context->>'phone'=%s AND status<>'blocked' AND created_at>now()-interval '30 days'
            ORDER BY created_at DESC LIMIT 3""", (phone,)).fetchall()
        greeting_rows=db.execute("""SELECT context FROM alli_appointment_dispatch
            WHERE context->>'phone'=%s AND status<>'blocked' ORDER BY created_at DESC""",(phone,)).fetchall()
        names = {r[0].get('recipient_name','').strip().casefold() for r in greeting_rows}
        matched = rows[0][0] if rows and len(names)==1 and not owner else None
        contact=db.execute('SELECT name,source,email FROM alli_contact_name WHERE phone=%s',(phone,)).fetchone()
        if owner:contact=('John Rector','owner_configured')
        elif contact and contact[1] in ('owner_saved_contact','apple_favorite'):pass
        elif len(names)>1:contact=None
        elif greeting_rows and len(names)==1:
            contact=(greeting_rows[0][0]['recipient_name'],'owner_supplied_mission',greeting_rows[0][0].get('email',''))
        history = db.execute("""SELECT channel,body,reply,report,transcript FROM (
            SELECT channel,body,reply,report,transcript,created_at FROM alli_inbound
                WHERE phone=%s AND created_at>now()-interval '7 days'
            UNION ALL
            SELECT 'sms','',body,NULL::jsonb,NULL::jsonb,send_at FROM alli_outbound_sms
                WHERE phone=%s AND send_at>now()-interval '7 days' AND request_id NOT LIKE 'dashboard-login:%%'
                AND status IN ('submitted','queued','sent','delivered')
            ) recent ORDER BY created_at DESC LIMIT 12""", (phone,phone)).fetchall()
    return {'mission':matched, 'previous_outcome': rows[0][1] if matched else None,
            'contact':dict(name=contact[0],source=contact[1],is_owner=owner,
                email=contact[2] if len(contact)>2 else '') if contact else None,
            'history':[dict(zip(('channel','body','reply','report','transcript'), r)) for r in reversed(history)]}

def save_contact(phone, name, email=''):
    """Owner-only callers of this function maintain the standing SMS directory."""
    phone=normalize_phone(phone)
    name=' '.join(name.strip().split())
    if not phone or not 1<=len(name)<=100 or any(c in name for c in '<>'):
        raise ValueError('Supply a valid phone number and contact name')
    if email:
        from google_integration import email_address
        email_address(email)
    initialize()
    with connection() as db:
        db.execute("""INSERT INTO alli_contact_name(phone,name,source,email)
            VALUES(%s,%s,'owner_saved_contact',%s) ON CONFLICT(phone) DO UPDATE SET
            name=EXCLUDED.name,source=EXCLUDED.source,email=EXCLUDED.email,updated_at=now()""",(phone,name,email))
    return {'ok':True,'phone':phone,'name':name,'email':email}

def import_apple_favorites(entries):
    """Merge an owner-authorized UI/export snapshot; no background Mac access."""
    if not isinstance(entries,list) or not 1<=len(entries)<=5000:
        raise ValueError('Supply a nonempty Favorites snapshot of at most 5000 entries')
    from google_integration import email_address
    prepared={}
    for entry in entries:
        phone=normalize_phone(entry.get('phone',''))
        name=' '.join(entry.get('name','').strip().split())
        email=entry.get('email','')
        if not phone or len(name)>100 or any(c in name for c in '<>'):
            raise ValueError('Favorites need valid phone numbers and optional names up to 100 characters')
        if email:email_address(email)
        previous=prepared.get(phone)
        if previous and previous['name'] and name and previous['name'].casefold()!=name.casefold():
            raise ValueError('A favorite number has conflicting names; resolve it before import')
        prepared[phone]={'name':name or (previous or {}).get('name',''),'email':email or (previous or {}).get('email','')}
    initialize()
    with connection() as db:
        db.execute('SELECT pg_advisory_xact_lock(301849630156)')
        for phone,entry in prepared.items():
            db.execute("""INSERT INTO alli_contact_name(phone,name,source,email)
                VALUES(%s,%s,'apple_favorite',%s) ON CONFLICT(phone) DO UPDATE SET
                name=CASE WHEN alli_contact_name.source='owner_saved_contact' THEN alli_contact_name.name
                          ELSE coalesce(nullif(EXCLUDED.name,''),alli_contact_name.name) END,
                email=CASE WHEN alli_contact_name.source='owner_saved_contact' AND alli_contact_name.email<>'' THEN alli_contact_name.email
                           ELSE coalesce(nullif(EXCLUDED.email,''),alli_contact_name.email) END,
                source=CASE WHEN alli_contact_name.source='owner_saved_contact' THEN 'owner_saved_contact' ELSE 'apple_favorite' END,
                updated_at=now()""",(phone,entry['name'],entry['email']))
        rows=db.execute('SELECT phone,name,source,email FROM alli_contact_name WHERE phone=ANY(%s)',(list(prepared),)).fetchall()
        if {r[0] for r in rows}!=set(prepared) or any(r[2] not in ('apple_favorite','owner_saved_contact') for r in rows):
            raise ValueError('Favorites import could not be verified')
    return {'ok':True,'imported_numbers':len(rows),'with_name':sum(bool(r[1]) for r in rows),
            'without_name':sum(not bool(r[1]) for r in rows),'automatic_sync':False}

def receive(sid, phone, channel, body='', context=None):
    initialize()
    with connection() as db:
        row = db.execute('''INSERT INTO alli_inbound(sid,phone,channel,status,body,context)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(sid) DO NOTHING RETURNING sid''',
            (sid,phone,channel,'queued' if channel=='sms' else 'active',body,Jsonb(context or {}))).fetchone()
        if row:
            from contact_approvals import enqueue
            enqueue(db,sid,phone,channel)
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
            notify(db,sid+':'+('transcript' if transcript is not None else status),dict(sid=sid,phone=row[0],channel=row[1],status=status,
                report=row[2],received_text=row[3],reply=row[4],transcript=transcript,follow_up_needed=status in ('failed','uncertain','fallback') or (row[1]=='voice' and not row[2]),
                summary='Inbound interaction '+status))

def delivery(sid, message_sid, status):
    initialize()
    with connection() as db:
        row=db.execute('''UPDATE alli_inbound SET status=%s,delivery_sid=COALESCE(delivery_sid,%s),updated_at=now() WHERE sid=%s AND (delivery_sid=%s OR delivery_sid IS NULL)
            AND status IN ('submitted','sending') RETURNING phone''',(status,message_sid,sid,message_sid)).fetchone()
        if row and status in ('undelivered','failed'):
            notify(db,sid+':delivery',dict(sid=sid,phone=row[0],channel='sms',status=status,follow_up_needed=True,
                summary='The SMS reply could not be delivered.'))


def action_result(sid, action, result):
    """Preserve actual business-tool receipts separately from model summaries."""
    import hashlib
    initialize()
    value=json.dumps(result,sort_keys=True,default=str)
    with connection() as db:
        row=db.execute('SELECT phone,channel FROM alli_inbound WHERE sid=%s',(sid,)).fetchone()
        if row:
            notify(db,sid+':action:'+hashlib.sha256((action+value).encode()).hexdigest(),
                dict(sid=sid,phone=row[0],channel=row[1],source='business_tool_result',
                     action=action,result=result,follow_up_needed=result.get('ok') is not True))
