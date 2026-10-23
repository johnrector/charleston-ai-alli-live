"""Owner-authorized SMS outbox. Claim durably before contacting Twilio; never blind-retry."""
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import Depends, Header, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from psycopg.types.json import Jsonb
from tac.server.signature_validation import build_http_signature_dependency
import communication_store as store

log = logging.getLogger('alli.sms')
TERMINAL = {'delivered', 'undelivered', 'failed', 'cancelled', 'expired'}
PROVIDER = {'queued', 'sending', 'sent', 'delivered', 'undelivered', 'failed'}
RANK = {'scheduled': 0, 'sending': 1, 'submitted': 2, 'queued': 2, 'sent': 3,
        'delivered': 4, 'undelivered': 4, 'failed': 4, 'uncertain': 1}
FIELDS = 'request_id,phone,recipient_name,body,send_at,expires_at,status,message_sid,error_code,created_at,updated_at'


class TextMessage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    phone: str = Field(min_length=7, max_length=30)
    recipient_name: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=1600)
    send_at: AwareDatetime | None = None

    @field_validator('phone')
    @classmethod
    def valid_phone(cls, value):
        import phonenumbers
        try:
            parsed = phonenumbers.parse(value, 'US')
            if parsed.extension or not phonenumbers.is_valid_number(parsed): raise ValueError()
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except (ValueError, phonenumbers.NumberParseException):
            raise ValueError('Supply a valid direct phone number')

    @field_validator('recipient_name', 'message')
    @classmethod
    def nonempty(cls, value):
        if not value.strip(): raise ValueError('A recipient and message are required')
        return value.strip()


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS alli_outbound_sms (
        request_id text PRIMARY KEY, fingerprint text NOT NULL, phone text NOT NULL,
        recipient_name text NOT NULL, body text NOT NULL, send_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL, status text NOT NULL DEFAULT 'scheduled',
        callback_token text NOT NULL, message_sid text UNIQUE, error_code text,
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())''')
    db.execute("CREATE INDEX IF NOT EXISTS alli_outbound_sms_due ON alli_outbound_sms(send_at) WHERE status='scheduled'")
    db.execute('CREATE INDEX IF NOT EXISTS alli_outbound_sms_phone ON alli_outbound_sms(phone,created_at DESC)')
    db.execute("ALTER TABLE alli_outbound_sms ADD COLUMN IF NOT EXISTS context jsonb NOT NULL DEFAULT '{}'::jsonb")


def row_dict(row):
    if not row: return None
    result = dict(zip(FIELDS.split(','), row))
    for key, value in result.items():
        if isinstance(value, datetime): result[key] = value.isoformat()
    result['message'] = result.pop('body')
    result['ok'] = result['status'] not in {'uncertain', 'failed', 'undelivered', 'expired'}
    result['from_phone'] = os.getenv('TWILIO_PHONE_NUMBER', '')
    result['timezone'] = 'America/New_York'
    result['delivery_confirmed'] = result['status'] == 'delivered'
    return result


def get(request_id):
    store.initialize()
    with store.connection() as db:
        row = db.execute(f'SELECT {FIELDS} FROM alli_outbound_sms WHERE request_id=%s', (request_id,)).fetchone()
    return row_dict(row) or {'ok': False, 'request_id': request_id, 'status': 'not_found'}


def enqueue(body: TextMessage, *, context=None):
    store.initialize()
    with store.connection() as db:
        return enqueue_in_db(db, body, context=context)


def enqueue_in_db(db, body: TextMessage, *, context=None):
    """Shared transaction for atomic promise/outbox hand-off. Caller initializes."""
    value = body.model_dump(mode='json')
    if body.send_at: value['send_at'] = body.send_at.astimezone(timezone.utc).isoformat()
    if context: value['context'] = context
    fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    # Serialize same-ID intent checks, including concurrent enqueue/cancel.
    db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,301849630157))', (body.request_id,))
    prior = db.execute(f'SELECT fingerprint,{FIELDS} FROM alli_outbound_sms WHERE request_id=%s', (body.request_id,)).fetchone()
    if prior:
        if prior[0] != fingerprint: raise ValueError('request_id already belongs to a different text; do not vary an uncertain request')
        return dict(row_dict(prior[1:]), duplicate=True)
    now = datetime.now(timezone.utc)
    when = body.send_at.astimezone(timezone.utc) if body.send_at else now
    if when < now - timedelta(seconds=60): raise ValueError('Choose a future send time with an explicit UTC offset')
    if when > now + timedelta(days=366): raise ValueError('Schedule texts at most one year ahead')
    row = db.execute(f'''INSERT INTO alli_outbound_sms
        (request_id,fingerprint,phone,recipient_name,body,send_at,expires_at,callback_token,context)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {FIELDS}''',
        (body.request_id, fingerprint, body.phone, body.recipient_name, body.message,
         when, when + timedelta(minutes=15), secrets.token_urlsafe(24), Jsonb(context or {}))).fetchone()
    store.notify(db, 'outbound-sms:' + body.request_id + ':scheduled',
                 dict(channel='sms', direction='outbound', request_id=body.request_id,
                      phone=body.phone, status='scheduled', send_at=when.isoformat(),
                      summary='Owner-authorized text saved to the cloud outbox.'))
    return row_dict(row)


def cancel(request_id):
    store.initialize()
    with store.connection() as db:
        row = db.execute(f"UPDATE alli_outbound_sms SET status='cancelled',updated_at=now() WHERE request_id=%s AND status='scheduled' RETURNING {FIELDS}", (request_id,)).fetchone()
        if row:
            store.notify(db, 'outbound-sms:' + request_id + ':cancelled',
                         dict(channel='sms', direction='outbound', request_id=request_id, status='cancelled'))
            return row_dict(row)
    result = get(request_id)
    if result['status'] != 'cancelled':
        result.update(ok=False, error='Only an unsent scheduled text can be cancelled. Sending or uncertain messages cannot be recalled.')
    return result


def claim():
    store.initialize()
    with store.connection() as db:
        for status, condition in (
            ('uncertain', "status='sending' AND updated_at<now()-interval '2 minutes'"),
            ('expired', "status='scheduled' AND expires_at<now()"),
        ):
            rows = db.execute(f"UPDATE alli_outbound_sms SET status=%s,updated_at=now() WHERE {condition} RETURNING request_id,phone", (status,)).fetchall()
            for request_id, phone in rows:
                store.notify(db, 'outbound-sms:' + request_id + ':' + status,
                             dict(channel='sms', direction='outbound', request_id=request_id,
                                  phone=phone, status=status, follow_up_needed=True,
                                  summary='Text was not retried; inspect its send attempt before taking further action.'))
        row = db.execute('''WITH due AS (
            SELECT request_id FROM alli_outbound_sms WHERE status='scheduled'
                AND send_at<=now() AND expires_at>=now()
            ORDER BY send_at FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE alli_outbound_sms s SET status='sending',updated_at=now() FROM due
            WHERE s.request_id=due.request_id RETURNING s.request_id,s.phone,s.body,s.callback_token,s.context''').fetchone()
    return dict(zip(('request_id', 'phone', 'body', 'callback_token', 'context'), row)) if row else None


def receipt(request_id, message_sid=None, status='submitted', error_code=None, callback_token=None, phone=None):
    store.initialize()
    if status not in PROVIDER | {'submitted', 'uncertain', 'cancelled'}: return False
    with store.connection() as db:
        row = db.execute('SELECT status,message_sid,callback_token,phone FROM alli_outbound_sms WHERE request_id=%s FOR UPDATE', (request_id,)).fetchone()
        if not row or row[0] in {'scheduled', 'cancelled', 'expired'}: return False
        current, prior_sid, token, destination = row
        if callback_token is not None and (not hmac.compare_digest(token, callback_token) or phone != destination): return False
        if prior_sid and prior_sid != message_sid: return False
        if current in TERMINAL or (status != 'cancelled' and current != 'uncertain' and RANK.get(status, 0) < RANK.get(current, 0)): return False
        if status == 'uncertain' and current not in {'sending', 'uncertain'}: return False
        # An early authenticated callback may bind the SID before create() returns.
        db.execute('''UPDATE alli_outbound_sms SET status=%s,message_sid=coalesce(message_sid,%s),
            error_code=coalesce(%s,error_code),updated_at=now() WHERE request_id=%s''',
            (status, message_sid, str(error_code)[:20] if error_code else None, request_id))
        if request_id.startswith('dashboard-login:') and status != 'uncertain':
            db.execute("UPDATE alli_outbound_sms SET body='Dashboard sign-in code [redacted after provider acceptance]' WHERE request_id=%s",(request_id,))
        if status in TERMINAL | {'submitted', 'uncertain'}:
            store.notify(db, 'outbound-sms:' + request_id + ':' + status,
                         dict(channel='sms', direction='outbound', request_id=request_id,
                              phone=destination, status=status, message_sid=message_sid,
                              follow_up_needed=status in {'failed', 'undelivered', 'uncertain'}))
    return True


class ScheduledSMS:
    def __init__(self, communications):
        self.communications = communications
        self.worker = None
        self.ready = False

    async def process_one(self):
        row = await asyncio.to_thread(claim)
        if not row: return False
        try:
            context = row.get('context') or {}
            if context.get('kind') == 'promise':
                from promises import revalidate
                if not await asyncio.to_thread(revalidate,context,row['phone']):
                    await asyncio.to_thread(receipt,row['request_id'],status='cancelled',error_code='promise_changed')
                    return True
            if context.get('kind') == 'appointment_reminder':
                from appointment_reminders import revalidate
                if not await asyncio.to_thread(revalidate,context,row['phone']):
                    await asyncio.to_thread(receipt,row['request_id'],status='cancelled',error_code='event_changed')
                    return True
            client = self.communications.client()
            callback = f'https://{self.communications.domain}/outbound/sms-status/{quote(row["request_id"], safe="")}/{row["callback_token"]}'
            result = await asyncio.to_thread(client.messages.create,
                to=row['phone'], from_=self.communications.tac.config.phone_number,
                body=row['body'], status_callback=callback, validity_period=900)
            await asyncio.to_thread(receipt, row['request_id'], result.sid,
                                    result.status if result.status in PROVIDER else 'submitted',
                                    getattr(result, 'error_code', None))
        except Exception as exc:
            # Only an explicit Twilio HTTP rejection is known to be unsent.
            from twilio.base.exceptions import TwilioRestException
            rejected = isinstance(exc, TwilioRestException) and 400 <= exc.status < 500
            await asyncio.to_thread(receipt, row['request_id'], status='failed' if rejected else 'uncertain',
                                    error_code=getattr(exc, 'code', None))
            log.warning('outbound_sms_attempt_ended request_id=%s status=%s', row['request_id'], 'failed' if rejected else 'uncertain')
        return True

    async def run(self):
        while True:
            try:
                if not await self.process_one(): await asyncio.sleep(2)
            except asyncio.CancelledError: raise
            except Exception:
                log.error('outbound_sms_worker_failed')
                await asyncio.sleep(5)

    def install(self, app):
        async def owner_key(x_demo_key: str = Header(default='')):
            expected = os.getenv('DEMO_KEY', '')
            if not expected or not hmac.compare_digest(expected, x_demo_key): raise HTTPException(401, 'Unauthorized')

        @app.post('/outbound/sms', dependencies=[Depends(owner_key)])
        async def schedule(body: TextMessage):
            try: return await asyncio.to_thread(enqueue, body)
            except ValueError as exc: raise HTTPException(409, str(exc))

        @app.get('/outbound/sms/{request_id}', dependencies=[Depends(owner_key)])
        async def read(request_id: str): return await asyncio.to_thread(get, request_id)

        @app.post('/outbound/sms/{request_id}/cancel', dependencies=[Depends(owner_key)])
        async def cancel_text(request_id: str): return await asyncio.to_thread(cancel, request_id)

        signed = build_http_signature_dependency(self.communications.tac.config.auth_token)
        @app.post('/outbound/sms-status/{request_id}/{token}', dependencies=[Depends(signed)])
        async def status(request_id: str, token: str, request: Request):
            form = await request.form()
            if form.get('AccountSid') != self.communications.tac.config.account_sid or form.get('From') != self.communications.tac.config.phone_number:
                raise HTTPException(403, 'Account or sender mismatch')
            sid = str(form.get('MessageSid', ''))
            if len(sid) != 34 or not sid.startswith('SM'): raise HTTPException(400, 'Invalid message SID')
            await asyncio.to_thread(receipt, request_id, sid, str(form.get('MessageStatus', '')),
                                    form.get('ErrorCode'), token, str(form.get('To', '')))
            return {'ok': True}

        original = app.router.lifespan_context
        @asynccontextmanager
        async def lifespan(app):
            await asyncio.to_thread(store.initialize)
            self.ready = True
            self.worker = asyncio.create_task(self.run())
            try:
                async with original(app): yield
            finally:
                self.ready = False
                self.worker.cancel()
                with contextlib.suppress(asyncio.CancelledError): await self.worker
        app.router.lifespan_context = lifespan
