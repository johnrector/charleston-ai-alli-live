"""Encrypted, deployment-independent storage on a private Render Postgres instance."""
import os
import hashlib
from contextlib import contextmanager
import psycopg
from cryptography.fernet import Fernet


def cipher():
    return Fernet(os.environ['GOOGLE_TOKEN_ENCRYPTION_KEY'].encode())


def connection():
    return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10)


def initialize():
    with connection() as db:
        db.execute('CREATE TABLE IF NOT EXISTS alli_google_token (id integer PRIMARY KEY CHECK(id=1), encrypted_token text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())')
        db.execute('CREATE TABLE IF NOT EXISTS alli_oauth_state (digest text PRIMARY KEY, expires_at timestamptz NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS alli_google_action (id text PRIMARY KEY, status text NOT NULL, result jsonb, created_at timestamptz NOT NULL DEFAULT now())')


def save_token(token):
    sealed = cipher().encrypt(token.encode()).decode()
    with connection() as db:
        db.execute('INSERT INTO alli_google_token(id,encrypted_token) VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET encrypted_token=EXCLUDED.encrypted_token,updated_at=now()', (sealed,))


def load_token():
    with connection() as db:
        row = db.execute('SELECT encrypted_token FROM alli_google_token WHERE id=1').fetchone()
    if not row:
        raise RuntimeError('Google account is not connected')
    return cipher().decrypt(row[0].encode()).decode()


def register_state(state):
    with connection() as db:
        db.execute('DELETE FROM alli_oauth_state WHERE expires_at < now()')
        db.execute("INSERT INTO alli_oauth_state VALUES(%s,now()+interval '10 minutes')", (hashlib.sha256(state.encode()).hexdigest(),))


def consume_state(state):
    with connection() as db:
        return db.execute('DELETE FROM alli_oauth_state WHERE digest=%s AND expires_at>now() RETURNING digest', (hashlib.sha256(state.encode()).hexdigest(),)).fetchone() is not None


@contextmanager
def action_lock():
    # Serialize demo writes, including check-before-create, across workers.
    with connection() as db:
        db.execute('SELECT pg_advisory_lock(301849630150)')
        try:
            yield db
        finally:
            db.execute('SELECT pg_advisory_unlock(301849630150)')
