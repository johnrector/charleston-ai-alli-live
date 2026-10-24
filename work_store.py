"""Phone-bound durable cloud work, artifacts and append-only execution receipts."""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import communication_store as store
import scheduled_sms as sms

FIELDS = 'id,phone,contact_name,scope,title,instruction,state,response_id,result,sources,error,outbound_id,notify,link_revoked,private_result,internal,created_at,updated_at,available_at,completed_at'
STATES = ('queued','running','waiting','needs_john','completed','partial','blocked','cancelled')

def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS alli_work (
      id text PRIMARY KEY,dedupe text UNIQUE NOT NULL,phone text NOT NULL,contact_name text NOT NULL,
      scope text NOT NULL CHECK(scope IN ('friend','owner')),title text NOT NULL,instruction text NOT NULL,
      state text NOT NULL DEFAULT 'queued',response_id text,result text NOT NULL DEFAULT '',
      sources jsonb NOT NULL DEFAULT '[]',context jsonb NOT NULL DEFAULT '{}',error text NOT NULL DEFAULT '',
      approval jsonb,outbound_id text,notify boolean NOT NULL DEFAULT true,
      created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now())''')
    db.execute('CREATE INDEX IF NOT EXISTS alli_work_queue ON alli_work(state,created_at)')
    db.execute('ALTER TABLE alli_work ADD COLUMN IF NOT EXISTS link_revoked boolean NOT NULL DEFAULT false')
    db.execute('ALTER TABLE alli_work ADD COLUMN IF NOT EXISTS private_result boolean NOT NULL DEFAULT false')
    db.execute('ALTER TABLE alli_work ADD COLUMN IF NOT EXISTS internal boolean NOT NULL DEFAULT false')
    db.execute('ALTER TABLE alli_work ADD COLUMN IF NOT EXISTS available_at timestamptz NOT NULL DEFAULT now()')
    db.execute('ALTER TABLE alli_work ADD COLUMN IF NOT EXISTS completed_at timestamptz')
    db.execute('CREATE INDEX IF NOT EXISTS alli_work_phone_history ON alli_work(phone,created_at DESC)')
    db.execute('''CREATE TABLE IF NOT EXISTS alli_work_file (
      id text PRIMARY KEY,work_id text NOT NULL REFERENCES alli_work(id),filename text NOT NULL,
      content_type text NOT NULL,content bytea NOT NULL,created_at timestamptz NOT NULL DEFAULT now())''')
    db.execute('ALTER TABLE alli_work_file ADD COLUMN IF NOT EXISTS provider_key text')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS alli_work_file_provider ON alli_work_file(work_id,provider_key)')
    db.execute('''CREATE TABLE IF NOT EXISTS alli_work_event (
      id bigserial PRIMARY KEY,work_id text NOT NULL REFERENCES alli_work(id),kind text NOT NULL,
      details jsonb NOT NULL DEFAULT '{}',created_at timestamptz NOT NULL DEFAULT now())''')
    db.execute('''CREATE TABLE IF NOT EXISTS alli_work_preference (
      phone text NOT NULL,preference text NOT NULL,updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(phone,preference))''')
    db.execute('''CREATE TABLE IF NOT EXISTS alli_work_skill (
      id integer PRIMARY KEY CHECK(id=1),digest text NOT NULL,bundle bytea NOT NULL,
      catalog jsonb NOT NULL,provider_id text,provider_version text,
      updated_at timestamptz NOT NULL DEFAULT now())''')

def clean(value, limit, required=True):
    if not isinstance(value,str) or len(value)>limit or (required and not value.strip()):
        raise ValueError('Supply a nonempty value of at most '+str(limit)+' characters')
    return value.strip()

def owner(phone):
    configured=store.normalize_phone(os.getenv('ALLI_OWNER_PHONE',''))
    return bool(configured and store.normalize_phone(phone)==configured)

def allowed(db,phone):
    if db.execute('SELECT 1 FROM alli_security_block WHERE phone=%s',(phone,)).fetchone():return False
    if owner(phone):return True
    row=db.execute('SELECT source FROM alli_contact_name WHERE phone=%s',(phone,)).fetchone()
    return bool(row and row[0] in ('owner_saved_contact','apple_favorite','owner_supplied_mission'))

def event(db,wid,kind,details=None):
    db.execute('INSERT INTO alli_work_event(work_id,kind,details) VALUES(%s,%s,%s)',(wid,kind,Jsonb(details or {})))

def heartbeat(wid):
    with store.connection() as db:db.execute("UPDATE alli_work SET updated_at=now() WHERE id=%s AND state='running'",(wid,))

def serial(row):
    return {k:v.isoformat() if isinstance(v,datetime) else v for k,v in dict(row).items()}

def read(wid,phone=None,internal=False):
    store.initialize()
    with store.connection() as db,db.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT '+(FIELDS+',context,approval' if internal else FIELDS)+' FROM alli_work WHERE id=%s'+(' AND phone=%s' if phone else ''),(wid,phone) if phone else (wid,))
        row=cur.fetchone()
        return serial(row) if row else None

def listing(phone=None):
    store.initialize()
    with store.connection() as db,db.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT '+FIELDS+' FROM alli_work'+(' WHERE phone=%s AND NOT internal' if phone else '')+' ORDER BY created_at DESC LIMIT 100',(phone,) if phone else ())
        return [serial(r) for r in cur.fetchall()]

def create(phone,name,title,instruction,sid,*,owner_scope=False,notify=True,context=None,internal=False):
    phone=store.normalize_phone(phone)
    if not phone:raise ValueError('A verified sender number is required')
    if owner_scope and not owner(phone):raise ValueError('Owner tools are restricted to John')
    if internal and (owner_scope or not (context or {}).get('call_recovery_sid')):raise ValueError('Internal review requires a captured call and no account tools')
    title=clean(title,180);instruction=clean(instruction,6000);sid=clean(sid,180)
    dedupe=hashlib.sha256((phone+'|'+sid+'|'+instruction).encode()).hexdigest()
    store.initialize()
    with store.connection() as db:
        db.execute('SELECT pg_advisory_xact_lock(301849630180)')
        prior=db.execute('SELECT id FROM alli_work WHERE dedupe=%s',(dedupe,)).fetchone()
        if prior:return dict(ok=True,work_id=prior[0],duplicate=True,state=read(prior[0],phone)['state'])
        if not allowed(db,phone):raise ValueError("John must approve this number before cloud work can start")
        from security_controls import reserve_in_db
        if not internal:reserve_in_db(db,'cloud_job',phone,dedupe)
        active=db.execute("SELECT count(*) FROM alli_work WHERE phone=%s AND NOT internal AND state IN ('queued','running','waiting','needs_john')",(phone,)).fetchone()[0]
        today=db.execute("SELECT count(*) FROM alli_work WHERE phone=%s AND NOT internal AND created_at>now()-interval '24 hours'",(phone,)).fetchone()[0]
        if not internal and (active>=int(os.getenv('ALLI_WORK_ACTIVE_PER_CONTACT','3')) or today>=int(os.getenv('ALLI_WORK_DAILY_PER_CONTACT','10'))):
            raise ValueError('Your cloud work limit is reached. Finish existing work or ask John to review it.')
        total=db.execute("SELECT count(*) FROM alli_work WHERE NOT internal AND created_at>now()-interval '24 hours'").fetchone()[0]
        if not internal and total>=int(os.getenv('ALLI_WORK_DAILY_TOTAL','100')):raise ValueError('Cloud work is at capacity; John can review it')
        wid=secrets.token_hex(6).upper()
        db.execute('''INSERT INTO alli_work(id,dedupe,phone,contact_name,scope,title,instruction,context,notify,private_result,internal)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(wid,dedupe,phone,clean(name or 'Saved contact',100),
          'owner' if owner_scope else 'friend',title,instruction,Jsonb(context or {}),notify,
          bool(internal or owner_scope or (owner(phone) and ((context or {}).get('group_notes') or (context or {}).get('group_delegation') or (context or {}).get('source_sid')))),internal))
        event(db,wid,'queued')
        store.notify(db,'work:'+wid+':queued',dict(source='cloud_work',work_id=wid,phone=phone,status='queued',summary='Cloud work queued: '+title))
    return dict(ok=True,work_id=wid,state='queued',message='Saved to the cloud work queue. Work continues after this conversation ends. Not completed yet.')

