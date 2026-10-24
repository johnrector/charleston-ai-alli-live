"""Harmless native-API probe: no phone calls, personal data or account tools.

Run with the deployment's OPENAI_API_KEY to verify that environment separately.
The managed-agent test uses only OpenAI's public documentation MCP and is
cancelled after 90 seconds. It closes its submitting client before checking work.
"""
import asyncio
import base64
import json
import logging
import time
import uuid

from openai import AsyncOpenAI
from alli_core.outcome import Outcome

logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpx2').setLevel(logging.WARNING)


async def agent_probe():
    sid = None
    key = 'foundation-'+uuid.uuid4().hex
    try:
        async with AsyncOpenAI(timeout=30, max_retries=0) as client:
            session = await client.beta.agents.sessions.create(
                agent={'model': 'gpt-6.1-sol', 'reasoning': {'effort': 'high'},
                    'instructions': 'This is a harmless API capability test. Follow the single task exactly. Return a valid structured outcome report.',
                    'text':{'format':Outcome.format()},
                    'tools': [{'type':'web_search'},{'type': 'mcp', 'server_label': 'openai_docs',
                        'transport': {'type': 'http', 'server_url': 'https://developers.openai.com/mcp'},
                        'connection_origin': 'service', 'required': True}]},
                environment={'type': 'none'}, input=
                    'Use the OpenAI documentation MCP to check the documented separation between GPT-Live speech and backend reasoning/tools. Return one short sentence naming gpt-live-1 and gpt-6.1-sol. Use no other services.',
                metadata={'purpose': 'executive-assistant-foundation-probe'})
            sid = session.id
            if session.agent.model!='gpt-6.1-sol' or session.agent.reasoning.effort!='high':
                raise RuntimeError('The requested reasoning model was not accepted')
        # The submitting client is closed. Work must continue at OpenAI.
        async with AsyncOpenAI(timeout=30, max_retries=0) as client:
            deadline = time.monotonic()+90
            while time.monotonic()<deadline:
                turns = await client.beta.agents.sessions.turns.list(sid, limit=1)
                if turns.data and turns.data[0].status in {'completed', 'failed', 'cancelled'}:
                    turn = turns.data[0]
                    page = await client.beta.agents.sessions.items.list(sid, order='desc', limit=100)
                    messages = [item for item in page.data if item.type == 'message'
                        and item.role == 'assistant' and item.phase == 'final_answer']
                    calls = [item for item in page.data if item.type == 'mcp_call']
                    return {'managed_agent': turn.status, 'model': 'gpt-6.1-sol',
                        'reasoning': 'high', 'environment': 'none',
                        'survived_client_disconnect': turn.status == 'completed',
                        'mcp_calls': [{'name': item.name, 'status': item.status, 'error': bool(item.error)} for item in calls],
                        'result': messages[0].output_text if messages else '',
                        'error_code': getattr(turn.error, 'code', None) if turn.error else None}
                await asyncio.sleep(2)
            return {'managed_agent': 'timeout', 'survived_client_disconnect': False}
    except Exception as exc:
        body = getattr(exc, 'body', {}) or {}
        detail = (body.get('error', body) if isinstance(body, dict) else {})
        return {'managed_agent': 'failed', 'error_type': type(exc).__name__,
                'status': getattr(exc, 'status_code', None), 'code': getattr(exc, 'code', None),
                'parameter': getattr(exc, 'param', None),
                'validation_detail': str(detail.get('message', ''))[:450] if getattr(exc, 'status_code', None) == 400 else ''}
    finally:
        if sid:
            async with AsyncOpenAI(timeout=20, max_retries=0) as client:
                session=await client.beta.agents.sessions.retrieve(sid)
                if session.status not in {'idle','failed'}:
                    await client.beta.agents.sessions.events.create(sid,events=[{'type':'agent.session.input.cancel'}])
                    for _ in range(20):
                        await asyncio.sleep(.5)
                        session=await client.beta.agents.sessions.retrieve(sid)
                        if session.status in {'idle','failed'}:break
                if session.status in {'idle','failed'}:await client.beta.agents.sessions.delete(sid)


async def live_probe():
    started = False
    errors = []
    try:
        async with AsyncOpenAI(timeout=25, max_retries=0) as client:
            async with client.live.connect() as connection:
                await connection.session.start(session={'model': 'gpt-live-1',
                    'instructions': 'Silent connection test. Do not speak. No caller is present.',
                    'audio': {'format': {'type': 'audio/pcmu', 'rate': 8000}, 'output': {'voice': 'marin'}},
                    'delegation': {'type': 'client'}})
                async with asyncio.timeout(20):
                    async for event in connection:
                        if event.type == 'session.started':
                            started = True
                            for _ in range(3):
                                await connection.session.input_audio.append(audio=base64.b64encode(b'\xff'*160).decode())
                                await asyncio.sleep(.02)
                            await connection.session.close()
                        elif event.type == 'error':
                            errors.append(getattr(event.error, 'code', 'unknown'))
                        elif event.type == 'session.closed':
                            return {'live_started': started, 'live_finalized': True, 'error_codes': errors}
        return {'live_started': started, 'live_finalized': False, 'error_codes': errors}
    except Exception as exc:
        return {'live_started': started, 'live_finalized': False,
                'error_type': type(exc).__name__, 'status': getattr(exc, 'status_code', None)}


async def main():
    results = await asyncio.gather(agent_probe(), live_probe())
    print(json.dumps({'context': 'local key; production credentials still require a separate probe',
                      'agent': results[0], 'voice': results[1]}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
