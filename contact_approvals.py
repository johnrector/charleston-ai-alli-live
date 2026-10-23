"""Durable new-contact notifications and deterministic owner SMS decisions."""
import os
import re
import secrets
import communication_store as store

TRUSTED = ('owner_saved_contact', 'owner_supplied_mission', 'apple_favorite')


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS alli_contact_approval (
        phone text PRIMARY KEY, code text UNIQUE NOT NULL, inbound_sid text NOT NULL,
        decision text NOT NULL DEFAULT 'pending', notification_status text NOT NULL DEFAULT 'queued',
        notification_owner text, notification_text text, message_sid text,
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())''')


def enqueue(db, sid, phone, channel):
    owner = store.normalize_phone(os.getenv('ALLI_OWNER_PHONE', ''))
    phone = store.normalize_phone(phone)
    if not owner or not phone or phone == owner or channel not in ('voice', 'sms'):
        return
    db.execute('''INSERT INTO alli_contact_approval(phone,code,inbound_sid)
        SELECT %s,%s,%s WHERE NOT EXISTS (
            SELECT 1 FROM alli_contact_name WHERE phone=%s AND source=ANY(%s))
        AND NOT EXISTS (SELECT 1 FROM alli_appointment_dispatch
            WHERE context->>'phone'=%s AND status<>'blocked'
            GROUP BY context->>'phone'
            HAVING count(DISTINCT lower(trim(context->>'recipient_name')))=1
                AND min(length(trim(coalesce(context->>'recipient_name','')))) BETWEEN 1 AND 100)
        ON CONFLICT(phone) DO NOTHING''', (phone, secrets.token_hex(4).upper(), sid, phone, list(TRUSTED),phone))


def alert_text(code, phone, channel, name='', summary=''):
    # Quoted caller text never participates in decision parsing.
    clean = lambda value, limit: ' '.join(str(value or '').split())[:limit]
    who = f"{clean(name, 80)} ({phone})" if name else phone
    detail = f' They said: "{clean(summary, 200)}"' if summary else ''
    return (f'Alli: New {"caller" if channel == "voice" else "texter"}: {who}.{detail}\n'
            f'Reply APPROVE {code} to save and approve them for scheduling, or DENY {code}. '
            f'You can add a name: APPROVE {code} Jane Doe. No calendar access until approved.')


def claim():
    owner = store.normalize_phone(os.getenv('ALLI_OWNER_PHONE', ''))
    if not owner:
        return None
    store.initialize()
    with store.connection() as db:
        # Only one dispatcher may cross the persist-before-send boundary.
        db.execute('SELECT pg_advisory_xact_lock(301849630157)')
        expired = db.execute("""UPDATE alli_contact_approval SET notification_status='uncertain',updated_at=now()
            WHERE notification_status='sending' AND updated_at<now()-interval '10 minutes' RETURNING code,phone""").fetchall()
        for code, phone in expired:
            store.notify(db, 'contact-approval:'+code+':uncertain', dict(channel='owner_notification',
                status='uncertain', phone=phone, follow_up_needed=True, summary='New-contact approval alert may not have arrived. Review before retrying.'))
        db.execute("""UPDATE alli_contact_approval a SET decision='approved',notification_status='skipped',updated_at=now()
            WHERE decision='pending' AND notification_status='queued' AND EXISTS (
            SELECT 1 FROM alli_contact_name c WHERE c.phone=a.phone AND c.source=ANY(%s))""", (list(TRUSTED),))
        row = db.execute("""SELECT a.code,a.phone,i.channel,c.name,i.body,i.report
            FROM alli_contact_approval a JOIN alli_inbound i ON i.sid=a.inbound_sid
            LEFT JOIN alli_contact_name c ON c.phone=a.phone
            WHERE a.decision='pending' AND a.notification_status='queued'
              AND (i.status NOT IN ('queued','processing','sending','active') OR i.created_at<now()-interval '2 minutes')
            ORDER BY a.created_at LIMIT 1 FOR UPDATE OF a SKIP LOCKED""").fetchone()
        if not row:
            return None
        code, phone, channel, name, body, report = row
        summary = body if channel == 'sms' else (report or {}).get('summary', '')
        text = alert_text(code, phone, channel, name, summary)
        db.execute("""UPDATE alli_contact_approval SET notification_status='sending',notification_owner=%s,
            notification_text=%s,updated_at=now() WHERE code=%s""", (owner, text, code))
    return dict(code=code, owner=owner, body=text)


def sent(code, message_sid=None, uncertain=False):
    store.initialize()
    with store.connection() as db:
        db.execute("""UPDATE alli_contact_approval SET notification_status=%s,message_sid=coalesce(message_sid,%s),updated_at=now()
            WHERE code=%s AND notification_status='sending'""", ('uncertain' if uncertain else 'submitted', message_sid, code))
        if uncertain:
            store.notify(db, 'contact-approval:'+code+':uncertain', dict(channel='owner_notification',
                status='uncertain', follow_up_needed=True, summary='New-contact approval SMS send is uncertain. Do not resend blindly.'))


def delivery(code, message_sid, status, recipient):
    if status not in ('delivered', 'undelivered', 'failed'):
        return
    store.initialize()
    with store.connection() as db:
        row = db.execute("""UPDATE alli_contact_approval SET notification_status=%s,message_sid=%s,updated_at=now()
            WHERE code=%s AND notification_owner=%s AND (message_sid=%s OR message_sid IS NULL)
              AND notification_status IN ('sending','submitted','uncertain') RETURNING phone""",
                         (status, message_sid, code, recipient, message_sid)).fetchone()
        if row and status != 'delivered':
            store.notify(db, 'contact-approval:'+code+':delivery', dict(channel='owner_notification',
                status=status, phone=row[0], follow_up_needed=True, summary='New-contact approval alert was not delivered.'))


def command(sender, text):
    """Only exact commands in the current signed owner SMS may decide a request."""
    owner = store.normalize_phone(os.getenv('ALLI_OWNER_PHONE', ''))
    if not owner or store.normalize_phone(sender) != owner:
        return None
    if not re.match(r'^\s*(?:APPROVE|DENY)\b', text, re.I):
        return None
    match = re.fullmatch(r'\s*(APPROVE|DENY)\s+([A-F0-9]{8})(?:\s+([^\r\n]{1,100}))?\s*', text, re.I)
    if not match or (match[1].upper() == 'DENY' and match[3]):
        return 'Use APPROVE followed by the 8-character code (and optional name), or DENY followed by the code.'
    action, code, name = match[1].upper(), match[2].upper(), ' '.join((match[3] or '').split())
    if any(c in name for c in '<>'):
        return 'Please supply a plain contact name without angle brackets.'
    store.initialize()
    with store.connection() as db:
        row = db.execute('SELECT phone,decision FROM alli_contact_approval WHERE code=%s FOR UPDATE', (code,)).fetchone()
        if not row:
            return 'I could not find that approval code. Please use the code from my new-contact text.'
        phone, decision = row
        if decision != 'pending':
            return f'That request for {phone} is already {decision}. No changes made.'
        existing=db.execute('SELECT source FROM alli_contact_name WHERE phone=%s',(phone,)).fetchone()
        if action == 'DENY' and existing and existing[0] in TRUSTED:
            db.execute("UPDATE alli_contact_approval SET decision='approved',updated_at=now() WHERE code=%s",(code,))
            return f'{phone} was already saved separately. This old request cannot revoke an approved contact.'
        if action == 'APPROVE':
            db.execute("""INSERT INTO alli_contact_name(phone,name,source) VALUES(%s,%s,'owner_saved_contact')
                ON CONFLICT(phone) DO UPDATE SET source='owner_saved_contact',
                name=coalesce(nullif(EXCLUDED.name,''),alli_contact_name.name),updated_at=now()""", (phone, name))
            saved = db.execute('SELECT name,source FROM alli_contact_name WHERE phone=%s', (phone,)).fetchone()
            if not saved or saved[1] != 'owner_saved_contact':
                raise RuntimeError('Contact approval could not be verified')
            answer = f'Approved and saved {saved[0] or phone}. They can now check availability and arrange their own meetings with you by call or text.'
            if not saved[0]:
                answer += " I'll ask their name before booking."
        else:
            answer = f'Declined {phone}. They remain unable to access your calendar or book. I will not send you repeat approval alerts for this number.'
        decision = 'approved' if action == 'APPROVE' else 'denied'
        db.execute('UPDATE alli_contact_approval SET decision=%s,updated_at=now() WHERE code=%s', (decision, code))
        store.notify(db, 'contact-approval:'+code+':decision', dict(channel='contact_approval', phone=phone,
            status=decision, summary=answer, follow_up_needed=False))
    return answer
