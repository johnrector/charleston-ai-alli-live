"""Outbound-only supervisor for OpenAI's official exec-server; no model loop."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from adapters.mac_executor import executor_arguments,executor_environment

from cryptography.hazmat.primitives.serialization import load_pem_private_key
import httpx


ROOT=Path(__file__).resolve().parents[1]
PRIVATE=Path.home()/'.config/alli'
CODEX='/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'


def main():
    config=json.loads((PRIVATE/'mac.json').read_text())
    for path in (PRIVATE/'device.key',PRIVATE/'executor.key'):
        if path.is_symlink() or path.stat().st_mode&0o077:raise RuntimeError('Executor credentials must be private files')
    key=load_pem_private_key((PRIVATE/'device.key').read_bytes(),None)
    child_env=executor_environment(os.environ,restricted_key=(PRIVATE/'executor.key').read_text().strip())
    url='https://charleston-ai-alli-gpt-live.onrender.com'
    path='/executor/lease'
    body=json.dumps({'device_id':config['id']},separators=(',',':')).encode()
    running={}
    with httpx.Client(timeout=20,follow_redirects=False) as http:
        while True:
            stamp,nonce=str(int(time.time())),secrets.token_hex(24)
            message='POST\n'+path+'\n'+stamp+'\n'+nonce+'\n'+hashlib.sha256(body).hexdigest()
            headers={'Content-Type':'application/json','x-alli-time':stamp,'x-alli-nonce':nonce,
                     'x-alli-signature':base64.b64encode(key.sign(message.encode())).decode()}
            try:
                response=http.post(url+path,content=body,headers=headers)
                response.raise_for_status()
                jobs=response.json()['jobs']
                active={job['session_id'] for job in jobs}
                for sid,process in list(running.items()):
                    if sid not in active:
                        if process.poll() is None:
                            process.terminate()
                            try:process.wait(timeout=5)
                            except subprocess.TimeoutExpired:process.kill();process.wait()
                        del running[sid]
                for job in jobs:
                    if job['workspace']!=config['workspace']:continue
                    arguments=executor_arguments(CODEX,environment_id=job['environment_id'],remote_url=job['remote_url'])
                    if job['session_id'] in running and running[job['session_id']].poll() is None:continue
                    running[job['session_id']]=subprocess.Popen(arguments,cwd=config['workspace'],env=child_env,
                        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            except (httpx.HTTPError,ValueError,KeyError):pass  # offline waits; existing cloud work is retained
            time.sleep(5)


if __name__=='__main__':main()
