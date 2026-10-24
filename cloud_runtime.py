"""Isolated, cloud-resident Codex app gateway. No model turns or desktop relay.

Only app/skill discovery and explicit MCP calls are exposed. OAuth credentials
stay in an owner-only temporary profile and encrypted Render Postgres storage.
"""
import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request
import communication_store as store
from google_store import cipher

VERSION='0.164.0'
PACKAGE='https://registry.npmjs.org/@openai/codex/-/codex-0.164.0-linux-x64.tgz'
INTEGRITY='XLAoaWsGOT2khzl+P4FYDPTkzAr9W0VFXFaVkqlFdkM0soa1gH/4S0wSD57Ek3HHwkirAEGz2/Mwehseh+l5Mg=='

def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS alli_cloud_runtime (
      id integer PRIMARY KEY CHECK(id=1),credentials text NOT NULL DEFAULT '',
      updated_at timestamptz NOT NULL DEFAULT now())''')

def binary():
    if os.getenv('ALLI_CODEX_BINARY'):return os.environ['ALLI_CODEX_BINARY']
    if os.uname().sysname!='Linux':
        return shutil.which('codex') or ''
    path=Path('/tmp/alli-runtime-'+VERSION)/'codex'
    if path.is_file():return str(path)
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    # Verify the pinned official npm artifact before extracting one executable.
    with tempfile.TemporaryFile() as tmp:
        digest=hashlib.sha512();size=0
        with urllib.request.urlopen(PACKAGE,timeout=60) as response:
            while chunk:=response.read(1024*1024):
                size+=len(chunk)
                if size>250*1024*1024:raise ValueError('Runtime package exceeds limit')
                digest.update(chunk);tmp.write(chunk)
        if base64.b64encode(digest.digest()).decode()!=INTEGRITY:raise ValueError('Runtime package integrity mismatch')
        tmp.seek(0)
        with tarfile.open(fileobj=tmp,mode='r:gz') as tar:
            matches=[m for m in tar if m.isfile() and m.name.endswith('/codex/codex')]
            if len(matches)!=1:raise ValueError('Unexpected runtime package')
            stream=tar.extractfile(matches[0])
            with path.with_suffix('.pending').open('wb') as target:shutil.copyfileobj(stream,target)
    path.with_suffix('.pending').chmod(0o700);path.with_suffix('.pending').replace(path)
    return str(path)

class CloudRuntime:
    def __init__(self):
        self.process=None;self.reader=None;self.pending={};self.sequence=0
        self.thread=None;self.apps=[];self.tools={};self.skills=[];self.login=None
        self.state='starting';self.error='';self.profile=None;self.lock_db=None
        self.refresh_lock=asyncio.Lock();self.last_credentials=''

    def status(self):
        return dict(status=self.state,error=self.error,version=VERSION,
          connected=self.state=='connected',apps=self.apps,tool_count=len(self.tools),
          skill_count=len(self.skills),login=self.login)

    async def send(self,value):
        if not self.process or self.process.returncode is not None:raise ValueError('Cloud tool runtime is not running')
        self.process.stdin.write((json.dumps(value)+'\n').encode());await self.process.stdin.drain()

    async def rpc(self,method,params=None,timeout=90):
        # Do not expose a generic RPC method to a model or web client.
        self.sequence+=1;i=self.sequence;future=asyncio.get_running_loop().create_future();self.pending[i]=future
        try:
            await self.send(dict(id=i,method=method,params=params or {}))
            return await asyncio.wait_for(future,timeout)
        finally:self.pending.pop(i,None)

    async def receive(self):
        while line:=await self.process.stdout.readline():
            try:message=json.loads(line)
            except (ValueError,UnicodeError):continue
            if 'id' in message and ('result' in message or 'error' in message):
                future=self.pending.get(message['id'])
                if future and not future.done():
                    if 'error' in message:future.set_exception(ValueError('Cloud tool request could not be completed'))
                    else:future.set_result(message['result'])
            elif 'id' in message:
                # Provider UI prompts need owner attention; never silently accept
                # agreements, external permissions, input or arbitrary commands.
                await self.send(dict(id=message['id'],error=dict(code=-32000,message='Owner interaction required in Alli dashboard')))
            elif message.get('method')=='account/login/completed':
                if message.get('params',{}).get('success'):
                    self.login=None;asyncio.create_task(self.refresh())
                else:self.state='needs_connection';self.error='Account authorization did not complete';self.login=None
        for future in list(self.pending.values()):
            if not future.done():future.set_exception(ValueError('Cloud tool runtime stopped'))

    def restore(self):
        with store.connection() as db:
            row=db.execute('SELECT credentials FROM alli_cloud_runtime WHERE id=1').fetchone()
        if row and row[0]:
            data=cipher().decrypt(row[0].encode())
            path=self.profile/'auth.json';path.write_bytes(data);path.chmod(0o600)
            self.last_credentials=hashlib.sha256(data).hexdigest()

    def persist(self):
        if not self.profile:return
        path=self.profile/'auth.json'
        if not path.is_file():return
        data=path.read_bytes();digest=hashlib.sha256(data).hexdigest()
        if digest==self.last_credentials:return
        # Parse before storing, but never log or return credential contents.
        json.loads(data)
        sealed=cipher().encrypt(data).decode()
        with store.connection() as db:
            db.execute('INSERT INTO alli_cloud_runtime(id,credentials) VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET credentials=EXCLUDED.credentials,updated_at=now()',(sealed,))
        self.last_credentials=digest

    async def start(self):
        try:
            executable=await asyncio.to_thread(binary)
            # A deployment can overlap the retiring instance. Only one app
            # runtime may rotate this account's refresh tokens at a time.
            self.lock_db=await asyncio.to_thread(store.connection)
            self.lock_db.autocommit=True
            while not (await asyncio.to_thread(self.lock_db.execute,'SELECT pg_try_advisory_lock(301849630187)')).fetchone()[0]:
                self.state='waiting_for_previous_instance';await asyncio.sleep(2)
            self.profile=Path(tempfile.mkdtemp(prefix='alli-codex-'));self.profile.chmod(0o700)
            await asyncio.to_thread(self.restore)
            self.profile.joinpath('config.toml').write_text('cli_auth_credentials_store = "file"\n[features]\napps = true\nplugins = true\n[apps._default]\nenabled = true\n')
            env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','SSL_CERT_FILE','SSL_CERT_DIR')}
            env.update(CODEX_HOME=str(self.profile),RUST_LOG='error')
            self.process=await asyncio.create_subprocess_exec(executable,'app-server',stdin=asyncio.subprocess.PIPE,
              stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env,limit=8*1024*1024,cwd=str(self.profile))
            self.reader=asyncio.create_task(self.receive())
            await self.rpc('initialize',dict(clientInfo=dict(name='alli_cloud_tools',title='Alli cloud tools',version='1.0'),capabilities=dict(experimentalApi=True)))
            await self.send(dict(method='initialized',params={}))
            await self.refresh()
            while True:
                await asyncio.sleep(30);await asyncio.to_thread(self.persist)
                if self.process.returncode is not None:raise ValueError('Runtime stopped')
        except asyncio.CancelledError:raise
        except Exception:
            self.state='needs_connection';self.error='Cloud tool runtime needs attention. Existing phone, calendar and research features remain available.'
        finally:
            if self.process and self.process.returncode is None:
                self.process.terminate()
                try:await asyncio.wait_for(self.process.wait(),10)
                except asyncio.TimeoutError:self.process.kill();await self.process.wait()
            with contextlib.suppress(Exception):await asyncio.to_thread(self.persist)
            if self.reader:self.reader.cancel()
            if self.lock_db:
                with contextlib.suppress(Exception):self.lock_db.close()
            if self.profile:shutil.rmtree(self.profile,ignore_errors=True)

    async def refresh(self):
        async with self.refresh_lock:
            try:
                account=await self.rpc('account/read',dict(refreshToken=False))
                if not account.get('account'):
                    self.state='needs_connection';self.error='Connect your ChatGPT account to this cloud runtime';return self.status()
                if not self.thread:
                    result=await self.rpc('thread/start',dict(ephemeral=True,cwd=str(self.profile),approvalPolicy='on-request',sandbox='read-only',model='gpt-6.1-sol'))
                    self.thread=result['thread']['id']
                installed=await self.rpc('app/installed',dict(threadId=self.thread,forceRefresh=True))
                self.apps=installed.get('apps',[])
                page=await self.rpc('mcpServerStatus/list',dict(threadId=self.thread,serverName='codex_apps',detail='toolsAndAuthOnly',limit=100))
                self.tools={}
                for server in page.get('data',[]):
                    if server['name']=='codex_apps':self.tools.update(server.get('tools',{}))
                skills=await self.rpc('skills/list',dict(cwds=[str(self.profile)],forceReload=True))
                self.skills=[s for group in skills.get('data',[]) for s in group.get('skills',[]) if s.get('enabled')]
                await asyncio.to_thread(self.persist)
                if not self.tools:
                    self.state='needs_connection';self.error='Account signed in, but callable app tools are not ready. Refresh connections.'
                else:self.state='connected';self.error=''
                return self.status()
            except Exception:
                self.state='needs_connection';self.error='Cloud account or app discovery could not be verified. Reconnect and refresh.'
                return self.status()

    async def begin_login(self):
        if self.login:return self.login
        result=await self.rpc('account/login/start',dict(type='chatgptDeviceCode'))
        self.login={k:result[k] for k in ('loginId','userCode','verificationUrl')}
        self.state='awaiting_authorization';return self.login

    async def invoke(self,name,args):
        if self.state!='connected' or name not in self.tools:raise ValueError('This account tool is not connected')
        result=await self.rpc('mcpServer/tool/call',dict(server='codex_apps',threadId=self.thread,tool=name,arguments=args),timeout=180)
        await asyncio.to_thread(self.persist)
        return result

runtime=CloudRuntime()
