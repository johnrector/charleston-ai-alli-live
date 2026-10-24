import asyncio
from unittest.mock import AsyncMock

from alli_core.actions import ActionGate, Tool


class Store:
    def __init__(self): self.allowed = True; self.actions = {}
    def permitted(self, task, **kwargs): return self.allowed
    def claim_action(self, task, call, name, args, writes):
        if call in self.actions: return self.actions[call]
        self.actions[call] = {'state': 'uncertain'}
        return {'state': 'claimed'}
    def save_action(self, task, call, result): self.actions[call] = {'state': 'done', 'result': result}


def test_write_receipt_is_reused_without_second_provider_request():
    store = Store(); gate = ActionGate(store)
    provider = AsyncMock(return_value={'ok': True, 'message_id': 'provider1'})
    tool = Tool({'name': 'email'}, provider, writes=True)
    for _ in range(2):
        result = asyncio.run(gate.execute('task', 'call1', tool, {'subject': 'Test'}))
        assert result['message_id'] == 'provider1'
    assert provider.await_count == 1


def test_interrupted_write_cannot_be_replayed():
    store = Store(); store.actions['call1'] = {'state': 'uncertain'}
    provider = AsyncMock(); tool = Tool({'name': 'call'}, provider, writes=True)
    result = asyncio.run(ActionGate(store).execute('task', 'call1', tool, {}))
    assert result['uncertain'] and provider.await_count == 0


def test_suspension_blocks_provider_access():
    store = Store(); store.allowed = False
    provider = AsyncMock(); tool = Tool({'name': 'calendar'}, provider)
    result = asyncio.run(ActionGate(store).execute('task', 'call1', tool, {}))
    assert result['not_attempted'] and provider.await_count == 0


def test_uncertain_provider_failure_is_recorded():
    store = Store(); provider = AsyncMock(side_effect=TimeoutError())
    tool = Tool({'name': 'email'}, provider, writes=True)
    result = asyncio.run(ActionGate(store).execute('task', 'call1', tool, {}))
    assert result['uncertain'] and store.actions['call1']['result'] == result
