import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from alli_core.managed import Assignment, ManagedRuntime, ToolConnection
from alli_core.profile import Profile


class Store:
    def __init__(self):
        self.sid = None; self.claimed = False; self.uncertain = False; self.outcomes = []
        self.allowed_environments = {'none', 'openai_hosted'}
    def execution_allowed(self, task, kind): return kind in self.allowed_environments
    def session_id(self, task): return self.sid
    def claim_dispatch(self, task):
        if self.claimed: return False
        self.claimed = True; return True
    def bind_session(self, task, sid): self.sid = sid
    def dispatch_uncertain(self, task): self.uncertain = True
    def record_outcome(self, *args, **kwargs): self.outcomes.append((args, kwargs))


@pytest.fixture
def setup():
    profile = Profile.load('profiles/alli.toml')
    store = Store()
    session = NS(id='native1', agent=NS(model='gpt-6.1-sol', reasoning=NS(effort='high')),
                 metadata={'assistant_id': 'alli', 'task_id': 'task1'}, status='in_progress')
    sessions = NS(create=AsyncMock(return_value=session),
        retrieve=AsyncMock(return_value=session),
        list=AsyncMock(return_value=NS(data=[session],has_next_page=lambda:False)),
        events=NS(create=AsyncMock()), turns=NS(list=AsyncMock()), items=NS(list=AsyncMock()))
    api = NS(beta=NS(agents=NS(sessions=sessions)))
    api.with_options = lambda **kwargs: api
    return ManagedRuntime(api, profile, store), store, sessions


def test_native_managed_configuration_and_existing_dispatch_not_repeated(setup):
    runtime, store, sessions = setup
    assignment = Assignment('task1', 'Finish this after hangup.', 'Use actual receipts.')
    connection = ToolConnection('https://example.com/tools', 'Bearer private-token', ('calendar',))
    result = asyncio.run(runtime.dispatch(assignment, connection))
    assert result['session_id'] == 'native1'
    kwargs = sessions.create.call_args.kwargs
    assert kwargs['agent']['model'] == 'gpt-6.1-sol'
    assert kwargs['agent']['reasoning'] == {'effort': 'high'}
    assert kwargs['environment'] == {'type': 'none'}
    assert kwargs['agent']['tools'][1]['allowed_tools'] == ['calendar']
    assert 'private-token' not in repr(connection)
    asyncio.run(runtime.dispatch(assignment, connection))
    assert sessions.create.await_count == 1


def test_lost_creation_reply_requires_recovery_not_second_agent(setup):
    runtime, store, sessions = setup
    sessions.create.side_effect = TimeoutError('reply lost')
    assignment = Assignment('task1', 'Do the work.', '')
    connection = ToolConnection('https://example.com/tools', 'Bearer token')
    with pytest.raises(TimeoutError): asyncio.run(runtime.dispatch(assignment, connection))
    assert store.uncertain
    assert asyncio.run(runtime.dispatch(assignment, connection))['state'] == 'recovering'
    assert sessions.create.await_count == 1
    assert asyncio.run(runtime.recover(assignment)) == 'native1'
    assert store.sid == 'native1'


def test_incorrect_reasoning_is_cancelled_and_not_bound(setup):
    runtime, store, sessions = setup
    sessions.create.return_value.agent.reasoning.effort = 'low'
    with pytest.raises(RuntimeError, match='configured model'):
        asyncio.run(runtime.dispatch(Assignment('task1', 'Work.', ''), ToolConnection('https://example.com/tools','Bearer token')))
    assert store.sid is None and store.uncertain
    assert sessions.events.create.call_args.kwargs['events'][0]['type'] == 'agent.session.input.cancel'


def test_follow_up_reuses_persisted_message_key(setup):
    runtime, _, sessions = setup
    asyncio.run(runtime.follow_up('native1', 'Friday, not Thursday.', 'sms-source-id'))
    assert sessions.events.create.call_args.kwargs['idempotency_key'] == 'sms-source-id'


def test_default_tool_connection_does_not_accidentally_disable_all_tools():
    assert 'allowed_tools' not in ToolConnection('https://example.com/tools','Bearer token').configuration()
    assert ToolConnection('https://example.com/tools','Bearer token',()).configuration()['allowed_tools']==[]


def test_idle_is_not_mistaken_for_success(setup):
    runtime, store, sessions = setup
    store.sid = 'native1'
    sessions.retrieve.return_value.status = 'idle'
    sessions.turns.list.return_value = NS(data=[NS(id='turn1', status='failed')])
    sessions.items.list.return_value = NS(data=[])
    result = asyncio.run(runtime.inspect('task1'))
    assert result['state'] == 'failed'
    assert store.outcomes[0][0][1] == 'failed'


def test_another_assignments_session_is_rejected(setup):
    runtime, store, sessions = setup
    store.sid = 'native1'
    sessions.retrieve.return_value.metadata['task_id'] = 'other-task'
    with pytest.raises(ValueError, match='does not belong'):
        asyncio.run(runtime.inspect('task1'))
    assert not store.outcomes


def test_mac_access_is_checked_before_creating_an_agent(setup):
    from alli_core.execution import Execution
    runtime, store, sessions = setup
    local = Execution('self_hosted', '/Users/owner/Alli/work')
    with pytest.raises(PermissionError):
        asyncio.run(runtime.dispatch(Assignment('task1','Use local files.',''),
                    ToolConnection('https://example.com/tools','Bearer token'), local))
    assert sessions.create.await_count == 0 and not store.claimed
    store.allowed_environments.add('self_hosted')
    asyncio.run(runtime.dispatch(Assignment('task1','Use local files.',''),
                ToolConnection('https://example.com/tools','Bearer token'), local))
    assert sessions.create.call_args.kwargs['environment']['type'] == 'self_hosted'
    assert sessions.create.call_args.kwargs['agent']['reasoning'] == {'effort':'high'}


def terminal(setup,text):
    runtime,store,sessions=setup
    store.sid='native1'
    sessions.retrieve.return_value.status='idle'
    sessions.turns.list.return_value=NS(data=[NS(id='turn1',status='completed')])
    sessions.items.list.return_value=NS(data=[NS(type='message',turn_id='turn1',role='assistant',
        phase='final_answer',status='completed',output_text=text)])
    return asyncio.run(runtime.inspect('task1')),store


def test_finished_turn_does_not_override_waiting_work(setup):
    result,store=terminal(setup,'{"status":"waiting","summary":"Which day?","remaining":["Need the date"],"notify":true}')
    assert result['state']=='waiting'
    assert store.outcomes[0][0][1]=='waiting'


def test_unresolved_or_invalid_completion_is_not_success(setup):
    result,store=terminal(setup,'{"status":"completed","summary":"I tried.","remaining":["Email not sent"],"notify":true}')
    assert result['state']=='partial'
    result,store=terminal(setup,'A plain sentence claiming everything is done.')
    assert result['state']=='partial'
