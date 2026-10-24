"""Durable admission quotas and contact blocking. No model can raise these limits."""
from datetime import datetime,timezone
import os
import communication_store as store

TRUSTED=('owner_saved_contact','apple_favorite','owner_supplied_mission')
class Limited(ValueError):pass

def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS alli_security_usage (
      kind text NOT NULL,reference text NOT NULL,phone text NOT NULL,units integer NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(kind,reference))''')
    db.execute('CREATE INDEX IF NOT EXISTS alli_security_usage_day ON alli_security_usage(kind,created_at)')
    db.execute('''CREATE TABLE IF NOT EXISTS alli_security_block (
      phone text PRIMARY KEY,reason text NOT NULL DEFAULT '',created_at timestamptz NOT NULL DEFAULT now())''')

def owner(phone):
    p=store.normalize_phone(os.getenv('ALLI_OWNER_PHONE',''))
    return bool(p and store.normalize_phone(phone)==p)

def tier(db,phone):
    if owner(phone):return 'owner'
    row=db.execute('SELECT source FROM alli_contact_name WHERE phone=%s',(phone,)).fetchone()
    return 'friend' if row and row[0] in TRUSTED else 'unknown'

# Global/day, per sender/day (owner, friend, unknown), global/minute.
LIMITS={
 'inbound_sms':(300,(100,40,2),40),
 'inbound_voice':(80,(20,10,2),10),
 'voice_seconds':(7200,(3600,1800,240),7200),
 'outbound_voice':(20,(20,0,0),5),
 'outbound_sms':(400,(120,40,2),60),
 'outbound_segments':(1600,(960,120,8),240),
 'approval_alert':(20,(20,1,1),5),
 'model_call':(250,(120,25,6),25),
 'model_output':(750000,(400000,80000,32000),200000),
 'calendar_write':(60,(30,4,0),10),
 'reminder_write':(40,(20,3,0),8),
 'cloud_job':(40,(20,5,0),10),
 'email_send':(40,(30,0,0),10),
 'account_write':(100,(60,0,0),20),
}

def reserve_in_db(db,kind,phone,reference,units=1):
    if kind not in LIMITS or not isinstance(units,int) or units<1:raise ValueError('Invalid usage reservation')
    db.execute('SELECT pg_advisory_xact_lock(301849630201)')
    if db.execute('SELECT 1 FROM alli_security_block WHERE phone=%s',(phone,)).fetchone():raise Limited('This number is blocked')
    if db.execute('SELECT 1 FROM alli_security_usage WHERE kind=%s AND reference=%s',(kind,reference)).fetchone():return
    role=tier(db,phone);total,per_sender,burst=LIMITS[kind]
    cap=per_sender[('owner','friend','unknown').index(role)]
    day,sender,minute=db.execute('''SELECT coalesce(sum(units),0),
      coalesce(sum(units) FILTER(WHERE phone=%s),0),
      coalesce(sum(units) FILTER(WHERE created_at>now()-interval '1 minute'),0)
      FROM alli_security_usage WHERE kind=%s AND created_at>=date_trunc('day',now() AT TIME ZONE 'America/New_York') AT TIME ZONE 'America/New_York' ''',(phone,kind)).fetchone()
    if day+units>total or sender+units>cap or minute+units>burst:raise Limited('Alli has reached a temporary usage limit; try later or ask John to review')
    if role=='unknown' and kind in ('inbound_sms','inbound_voice'):
        unknown=db.execute('''SELECT count(*) FROM alli_security_usage u WHERE kind=%s
          AND phone<>%s AND created_at>now()-interval '1 day' AND NOT EXISTS(
          SELECT 1 FROM alli_contact_name c WHERE c.phone=u.phone AND c.source=ANY(%s))''',(kind,store.normalize_phone(os.getenv('ALLI_OWNER_PHONE','')) or '',list(TRUSTED))).fetchone()[0]
        if unknown>=(20 if kind=='inbound_sms' else 5):raise Limited('New-contact traffic is temporarily limited')
    db.execute('INSERT INTO alli_security_usage(kind,reference,phone,units) VALUES(%s,%s,%s,%s)',(kind,reference,phone,units))

def reserve(kind,phone,reference,units=1):
    store.initialize()
    if kind=='reminder_write':reference=phone+':'+reference
    try:
        with store.connection() as db:reserve_in_db(db,kind,store.normalize_phone(phone) or phone,reference,units)
    except Limited:
        with store.connection() as db:
            bucket=str(int(__import__('time').time())//900)
            store.notify(db,'limit:'+kind+':'+phone+':'+bucket,dict(source='security_limit',phone=phone,status='limited',follow_up_needed=True,summary='Usage ceiling stopped '+kind.replace('_',' ')))
        raise

def model(phone,reference,max_output):
    store.initialize()
    with store.connection() as db:
        reserve_in_db(db,'model_call',phone,reference)
        reserve_in_db(db,'model_output',phone,reference,max_output)

def reserve_sms(phone,reference,text):
    # Conservative UTF-16 multipart estimate, including emoji. The carrier may
    # encode plain GSM text more efficiently; the budget never assumes it will.
    units=len(text.encode('utf-16-be'))//2
    segments=1 if units<=70 else (units+66)//67
    store.initialize()
    with store.connection() as db:
        reserve_in_db(db,'outbound_sms',phone,reference)
        reserve_in_db(db,'outbound_segments',phone,reference,segments)

def settle_model(reference,actual):
    if actual is None:return
    with store.connection() as db:
        db.execute("UPDATE alli_security_usage SET units=least(units,%s) WHERE kind='model_output' AND reference=%s",(max(0,int(actual)),reference))

def finish_voice(reference,seconds):
    with store.connection() as db:
        db.execute("UPDATE alli_security_usage SET units=least(units,%s) WHERE kind='voice_seconds' AND reference=%s",(max(1,int(seconds)),reference))

def extend_voice(phone,reference,target):
    with store.connection() as db:
        db.execute('SELECT pg_advisory_xact_lock(301849630201)')
        row=db.execute("SELECT units FROM alli_security_usage WHERE kind='voice_seconds' AND reference=%s AND phone=%s",(reference,phone)).fetchone()
        if not row:raise Limited('No active call budget')
        if row[0]>=target:return row[0]
        reserve_in_db(db,'voice_seconds',phone,reference+':extension',target-row[0])
        db.execute("UPDATE alli_security_usage SET units=%s WHERE kind='voice_seconds' AND reference=%s",(target,reference))
        db.execute("DELETE FROM alli_security_usage WHERE kind='voice_seconds' AND reference=%s",(reference+':extension',))
        return target

def settle_work(wid,tokens):
    with store.connection() as db:
        row=db.execute("SELECT reference FROM alli_security_usage WHERE kind='model_output' AND reference LIKE %s ORDER BY created_at DESC LIMIT 1",('work:'+wid+':%',)).fetchone()
    if row:settle_model(row[0],tokens)

def blocked(phone):
    store.initialize()
    with store.connection() as db:return bool(db.execute('SELECT 1 FROM alli_security_block WHERE phone=%s',(phone,)).fetchone())

def set_block(phone,enabled,reason=''):
    phone=store.normalize_phone(phone)
    if not phone or owner(phone):raise ValueError('Choose a contact number, not John’s owner number')
    with store.connection() as db:
        if enabled:db.execute('INSERT INTO alli_security_block(phone,reason) VALUES(%s,%s) ON CONFLICT(phone) DO UPDATE SET reason=EXCLUDED.reason',(phone,reason[:300]))
        else:
            db.execute('DELETE FROM alli_security_block WHERE phone=%s',(phone,))
            db.execute('UPDATE alli_contact_name SET deleted_at=NULL,updated_at=now() WHERE phone=%s',(phone,))
        if enabled:
            db.execute("UPDATE alli_inbound SET context=jsonb_set(context,'{followthrough_suppressed}','true') WHERE phone=%s AND channel='voice'",(phone,))
            db.execute("UPDATE alli_work SET state='cancelled',error='Contact suspended by John',updated_at=now() WHERE phone=%s AND state IN ('queued','running','waiting','needs_john')",(phone,))
            db.execute("UPDATE alli_outbound_sms SET status='cancelled',updated_at=now() WHERE phone=%s AND status='scheduled'",(phone,))
        store.notify(db,'contact-security:'+phone+':'+str(__import__('time').time_ns()),dict(source='contact_security',phone=phone,status='suspended' if enabled else 'reactivated',summary='John '+('suspended' if enabled else 'reactivated')+' this contact'))

def delete_contact(phone):
    # A tombstone prevents old call missions or past approvals from restoring
    # access. Activity records remain available to the owner.
    set_block(phone,True,'Contact deleted by John')
    phone=store.normalize_phone(phone)
    with store.connection() as db:
        db.execute('UPDATE alli_contact_name SET deleted_at=now(),updated_at=now() WHERE phone=%s',(phone,))
        db.execute("UPDATE alli_contact_approval SET decision='denied' WHERE phone=%s",(phone,))
        store.notify(db,'contact-deleted:'+phone+':'+str(__import__('time').time_ns()),dict(source='contact_security',phone=phone,status='deleted',summary='John moved this contact to Deleted; history retained and access suspended'))

def calendar_write(phone,reference):
    store.initialize()
    reference=phone+':'+reference
    with store.connection() as db:
        reserve_in_db(db,'calendar_write',phone,reference)
        if not owner(phone):
            count=db.execute("SELECT count(*) FROM alli_security_usage WHERE kind='calendar_write' AND phone=%s AND created_at>now()-interval '7 days'",(phone,)).fetchone()[0]
            if count>8:raise Limited('This contact has reached the limit of 8 calendar changes in 7 days; John can review the request')

def snapshot():
    with store.connection() as db:
        blocks=db.execute('SELECT phone,reason FROM alli_security_block ORDER BY created_at DESC').fetchall()
        usage=db.execute("SELECT kind,sum(units) FROM alli_security_usage WHERE created_at>now()-interval '24 hours' GROUP BY kind").fetchall()
    return dict(blocked=[dict(phone=p,reason=r) for p,r in blocks],usage=dict(usage),limits=LIMITS)
