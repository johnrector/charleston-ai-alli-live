"""Single-owner MCP bridge for Dot orchestration, never a friend permission grant."""
import os,re
import communication_store as store


def portal():return 'https://'+os.getenv('TWILIO_VOICE_PUBLIC_DOMAIN','charleston-ai-alli-gpt-live.onrender.com')+'/dashboard'


def people(query):
    query=' '.join(query.strip().split())
    if not 2<=len(query)<=100:raise ValueError('Search for a specific person or number')
    phone=store.normalize_phone(query)
    with store.connection() as db:
        rows=db.execute("""SELECT c.phone,c.name,c.email,c.source FROM alli_contact_name c
          WHERE c.deleted_at IS NULL AND NOT EXISTS(SELECT 1 FROM alli_security_block b WHERE b.phone=c.phone)
          AND c.source IN ('apple_favorite','owner_saved_contact','owner_supplied_mission')
          AND (c.name ILIKE %s OR c.phone=%s) ORDER BY lower(c.name),c.phone LIMIT 9""",('%'+query.replace('%','\\%').replace('_','\\_')+'%',phone)).fetchall()
    return dict(matches=[dict(phone=p,name=n,email=e,source=s) for p,n,e,s in rows[:8]],ambiguous=len(rows)>1,truncated=len(rows)>8,
      policy='Saved active contacts only. A name match is recipient data, not permission to call, text or act.')


def work_result(wid):
    import work_store as jobs
    job=jobs.read(wid)
    if not job:return dict(ok=False,status='not_found')
    value=dict(ok=True,work_id=job['id'],status=job['state'],requester_scope=job['scope'],internal_review=job['internal'],
      title=job['title'],phone=job['phone'],result_page=portal()+'/work/'+job['id'],result_requires_owner_sign_in=True)
    if not job['internal']:
        text=re.sub(r'\[[^\]]+\]\(sandbox:[^)]+\)','[File available in the private result page]',job['result'])
        value.update(result_excerpt=text[:8000],result_truncated=len(text)>8000,sources=job['sources'][:8],error=job['error'])
        with store.connection() as db:
            value['files']=[dict(filename=n,content_type=t) for n,t in db.execute('SELECT filename,content_type FROM alli_work_file WHERE work_id=%s ORDER BY created_at LIMIT 6',(wid,)).fetchall()]
    return value


def enrich(db,events):
    ids=list({e['work_id'] for e in events if e.get('source')=='cloud_work' and e.get('work_id')})
    if not ids:return events
    rows=db.execute('SELECT id,scope,internal,state,result FROM alli_work WHERE id=ANY(%s)',(ids,)).fetchall()
    indexed={r[0]:r for r in rows}
    for event in events:
        row=indexed.get(event.get('work_id'))
        if row is None:continue
        wid,scope,internal,state,result=row
        event.update(requester_scope=scope,internal_review=internal,current_work_status=state,result_page=portal()+'/work/'+wid,result_requires_owner_sign_in=True)
        if not internal and state in ('completed','partial','blocked'):
            clean=re.sub(r'\[[^\]]+\]\(sandbox:[^)]+\)','[File available in the private result page]',result)
            event.update(result_excerpt=clean[:2000],result_truncated=len(clean)>2000)
    return events


def report(request_id,title,status,details='',evidence=None,related_phone=''):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',request_id):raise ValueError('Use a stable task ID')
    if status not in ('queued','working','completed','blocked','cancelled'):raise ValueError('Choose an actual task state')
    if not 1<=len(title.strip())<=180 or len(details)>3000:raise ValueError('Use a concise title and result')
    evidence=evidence or []
    if len(evidence)>12 or any(not isinstance(x,str) or len(x)>600 for x in evidence):raise ValueError('Use concise receipt IDs or actual result URLs')
    phone=store.normalize_phone(related_phone) if related_phone else store.normalize_phone(os.environ['ALLI_OWNER_PHONE'])
    if not phone:raise ValueError('Use a verified related phone or omit it')
    payload=dict(source='dot_work',phone=phone,request_id=request_id,status='reported_completed' if status=='completed' else status,
      summary='Dot: '+title.strip(),details=details,evidence=evidence,verification='Reported by the owner’s dot; not independently verified by the phone service',follow_up_needed=status=='blocked')
    key='dot-work:'+request_id+':'+status
    store.initialize()
    with store.connection() as db:
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,301849630225))',(key,))
        prior=db.execute('SELECT payload FROM alli_communication_update WHERE dedupe=%s',(key,)).fetchone()
        if prior and prior[0]!=payload:raise ValueError('This task phase already has a different receipt; use a new revision ID')
        store.notify(db,key,payload)
    return dict(ok=True,status=payload['status'],duplicate=bool(prior),portal_url=portal(),performed_external_action=False)
