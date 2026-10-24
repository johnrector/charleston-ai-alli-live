"""Verify OpenAI's cloud harness can use the official local Mac executor.

Creates only a disposable text file. No plugins, account tools, or personal
files are requested. The application key never enters the executor environment.
The separate environment key must already exist in the private local key file.
"""
import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

from openai import AsyncOpenAI


CODEX = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
KEY_FILE = Path.home()/'.config'/'alli'/'executor.key'


async def stop_session(client, session_id):
    session = await client.beta.agents.sessions.retrieve(session_id)
    if session.status not in {'idle', 'failed'}:
        await client.beta.agents.sessions.events.create(session_id,
            events=[{'type': 'agent.session.input.cancel'}])
        for _ in range(20):
            await asyncio.sleep(.5)
            session = await client.beta.agents.sessions.retrieve(session_id)
            if session.status in {'idle', 'failed'}:
                break
    if session.status in {'idle', 'failed'}:
        await client.beta.agents.sessions.delete(session_id)


async def main():
    if not CODEX.is_file():
        raise SystemExit('The official installed Codex CLI was not found.')
    mode = KEY_FILE.stat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o077 or KEY_FILE.is_symlink():
        raise SystemExit('The executor key must be a private regular file.')
    environment_key = KEY_FILE.read_text().strip()
    if len(environment_key) < 24 or any(c.isspace() for c in environment_key):
        raise SystemExit('Invalid executor key file.')
    child_environment = {name: os.environ[name]
        for name in ('PATH', 'HOME', 'USER', 'TMPDIR', 'LANG') if name in os.environ}
    child_environment['CODEX_API_KEY'] = environment_key
    assert 'OPENAI_API_KEY' not in child_environment
    session_id, executor = None, None
    with tempfile.TemporaryDirectory(prefix='alli-executor-probe-') as directory:
        workspace = Path(directory)
        with (workspace/'executor.log').open('wb') as log:
            async with AsyncOpenAI(timeout=25, max_retries=0) as client:
                try:
                    session = await client.beta.agents.sessions.create(
                        agent={'model': 'gpt-6.1-sol', 'reasoning': {'effort': 'high'},
                            'instructions': 'This is an authorized connection test. Use only the supplied workspace. '
                                'Do not read personal files, load plugins, or use any account or external service. '
                                'Do not read or print environment variables or credentials.'},
                        environment={'type': 'self_hosted', 'workspace_directory': str(workspace)},
                        metadata={'purpose': 'alli-mac-executor-connection-probe'})
                    session_id = session.id
                    if session.agent.model != 'gpt-6.1-sol' or session.agent.reasoning.effort != 'high':
                        raise RuntimeError('Configured reasoning model was not accepted.')
                    print(json.dumps({'phase': 'cloud_session_created', 'model': session.agent.model,
                        'reasoning': session.agent.reasoning.effort}), flush=True)
                    executor = subprocess.Popen([str(CODEX), 'exec-server',
                        '--remote', session.environment.remote_url,
                        '--environment-id', session.environment.id],
                        env=child_environment, cwd=workspace, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT)
                    await client.beta.agents.sessions.events.create(session_id,
                        idempotency_key='file-probe', events=[{'type': 'agent.session.input.message',
                            'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text':
                                'Using the local execution tool, write the exact text ALLI_EXECUTOR_CONNECTED '
                                'to connection-proof.txt in the workspace, read that file back, and report success. '
                                'Do not touch any other file. Do not use the network or read credentials.'}]}]}])
                    deadline = time.monotonic()+120
                    while time.monotonic() < deadline:
                        if executor.poll() is not None:
                            print(json.dumps({'phase': 'executor_exited', 'exit_code': executor.returncode}), flush=True)
                            return
                        turns = await client.beta.agents.sessions.turns.list(session_id, limit=1, order='desc')
                        if turns.data and turns.data[0].status in {'completed', 'failed', 'cancelled'}:
                            turn = turns.data[0]
                            items = await client.beta.agents.sessions.items.list(session_id, limit=100, order='desc')
                            proof = workspace/'connection-proof.txt'
                            print(json.dumps({'phase': 'finished', 'turn_status': turn.status,
                                'file_verified': proof.is_file() and proof.read_text().strip() == 'ALLI_EXECUTOR_CONNECTED',
                                'item_types': sorted({item.type for item in items.data}),
                                'application_key_withheld': True,
                                'error_code': getattr(turn.error, 'code', None) if turn.error else None}), flush=True)
                            return
                        await asyncio.sleep(2)
                    print(json.dumps({'phase': 'timeout', 'file_verified': False}), flush=True)
                except Exception as error:
                    print(json.dumps({'phase': 'failed', 'error_type': type(error).__name__,
                        'status': getattr(error, 'status_code', None),
                        'code': getattr(error, 'code', None), 'parameter': getattr(error, 'param', None)}), flush=True)
                finally:
                    if session_id:
                        try:
                            await stop_session(client, session_id)
                        except Exception as error:
                            print(json.dumps({'phase': 'cleanup_attention', 'error_type': type(error).__name__}), flush=True)
                    if executor is not None and executor.poll() is None:
                        executor.terminate()
                        try:
                            await asyncio.to_thread(executor.wait, timeout=10)
                        except subprocess.TimeoutExpired:
                            executor.kill()
                            await asyncio.to_thread(executor.wait)


if __name__ == '__main__':
    asyncio.run(main())
