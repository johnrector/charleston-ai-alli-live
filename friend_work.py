"""Bounded calendar work that continues after a saved caller hangs up."""
import asyncio,hashlib,json
import communication_store as store
import google_integration as g
import security_controls as security

def private_allowed(job):
    context=job.get('context') or {};origin=context.get('origin') or {}
    return not context.get('group_delegation') and (origin.get('channel')!='voice' or origin.get('number_verified') is True)

NAMES={'check_available_time','book_my_meeting','move_my_meeting','cancel_my_meeting','remind_john'}

def schemas(schema,job):
    result=[schema('check_available_time','Check live availability for this saved caller. Returns availability only, never other people’s event details.',dict(start_datetime=dict(type='string'),duration_minutes=dict(type='integer')),('start_datetime','duration_minutes')),
      schema('book_my_meeting','Book this caller’s explicitly requested meeting with John. Use the saved email; ask only for missing essentials. Application checks limits, consent and duplicates.',dict(start_datetime=dict(type='string'),duration_minutes=dict(type='integer'),title=dict(type='string'),email=dict(type='string')),('start_datetime','duration_minutes','title','email')),
      schema('remind_john','Save this caller’s specifically requested timed reminder for John.',dict(text=dict(type='string'),remind_at=dict(type='string')),('text','remind_at'))]
    if private_allowed(job):result += [schema('move_my_meeting','Move only the caller’s own meeting after finding its exact event ID.',dict(event_id=dict(type='string'),start_datetime=dict(type='string'),duration_minutes=dict(type='integer')),('event_id','start_datetime','duration_minutes')),
      schema('cancel_my_meeting','Cancel only the caller’s explicitly identified own meeting.',dict(event_id=dict(type='string')),('event_id',))]
    return result

async def execute(job,call,args,client):
    import work_agent,work_store
    from contact_concierge import trusted_contact,meeting_time_tool
    context=await asyncio.to_thread(store.context_for,job['phone']);contact=context.get('contact') or {}
    if not trusted_contact(context,job['phone'],'sms') or await asyncio.to_thread(security.blocked,job['phone']):
        return dict(ok=False,error='This action requires an approved contact; private owner work requires owner authorization')
    if call.name in ('move_my_meeting','cancel_my_meeting') and not private_allowed(job):
        raise ValueError('Private meeting changes require the sender’s own verified call/text, not a shared or unauthenticated call')
    if call.name=='check_available_time':return await meeting_time_tool()(**args)
    if not contact.get('name'):raise ValueError('The caller’s name is required before creating an appointment')
    actual_args=dict(args)
    if call.name=='book_my_meeting':
        email=contact.get('email') or args.get('email','')
        if not email:raise ValueError('Ask the caller for the invitation email by text')
        if contact.get('email') and args.get('email') and email.casefold()!=args['email'].casefold():raise ValueError('Use this contact’s saved invitation email')
        original=(job.get('context') or {}).get('delegation_source') or job['instruction']
        if not contact.get('email') and email.casefold() not in original.casefold():raise ValueError('Ask the caller to text the exact invitation email; do not invent one from other content')
        actual_args['email']=email
    if call.name in ('move_my_meeting','cancel_my_meeting'):
        from call_actions import event_path
        from contact_events import belongs
        event=await asyncio.to_thread(g.api,'GET',event_path(args['event_id']))
        if not await asyncio.to_thread(belongs,event,job['phone'],contact.get('email','')):raise ValueError('That event is not linked to this caller')
        actual_args['verified_current_meeting']={k:event.get(k) for k in ('id','summary','start','end')}
    tool=dict(name=call.name,description='Bounded action for this saved contact’s own appointment/reminder, with no access to other people’s events')
    authority=await work_agent.delegated_authority(job,tool,actual_args,client)
    if not authority['allowed']:
        raise work_agent.Waiting(dict(kind='clarification',question=authority['reason'],ask_john=False))
    key=hashlib.sha256(json.dumps([call.name,actual_args],sort_keys=True).encode()).hexdigest()
    with store.connection() as db:
        prior=db.execute("SELECT kind,details FROM alli_work_event WHERE work_id=%s AND details->>'action_key'=%s AND kind IN ('tool_attempt','tool_result') ORDER BY id DESC LIMIT 1",(job['id'],key)).fetchone()
        if prior:
            if prior[0]=='tool_result':return prior[1]['result']
            raise ValueError('A previous action is uncertain; John must review the provider receipt before any retry')
        work_store.event(db,job['id'],'tool_attempt',dict(call_id=call.call_id,action_key=key,tool=call.name))
    if call.name=='remind_john':
        from contact_concierge import reminder
        await asyncio.to_thread(security.reserve,'reminder_write',job['phone'],job['id']+':'+key)
        result=await g.safely(reminder,job['phone'],contact['name'],args['text'],args['remind_at'])
    else:
        await asyncio.to_thread(security.calendar_write,job['phone'],job['id']+':'+key)
        if call.name=='book_my_meeting':result=await g.safely(g.meeting,contact['name'],actual_args['email'],args['start_datetime'],args['duration_minutes'],args['title'],'Arranged at this contact’s request through Alli.',True,job['phone'])
        else:
            from call_actions import reschedule,cancel
            if call.name=='move_my_meeting':result=await g.safely(reschedule,args['event_id'],contact['name'],contact.get('email',''),args['start_datetime'],args['duration_minutes'],job['phone'])
            else:result=await g.safely(cancel,args['event_id'],contact['name'],contact.get('email',''),job['phone'])
    with store.connection() as db:
        work_store.event(db,job['id'],'tool_result',dict(call_id=call.call_id,action_key=key,result=result))
        store.notify(db,'work-action:'+job['id']+':'+key,dict(source='business_tool_result',sid=(job.get('context') or {}).get('source_sid'),phone=job['phone'],work_id=job['id'],action=call.name,result=result,summary='Cloud calendar/reminder action: '+call.name))
    return result
