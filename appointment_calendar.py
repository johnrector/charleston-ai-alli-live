"""Read-only, fail-closed selection of explicitly opted-in appointment calls.

Configuration is supplied by the server, never by an event title/description or
an unauthenticated request. This module does not load environment variables,
change Google data, start timers, or dial. All switches default off.

The fixed event opt-in contract uses *private* extended properties on the
connected owner's primary calendar: ``alli_owner_opt_in=true``,
``alli_contact_id=<trusted directory recipient_id>``, and exactly
``alli_confirmation_opt_in=true`` / ``alli_follow_up_opt_in=true`` for a mode.
A tag cannot verify a phone, grant contact consent, or approve policy. Recurring
appointments use Google's expanded instance ID, including on fresh GETs, never
the recurring master ID. Moving an instance does not reset its deduplication ID.

"Latest" is deliberately bounded: all pages in the previous 30 days and next
90 days are checked. Newer non-cancelled events with the same explicit contact
ID OR the directory's verified recipient email suppress follow-ups, regardless
of their opt-in tags. This is association for suppression only; an email match
never makes an event callable. Untagged events without that known email and
appointments outside this horizon cannot be attributed or ruled out. Google
pagination is not transactional; fresh reads minimize but cannot eliminate the
small race between revalidation and an external dial.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from typing import Callable, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator, model_validator

from appointment_confirmation import Appointment, ConfirmationPolicy, StrictModel, eligibility
from outbound_profiles import OutboundCall

Mode = Literal['confirmation', 'follow_up']
LOOKBACK_DAYS = 30
LOOKAHEAD_DAYS = 90
EVENT_FIELDS = ('id,etag,status,start,end,recurringEventId,originalStartTime,recurrence,'
                'attendees(email,responseStatus,self),attendeesOmitted,extendedProperties/private')
SCOPE_LIMIT = ('Latest appointment is checked only within the complete 30-day lookback/90-day '
               'lookahead on the owned primary calendar, using explicit contact IDs and known '
               'recipient attendee emails; unrelated/untagged events cannot be inferred.')


class SelectionError(ValueError):
    """Sanitized fail-closed reason, safe to report without Google/contact payloads."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ContactBinding(StrictModel):
    """Owner-supplied record; model validity alone never grants permission to dial."""
    recipient_id: str = Field(min_length=1, max_length=300, strict=True)
    recipient_name: str = Field(min_length=1, max_length=200, strict=True)
    phone: str = Field(pattern=r'^\+[1-9][0-9]{7,14}$', strict=True)
    phone_verified: bool = Field(default=False, strict=True)
    verified_source: str = Field(default='', max_length=500, strict=True)
    verified_at: AwareDatetime | None = None
    recipient_email: str = Field(default='', max_length=320, strict=True)
    recipient_timezone: str = Field(default='', max_length=100, strict=True)
    confirmation_consent: bool = Field(default=False, strict=True)
    follow_up_consent: bool = Field(default=False, strict=True)
    approved_logistics: str = Field(default='', max_length=1500, strict=True)

    @field_validator('recipient_id', 'recipient_name')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Contact identity cannot be blank')
        return value

    @field_validator('recipient_email')
    @classmethod
    def valid_email(cls, value):
        if value:
            from google_integration import email_address
            email_address(value)
        return value


