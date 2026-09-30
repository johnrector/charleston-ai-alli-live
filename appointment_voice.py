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

    async def initiate(self, body):
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
            if existing and existing['context'] != context:
                raise HTTPException(409, 'This request ID belongs to different call details; do not reuse it')
            return await self.read_result(body.request_id)
        token = secrets.token_urlsafe(32)
        tool = outcome_tool(key, self.store)
        call_sid_ref = {'value': None}

        @function_tool()
        async def end_call() -> dict:
            """End this call after a wrong person, refusal, voicemail, or completed conversation. Save the outcome first when appropriate. No other phone call can be selected."""
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
        session['instructions'] += '\nCALL ENDING: After recording the final outcome, use end_call to hang up. For a wrong person, refusal or voicemail, end_call may be used immediately. It can only end this call.\n'
        channel = VoiceChannel(self.tac, config=GPTLiveProviderConfig(
            tools=tools_for(body, tool) + [end_call], default_session_config=None,
            welcome_instruction='Speak immediately. Follow the purpose-specific OPENING in this session and then listen.'))
        channel._alli_ledger_key = key
        self.channels[token] = channel
        domain = os.environ.get('TWILIO_VOICE_PUBLIC_DOMAIN', '')
        try:
            if not domain or '/' in domain or '?' in domain:
                raise ValueError('Valid public voice domain is required')
            result = await channel.initiate_outbound_conversation(InitiateVoiceConversationOptionsGPTLive(
                to=body.phone, websocket_url=f'wss://{domain}/outbound/ws/{token}',
                session_config=session,
                call_options=CallOptions(machine_detection='Enable', async_amd=True,
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
                'outcome': row.get('outcome')}

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
        return {'provider_lookup': 'verified', 'provider_status': call.status,
                'answered_by': call.answered_by, 'duration_seconds': call.duration}

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
                    'calendar event enumeration and revalidation adapter not installed',
                    'owner-approved eligible event and verified contact source not configured',
                    'recipient timezone, quiet hours and voicemail policy need approval',
                    'weekly follow-up latest-appointment selection not installed',
                    'approved real-call and database integration validation still required'],
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
            # Conservative hang-up policy: machines, fax, unknown all stop.
            detected = str(form.get('AnsweredBy', 'unknown'))
            if detected not in {'human', 'fax', 'unknown', 'machine_start', 'machine_end_beep', 'machine_end_silence', 'machine_end_other'}:
                detected = 'unknown'
            logging.getLogger('alli.call').info('outbound_amd call_sid=%s answered_by=%s', sid, detected)
            if detected != 'human':
                await asyncio.to_thread(self.store.record_outcome, channel._alli_ledger_key, {
                    'attendance': 'unclear', 'questions': [], 'follow_up_needed': True,
                    'summary': 'Answer classification was ' + detected + '; the conservative gate requested hang-up before a human conversation was confirmed.',
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
