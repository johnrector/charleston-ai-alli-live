"""Owner-invoked daily dependency check; never part of conversation processing."""
import asyncio
import base64
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from alli_core.authority import PermissionDenied


ERROR_CODES = {'invalid_api_key', 'authentication_error', 'insufficient_quota',
               'usage_limit_exceeded', 'rate_limit_exceeded', 'invalid_beta',
               'permission_denied', 'model_not_found', 'project_not_found'}


def failure(error):
    status = getattr(error, 'status_code', None)
    if status is None:
        status = getattr(getattr(error, 'response', None), 'status_code', None)
    if not isinstance(status, int) or not 100 <= status <= 599:
        status = None
    code = getattr(error, 'code', None)
    return {'ok': False, 'error': 'timeout' if isinstance(error, TimeoutError) else 'provider_failure',
            'http_status': status, 'code': code if isinstance(code, str) and code in ERROR_CODES else None}


class DiagnosticStore:
    def __init__(self, repository):
        self.repository = repository

    def claim(self, actor, request_id, today, release):
        if not re.fullmatch(r'daily-health:\d{4}-\d{2}-\d{2}', request_id):
            raise ValueError('Use daily-health:YYYY-MM-DD in the assistant timezone')
        from datetime import date
        day = date.fromisoformat(request_id.removeprefix('daily-health:'))
        with self.repository.connection() as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            self.repository._person(db, actor, owner=True)
            prior = db.execute('SELECT * FROM ea_diagnostic_run WHERE assistant_id=%s AND check_date=%s',
                               (self.repository.profile.id, day)).fetchone()
            if prior:
                return prior, False
            if day != today:
                raise ValueError('A new diagnostic must use today in the assistant timezone')
            row = db.execute('''INSERT INTO ea_diagnostic_run(assistant_id,check_date,request_id,release)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *''',
                (self.repository.profile.id, day, request_id, release)).fetchone()
            if row:
                return row, True
            return db.execute('SELECT * FROM ea_diagnostic_run WHERE assistant_id=%s AND check_date=%s',
                              (self.repository.profile.id, day)).fetchone(), False

    def existing_session(self):
        with self.repository.connection() as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            return db.execute('''SELECT r.session_id,t.id AS task_id FROM ea_agent_run r
                JOIN ea_task t ON t.id=r.task_id WHERE t.assistant_id=%s AND t.scope='owner'
                AND r.session_id IS NOT NULL ORDER BY t.created_at DESC LIMIT 1''',
                (self.repository.profile.id,)).fetchone()

    def finish(self, request_id, result):
        with self.repository.connection() as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            db.execute('''UPDATE ea_diagnostic_run SET result=%s,finished_at=now()
                WHERE assistant_id=%s AND request_id=%s AND result IS NULL''',
                (Jsonb(result), self.repository.profile.id, request_id))


class ServiceDiagnostics:
    TIMEOUT = 20

    def __init__(self, repository, api, release, *, store=None, clock=None):
        self.repository, self.profile = repository, repository.profile
        self.api = api.with_options(max_retries=0, timeout=8)
        self.release = release
        self.store = store or DiagnosticStore(repository)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def bounded(self, operation):
        try:
            async with asyncio.timeout(self.TIMEOUT):
                return await operation()
        except Exception as error:
            return failure(error)

    async def voice(self):
        # Native connection options also bound connection setup and cleanup.
        async with self.api.live.connect(max_retries=0,
                websocket_connection_options={'open_timeout': 5, 'close_timeout': 1}) as connection:
            await connection.session.start(session={
                'model': self.profile.voice_model,
                'instructions': 'Silent diagnostic. No caller is present. Do not speak or delegate work.',
                'audio': {'format': {'type': 'audio/pcmu', 'rate': 8000},
                          'output': {'voice': self.profile.voice}},
                'delegation': {'type': 'client'}})
            started = False
            async for event in connection:
                if event.type == 'session.started' and not started:
                    started = True
                    await connection.session.input_audio.append(audio=base64.b64encode(b'\xff' * 160).decode())
                    await connection.session.close()
                elif event.type == 'error':
                    code = getattr(event.error, 'code', None)
                    return {'ok': False, 'error': 'live_error',
                            'code': code if isinstance(code, str) and code in ERROR_CODES else None}
                elif event.type == 'session.closed':
                    reason = getattr(event, 'reason', None)
                    return {'ok': started and reason in {None, 'close_requested'},
                            'started': started, 'finalized': True, 'model': self.profile.voice_model}
            return {'ok': False, 'error': 'live_not_finalized', 'started': started}

    async def text(self):
        result = await self.api.responses.create(model=self.profile.work_model,
            reasoning={'effort': self.profile.reasoning_effort},
            input='Harmless connection diagnostic. Reply only OK. No tools or personal information.',
            tools=[], store=False, max_output_tokens=256)
        return {'ok': result.status == 'completed' and result.model.startswith(self.profile.work_model),
                'completed': result.status == 'completed', 'model': self.profile.work_model}

    async def agents(self):
        reference = await asyncio.to_thread(self.store.existing_session)
        if not reference:
            return {'ok': False, 'error': 'no_existing_session', 'tested': False}
        session = await self.api.beta.agents.sessions.retrieve(reference['session_id'])
        matches = (session.id == reference['session_id']
            and session.metadata.get('assistant_id') == self.profile.id
            and session.metadata.get('task_id') == reference['task_id'])
        return {'ok': matches, 'existing_session_access': matches,
                'scope': 'read_only_session_access_not_work_dispatch'}

    async def run(self, actor, request_id):
        try:
            async with asyncio.timeout(45):
                return await self._run(actor, request_id)
        except (PermissionDenied, ValueError):
            raise
        except Exception as error:
            # A lost database response may conceal an already committed claim.
            # Return no raw exception/body and never launch a replacement run.
            return {'ok': False, 'request_id': request_id, 'state': 'uncertain',
                    'error': 'diagnostic_timeout' if isinstance(error, TimeoutError) else 'diagnostic_state_unavailable',
                    'automatic_retry': False}

    async def _run(self, actor, request_id):
        now = self.clock()
        today = now.astimezone(ZoneInfo(self.profile.timezone)).date()
        row, claimed = await asyncio.to_thread(self.store.claim, actor, request_id, today, self.release)
        if not claimed:
            if row['result'] is not None:
                return {**row['result'], 'reused': True}
            age = (now - row['started_at']).total_seconds()
            return {'ok': False, 'request_id': row['request_id'], 'reused': True,
                    'state': 'uncertain' if age > 60 else 'running',
                    'automatic_retry': False, 'release': row['release']}
        # A crash/cancellation leaves the durable claim; it is never reclaimed.
        # No ordinary task, ledger notification, outbox or carrier is involved.
        voice, text, agents = await asyncio.gather(*(self.bounded(operation)
            for operation in (self.voice, self.text, self.agents)))
        result = {'ok': all(check['ok'] for check in (voice, text, agents)),
                  'state': 'completed', 'request_id': request_id, 'reused': False,
                  'release': self.release, 'timezone': self.profile.timezone,
                  'started_at': row['started_at'].isoformat(), 'finished_at': self.clock().isoformat(),
                  'checks': {'voice': voice, 'text': text, 'agents': agents},
                  'limits': 'AI dependencies only; no carrier delivery, business tools or future-availability guarantee'}
        await asyncio.to_thread(self.store.finish, request_id, result)
        return result
