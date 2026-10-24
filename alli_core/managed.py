"""OpenAI owns the durable agent, reasoning loop and MCP tool connections.

The application records dispatch intent before creation and never retries an
uncertain session creation. Recovery finds its saved session by task metadata.
"""
import asyncio
import json
from dataclasses import dataclass, field
from typing import Protocol
from openai import APIStatusError

from .profile import Profile
from .prompts import work_instructions
from .execution import Execution
from .outcome import Outcome


@dataclass(frozen=True)
class Assignment:
    id: str
    input_text: str
    instructions: str


@dataclass(frozen=True)
class ToolConnection:
    url: str
    authorization: str = field(repr=False)
    allowed_tools: tuple[str, ...] | None = None

    def configuration(self):
        configuration = {'type': 'mcp', 'server_label': 'executive_tools',
            'transport': {'type': 'http', 'server_url': self.url,
                          'authorization': self.authorization},
            'connection_origin': 'service', 'required': True}
        if self.allowed_tools is not None:
            configuration['allowed_tools'] = list(self.allowed_tools)
        return configuration


class TaskStore(Protocol):
    def execution_allowed(self, task_id: str, kind: str) -> bool: ...
    def session_id(self, task_id: str) -> str | None: ...
    def claim_dispatch(self, task_id: str) -> bool: ...
    def bind_session(self, task_id: str, session_id: str) -> None: ...
    def dispatch_uncertain(self, task_id: str) -> None: ...
    def record_outcome(self, task_id: str, status: str, text: str, *, turn_id: str,
                       remaining: tuple[str, ...] = (), notify: bool = True,
                       provider_error: str | None = None): ...


