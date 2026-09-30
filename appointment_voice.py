"""Isolated outbound profiles on TAC 2.5. No schedules or calls at import time."""
import asyncio
import hashlib
import logging
import os
import secrets
from typing import Literal
from fastapi import Depends, Header, HTTPException, Request, WebSocket
from tac.channels.voice import VoiceChannel
from tac.channels.voice.media_streams.gpt_live import GPTLiveProviderConfig
from tac.models.outbound import InitiateVoiceConversationOptionsGPTLive, CallOptions
from tac.server.fastapi_server import FastAPIWebSocketAdapter
from tac.server.signature_validation import build_http_signature_dependency, build_websocket_signature_dependency
from tac.tools import function_tool
from pydantic import BaseModel, ConfigDict, Field
import appointment_store as ledger
from google_integration import require_admin


def voicemail_message(body):
    # Do not copy mission, recipient name, event title, logistics, times, addresses
    # or any private calendar/contact data into a mailbox. No unverified callback promise.
    reason = "I am calling on John's behalf."
    if body.preset == 'scheduling':
        reason = 'I am calling to find a time to connect with John.'
    elif body.preset == 'follow_up':
        reason = "I am following up on John's behalf."
    return ("Hello, this is Alli, John Rector's AI assistant with Charleston AI. "
            + reason + ' I will let John know I reached your voicemail. Thank you.')


def request_key(request_id):
    return hashlib.sha256(('outbound:' + request_id).encode()).hexdigest()


