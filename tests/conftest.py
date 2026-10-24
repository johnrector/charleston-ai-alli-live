"""An isolated real Postgres cluster; never reads DATABASE_URL or production data."""
import getpass
from pathlib import Path
import shutil
import subprocess
import tempfile
from uuid import uuid4

import psycopg
import pytest


@pytest.fixture(scope='session')
def postgres_cluster():
    bin_dir = Path('/opt/homebrew/opt/postgresql@17/bin')
    if not (bin_dir/'initdb').is_file():
        executable = shutil.which('initdb')
        if not executable:
            pytest.skip('Postgres binaries are required for database integration tests')
        bin_dir = Path(executable).parent
    with tempfile.TemporaryDirectory(prefix='alli-pg-') as temporary:
        root = Path(temporary)
        data, sockets = root/'data', root/'sockets'
        sockets.mkdir()
        subprocess.run([str(bin_dir/'initdb'), '-D', str(data), '--auth-local=trust',
                        '--auth-host=reject', '--no-locale', '--encoding=UTF8'], check=True, capture_output=True)
        subprocess.run([str(bin_dir/'pg_ctl'), '-D', str(data), '-l', str(root/'server.log'),
                        '-o', f"-h '' -k {sockets} -F", '-w', 'start'], check=True, capture_output=True)
        try:
            yield f'dbname=postgres user={getpass.getuser()} host={sockets}'
        finally:
            subprocess.run([str(bin_dir/'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           check=True, capture_output=True)


@pytest.fixture
def database(postgres_cluster):
    schema = 'test_'+uuid4().hex
    with psycopg.connect(postgres_cluster) as db:
        db.execute(f'CREATE SCHEMA {schema}')  # generated identifier, never user input
    try:
        yield postgres_cluster+f' options=-csearch_path={schema}'
    finally:
        with psycopg.connect(postgres_cluster) as db:
            db.execute(f'DROP SCHEMA {schema} CASCADE')
