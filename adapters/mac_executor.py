"""Launch OpenAI's official executor. It does not run another reasoning agent.

Provisioning supplies an environment-only API key, never the application key.
No public port, shell command interpolation or copied plugin archive is used.
"""
import asyncio
from pathlib import Path
from urllib.parse import urlsplit


def executor_arguments(codex, *, environment_id, remote_url):
    url = urlsplit(remote_url)
    if ((url.scheme,url.hostname) not in {('https','api.openai.com'),('wss','codex-cloud-environments.chatgpt.com')}
            or url.port not in {None,443} or url.username or url.password or url.fragment):
        raise ValueError('Unexpected OpenAI environment endpoint')
    if not environment_id or len(environment_id)>200 or any(c.isspace() for c in environment_id):
        raise ValueError('Invalid environment identity')
    return [str(codex), 'exec-server', '--remote', remote_url, '--environment-id', environment_id]


def executor_environment(inherited, *, restricted_key):
    if not restricted_key:
        raise ValueError('The Mac executor requires a separate environment-only API key')
    if restricted_key == inherited.get('OPENAI_API_KEY'):
        raise ValueError('The application API key must not be passed to the executor')
    keep = {'HOME','PATH','TMPDIR','USER','LOGNAME','LANG','LC_ALL','SHELL','TERM'}
    result = {key:value for key,value in inherited.items() if key in keep}
    result['CODEX_API_KEY'] = restricted_key
    return result


async def start_executor(*, codex, environment_id, remote_url, workspace, restricted_key, inherited):
    arguments = executor_arguments(codex, environment_id=environment_id, remote_url=remote_url)
    environment = executor_environment(inherited, restricted_key=restricted_key)
    directory = Path(workspace)
    if not directory.is_absolute() or '..' in directory.parts:
        raise ValueError('Mac workspace must be an absolute path')
    directory.mkdir(parents=True, exist_ok=True)
    # The supervisor owns process lifetime and reconnects to this same native
    # environment. It must not create another Agent when the Mac reconnects.
    return await asyncio.create_subprocess_exec(*arguments, cwd=str(directory), env=environment,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
