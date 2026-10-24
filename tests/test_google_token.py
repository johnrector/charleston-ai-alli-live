from cryptography.fernet import Fernet
from adapters.google import GoogleTokenStore
from adapters.postgres import PostgresRepository
from alli_core.profile import Profile


def test_existing_encrypted_refresh_token_is_reused_without_reauthorization(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'));repo.initialize()
    key=Fernet.generate_key();cipher=Fernet(key)
    with repo.connection() as db:
        db.execute('CREATE TABLE alli_google_token(id integer,encrypted_token text,updated_at timestamptz)')
        db.execute('INSERT INTO alli_google_token VALUES(1,%s,now())',(cipher.encrypt(b'private-refresh-token').decode(),))
    store=GoogleTokenStore(repo.connection,key,client_id='client',client_secret='private-client-secret')
    credentials=store.credentials()
    assert credentials.refresh_token=='private-refresh-token' and credentials.client_id=='client'
    store.save(credentials)
    with repo.connection() as db:
        saved=db.execute('SELECT encrypted_token FROM alli_google_token').fetchone()['encrypted_token']
    assert cipher.decrypt(saved.encode())==b'private-refresh-token'
    assert 'private-client-secret' not in cipher.decrypt(saved.encode()).decode()