class ContactDirectory(StrictModel):
    """Load afresh from authenticated server-owned storage before every dial.

    Empty/unapproved directories are valid configuration objects but always
    block dispatch. Revision should change whenever an owner edits the source.
    Consent, verification timestamps and source attestations are not inferred.
    """
    approved: bool = Field(default=False, strict=True)
    source: str = Field(default='', max_length=500, strict=True)
    revision: str = Field(default='', max_length=300, strict=True)
    contacts: tuple[ContactBinding, ...] = Field(default_factory=tuple, max_length=10000)

    @model_validator(mode='after')
    def unique_contacts(self):
        ids = [contact.recipient_id for contact in self.contacts]
        if len(ids) != len(set(ids)):
            raise ValueError('Contact IDs must be unique')
        emails = [contact.recipient_email.casefold() for contact in self.contacts if contact.recipient_email]
        if len(emails) != len(set(emails)):
            raise ValueError('Recipient emails must identify a unique contact')
        return self

    def blockers(self, policy):
        reasons = []
        if not self.approved: reasons.append('contact_directory_not_approved')
        if not self.revision.strip(): reasons.append('missing_contact_directory_revision')
        if not self.source.strip() or self.source != policy.verified_phone_source:
            reasons.append('contact_directory_source_not_verified')
        if not self.contacts: reasons.append('missing_contact_bindings')
        return reasons


class CalendarSelectionPolicy(ConfirmationPolicy):
    # Approval covers the fixed tags, directory verification source, quiet hours,
    # the voicemail choice and any configured follow-up timing. It cannot be supplied
    # by a caller or derived from calendar content.
    enabled: bool = Field(default=False, strict=True)
    approved: bool = Field(default=False, strict=True)
    allowed_capabilities: tuple[Literal['check_calendar', 'create_meeting'], ...] = ()
    follow_up_enabled: bool = Field(default=False, strict=True)
    follow_up_delay_days: int = Field(default=7, ge=1, le=27)
    follow_up_local_hour: int | None = Field(default=None, ge=0, le=23)
    follow_up_window_minutes: int = Field(default=60, ge=1, le=720)

    @model_validator(mode='after')
    def capability_envelope(self):
        if len(self.allowed_capabilities) != len(set(self.allowed_capabilities)):
            raise ValueError('Capabilities must be unique')
        if 'create_meeting' in self.allowed_capabilities and 'check_calendar' not in self.allowed_capabilities:
            raise ValueError('Creating meetings requires check_calendar capability')
        return self

    def blockers(self, mode: Mode = 'confirmation'):
        reasons = super().blockers()
        if mode == 'follow_up':
            if not self.follow_up_enabled: reasons.append('follow_up_disabled')
            if self.follow_up_local_hour is None:
                reasons.append('missing_follow_up_local_hour')
            elif self.follow_up_local_hour * 60 + self.follow_up_window_minutes > 24 * 60:
                reasons.append('follow_up_window_crosses_local_day')
        return reasons


class Selection(StrictModel):
    appointment: Appointment
    contact: ContactBinding
    directory_revision: str
    mode: Mode
    blockers: tuple[str, ...] = ()

    @property
    def dispatch_ready(self):
        return not self.blockers

    @property
    def dry_run_ready(self):
        # Disabled is distinct from ready-to-enable: policy, verification, consent,
        # quiet hours, etc. must already pass. This property never grants a dial.
        return not set(self.blockers).difference({'disabled', 'follow_up_disabled'})

    def dispatch_key(self):
        data = [self.appointment.calendar_id, self.appointment.event_id,
                self.contact.recipient_id, self.mode]
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()

    def to_outbound_call(self, policy=None):
        """Prepare a policy-bounded call body, without approving or dispatching it.

        The adapter must revalidate this Selection immediately before dialing;
        a body alone is not proof of eligibility. Only an explicitly approved
        server policy can grant tools or a voicemail message; omission defaults
        to no tools and hang-up. Policy approval still cannot bypass revalidation.
        """
        mission = ('Confirm the existing appointment with John, ask whether the recipient is still '
                   'attending, and ask whether they have any questions.' if self.mode == 'confirmation'
                   else 'Follow up about the most recent selected appointment with John. Ask whether '
                   'the meeting took place and how it went, and ask about outstanding questions or '
                   'next steps. Do not assume attendance or completion.')
        return OutboundCall(
            request_id=f'appointment:{self.mode}:{self.dispatch_key()}',
            phone=self.contact.phone, recipient_name=self.contact.recipient_name,
            mission=mission, purpose=('Appointment confirmation' if self.mode == 'confirmation'
                                      else 'Appointment follow-up'),
            preset=self.mode,
            capabilities=list(policy.allowed_capabilities) if policy is not None and policy.approved else [],
            voicemail_policy=(policy.voicemail if policy is not None and policy.approved
                              and policy.voicemail in ('hang_up', 'generic_message') else 'hang_up'),
            email=self.contact.recipient_email,
            appointment_start=self.appointment.start, appointment_end=self.appointment.end,
            approved_logistics=self.contact.approved_logistics,
        )