def claim():
    store.initialize()
    with store.connection() as db:
        # A provider request accepted without a persisted ID is uncertain, never recreated.
        stale=db.execute("UPDATE alli_work SET state='blocked',error='Worker interrupted before a provider receipt was saved; review before retrying',updated_at=now() WHERE state='running' AND response_id IS NULL AND updated_at<now()-interval '10 minutes' RETURNING id,phone,contact_name,title,notify").fetchall()
        for wid,phone,name,title,notify in stale:
            event(db,wid,'blocked_before_provider_receipt')
            store.notify(db,'work:'+wid+':interrupted',dict(source='cloud_work',work_id=wid,status='blocked',follow_up_needed=True,summary='Interrupted cloud work needs review: '+title))
            if notify and allowed(db,phone):
                sms.enqueue_in_db(db,sms.TextMessage(request_id='work-interrupted:'+wid,phone=phone,recipient_name=name,
                  message='Alli: I hit an interruption with '+title+'. Your request is saved for review; I have not treated it as completed. Reply here for an update.'))
        # One cross-deploy worker owns a job; fresh running jobs are not re-claimed.
        db.execute('SELECT pg_advisory_xact_lock(301849630181)')
        row=db.execute("""SELECT id FROM alli_work WHERE available_at<=now() AND (state='queued' OR
          (state='running' AND response_id IS NOT NULL AND updated_at<now()-interval '2 minutes'))
          ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not row:return None
        db.execute("UPDATE alli_work SET state='running',updated_at=now() WHERE id=%s",row)
        store.notify(db,'work:'+row[0]+':started',dict(source='cloud_work',work_id=row[0],status='working',summary='Cloud work started'))
    return read(row[0],internal=True)

def update(wid,**values):
    permitted={'state','response_id','result','sources','error','approval'}
    if not values or not set(values)<=permitted:raise ValueError('Invalid work update')
    if 'state' in values and values['state'] not in STATES:raise ValueError('Invalid work state')
    with store.connection() as db:
        sql=','.join(k+'=%s' for k in values)
        db.execute('UPDATE alli_work SET '+sql+",updated_at=now() WHERE id=%s AND state<>'cancelled'",tuple(Jsonb(v) if k in ('sources','approval') and v is not None else v for k,v in values.items())+(wid,))

def result_token(job):
    return hmac.new(os.environ['DEMO_KEY'].encode(),('work-result:'+job['id']+':'+job['phone']).encode(),hashlib.sha256).hexdigest()

def result_url(job):
    return 'https://'+os.getenv('TWILIO_VOICE_PUBLIC_DOMAIN','charleston-ai-alli-gpt-live.onrender.com')+'/work/'+job['id']+'/'+result_token(job)

def valid_token(job,token):
    if not job or job.get('link_revoked') or not hmac.compare_digest(result_token(job),token):return False
    if __import__('security_controls').blocked(job['phone']):return False
    stamp=job.get('completed_at') or job['created_at']
    created=datetime.fromisoformat(stamp) if isinstance(stamp,str) else stamp
    return created>datetime.now(timezone.utc)-timedelta(days=7)

def cached_file(wid,key):
    with store.connection() as db:
        return bool(db.execute('SELECT 1 FROM alli_work_file WHERE work_id=%s AND provider_key=%s',(wid,key)).fetchone())

def cache_file(wid,key,filename,content):
    import mimetypes
    if len(content)>8*1024*1024:raise ValueError('File exceeds the 8 MB delivery limit')
    filename=os.path.basename(filename)[:120] or 'result.txt'
    with store.connection() as db:
        db.execute('SELECT pg_advisory_xact_lock(301849630188)')
        db.execute("DELETE FROM alli_work_file WHERE created_at<now()-interval '14 days'")
        if db.execute('SELECT 1 FROM alli_work_file WHERE work_id=%s AND provider_key=%s',(wid,key)).fetchone():return
        total=db.execute('SELECT coalesce(sum(octet_length(content)),0) FROM alli_work_file').fetchone()[0]
        per_job=db.execute('SELECT coalesce(sum(octet_length(content)),0) FROM alli_work_file WHERE work_id=%s',(wid,)).fetchone()[0]
        if total+len(content)>300*1024*1024 or per_job+len(content)>16*1024*1024:raise ValueError('Artifact storage requires John’s review')
        db.execute('''INSERT INTO alli_work_file(id,work_id,provider_key,filename,content_type,content)
          VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(work_id,provider_key) DO NOTHING''',
          (secrets.token_hex(8),wid,key,filename,mimetypes.guess_type(filename)[0] or 'application/octet-stream',content))

def finish(wid,result,sources,files,*,outcome='completed'):
    import mimetypes
    store.initialize()
    with store.connection() as db:
        job=db.execute('SELECT phone,contact_name,title,state,notify FROM alli_work WHERE id=%s FOR UPDATE',(wid,)).fetchone()
        if not job or job[3]=='cancelled':return
        if not allowed(db,job[0]):raise ValueError('Contact is no longer approved')
        db.execute("DELETE FROM alli_work_file WHERE created_at<now()-interval '14 days'")
        stored=db.execute('SELECT coalesce(sum(octet_length(content)),0) FROM alli_work_file').fetchone()[0]
        if stored+sum(len(f['content']) for f in files)>300*1024*1024:raise ValueError('Artifact storage requires John’s review')
        for f in files:
            filename=os.path.basename(f['filename'])[:120] or 'result.txt'
            db.execute('INSERT INTO alli_work_file(id,work_id,filename,content_type,content) VALUES(%s,%s,%s,%s,%s)',
              (secrets.token_hex(8),wid,filename,mimetypes.guess_type(filename)[0] or 'application/octet-stream',f['content']))
        if outcome not in ('completed','partial','blocked'):raise ValueError('Invalid final outcome')
        db.execute("UPDATE alli_work SET state=%s,result=%s,sources=%s,error='',completed_at=coalesce(completed_at,now()),updated_at=now() WHERE id=%s",(outcome,result[:60000],Jsonb(sources),wid))
        artifact_count=db.execute('SELECT count(*) FROM alli_work_file WHERE work_id=%s',(wid,)).fetchone()[0]
        event(db,wid,outcome,dict(artifact_count=artifact_count,source_count=len(sources)))
        if job[4]:
            row=dict(id=wid,phone=job[0]);link=result_url(row)
            lead={'completed':'Your work is ready','partial':'I finished part of your work; some steps still need attention','blocked':'Your work needs attention; I saved the details'}[outcome]
            message=f"Alli: {lead} — {job[2]}.\n{link}\nReply here with any changes you'd like. This result link expires in 7 days."
            rid='work-result:'+wid
            sms.enqueue_in_db(db,sms.TextMessage(request_id=rid,phone=job[0],recipient_name=job[1],message=message),context=dict(kind='cloud_work',work_id=wid))
            db.execute('UPDATE alli_work SET outbound_id=%s WHERE id=%s',(rid,wid))
        store.notify(db,'work:'+wid+':'+outcome,dict(source='cloud_work',work_id=wid,phone=job[0],status=outcome,follow_up_needed=outcome!='completed',summary='Cloud work '+outcome+': '+job[2]))

def cancel(wid,phone=None):
    with store.connection() as db:
        row=db.execute("SELECT outbound_id,state FROM alli_work WHERE id=%s"+(' AND phone=%s' if phone else '')+' FOR UPDATE',(wid,phone) if phone else (wid,)).fetchone()
        if not row:raise ValueError('Work not found for this person')
        if row[1]=='completed':raise ValueError('Work is complete; already sent results cannot be recalled')
        db.execute("UPDATE alli_work SET state='cancelled',updated_at=now() WHERE id=%s",(wid,))
        event(db,wid,'cancelled')
    return dict(ok=True,state='cancelled',work_id=wid)

def revalidate(context,phone):
    with store.connection() as db:
        row=db.execute("SELECT phone,state FROM alli_work WHERE id=%s",(context.get('work_id'),)).fetchone()
        return bool(row and row[0]==phone and row[1] in ('completed','partial','blocked') and allowed(db,phone))

def remember(phone,preference):
    preference=clean(preference,400)
    with store.connection() as db:
        if not allowed(db,phone):raise ValueError('Approved contact required')
        db.execute('INSERT INTO alli_work_preference(phone,preference) VALUES(%s,%s) ON CONFLICT(phone,preference) DO UPDATE SET updated_at=now()',(phone,preference))
    return dict(ok=True,source='explicit_contact_preference',permission_change=False)

def preferences(phone):
    with store.connection() as db:
        return [r[0] for r in db.execute('SELECT preference FROM alli_work_preference WHERE phone=%s ORDER BY updated_at DESC LIMIT 20',(phone,)).fetchall()]
