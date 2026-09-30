"""Optional server-owned calendar polling. Nothing runs unless explicitly enabled."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import os
from fastapi import Header, HTTPException
from google_integration import require_admin
from appointment_calendar import (
    CalendarSelectionPolicy, ContactDirectory, GoogleCalendarReader, CalendarSelector,
)

log = logging.getLogger('alli.call')


def load_configuration():
    # Policy/contact data are controlled by the administrator, never by the caller,
    # calendar description, recipient speech, or model-generated tool arguments.
    policy = CalendarSelectionPolicy.model_validate_json(os.getenv('APPOINTMENT_POLICY_JSON', '{}'))
    directory = ContactDirectory.model_validate_json(os.getenv('APPOINTMENT_CONTACTS_JSON', '{}'))
    return policy, directory


def automation_enabled():
    return os.getenv('APPOINTMENT_AUTOMATION_ENABLED', '').lower() == 'true'


class AppointmentAutomation:
    def __init__(self, voice, config_loader=load_configuration, reader=None, clock=None):
        self.voice = voice
        self.config_loader = config_loader
        self.reader = reader or GoogleCalendarReader()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._running = asyncio.Lock()

    async def preview(self):
        policy, directory = self.config_loader()
        selector = CalendarSelector(self.reader, lambda: directory, policy)
        batch = await asyncio.to_thread(selector.preview, self.clock(), dry_run=True)
        return {'automatic_calls_enabled': automation_enabled(), 'complete': batch.complete,
                'blockers': batch.blockers, 'scope_limit': batch.scope_limit,
                'skipped': batch.skipped,
                'candidates': [self.describe(item) for item in batch.selections]}

    @staticmethod
    def describe(item):
        return {'dispatch_id': item.dispatch_key(), 'mode': item.mode,
                'event_id': item.appointment.event_id,
                'recipient_id': item.appointment.recipient_id,
                'appointment_start': item.appointment.start.isoformat(),
                'blockers': item.blockers, 'dispatch_ready': item.dispatch_ready,
                'dry_run_ready': item.dry_run_ready}

    async def run_once(self):
        if not automation_enabled():
            return {'ok': False, 'status': 'disabled', 'calls': []}
        if os.getenv('OUTBOUND_CALLS_ENABLED', '').lower() != 'true':
            return {'ok': False, 'status': 'blocked', 'blockers': ['general_outbound_disabled'], 'calls': []}
        async with self._running:
            policy, directory = self.config_loader()
            selector = CalendarSelector(self.reader, lambda: directory, policy)
            batch = await asyncio.to_thread(selector.preview, self.clock(), dry_run=False)
            if not batch.complete or batch.blockers:
                return {'ok': False, 'status': 'blocked', 'blockers': batch.blockers, 'calls': []}
            calls = []
            for item in batch.selections:
                if not item.dispatch_ready:
                    continue
                if not automation_enabled():
                    break
                body = item.to_outbound_call(policy)

                async def before_dial(selected=item, expected_body=body):
                    if not automation_enabled():
                        return False
                    fresh_policy, _ = self.config_loader()
                    if fresh_policy != policy:
                        return False
                    fresh_selector = CalendarSelector(self.reader, lambda: self.config_loader()[1], fresh_policy)
                    current = await asyncio.to_thread(fresh_selector.revalidate, selected, self.clock(), dry_run=False)
                    return current.dispatch_ready and current.to_outbound_call(fresh_policy) == expected_body

                try:
                    result = await self.voice.initiate(body, before_dial=before_dial)
                except Exception:
                    # A downstream ledger claim may already be durable. Never mint
                    # a new request ID or retry blindly after an uncertain response.
                    result = {'ok': False, 'request_id': body.request_id,
                              'status': 'error_or_uncertain', 'requires_review': True}
                calls.append(result)
            return {'ok': True, 'status': 'checked', 'calls': calls}

    async def loop(self):
        while automation_enabled():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Only exception type is logged; no contacts, event descriptions or secrets.
                log.warning('appointment_scan_failed error_type=%s', type(exc).__name__)
            await asyncio.sleep(60)

    def install(self, app):
        @app.get('/appointment-automation/preview')
        async def preview(x_demo_key: str = Header(default='')):
            require_admin(x_demo_key)
            try:
                return await self.preview()
            except Exception:
                raise HTTPException(503, 'Calendar preview unavailable; verify approved policy, contacts and Google connection') from None

        @app.post('/appointment-automation/run-once')
        async def run_once(x_demo_key: str = Header(default='')):
            require_admin(x_demo_key)
            return await self.run_once()

        original_lifespan = app.router.lifespan_context
        @asynccontextmanager
        async def lifespan(fastapi_app):
            async with original_lifespan(fastapi_app):
                task = asyncio.create_task(self.loop()) if automation_enabled() else None
                try:
                    yield
                finally:
                    if task is not None:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
        app.router.lifespan_context = lifespan
