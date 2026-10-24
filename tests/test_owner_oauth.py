import hashlib
import json
import time
from types import SimpleNamespace as NS

from cryptography.fernet import Fernet
from mcp.server.auth.provider import TokenError
import pytest

from adapters.owner_oauth import OwnerOAuth
from adapters.postgres import PostgresRepository
from alli_core.profile import Profile


@pytest.mark.asyncio
async def test_existing_connector_refresh_is_preserved_and_rotates_once(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'));repo.initialize();repo.bootstrap_owner('+18435550100')
    key=Fernet.generate_key();oauth=OwnerOAuth(repo,'https://testserver','same-secret',key)
    old_refresh='existing-private-refresh'
    data={'client_id':'existing-client','expires_at':time.time()+86400}
    with repo.connection() as db:
        db.execute('CREATE TABLE alli_mcp_auth(kind text,digest text,sealed text,expires_at double precision)')
        db.execute('INSERT INTO alli_mcp_auth VALUES(%s,%s,%s,%s)',('refresh',hashlib.sha256(old_refresh.encode()).hexdigest(),Fernet(key).encrypt(json.dumps(data).encode()).decode(),data['expires_at']))
    client=NS(client_id='existing-client')
    grant=await oauth.load_refresh_token(client,old_refresh)
    assert grant.subject=='alli-owner'
    new=await oauth.exchange_refresh_token(client,grant,['calls:write'])
    assert new.refresh_token!=old_refresh and await oauth.load_access_token(new.access_token)
    assert await oauth.load_refresh_token(client,old_refresh) is None
    with pytest.raises(TokenError):await oauth.exchange_refresh_token(client,grant,['calls:write'])
    access=await oauth.load_access_token(new.access_token)
    await oauth.revoke_token(access)
    assert await oauth.load_access_token(new.access_token) is None
