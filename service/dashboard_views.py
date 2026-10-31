"""Escaped, server-rendered oversight views. No scripts or new execution paths."""
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

from .web_helpers import escape as e


def badge(value):
    color='green' if value in {'delivered','completed','approved','passed'} else 'amber' if value in {
        'failed','partial','uncertain','needs_owner','blocked','attempting','creating','needs review','unconfirmed'} else 'blue' if value in {
        'queued','working','waiting','active','planned','accepted'} else ''
    return '<span class="badge '+color+'">'+e(value.replace('_',' '))+'</span>'


def when(value, zone):
    if value is None:return 'Time not recorded'
    if isinstance(value,(int,float)):value=datetime.fromtimestamp(value,timezone.utc)
    elif isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00'))
    return value.astimezone(ZoneInfo(zone)).strftime('%a, %b %-d · %-I:%M %p %Z')


def stamp(zone):
    return '<div class="topline"><span>'+e(when(datetime.now(timezone.utc),zone))+'</span><span><i class="dot"></i>Saved records · reload for updates</span></div>'


def title(name, description, zone):
    return stamp(zone)+'<header class="page-heading"><p class="eyebrow">Your executive assistant</p><h1>'+e(name)+'</h1><p class="intro">'+e(description)+'</p></header>'


def evidence(label, values):
    return '<details><summary>'+e(label)+'</summary><pre>'+e(json.dumps(values,default=str,indent=2,ensure_ascii=False))+'</pre></details>'


def upcoming(schedules, commitments, zone):
    rows=[]
    for row in schedules:
        if row['state'] not in {'creating','active','paused'}:continue
        timing=row['timing']
        at=when(timing['run_at'],zone) if timing['type']=='once' else 'Recurring · '+timing['expression']+' · '+timing['timezone']
        rows.append((timing.get('run_at',float('inf')),row['title'],at,row['state'],'Scheduled work'))
    for row in commitments:
        if row['state'] not in {'planned','queued','blocked'}:continue
        rows.append((row['due_at'].timestamp(),row['kind'].upper()+' · '+(row.get('person_name') or 'Saved recipient'),when(row['due_at'],zone),row['state'],'Saved commitment'))
    content=''
    for _,name,at,state,kind in sorted(rows,key=lambda r:r[0])[:8]:
        content+='<div class="list-row"><span class="row-icon" aria-hidden="true">◷</span><div><h3>'+e(name)+'</h3><p>'+e(at)+' · '+e(kind)+'</p></div>'+badge(state)+'</div>'
    return content or '<div class="empty"><div class="empty-icon" aria-hidden="true">◷</div><h3>No future work saved.</h3><p>A calendar appointment alone does not create a confirmation call.</p></div>'