class SelectionBatch(StrictModel):
    selections: tuple[Selection, ...] = ()
    blockers: tuple[str, ...] = ()
    complete: bool = False
    window_start: AwareDatetime
    window_end: AwareDatetime
    skipped: dict[str, int] = Field(default_factory=dict)
    scope_limit: str = SCOPE_LIMIT


@dataclass(frozen=True)
class CalendarSnapshot:
    events: tuple[dict, ...]
    window_start: datetime
    window_end: datetime


class GoogleCalendarReader:
    """Only GET primary events; no free/busy POST, writes or credential grants.

    Every list page must attest accessRole=owner. Bounded pagination, malformed
    pages, recurring masters, repeated page tokens and conflicting repeated
    events fail closed. No partial list is returned on errors or limits.
    """
    def __init__(self, api_call=None, *, max_pages=50, max_events=10000):
        if max_pages < 1 or max_events < 1:
            raise ValueError('Positive read bounds required')
        if api_call is None:
            from google_integration import api
            api_call = api
        self.api_call = api_call
        self.max_pages = min(max_pages, 100)
        self.max_events = min(max_events, 100000)

    def _get(self, path, params):
        try:
            response = self.api_call('GET', path, params=params)
        except Exception as exc:
            raise SelectionError('calendar_read_failed') from exc
        if not isinstance(response, dict):
            raise SelectionError('invalid_calendar_response')
        return response

    def list_events(self, start, end):
        _aware(start)
        _aware(end)
        if end <= start or end - start > timedelta(days=LOOKBACK_DAYS + LOOKAHEAD_DAYS):
            raise SelectionError('invalid_calendar_horizon')
        params = {'timeMin': start.isoformat(), 'timeMax': end.isoformat(),
                  'singleEvents': 'true', 'showDeleted': 'false', 'showHiddenInvitations': 'true',
                  'orderBy': 'startTime',
                  'maxResults': 2500, 'fields': 'accessRole,items(' + EVENT_FIELDS + '),nextPageToken'}
        seen_tokens, events = set(), {}
        for _ in range(self.max_pages):
            data = self._get('calendar/v3/calendars/primary/events', params)
            if data.get('accessRole') != 'owner':
                raise SelectionError('calendar_ownership_not_verified')
            items = data.get('items', [])
            if not isinstance(items, list):
                raise SelectionError('invalid_calendar_page')
            for event in items:
                if not isinstance(event, dict) or not isinstance(event.get('id'), str) or not event['id']:
                    raise SelectionError('invalid_calendar_event')
                if event['id'] in events and events[event['id']] != event:
                    raise SelectionError('calendar_changed_during_pagination')
                events[event['id']] = event
                if len(events) > self.max_events:
                    raise SelectionError('calendar_event_limit_exceeded')
            token = data.get('nextPageToken')
            if token is None or token == '':
                return CalendarSnapshot(tuple(events.values()), start, end)
            if not isinstance(token, str) or token in seen_tokens:
                raise SelectionError('invalid_calendar_pagination')
            seen_tokens.add(token)
            params = dict(params, pageToken=token)
        raise SelectionError('calendar_pagination_incomplete')

    def get_event(self, event_id):
        if not isinstance(event_id, str) or not event_id or len(event_id) > 1024:
            raise SelectionError('invalid_event_id')
        event = self._get('calendar/v3/calendars/primary/events/' + quote(event_id, safe=''),
                          {'fields': EVENT_FIELDS})
        if event.get('id') != event_id:
            raise SelectionError('calendar_event_identity_changed')
        return event


