"""One-time local storage for OpenAI's environment-only executor key.

Run interactively. Input is hidden and the key is never printed or sent anywhere.
Do not use the application's API key: OpenAI requires an environment-only key.
"""
import getpass
import os
from pathlib import Path
import tempfile


def main():
    directory = Path.home()/'.config'/'alli'
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    directory.chmod(0o700)
    key = getpass.getpass('Paste the OpenAI environment-only key (input hidden): ').strip()
    if len(key)<20 or any(c.isspace() for c in key):
        raise SystemExit('No valid key supplied; nothing was saved.')
    fd, temporary = tempfile.mkstemp(prefix='.executor-',dir=directory)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as destination:
            destination.write(key+'\n')
            destination.flush()
            os.fsync(destination.fileno())
        path=directory/'executor.key'
        os.replace(temporary,path)
        print('Executor key saved privately. The assistant can now test the connection.')
    finally:
        if Path(temporary).exists(): Path(temporary).unlink()


if __name__=='__main__': main()