class CallOutcome(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attendance: Literal['confirmed', 'not_attending', 'reschedule_requested', 'unclear', 'not_applicable']
    questions: list[str] = Field(max_length=10)
    follow_up_needed: bool
    summary: str = Field(max_length=2000)


def outcome_tool(key, store=ledger):
    # A distinct closure for every call. The model cannot select another call ID.
    @function_tool()
    async def report_call_outcome(attendance: str, questions: list[str], follow_up_needed: bool, summary: str) -> dict:
        """Record the recipient's stated outcome once at the end of this call. Never infer attendance from silence; use unclear. This records a report, not a calendar or email action."""
        outcome = CallOutcome(attendance=attendance, questions=questions,
                              follow_up_needed=follow_up_needed, summary=summary)
        if any(len(question) > 500 for question in outcome.questions):
            return {'ok': False, 'error': 'Question exceeds length limit'}
        value = outcome.model_dump()
        value['source'] = 'conversation_report'
        saved = await asyncio.to_thread(store.record_outcome, key, value)
        return {'ok': saved, 'recorded': saved}
    return report_call_outcome


class OutboundVoice:
    """One executable tool registry and outcome closure per outbound call.

    Channels are retained only while pending/active. Default sessions are absent:
    an uncorrelated/replayed connection cannot fall back to the demo instructions.
    Requires one process, as TAC's session transport is process-local.
    """
    def __init__(self, tac, base_session, store=ledger, read_provider=False):
        self.tac, self.base_session, self.store = tac, base_session, store
        self.channels = {}
        self.max_pending = 100
        self.read_provider = read_provider

    async def initiate(self, body, before_dial=None):
        enabled = os.getenv('OUTBOUND_CALLS_ENABLED', '').lower() == 'true'
        test_phone = os.getenv('OUTBOUND_TEST_PHONE', '')
        if not enabled:
            if not test_phone or body.phone != test_phone:
                raise HTTPException(503, 'General outbound calls are disabled; only the configured owner test recipient is permitted')
            if body.capabilities:
                raise HTTPException(503, 'Owner test mode does not permit calendar actions')
        from outbound_profiles import session_for, tools_for
        key = request_key(body.request_id)
        context = body.model_dump(mode='json')
        if len(self.channels) >= self.max_pending:
            raise HTTPException(503, 'Call capacity reached; review existing calls before retrying')
        if not await asyncio.to_thread(self.store.claim, key, context):
            existing = await asyncio.to_thread(self.store.get, key)
            comparison = dict(context)
            if existing and 'voicemail_policy' not in existing['context']:
                # Pre-voicemail records remain readable after this schema addition.
                # This branch never dials or rewrites the old intent.
                comparison.pop('voicemail_policy', None)
            if existing and existing['context'] != comparison:
                raise HTTPException(409, 'This request ID belongs to different call details; do not reuse it')
            return await self.read_result(body.request_id)
        if before_dial is not None:
            try:
                if not await before_dial():
                    raise ValueError('Fresh appointment validation did not approve dispatch')
            except Exception:
                await asyncio.to_thread(self.store.blocked, key, 'fresh_appointment_validation_failed')
                return {'ok': False, 'request_id': body.request_id, 'status': 'blocked'}
        token = secrets.token_urlsafe(32)
        tool = outcome_tool(key, self.store)
        call_sid_ref = {'value': None}

        @function_tool()
        async def end_call(reason: str = 'completed') -> dict:
            """End this call after a wrong person, refusal, or completed human conversation. Do not end voicemail greetings; the service waits for the beep. Save the outcome first when appropriate. No other phone call can be selected."""
            if reason not in {'completed', 'wrong_person', 'refused'}:
                return {'ok': False, 'error': 'Use completed, wrong_person, or refused'}
            if reason == 'completed' and getattr(channel, '_alli_answered_by', None) != 'human':
                return {'ok': False, 'error': 'Wait for answer detection; voicemail is handled by the service'}
            sid = call_sid_ref['value']
            if not sid:
                row = await asyncio.to_thread(self.store.get, key)
                sid = row.get('call_sid') if row else None
            if not sid:
                return {'ok': False, 'error': 'This call binding is not ready'}
            logging.getLogger('alli.call').info('outbound_end_call call_sid=%s source=conversation_tool', sid)
            await channel.end_call(sid)
            return {'ok': True}

        session = session_for(body, self.base_session, tool)
        session['delegation']['responses']['tools'].append(end_call.to_realtime_format())
        session['instructions'] += '\nCALL ENDING: After recording the final outcome, use end_call to hang up. For a wrong person or refusal, use end_call with reason=wrong_person or reason=refused immediately. If you hear voicemail, stop speaking and wait for the service, rather than using end_call. It can only end this call.\n'
        channel = VoiceChannel(self.tac, config=GPTLiveProviderConfig(
            tools=tools_for(body, tool) + [end_call], default_session_config=None,
            welcome_instruction='Speak immediately. Follow the purpose-specific OPENING in this session and then listen.'))
        channel._alli_ledger_key = key
        channel._alli_voicemail_message = voicemail_message(body)
        channel._alli_voicemail_policy = body.voicemail_policy
        channel._alli_answered_by = None
        self.channels[token] = channel
        domain = os.environ.get('TWILIO_VOICE_PUBLIC_DOMAIN', '')
        try:
            if not domain or '/' in domain or '?' in domain:
                raise ValueError('Valid public voice domain is required')
            result = await channel.initiate_outbound_conversation(InitiateVoiceConversationOptionsGPTLive(
                to=body.phone, websocket_url=f'wss://{domain}/outbound/ws/{token}',
                session_config=session,
                call_options=CallOptions(machine_detection=('DetectMessageEnd' if body.voicemail_policy == 'generic_message' else 'Enable'), async_amd=True,
                    async_amd_status_callback=f'https://{domain}/outbound/amd/{token}',
                    status_callback=f'https://{domain}/outbound/status/{token}',
                    status_callback_event=['completed'], timeout=25, time_limit=900)))
            call_sid_ref['value'] = result.call_sid
            if not await asyncio.to_thread(self.store.queued, key, result.call_sid):
                raise RuntimeError('Call accepted but durable SID binding could not be verified; do not retry')
            # Terminal callbacks or timeout remove transport state; ledger remains durable.
            asyncio.get_running_loop().call_later(960, self.channels.pop, token, None)
            return {'ok': True, 'request_id': body.request_id, 'status': 'queued', 'call_sid': result.call_sid,
                    'outcome': None, 'disposition': None}
        except BaseException:
            # Keep channel briefly for an accepted call whose HTTP response was lost.
            asyncio.get_running_loop().call_later(960, self.channels.pop, token, None)
            await asyncio.to_thread(self.store.uncertain, key)
            raise

    @staticmethod
    def public_result(request_id, row):
        if not row: return {'ok': False, 'request_id': request_id, 'status': 'not_found'}
        return {'ok': True, 'request_id': request_id, 'status': row['status'],
                'call_sid': row.get('call_sid'), 'disposition': row.get('disposition'),
                'outcome': row.get('outcome'), 'voicemail': row.get('voicemail')}

    async def read_result(self, request_id):
        row = await asyncio.to_thread(self.store.get, request_key(request_id))
        result = self.public_result(request_id, row)
        if self.read_provider and row and row.get('call_sid'):
            try:
                details = await asyncio.to_thread(self._provider_details, row['call_sid'])
                result.update(details)
            except Exception:
                result['provider_lookup'] = 'unavailable'
        return result

    def _provider_details(self, call_sid):
        # Exact previously claimed call only. Never enumerate calls or create one.
        from twilio.rest import Client
        from twilio.http.http_client import TwilioHttpClient
        client = Client(self.tac.config.api_key, self.tac.config.api_secret,
                        self.tac.config.account_sid, http_client=TwilioHttpClient(timeout=5))
        call = client.calls(call_sid).fetch()
        details = {'provider_lookup': 'verified', 'provider_status': call.status,
                   'answered_by': call.answered_by, 'duration_seconds': call.duration}
        try:
            numbers = client.incoming_phone_numbers.list(phone_number=self.tac.config.phone_number, limit=2)
            if len(numbers) == 1:
                number = numbers[0]
                details['callback_support'] = {
                    'configuration_checked': True,
                    'voice_capable': bool((number.capabilities or {}).get('voice')),
                    'voice_url': number.voice_url or None,
                    'voice_application_configured': bool(number.voice_application_sid),
                    'verified_end_to_end': False,
                    'return_route_in_voicemail': False,
                }
            else:
                details['callback_support'] = {'configuration_checked': False, 'verified_end_to_end': False}
        except Exception:
            details['callback_support'] = {'configuration_checked': False, 'verified_end_to_end': False}
        return details

    @staticmethod
    def _submit_voicemail(channel, call_sid, message):
        from twilio.twiml.voice_response import VoiceResponse
        response = VoiceResponse()
        response.say(message, voice='Polly.Joanna', language='en-US')
        response.hangup()
        # Replacing TwiML stops the live media stream and plays only this escaped,
        # fixed privacy-safe text after AMD's end-of-greeting callback.
        return channel._get_twilio_client().calls(call_sid).update(twiml=str(response))

    def install(self, app, call_model):
        http_sig = build_http_signature_dependency(self.tac.config.auth_token)
        ws_sig = build_websocket_signature_dependency(self.tac.config.auth_token)

        @app.get('/outbound-readiness')
        async def readiness(x_demo_key: str = Header(default='')):
            require_admin(x_demo_key)
            return {
                'manual_calls_enabled': os.getenv('OUTBOUND_CALLS_ENABLED', '').lower() == 'true',
                'owner_only_test_configured': bool(os.getenv('OUTBOUND_TEST_PHONE')),
                'persistence_configured': bool(os.getenv('DATABASE_URL')),
                'voice_domain_configured': bool(os.getenv('TWILIO_VOICE_PUBLIC_DOMAIN')),
                'automatic_calls_enabled': False,
                'automatic_call_blockers': [
                    'automation disabled unless APPOINTMENT_AUTOMATION_ENABLED is true',
                    'approved APPOINTMENT_POLICY_JSON and APPOINTMENT_CONTACTS_JSON required',
                    'explicit owned-calendar event opt-ins and verified consent required',
                    'quiet hours, follow-up timing and voicemail policy require approval',
                    'end-to-end conversation/voicemail validation remains required'],
                'note': 'Configuration presence is not a live integration check',
            }

        @app.post('/outbound-call')
        async def outbound_call(body: call_model, x_demo_key: str = Header(default='')):
            require_admin(x_demo_key)
            return await self.initiate(body)

        @app.get('/outbound-calls/{request_id}')
        async def result(request_id: str, x_demo_key: str = Header(default='')):
            require_admin(x_demo_key)
            return await self.read_result(request_id)

        @app.websocket('/outbound/ws/{token}')
        async def websocket(websocket: WebSocket, token: str, _: None = Depends(ws_sig)):
            channel = self.channels.get(token)
            if channel is None:
                await websocket.close(code=1008)
                return
            await channel.handle_websocket(FastAPIWebSocketAdapter(websocket))

        @app.post('/outbound/amd/{token}', dependencies=[Depends(http_sig)])
        async def amd(token: str, request: Request):
            channel = self.channels.get(token)
            if channel is None: return {'ok': False, 'status': 'expired'}
            form = await request.form()
            sid = str(form.get('CallSid', ''))
            row = await asyncio.to_thread(self.store.get, channel._alli_ledger_key)
            if row and row.get('call_sid') and row['call_sid'] != sid:
                raise HTTPException(409, 'Call binding mismatch')
            # Only an end-of-greeting signal may trigger the approved message.
            # machine_start is never treated as a beep or immediate hang-up.
            detected = str(form.get('AnsweredBy', 'unknown'))
            if detected not in {'human', 'fax', 'unknown', 'machine_start', 'machine_end_beep', 'machine_end_silence', 'machine_end_other'}:
                detected = 'unknown'
            channel._alli_answered_by = detected
            logging.getLogger('alli.call').info('outbound_amd call_sid=%s answered_by=%s', sid, detected)
            if getattr(channel, '_alli_voicemail_policy', 'generic_message') == 'hang_up' and detected.startswith('machine'):
                await asyncio.to_thread(self.store.record_outcome, channel._alli_ledger_key, {
                    'attendance': 'unclear', 'questions': [], 'follow_up_needed': True,
                    'summary': 'Voicemail detected; per-call no-message policy requested hang-up.',
                    'source': 'telephony_detection', 'answered_by': detected,
                })
                await channel.end_call(sid)
            elif detected in {'machine_end_beep', 'machine_end_silence', 'machine_end_other'}:
                message = channel._alli_voicemail_message
                claimed = await asyncio.to_thread(self.store.claim_voicemail, channel._alli_ledger_key, sid, message)
                if claimed:
                    try:
                        await asyncio.to_thread(self._submit_voicemail, channel, sid, message)
                        await asyncio.to_thread(self.store.finish_voicemail, channel._alli_ledger_key, 'submitted')
                    except Exception:
                        await asyncio.to_thread(self.store.finish_voicemail, channel._alli_ledger_key, 'uncertain')
                        # No retry: playback may already have started.
                        raise HTTPException(503, 'Voicemail submission uncertain; do not replay') from None
            elif detected in {'fax', 'unknown'}:
                await asyncio.to_thread(self.store.record_outcome, channel._alli_ledger_key, {
                    'attendance': 'unclear', 'questions': [], 'follow_up_needed': True,
                    'summary': 'Answer classification was ' + detected + '; no voicemail was attempted because greeting completion was not verified.',
                    'source': 'telephony_detection', 'answered_by': detected,
                })
                await channel.end_call(sid)
            return {'ok': True}

        @app.post('/outbound/status/{token}', dependencies=[Depends(http_sig)])
        async def status(token: str, request: Request):
            channel = self.channels.get(token)
            if channel is None: return {'ok': False, 'status': 'expired'}
            form = await request.form()
            disposition = str(form.get('CallStatus', ''))
            saved = await asyncio.to_thread(self.store.record_disposition, channel._alli_ledger_key,
                                           str(form.get('CallSid', '')), disposition)
            if saved and disposition in {'completed','busy','failed','no-answer','canceled'}:
                # Leave a short window for the final outcome tool request to finish.
                asyncio.get_running_loop().call_later(30, self.channels.pop, token, None)
            return {'ok': saved}
