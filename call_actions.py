"""Mission-scoped calendar and email actions for owner-authorized manual calls."""
import re
from datetime import timedelta, timezone, datetime
from urllib.parse import quote
from tac.tools import function_tool
import google_integration as g


def associated(event, name, email):
    guests = event.get('attendees', [])
    if email:
        return any(a.get('email', '').casefold() == email.casefold() for a in guests)
    norm = lambda s: ' '.join(re.findall(r'\w+', s.casefold()))
    wanted = norm(name)
    return bool(wanted) and (any(norm(a.get('displayName', '')) == wanted for a in guests)
        or (len(wanted.split()) >= 2 and (' ' + wanted + ' ') in (' ' + norm(event.get('summary', '')) + ' ')))


def events_in_window(start, end):
    start, end = g.local_time(start), g.local_time(end)
    if not timedelta(0) < end - start <= timedelta(days=31):
        raise ValueError('Search an ordered window of at most 31 days')
    params = {'timeMin': start.isoformat(), 'timeMax': end.isoformat(),
              'singleEvents': True, 'maxResults': 250}
    events = []
    for _ in range(20):
        page = g.api('GET', 'calendar/v3/calendars/primary/events', params=params)
        events.extend(page.get('items', []))
        if not page.get('nextPageToken'):
            return events
        params['pageToken'] = page['nextPageToken']
    raise ValueError('Calendar result too large; narrow the window')


def event_path(event_id):
    return 'calendar/v3/calendars/primary/events/' + quote(event_id, safe='')


def reschedule(event_id, name, email, start_datetime, duration_minutes):
    start = g.local_time(start_datetime)
    if start <= datetime.now(g.TZ) or type(duration_minutes) is not int or not 5 <= duration_minutes <= 480:
        raise ValueError('Use a future time and a duration of 5–480 minutes')
    end = (start.astimezone(timezone.utc) + timedelta(minutes=duration_minutes)).astimezone(g.TZ)
    path = event_path(event_id)
    with g.store.action_lock():
        event = g.api('GET', path)
        if event.get('status') != 'confirmed' or not associated(event, name, email):
            raise ValueError('Event is not a confirmed meeting with this recipient')
        if 'dateTime' not in event.get('start', {}) or event.get('recurrence'):
            raise ValueError('Select a timed meeting instance, not a recurring series or all-day event')
        if not event.get('etag'):
            raise ValueError('Google did not return a version for this event')
        if not (g.local_time(event['start']['dateTime']) == start and g.local_time(event['end']['dateTime']) == end):
            for other in events_in_window(start.isoformat(), end.isoformat()):
                if other.get('id') != event_id and other.get('status') != 'cancelled' and other.get('transparency') != 'transparent' and not any(a.get('self') and a.get('responseStatus') == 'declined' for a in other.get('attendees', [])):
                    raise ValueError('John has another event during that time')
            g.api('PATCH', path, body={'start': {'dateTime': start.isoformat(), 'timeZone': str(g.TZ)},
                'end': {'dateTime': end.isoformat(), 'timeZone': str(g.TZ)}},
                params={'sendUpdates': 'all'}, extra_headers={'If-Match': event['etag']})
        verified = g.api('GET', path)
        if (verified.get('status') != 'confirmed' or not associated(verified, name, email)
            or g.local_time(verified['start']['dateTime']) != start or g.local_time(verified['end']['dateTime']) != end):
            raise g.GoogleError('Calendar change could not be verified')
        return {'ok': True, 'event_id': event_id, 'start': verified['start'], 'end': verified['end'], 'invitation_update_requested': True}


def cancel(event_id, name, email):
    path = event_path(event_id)
    with g.store.action_lock():
        event = g.api('GET', path)
        if not associated(event, name, email) or event.get('recurrence') or not event.get('etag'):
            raise ValueError('Select this recipient’s meeting instance')
        if event.get('status') != 'cancelled':
            g.api('DELETE', path, params={'sendUpdates': 'all'}, extra_headers={'If-Match': event['etag']})
        try:
            verified = g.api('GET', path)
            if verified.get('status') != 'cancelled':
                raise g.GoogleError('Cancellation could not be verified')
        except g.GoogleError as exc:
            if 'HTTP 404' not in str(exc) and 'HTTP 410' not in str(exc):
                raise
        return {'ok': True, 'event_id': event_id, 'cancelled': True, 'invitation_update_requested': True}


