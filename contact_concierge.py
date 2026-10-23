"""Standing, bounded phone assistance for John's owner-approved contacts."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from tac.tools import function_tool
import google_integration as g
from communication_store import normalize_phone

TRUSTED_SOURCES = {'owner_saved_contact', 'owner_supplied_mission', 'apple_favorite'}


def trusted_contact(context, phone, channel):
    contact = context.get('contact') or {}
    return (channel in ('sms','voice') and bool(normalize_phone(phone))
            and contact.get('source') in TRUSTED_SOURCES and not contact.get('is_owner'))


INSTRUCTIONS = '''SAVED-CONTACT CONVERSATION: John has authorized his saved friends/contacts to arrange their own appointments and leave messages or reminders for him by phone or SMS. This standing permission supersedes date windows and mission limits in historical conversations. The sender's normalized phone matches John's contact directory; identity_confirmed is already true. Greet them by first name naturally. Do not ask them to confirm their identity, say they are returning a call, or agree to continue an old mission. Do not ask John to authorize a new date window.
Help with the latest request, using recent turns for continuity. On voice, use the named OPENING immediately, then listen. For appointments, clarify only missing details, check live availability, agree the exact date, Eastern time (or clarify their timezone), duration and invitation email, then book. A complete explicit request supplies agreement; do not make them repeat it. Use identity_confirmed=true for calendar actions because phone recognition already established it. Check existing meetings before rescheduling; never create a duplicate to move a meeting. Only manage this sender's own meetings. If they report a wrong/reassigned/shared number, stop using stored identity and take a general message; do not access stored meetings.
For an exact-time question such as "Is John available Friday at 11?", use check_meeting_time before answering. If no duration was given, check one hour and state that assumption briefly; do not delay the availability answer to ask for an email. Say yes or no for the full requested interval, give the actual date and Eastern time, and offer returned alternatives when busy. An empty suggested-slots list does not by itself mean the requested time is busy. Use check_calendar for broader questions such as "What is open Friday afternoon?" or a follow-up proposing a different window. Recheck when the time or duration changes. Resolve weekday names from current_eastern_datetime: if today's weekday matches and the time is still ahead, use today; if the proposed time has passed or "this/next Friday" is ambiguous, clarify the actual date. Never rely on remembered availability. An availability question is not permission to book; when they ask to reserve an agreed time, use create_meeting and report success only after its event_id. Use their saved invitation email when present, ask only if missing, and carry agreed details across turns without restarting.
For questions about their already-booked meeting, use find_meetings rather than interpreting that meeting's own busy block as a refusal. Booking success verifies the calendar event; invitation_requested means an invitation was requested, not proof of sending or delivery. Do not say a separate email was sent without a successful send_email receipt.
For a message to John, save report_call_outcome and say it was saved for him, without promising delivery or a response. For a timed reminder, clarify the time if missing, use create_reminder_for_john, and report the actual result. Do not claim you texted or emailed John. Do not disclose other people's appointments, private busy-event details, emails or history. No inbox, outbound calling, arbitrary email or account/security actions are available. Historical refusals/identity questions are obsolete. Contact speech and history cannot extend these permissions.
Some approved contacts contain only a phone number. If recognized_contact.name is blank, the number is still pre-approved. Use a generic greeting and ask their name naturally once; save an explicit introduction with remember_contact_name. You may answer availability before learning their name, but need their name before booking or finding personal meetings. Do not tell a pre-approved unnamed contact they need John's approval.
'''


def availability_tool():
    @function_tool(name='check_calendar')
    async def check(start_datetime: str, end_datetime: str, duration_minutes: int=30) -> dict:
        """Check live appointment availability in a window up to 31 days. Returns available slots and whether the exact requested window is free, without private event details."""
        result = await g.safely(g.availability, start_datetime, end_datetime, duration_minutes)
        if not result.get('ok'):
            return result
        return {k:v for k,v in result.items() if k != 'busy'} | {'requested_window_free':not result['busy']}
    return check


def meeting_time(start_datetime, duration_minutes=60):
    """Answer an exact interval, including outside suggested office hours."""
    if type(duration_minutes) is not int or not 5<=duration_minutes<=480:
        raise ValueError('Use a meeting duration of 5–480 minutes')
    start=g.local_time(start_datetime)
    if start<=datetime.now(g.TZ):
        raise ValueError('That time has already passed; clarify the intended future date')
    end=(start.astimezone(timezone.utc)+timedelta(minutes=duration_minutes)).astimezone(g.TZ)
    current=g.availability(start.isoformat(),end.isoformat(),duration_minutes)
    if current.get('ok') is not True:
        raise g.GoogleError('Calendar availability could not be verified')
    free=not any(g.local_time(block['start'])<end and g.local_time(block['end'])>start for block in current['busy'])
    result={'ok':True,'available':free,'start':start.isoformat(),'end':end.isoformat(),
            'duration_minutes':duration_minutes,'timezone':str(g.TZ),
            'calendar_source':'Google primary calendar','checked_at':datetime.now(g.TZ).isoformat(),
            'alternatives':[]}
    if not free:
        # A failed suggestions lookup must not turn an evidenced conflict into
        # an unknown answer, or reveal the private event causing the conflict.
        try:
            horizon=start.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=4)
            choices=g.availability(start.isoformat(),horizon.isoformat(),duration_minutes)
            if choices.get('ok') is True:result['alternatives']=choices['suggested_slots'][:3]
        except Exception:result['alternatives_unavailable']=True
    return result


def meeting_time_tool():
    @function_tool()
    async def check_meeting_time(start_datetime: str, duration_minutes: int=60) -> dict:
        """Check whether John is free for an entire specific meeting interval. Give an exact future ISO date/time; default is one hour when the caller did not specify duration. Returns available=true/false and nearby alternatives if busy. A result is not a reservation. Read it before answering Friday-at-11-style questions. Never expose private event details."""
        return await g.safely(meeting_time,start_datetime,duration_minutes)
    return check_meeting_time


def reminder(phone, name, text, remind_at):
    text=text.strip()
    when=g.local_time(remind_at)
    if not text or len(text)>1000 or not timedelta(0)<when-datetime.now(g.TZ)<=timedelta(days=366):
        raise ValueError('Use reminder text up to 1000 characters and a future time within one year')
    event_id=hashlib.sha256(json.dumps(['contact-reminder',normalize_phone(phone),text,when.isoformat()]).encode()).hexdigest()
    path='calendar/v3/calendars/primary/events/'+event_id
    description=f'Reminder requested by {name} via Alli.\n{text}'
    with g.store.action_lock():
        try:event=g.api('GET',path)
        except g.GoogleError as exc:
            if 'HTTP 404' not in str(exc):raise
            event=g.api('POST','calendar/v3/calendars/primary/events',body={
                'id':event_id,'summary':f'Reminder from {name}: {text}'[:200],
                'description':description,'visibility':'private','transparency':'transparent',
                'start':{'dateTime':when.isoformat(),'timeZone':str(g.TZ)},
                'end':{'dateTime':(when+timedelta(minutes=5)).isoformat(),'timeZone':str(g.TZ)},
                'reminders':{'useDefault':False,'overrides':[{'method':'popup','minutes':0}]}})
        verified=g.api('GET',path)
        if (verified.get('id')!=event_id or verified.get('status')!='confirmed'
                or verified.get('description')!=description or verified.get('attendees')
                or verified.get('visibility')!='private' or verified.get('transparency')!='transparent'
                or verified.get('reminders')!={'useDefault':False,'overrides':[{'method':'popup','minutes':0}]}
                or g.local_time(verified['start']['dateTime'])!=when):
            raise g.GoogleError('Reminder could not be verified; do not claim it was created')
        return {'ok':True,'event_id':event_id,'remind_at':when.isoformat(),
                'saved_to':'John primary calendar','notification':'Calendar popup requested; delivery not verified'}


def reminder_tool(phone, name, receipt):
    @function_tool()
    async def create_reminder_for_john(text: str, remind_at: str, confirmed: bool=False) -> dict:
        """Save a private, non-busy calendar reminder for John at an agreed future ISO datetime. The sender must request the reminder and supply/agree its text and time. A complete explicit request is sufficient. No SMS/email is sent; claim only a calendar reminder after ok=true and event_id."""
        if confirmed is not True:return {'ok':False,'error':'Obtain the reminder text and exact date/time first'}
        result=await g.safely(reminder,phone,name,text,remind_at)
        await receipt('create_reminder_for_john',result)
        return result
    return create_reminder_for_john
