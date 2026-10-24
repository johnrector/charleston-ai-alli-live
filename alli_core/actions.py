"""Execute a permitted operation once and keep its real provider receipt."""
import asyncio
from weakref import WeakValueDictionary
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol


@dataclass(frozen=True)
class Tool:
    definition: dict
    invoke: Callable[..., Awaitable[dict]]
    writes: bool = False

    @property
    def name(self):
        return self.definition['name']


class ActionStore(Protocol):
    def permitted(self, task_id: str, *, writes: bool) -> bool: ...
    def claim_action(self, task_id: str, call_id: str, tool: str,
                     arguments: dict, writes: bool) -> dict: ...
    def save_action(self, task_id: str, call_id: str, result: dict) -> None: ...


class ActionGate:
    def __init__(self, store: ActionStore):
        self.store = store
        self._write_locks = WeakValueDictionary()

    async def execute(self, task_id, call_id, tool: Tool, arguments: dict):
        if tool.writes:
            # Native Agents may request independent tools concurrently. Serialize
            # a task's writes here; its database ledger remains the cross-process
            # authority. Do not mistake this process's active write for a crash.
            lock = self._write_locks.get(task_id)
            if lock is None:
                lock = asyncio.Lock()
                self._write_locks[task_id] = lock
            async with lock:
                return await self._execute(task_id, call_id, tool, arguments)
        return await self._execute(task_id, call_id, tool, arguments)

    async def _execute(self, task_id, call_id, tool: Tool, arguments: dict):
        if not isinstance(arguments, dict):
            return {'ok': False, 'not_attempted': True, 'error': 'Invalid tool arguments.'}
        if not await asyncio.to_thread(self.store.permitted, task_id, writes=tool.writes):
            return {'ok': False, 'not_attempted': True,
                    'error': 'This operation is not authorized for the current assignment.'}
        claim = await asyncio.to_thread(self.store.claim_action, task_id,
            call_id, tool.name, arguments, tool.writes)
        if claim['state'] == 'done':
            return claim['result']
        if claim['state'] != 'claimed':
            return {'ok': False, 'uncertain': True,
                    'error': 'An earlier attempt has no verified receipt. Do not retry.'}
        try:
            result = await tool.invoke(**arguments)
            if not isinstance(result, dict):
                raise TypeError('Tools must return a receipt object')
        except Exception:
            result = {'ok': False, 'uncertain': tool.writes,
                      'error': 'Provider action is unconfirmed.' if tool.writes else 'Lookup failed.'}
        await asyncio.to_thread(self.store.save_action, task_id, call_id, result)
        return result
