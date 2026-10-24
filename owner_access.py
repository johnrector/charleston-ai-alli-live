"""Possession checks: 12-hour SMS access; voice authorization bound to one CallSid."""
import hashlib,hmac,os,re,secrets
from datetime import datetime,timezone
import communication_store as store
import security_controls as security

def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS alli_owner_access (
      channel text NOT NULL,binding text NOT NULL,expires_at timestamptz NOT NULL,
      PRIMARY KEY(channel,binding))''')
    db.execute('''CREATE TABLE IF NOT EXISTS alli_owner_challenge (
      id text PRIMARY KEY,channel text NOT NULL,sid text NOT NULL UNIQUE,digest text NOT NULL,
      attempts integer NOT NULL DEFAULT 0,consumed boolean NOT NULL DEFAULT false,
      expires_at timestamptz NOT NULL,created_at timestamptz NOT NULL DEFAULT now())''')
    db.execute('ALTER TABLE alli_inbound ADD COLUMN IF NOT EXISTS owner_verified boolean NOT NULL DEFAULT false')

def code_digest(cid,code):
    return hmac.new(os.environ['DEMO_KEY'].encode(),('owner-session:'+cid+':'+code).encode(),hashlib.sha256).hexdigest()

def authorized(phone,channel,sid=''):
    if not security.owner(phone):return False
    try:
        store.initialize()
        with store.connection() as db:
            return bool(db.execute('SELECT 1 FROM alli_owner_access WHERE channel=%s AND binding=%s AND expires_at>now()',
              (channel,phone if channel=='sms' else sid)).fetchone())
    except Exception:return False

def grant_sms(db):
    phone=store.normalize_phone(os.environ['ALLI_OWNER_PHONE'])
    db.execute("INSERT INTO alli_owner_access(channel,binding,expires_at) VALUES('sms',%s,now()+interval '12 hours') ON CONFLICT(channel,binding) DO UPDATE SET expires_at=EXCLUDED.expires_at",(phone,))

def request(phone,channel,sid,preview=''):
    if not security.owner(phone):return dict(ok=False,error='Owner access unavailable')
    if authorized(phone,channel,sid):return dict(ok=True,verified=True)
    import scheduled_sms as sms
    store.initialize()
    with store.connection() as db:
        db.execute('SELECT pg_advisory_xact_lock(301849630202)')
        prior=db.execute('SELECT id FROM alli_owner_challenge WHERE sid=%s AND NOT consumed AND expires_at>now()',(sid,)).fetchone()
        if not prior:
            count=db.execute("SELECT count(*) FROM alli_owner_challenge WHERE created_at>now()-interval '15 minutes'").fetchone()[0]
            if count>=3:return dict(ok=False,verification_required=True,message='A verification is already pending on John’s phone. Use that code or wait 15 minutes.')
            cid=secrets.token_hex(12);code=f'{secrets.randbelow(1000000):06}'
            db.execute("INSERT INTO alli_owner_challenge(id,channel,sid,digest,expires_at) VALUES(%s,%s,%s,%s,now()+interval '10 minutes') ON CONFLICT(sid) DO UPDATE SET id=EXCLUDED.id,digest=EXCLUDED.digest,attempts=0,consumed=false,expires_at=EXCLUDED.expires_at,created_at=now()",(cid,channel,sid,code_digest(cid,code)))
            description='this private call only' if channel=='voice' else 'owner texting for 12 hours'
            summary=' '.join(preview.split())[:180]
            text=f'Alli owner verification for {description}. Reply UNLOCK {code} to Alli. Expires in 10 minutes.'
            if summary:text+='\nRequested: '+summary
            text+='\nOnly unlock if YOU initiated this. Never give this code to another caller or website.'
            sms.enqueue_in_db(db,sms.TextMessage(request_id='owner-auth:'+cid,phone=phone,recipient_name='John Rector',message=text),context=dict(kind='owner_auth'))
    return dict(ok=False,verification_required=True,message='For private owner access, reply UNLOCK followed by the code I texted your configured phone. This verifies this call once, or a 12-hour texting session. Routine tasks then continue without further approvals.')

def command(phone,body):
    if not security.owner(phone) or not re.match(r'^\s*UNLOCK\b',body,re.I):return None
    match=re.fullmatch(r'\s*UNLOCK\s+(\d{6})\s*',body,re.I)
    if not match:return 'Reply UNLOCK followed by the six-digit code from Alli’s owner verification text.'
    store.initialize();verified=None
    with store.connection() as db:
        db.execute('SELECT pg_advisory_xact_lock(301849630202)')
        rows=db.execute('SELECT id,channel,sid,digest,attempts FROM alli_owner_challenge WHERE NOT consumed AND expires_at>now() ORDER BY created_at DESC FOR UPDATE').fetchall()
        for cid,channel,sid,digest,attempts in rows:
            if attempts>=5:continue
            db.execute('UPDATE alli_owner_challenge SET attempts=attempts+1 WHERE id=%s',(cid,))
            if hmac.compare_digest(digest,code_digest(cid,match[1])):
                db.execute('UPDATE alli_owner_challenge SET consumed=true WHERE id=%s',(cid,))
                if channel=='sms':
                    grant_sms(db)
                    # Resume only the request named in this possession challenge.
                    db.execute("UPDATE alli_inbound SET status='queued',reply=NULL,delivery_sid=NULL,owner_verified=true,updated_at=now() WHERE sid=%s AND channel='sms' AND status IN ('submitted','delivered','sending','undelivered')",(sid,))
                else:
                    active=db.execute("SELECT 1 FROM alli_inbound WHERE sid=%s AND channel='voice' AND status='active'",(sid,)).fetchone()
                    if not active:return 'That call has ended. Call Alli again for a new call-specific check.'
                    db.execute('UPDATE alli_inbound SET owner_verified=true WHERE sid=%s',(sid,))
                    db.execute("INSERT INTO alli_owner_access(channel,binding,expires_at) VALUES('voice',%s,now()+interval '30 minutes') ON CONFLICT(channel,binding) DO UPDATE SET expires_at=EXCLUDED.expires_at",(sid,))
                    grant_sms(db)
                verified=channel;break
    if not verified:return 'That code is incorrect, expired, or already used. Use the latest verification text.'
    return 'This call is verified. Tell Alli “continue” on the call.' if verified=='voice' else 'Owner texting is verified for 12 hours. Your requested work will continue automatically.'

def mark_sms(sid):
    with store.connection() as db:db.execute('UPDATE alli_inbound SET owner_verified=true WHERE sid=%s',(sid,))

def reuse_dashboard_proof(db):
    phone=store.normalize_phone(os.getenv('ALLI_OWNER_PHONE',''))
    if phone:db.execute("""INSERT INTO alli_owner_access(channel,binding,expires_at)
      SELECT 'sms',%s,max(expires_at) FROM alli_dashboard_session WHERE expires_at>now() HAVING count(*)>0
      ON CONFLICT(channel,binding) DO NOTHING""",(phone,))

def end_call(sid):
    with store.connection() as db:db.execute("DELETE FROM alli_owner_access WHERE channel='voice' AND binding=%s",(sid,))

def redact(text):
    return re.sub(r'(?i)\bUNLOCK\s+\d{6}\b','UNLOCK [redacted]',text)