def overview(summary, schedules, commitments, zone, release=''):
    active=[s for s in schedules if s['state'] in {'active','creating'}]
    queued=[c for c in summary['calls'] if c['direction']=='outbound' and c['state'] in {'queued','attempting','accepted','uncertain'}]
    content=title('A little clarity for your day.','Upcoming commitments, work to review, and the records behind every outcome.',zone)
    content+='<div class="metrics"><div class="metric"><div class="metric-label">Future assignments</div><strong>'+str(len(active))+'</strong><span>Active schedules in this view</span></div><div class="metric"><div class="metric-label">Call attempts</div><strong>'+str(len(queued))+'</strong><span>Pending or unconfirmed in this view</span></div><div class="metric"><div class="metric-label">Appointment calls</div><strong style="font-size:23px">Not configured</strong><span>Operating policy needed</span></div></div>'
    content+='<div class="columns"><div class="stack"><section class="panel"><div class="panel-head"><h2>Upcoming commitments</h2><a href="/dashboard/promises">View all</a></div>'+upcoming(schedules,commitments,zone)+'</section>'
    content+='<section class="panel"><div class="panel-head"><h2>Recent work to review</h2><a href="/dashboard/work">All work</a></div>'
    for task in summary['work']:
        content+='<div class="list-row"><span class="row-icon" aria-hidden="true">↗</span><a href="/dashboard/work/'+e(task['id'])+'"><h3>'+e(task['title'])+'</h3><p>'+e(when(task['updated_at'],zone))+'</p></a>'+badge(task['state'])+'</div>'
    if not summary['work']:content+='<p class="small">No pending or unsuccessful work in this view.</p>'
    content+='</section></div><aside class="stack"><section class="panel attention"><div class="attention-title"><span class="badge amber">Operating rule</span></div><h3>Appointment confirmation</h3><p>Decide which appointments need a call, when to call, and what Alli may agree to. No automatic confirmation calls are enabled.</p><div class="reason">The calendar records an appointment. A saved assignment authorizes future work.</div><a class="button-link" href="/dashboard/policies">Inspect operating rules →</a></section><section class="panel"><div class="panel-head"><h2>Latest AI check</h2><a href="/dashboard/health">Details</a></div>'
    diagnostic=summary['diagnostic']
    if diagnostic:
        result=diagnostic['result']
        state='AI checks passed' if result and result.get('ok') else 'Review saved result' if result else 'Unconfirmed attempt'
        content+='<h3>'+e(state)+'</h3><p class="small">'+e(when(diagnostic['started_at'],zone))+'</p><p class="small">AI dependency checks. This does not prove carrier delivery or future availability.</p>'
        if release and diagnostic['release'] and release!=diagnostic['release']:
            content+='<p class="small">This saved check belongs to an earlier application release.</p>'
    else:content+='<h3>No saved check yet.</h3><p class="small">Opening this page does not run a paid probe or contact anyone.</p>'
    return content+'</section></aside></div>'


def policies(data, zone):
    content=title('The rules behind the work.','Saved business knowledge, its source, and the boundaries Alli works within.',zone)
    content+='<section class="panel unconfigured"><div class="panel-head"><h2>Appointment confirmation</h2>'+badge('not configured')+'</div><p class="small">No calendar-to-call planner is enabled. No calls are activated by this page or by a saved example.</p><div class="draft-grid">'
    for label,value in [('Applies to','Which calendars, appointments and attendees qualify?'),('When','Call lead time, timezone and permissible hours are not set.'),('Mission','Confirm attendance and answer questions using verified appointment details.'),('Boundaries','Voicemail, retries, exclusions and authority to change arrangements need a decision.')]:
        content+='<div class="draft-item"><strong>'+e(label)+'</strong><p>'+e(value)+'</p></div>'
    content+='</div><p class="prototype-note">These are open decisions, not an active policy. Ask Alli to draft a concrete rule for review.</p></section><h2 class="section-heading">Saved operating guidance</h2>'
    for row in data['records']:
        body=row['body']; label=body.get('rule_key') or body.get('title') or row['collection']
        content+='<article class="panel"><div class="panel-head"><div><p class="eyebrow">'+e(body.get('business') or row['collection'])+'</p><h2>'+e(str(label).replace('_',' '))+'</h2></div><span class="badge outline">Revision '+e(row['revision'])+'</span></div><p class="small">Saved '+e(when(row['updated_at'],zone))+'. Guidance is evaluated by delegated owner-authorized work; it does not schedule itself.</p>'+evidence('Read policy, source and supersession',body)+'<p class="source">Record '+e(row['id'])+' · '+e(row['collection'])+'</p></article>'
    if not data['records']:content+='<section class="panel"><p class="small">No saved operating-rule records were found. General application permissions still apply.</p></section>'
    return content+'<section class="panel"><h2>How a rule becomes work</h2><div class="flow"><span>Owner instruction</span>→<span>Saved policy</span>→<span>Delegated work</span>→<span>Verified receipt</span></div><p class="small">A future assignment also needs an OpenAI native schedule. Rules cannot grant account access or restore canceled work.</p></section>'