class ManagedRuntime:
    def __init__(self, api, profile: Profile, store: TaskStore):
        # Session creation has no verified provider idempotency guarantee. An
        # SDK retry after a lost reply could otherwise create a second worker.
        self.api, self.profile, self.store = api.with_options(max_retries=0), profile, store

    async def dispatch(self, assignment: Assignment, connection: ToolConnection,
                       execution: Execution = Execution()):
        configuration = execution.configuration()
        if not await asyncio.to_thread(self.store.execution_allowed, assignment.id, execution.kind):
            raise PermissionError('This assignment cannot use the requested execution environment')
        existing = await asyncio.to_thread(self.store.session_id, assignment.id)
        if existing:
            return {'state': 'submitted', 'session_id': existing}
        if not await asyncio.to_thread(self.store.claim_dispatch, assignment.id):
            return {'state': 'recovering', 'session_id': None}
        try:
            session = await self.api.beta.agents.sessions.create(
                agent={'model': self.profile.work_model,
                    'reasoning': {'effort': self.profile.reasoning_effort},
                    'instructions': self.profile.charter+'\n'+work_instructions(self.profile)+'\n'+assignment.instructions,
                    'text': {'format': Outcome.format()},
                    'tools': [{'type': 'web_search'}, connection.configuration()]},
                environment=configuration, input=assignment.input_text,
                metadata={'assistant_id': self.profile.id, 'task_id': assignment.id})
            if session.agent.model != self.profile.work_model or session.agent.reasoning.effort != self.profile.reasoning_effort:
                await self.api.beta.agents.sessions.events.create(session.id,
                    events=[{'type': 'agent.session.input.cancel'}])
                raise RuntimeError('OpenAI did not accept the configured model and reasoning')
            await asyncio.to_thread(self.store.bind_session, assignment.id, session.id)
            return {'state': 'submitted', 'session_id': session.id}
        except APIStatusError as error:
            if 400<=error.status_code<500 and error.status_code not in {408,409,429}:
                await asyncio.to_thread(self.store.record_outcome,assignment.id,'failed',
                    'I could not start this assignment because the work service rejected the request '
                    f'(HTTP {error.status_code}). No task tool actions were attempted.',
                    turn_id='dispatch-rejected:'+str(error.status_code))
            else:await asyncio.to_thread(self.store.dispatch_uncertain,assignment.id)
            raise
        except BaseException:
            # Creation may have succeeded even if its reply or database commit
            # was lost. Do not create a second agent that could repeat actions.
            await asyncio.to_thread(self.store.dispatch_uncertain, assignment.id)
            raise

    async def recover(self, assignment: Assignment):
        existing = await asyncio.to_thread(self.store.session_id, assignment.id)
        if existing:
            return existing
        page = await self.api.beta.agents.sessions.list(limit=100, order='desc')
        matches = []
        while True:
            matches.extend(session for session in page.data
                if session.metadata.get('assistant_id') == self.profile.id
                and session.metadata.get('task_id') == assignment.id)
            if not page.has_next_page():
                break
            page = await page.get_next_page()
        if len(matches) != 1:
            return None
        await asyncio.to_thread(self.store.bind_session, assignment.id, matches[0].id)
        return matches[0].id

    async def follow_up(self, session_id, text, submission_key, *, role='user'):
        if role not in {'user','developer'}:
            raise ValueError('Invalid application input role')
        # The native API accepts user messages only. Keep application evidence
        # provenance explicit in the content and in the saved input record;
        # do not send an unsupported developer wire role.
        content=text if role=='user' else json.dumps({'type':'application_evidence',
            'new_authorization':False,'text':text},ensure_ascii=False)
        # The caller persists this logical message and key before invoking us.
        await self.api.beta.agents.sessions.events.create(session_id,
            idempotency_key=submission_key, events=[{
                'type': 'agent.session.input.message',
                'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': content}]}]}])

    async def inspect(self, task_id):
        sid = await asyncio.to_thread(self.store.session_id, task_id)
        if not sid:
            return {'state': 'recovering'}
        session = await self.api.beta.agents.sessions.retrieve(sid)
        if session.metadata.get('assistant_id') != self.profile.id or session.metadata.get('task_id') != task_id:
            raise ValueError('Agent session does not belong to this assignment')
        if session.status == 'in_progress':
            return {'state': 'working'}
        if session.status == 'requires_action':
            actions=getattr(session,'required_actions',[])
            kinds=sorted({action.type for action in actions})
            if any(kind!='environment_connection' for kind in kinds):
                import hashlib
                identity=hashlib.sha256(str([(a.type,getattr(a,'request_id','')) for a in actions]).encode()).hexdigest()
                await asyncio.to_thread(self.store.record_outcome,task_id,'waiting',
                    'This assignment needs a one-time account connection or permission before it can continue. '
                    'The native session is retained; completed actions will not be repeated.',
                    turn_id='required-action:'+identity,remaining=tuple(kinds),notify=True)
            return {'state': 'needs_attention'}
        page = await self.api.beta.agents.sessions.turns.list(sid, limit=1, order='desc')
        if not page.data:
            return {'state': 'working'}
        turn = page.data[0]
        if turn.status not in {'completed', 'failed', 'cancelled'}:
            return {'state': 'working'}
        items = await self.api.beta.agents.sessions.items.list(sid, order='desc', limit=100)
        finals = [item for item in items.data if item.type == 'message'
            and item.turn_id == turn.id and item.role == 'assistant'
            and item.phase == 'final_answer' and item.status == 'completed']
        text = finals[0].output_text if finals else ''
        status = turn.status
        remaining, notify = (), True
        code=getattr(getattr(turn,'error',None),'code',None)
        if status in {'failed','cancelled'} and not text:
            problems={
                'usage_limit_exceeded':'OpenAI has stopped further AI work because the API organization reached a usage or billing limit. John needs to check API credits and spending limits. This is separate from ChatGPT/Codex allowances.',
                'insufficient_quota':'OpenAI API credits or spending limits currently block further AI work. John needs to check API billing.',
                'rate_limit_exceeded':'OpenAI temporarily rate-limited this assignment. Completed actions are retained; no uncertain action was repeated.'}
            text=problems.get(code,'I could not complete this assignment. Its activity and any actual action receipts are saved for review.')
            if code in problems:remaining=(text,)
        if turn.status == 'completed':
            try:
                report = Outcome.model_validate_json(text)
                status, text = report.status, report.summary
                remaining, notify = tuple(report.remaining), report.notify
                if report.remaining and status == 'completed':
                    status = 'partial'
            except ValueError:
                status = 'partial'
                text = 'The agent stopped without a valid completion report. Review the saved activity.'
        # The store reconciles actual action receipts before deciding that the
        # assignment succeeded or notifying its recipient.
        reconciled = await asyncio.to_thread(self.store.record_outcome, task_id, status,
                                text, turn_id=turn.id, remaining=remaining, notify=notify,
                                provider_error=code)
        return {'state': reconciled.state if reconciled else status,
                'text': reconciled.summary if reconciled else text, 'turn_id': turn.id}