def _aware(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Timezone-aware clock required')
    return now.astimezone(timezone.utc)


def _bounds(now):
    utc = _aware(now)
    return utc - timedelta(days=LOOKBACK_DAYS), utc + timedelta(days=LOOKAHEAD_DAYS)


def _private(event):
    properties = event.get('extendedProperties', {})
    result = properties.get('private', {}) if isinstance(properties, dict) else {}
    return result if isinstance(result, dict) else {}


def _datetime(part):
    if not isinstance(part, dict) or not isinstance(part.get('dateTime'), str):
        raise SelectionError('all_day_or_missing_event_time')
    try:
        result = datetime.fromisoformat(part['dateTime'].replace('Z', '+00:00'))
        return _aware(result)
    except (ValueError, TypeError) as exc:
        raise SelectionError('invalid_event_time') from exc


def _attendees(event):
    values = event.get('attendees', [])
    if event.get('attendeesOmitted') or not isinstance(values, list) or any(not isinstance(a, dict) for a in values):
        raise SelectionError('incomplete_event_attendees')
    return values


def _associated(event, contact):
    if _private(event).get('alli_contact_id') == contact.recipient_id:
        return True
    if not contact.recipient_email:
        return False
    return any(isinstance(a.get('email'), str) and a['email'].casefold() == contact.recipient_email.casefold()
               for a in _attendees(event))


def _declined_or_tentative(event, contact):
    for attendee in _attendees(event):
        email = attendee.get('email', '')
        if (attendee.get('self') is True or
                (contact.recipient_email and isinstance(email, str) and email.casefold() == contact.recipient_email.casefold())):
            if attendee.get('responseStatus') in ('declined', 'tentative'):
                return True
    return False


def _appointment(event, contact):
    if event.get('status') != 'confirmed':
        raise SelectionError('event_not_confirmed')
    if not event.get('etag') or not isinstance(event['etag'], str):
        raise SelectionError('event_revision_missing')
    if event.get('recurrence') or (event.get('recurringEventId') and not event.get('originalStartTime')):
        raise SelectionError('recurring_instance_required')
    if _declined_or_tentative(event, contact):
        raise SelectionError('event_declined_or_tentative')
    start, end = _datetime(event.get('start')), _datetime(event.get('end'))
    if end <= start:
        raise SelectionError('invalid_event_duration')
    return Appointment(calendar_id='primary', event_id=event['id'], revision=event['etag'],
                       start=start, end=end, status='confirmed', eligible=True,
                       recipient_id=contact.recipient_id, recipient_name=contact.recipient_name,
                       phone=contact.phone, phone_verified=contact.phone_verified,
                       phone_source=contact.verified_source, recipient_timezone=contact.recipient_timezone,
                       logistics=contact.approved_logistics)


def _contact_blockers(contact, directory, policy, now, mode):
    reasons = directory.blockers(policy)
    if (not contact.phone_verified or not contact.verified_source or
            contact.verified_source != directory.source or
            contact.verified_source != policy.verified_phone_source or
            contact.verified_at is None or contact.verified_at > now):
        reasons.append('phone_verification_missing_or_invalid')
    if 'create_meeting' in policy.allowed_capabilities and not contact.recipient_email:
        reasons.append('booking_recipient_email_missing')
    if not getattr(contact, mode + '_consent'):
        reasons.append('recipient_consent_missing')
    return reasons


def _follow_up_due(appointment, policy):
    if policy.follow_up_local_hour is None:
        raise SelectionError('missing_follow_up_local_hour')
    try:
        zone = ZoneInfo(appointment.recipient_timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise SelectionError('recipient_timezone_missing_or_invalid') from exc
    day = appointment.end.astimezone(zone).date() + timedelta(days=policy.follow_up_delay_days)
    due = datetime.combine(day, time(hour=policy.follow_up_local_hour), tzinfo=zone)
    # Local daytime windows cannot straddle midnight or an ambiguous/nonexistent
    # clock time. Skip a DST transition boundary rather than guess the offset.
    finish = due + timedelta(minutes=policy.follow_up_window_minutes)
    for value in (due, finish):
        if (value.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != value.replace(tzinfo=None)
                or value.replace(fold=0).utcoffset() != value.replace(fold=1).utcoffset()):
            raise SelectionError('ambiguous_follow_up_local_time')
    if finish.date() != due.date() and finish.time() != time(0):
        raise SelectionError('follow_up_window_crosses_local_day')
    return due.astimezone(timezone.utc), finish.astimezone(timezone.utc)


def _follow_up_blockers(appointment, policy, now):
    # Reuse exactly the confirmation policy's quiet-hour and phone checks; only
    # its hour-before window is intentionally replaced by the follow-up window.
    reasons = [r for r in eligibility(appointment, policy, now) if r != 'outside_dispatch_window']
    reasons += policy.blockers('follow_up')
    # A separately approved window must be daytime, even when broad quiet-hours
    # configuration would technically permit an overnight follow-up.
    try:
        local_hour = now.astimezone(ZoneInfo(appointment.recipient_timezone)).hour
        if not 8 <= local_hour < 20:
            reasons.append('outside_follow_up_daytime')
    except (ValueError, ZoneInfoNotFoundError):
        reasons.append('recipient_timezone_missing_or_invalid')
    return reasons


def _has_newer(event, appointment, contact, events):
    for other in events:
        if other.get('id') == event['id'] or other.get('status') == 'cancelled':
            continue
        # Incomplete attendee lists anywhere in this bounded snapshot can conceal
        # a newer match. Do not pretend the latest appointment is established.
        _attendees(other)
        if not _associated(other, contact):
            continue
        try:
            start = _datetime(other.get('start'))
        except SelectionError:
            part = other.get('start', {})
            if isinstance(part, dict) and isinstance(part.get('date'), str):
                # All-day events cannot be called, but may be a newer appointment.
                try:
                    zone = ZoneInfo(contact.recipient_timezone)
                    start = datetime.combine(datetime.fromisoformat(part['date']).date(), time(), tzinfo=zone)
                except (ValueError, ZoneInfoNotFoundError) as exc:
                    raise SelectionError('latest_appointment_unknown') from exc
            else:
                raise SelectionError('latest_appointment_unknown')
        # Equal-time duplicate records use a stable event-ID tie-break. There is
        # still at most one latest candidate for this contact, even across modes
        # and subsequent scans; changing revisions never alters the winner.
        if (start, other['id']) > (appointment.start, event['id']):
            return True
    return False


def _select_snapshot(snapshot, directory, policy, now):
    contacts = {contact.recipient_id: contact for contact in directory.contacts}
    results, skipped = [], Counter()
    for event in snapshot.events:
        private = _private(event)
        if private.get('alli_owner_opt_in') != 'true':
            continue
        contact_id = private.get('alli_contact_id')
        contact = contacts.get(contact_id) if isinstance(contact_id, str) else None
        if contact is None:
            skipped['missing_contact_binding'] += 1
            continue
        try:
            appointment = _appointment(event, contact)
        except (SelectionError, ValueError) as exc:
            skipped[getattr(exc, 'reason', 'invalid_appointment')] += 1
            continue
        for mode in ('confirmation', 'follow_up'):
            if private.get('alli_' + mode + '_opt_in') != 'true':
                continue
            try:
                if mode == 'confirmation':
                    due = appointment.start - timedelta(minutes=policy.lead_minutes)
                    finish = due + timedelta(minutes=policy.dispatch_window_minutes)
                    reasons = eligibility(appointment, policy, now)
                else:
                    due, finish = _follow_up_due(appointment, policy)
                    reasons = _follow_up_blockers(appointment, policy, now)
                if not due <= now < finish:
                    continue
                if mode == 'follow_up' and _has_newer(event, appointment, contact, snapshot.events):
                    skipped['newer_appointment_exists'] += 1
                    continue
            except SelectionError as exc:
                skipped[exc.reason] += 1
                continue
            reasons += _contact_blockers(contact, directory, policy, now, mode)
            results.append(Selection(appointment=appointment, contact=contact,
                                     directory_revision=directory.revision, mode=mode,
                                     blockers=tuple(dict.fromkeys(reasons))))
    blockers = tuple(dict.fromkeys(policy.blockers() + directory.blockers(policy)))
    return SelectionBatch(selections=tuple(sorted(results, key=lambda x: (x.appointment.start, x.dispatch_key()))),
                          blockers=blockers, complete=True, window_start=snapshot.window_start,
                          window_end=snapshot.window_end, skipped=dict(skipped))


def select_appointments(reader, directory, policy, now, *, dry_run=True):
    """One bounded, read-only pass. Dry run never relaxes permission checks.

    A disabled but otherwise approved candidate is dry_run_ready and still not
    dispatch_ready. Read/pagination failure returns no partial selections.
    """
    start, end = _bounds(now)
    try:
        snapshot = reader.list_events(start, end)
        return _select_snapshot(snapshot, directory, policy, _aware(now))
    except SelectionError as exc:
        return SelectionBatch(window_start=start, window_end=end, complete=False,
                              blockers=tuple(dict.fromkeys([exc.reason] + policy.blockers() + directory.blockers(policy))))


def revalidate_selection(selection, reader, directory_loader, policy, now, *, dry_run=False):
    """Fresh directory + complete horizon + final exact-instance GET before dial.

    Raises on any changed event revision/time/recipient/contact/directory or any
    current blocker. Dispatchers must place this after their durable dedup claim
    and immediately before the outbound side effect, never on a cached timer.
    """
    start, end = _bounds(now)
    try:
        directory = directory_loader()
    except Exception as exc:
        raise SelectionError('contact_directory_read_failed') from exc
    if not isinstance(directory, ContactDirectory):
        raise SelectionError('invalid_contact_directory')
    snapshot = reader.list_events(start, end)
    if not any(e['id'] == selection.appointment.event_id for e in snapshot.events):
        raise SelectionError('selected_event_missing_from_horizon')
    current_event = reader.get_event(selection.appointment.event_id)
    events = tuple(current_event if e['id'] == selection.appointment.event_id else e for e in snapshot.events)
    batch = _select_snapshot(CalendarSnapshot(events, start, end), directory, policy, _aware(now))
    current = next((item for item in batch.selections if item.dispatch_key() == selection.dispatch_key()), None)
    if current is None:
        raise SelectionError('selection_no_longer_eligible')
    if (current.appointment != selection.appointment or current.contact != selection.contact
            or current.directory_revision != selection.directory_revision):
        raise SelectionError('appointment_or_contact_changed')
    if not (current.dry_run_ready if dry_run else current.dispatch_ready):
        raise SelectionError('selection_blocked:' + ','.join(current.blockers))
    return current


class CalendarSelector:
    """Small server integration facade; has no schedule or dialing capability."""
    def __init__(self, reader, directory_loader: Callable[[], ContactDirectory], policy):
        self.reader, self.directory_loader, self.policy = reader, directory_loader, policy

    def preview(self, now, *, dry_run=True):
        start, end = _bounds(now)
        try:
            directory = self.directory_loader()
            if not isinstance(directory, ContactDirectory):
                raise SelectionError('invalid_contact_directory')
        except Exception:
            return SelectionBatch(window_start=start, window_end=end, blockers=('contact_directory_read_failed',))
        return select_appointments(self.reader, directory, self.policy, now, dry_run=dry_run)

    def revalidate(self, selection, now, *, dry_run=False):
        return revalidate_selection(selection, self.reader, self.directory_loader, self.policy, now, dry_run=dry_run)