def health(summary, release, zone):
    content=title('Health, with evidence.','The latest saved dependency check and what it actually verifies.',zone)
    row=summary['diagnostic']
    content+='<section class="panel"><div class="panel-head"><h2>Latest AI dependency diagnostic</h2>'
    if row:
        result=row['result']
        content+=badge('completed' if result and result.get('ok') else 'needs review' if result else 'unconfirmed')+'</div>'
        content+='<p class="small">Attempt started '+e(when(row['started_at'],zone))+'</p><p class="source">Checked release: <code>'+e(row['release'])+'</code></p>'
        if release and row['release'] and release!=row['release']:
            content+='<p class="callout amber">This saved check is from an earlier application release. It has not tested this release.</p>'
        if result:
            for name,check in result.get('checks',{}).items():
                content+='<div class="health-item"><div class="health-item-head"><h3>'+e({'voice':'GPT-Live connection','text':'Text response','agents':'Existing Agents session access'}.get(name,name))+'</h3>'+badge('passed' if check.get('ok') else 'needs review')+'</div>'+evidence('Read check evidence',check)+'</div>'
            content+='<p class="source">'+e(result.get('limits','Dependency checks only. Carrier delivery is not tested.'))+'</p>'+evidence('Full saved result',result)
        else:content+='<p class="small">An attempt was saved without a final result. Its outcome is unconfirmed; it will not be automatically repeated today.</p>'
    else:content+='<span class="badge outline">Not tested</span></div><p class="small">No saved diagnostic. This page never launches a paid check.</p>'
    content+='</section><section class="panel"><h2>What the system uses</h2><dl class="facts"><dt>Conversation</dt><dd>GPT-Live · fast, full-duplex voice</dd><dt>Work</dt><dd>Durable delegated Agents sessions</dd><dt>Future timing</dt><dd>OpenAI native Automations</dd><dt>Relationship records</dt><dd>Existing Postgres · source-linked owner-private notes</dd><dt>Application release</dt><dd><code>'+e(release or 'Not recorded')+'</code></dd></dl><hr class="divider"><a class="text-link" href="/dashboard/connections">Account connections →</a></section>'
    return content


def relationship(data,zone):
    content='<section class="panel"><div class="panel-head"><h2>Relationship context</h2><span class="badge outline">Owner-private</span></div><p class="small">Notes are worker observations with saved sources. They are not independent verification or new authority.</p>'
    if data['last_interaction']:content+='<p class="small">Last interaction: '+e(when(data['last_interaction']['started_at'],zone))+' · '+e(data['last_interaction']['channel'])+'</p>'
    content+='<ul class="note-list">'
    for note in data['notes']:
        source=note['source_task_id'] or str(note['source_conversation_id'])
        content+='<li><p>'+e(note['body'])+'</p><span class="evidence-label">Source '+e(source)+' · '+e(when(note['evidence_at'],zone))+'<br>Recorded '+e(when(note['created_at'],zone))+'</span></li>'
    content+='</ul>'
    if not data['notes']:content+='<p class="small">No sourced relationship notes yet. Relevant observations can be saved by delegated owner-authorized work.</p>'
    if data['commitments']:
        content+='<h3>Open commitments</h3>'
        for row in data['commitments']:content+='<p class="small">'+e(row['kind'].upper())+' · '+e(when(row['due_at'],zone))+' '+badge(row['state'])+'</p>'
    return content+'</section>'


def call_history(calls,zone):
    content='<section class="panel"><div class="panel-head"><h2>Actual call activity</h2><span class="badge outline">Transport receipts</span></div><p class="small">Carrier completion and transcript finalization do not prove a human answered or an appointment was confirmed. Read the mission outcome.</p>'
    for call in calls:
        content+='<div class="list-row"><span class="row-icon" aria-hidden="true">↗</span><div><h3>'+e(call['name'] or 'Unnamed person')+' · '+e(call['direction'])+'</h3><p>'+e(when(call['created_at'],zone))+'</p><div class="call-stages"><span>Intent: '+e(call['state'])+'</span><span>Carrier: '+e(call['carrier_status'] or 'No receipt')+'</span><span>Transcript: '+('Finalized' if call['transcript_finalized'] else 'Unconfirmed')+'</span></div>'
        if call['parent_task_id']:content+='<a class="text-link" href="/dashboard/work/'+e(call['parent_task_id'])+'">Read mission outcome →</a>'
        content+='</div>'+badge(call['state'])+'</div>'
    return content+('' if calls else '<p class="small">No saved calls in this view.</p>')+'</section>'