def tools_for_call(body):
    """Each registry remembers only matching events read during this call."""
    found = set()
    def consent(identity_confirmed, confirmed):
        return identity_confirmed is True and confirmed is True
    def recipient_email(value):
        if body.email and value.casefold() != body.email.casefold():
            raise ValueError('Use the recipient email supplied by John')
        return g.email_address(body.email or value)

    @function_tool()
    async def find_meetings(start_datetime: str, end_datetime: str) -> dict:
        """Find this recipient's meetings in an exact date window before rescheduling or cancelling. Returns only associated meetings, never unrelated calendar content."""
        def run():
            matches = []
            for event in events_in_window(start_datetime, end_datetime):
                if event.get('status') == 'confirmed' and associated(event, body.recipient_name, body.email):
                    found.add(event['id'])
                    matches.append({k: event.get(k) for k in ('id', 'summary', 'start', 'end', 'location')})
            return {'ok': True, 'meetings': matches}
        return await g.safely(run)

    @function_tool()
    async def update_meeting(event_id: str, start_datetime: str, duration_minutes: int,
                             identity_confirmed: bool = False, confirmed: bool = False) -> dict:
        """Move a meeting returned by find_meetings after the verified recipient agrees to the exact date/time/duration. Preserves event ID, guests and other details; sends Calendar updates. Success requires ok=true and event_id."""
        if event_id not in found or not consent(identity_confirmed, confirmed):
            return {'ok': False, 'error': 'Find the matching meeting and confirm the recipient and exact new time first'}
        return await g.safely(reschedule, event_id, body.recipient_name, body.email, start_datetime, duration_minutes)

    @function_tool()
    async def cancel_meeting(event_id: str, identity_confirmed: bool = False, confirmed: bool = False) -> dict:
        """Cancel this recipient's identified meeting only when the approved mission permits cancellation and the verified recipient explicitly agrees."""
        if event_id not in found or not consent(identity_confirmed, confirmed):
            return {'ok': False, 'error': 'Find and explicitly confirm the meeting cancellation first'}
        return await g.safely(cancel, event_id, body.recipient_name, body.email)

    @function_tool(name='create_meeting')
    async def create(start_datetime: str, duration_minutes: int, title: str, recipient_email_address: str,
                     identity_confirmed: bool = False, confirmed: bool = False) -> dict:
        """Create the agreed new meeting. Use John's supplied email, or ask the verified recipient for their email when absent. Never create a replacement for an existing meeting; update it instead."""
        if not consent(identity_confirmed, confirmed):
            return {'ok': False, 'error': 'Verify identity and agreement to exact meeting details and invitation email'}
        try: email = recipient_email(recipient_email_address)
        except ValueError as exc: return {'ok': False, 'error': str(exc)}
        return await g.create_meeting(attendee_name=body.recipient_name, attendee_email=email, start_datetime=start_datetime, duration_minutes=duration_minutes, title=title, context=body.approved_logistics, confirmed=True)

    @function_tool(name='send_email')
    async def send(recipient_email_address: str, subject: str, message: str,
                   identity_confirmed: bool = False, confirmed: bool = False) -> dict:
        """Send mission-related email to the verified recipient. Confirm address and purpose; use John's supplied email, or ask the recipient when absent. Never claim success without message_id."""
        if not consent(identity_confirmed, confirmed):
            return {'ok': False, 'error': 'Verify the recipient and confirm the email address and purpose'}
        try: email = recipient_email(recipient_email_address)
        except ValueError as exc: return {'ok': False, 'error': str(exc)}
        return await g.send_email(recipient_email=email, subject=subject, body=message, confirmed=True)

    tools = []
    if 'manage_calendar' in body.capabilities:
        tools.extend([find_meetings, update_meeting, cancel_meeting])
        if 'create_meeting' in body.capabilities: tools.append(create)
    if 'send_email' in body.capabilities: tools.append(send)
    return tools


ACTION_INSTRUCTIONS = """FULL MANUAL CALL WORKFLOW: Complete the owner's approved mission, including relevant calendar and email actions using your granted tools. You may create, move or cancel meetings and send related emails when those tools are present. These are actions to complete during the call, not requests to defer to John.
For rescheduling, find_meetings locates this recipient's existing event. If more than one matches, clarify which one. Preserve its duration unless another duration is agreed. Use update_meeting after agreement to the exact date and time. Never create a second event as a substitute for moving one. Calendar updates notify existing attendees. Owner-requested evening times are valid; the 9–5 suggested-slot policy is not a restriction.
For a new meeting, check live availability and obtain agreement to the date, time, duration and email. Use John's supplied recipient email; if absent, ask the verified recipient for their own email. For mission-related email, confirm its purpose and address. Never disclose unrelated calendar details or send to unrelated parties.
Set identity_confirmed and confirmed true only after the corresponding verification and agreement. Claim an action completed only after its tool returns ok=true and the event_id or message_id. On uncertainty, report it honestly and do not blindly retry. Finish by saving report_call_outcome, including agreed time and actual action results, before saying goodbye and end_call.
"""
