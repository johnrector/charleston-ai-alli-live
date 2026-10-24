"""Owner-authorized cloud connections. Credentials never enter model context."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import secrets

from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb


GOOGLE_SCOPES=('openid','https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/calendar.freebusy',
    'https://www.googleapis.com/auth/calendar.events.owned',
    'https://www.googleapis.com/auth/gmail.send','https://www.googleapis.com/auth/gmail.readonly')


class AccountConnections:
    def __init__(self,repository,key,base_url):
        self.repository,self.cipher,self.base_url=repository,Fernet(key),base_url.rstrip('/')

    def get(self,provider):
        with self.repository.connection() as db:
            row=db.execute('SELECT details FROM ea_account_connection WHERE assistant_id=%s AND provider=%s',
                (self.repository.profile.id,provider)).fetchone()
        return row['details'] if row else None

    def failed(self,actor,provider,stage,error):
        with self.repository.connection() as db:
            self.repository._event(db,'account-failure:'+secrets.token_hex(12),'account.connection_failed',
                {'provider':provider,'stage':stage,'error_type':type(error).__name__,
                 'http_status':getattr(getattr(error,'response',None),'status_code',None)},
                person_id=actor.person_id)

    def save(self,actor,provider,details,*,google_refresh=None,secret=None):
        if provider not in {'google','shopify'}:raise ValueError('Unknown provider')
        # Only explicitly safe connection metadata is persisted in this table.
        allowed={'account','scopes','store','domain','shop','verified_at'}
        if set(details)-allowed:raise ValueError('Unsupported connection metadata')
        with self.repository.connection() as db:
            self.repository._person(db,actor,owner=True)
            if google_refresh is not None:
                if provider!='google' or not google_refresh:raise ValueError('Invalid Google grant')
                encrypted=self.cipher.encrypt(google_refresh.encode()).decode()
                db.execute('''INSERT INTO alli_google_token(id,encrypted_token,updated_at) VALUES(1,%s,now())
                    ON CONFLICT(id) DO UPDATE SET encrypted_token=EXCLUDED.encrypted_token,updated_at=now()''',(encrypted,))
            if secret is not None:
                if provider!='shopify':raise ValueError('Unsupported private account')
                db.execute('''INSERT INTO ea_account_secret(assistant_id,provider,sealed) VALUES(%s,%s,%s)
                    ON CONFLICT(assistant_id,provider) DO UPDATE SET sealed=EXCLUDED.sealed,updated_at=now()''',
                    (self.repository.profile.id,provider,self.cipher.encrypt(json.dumps(secret).encode()).decode()))
            db.execute('''INSERT INTO ea_account_connection(assistant_id,provider,details) VALUES(%s,%s,%s)
                ON CONFLICT(assistant_id,provider) DO UPDATE SET details=EXCLUDED.details,updated_at=now()''',
                (self.repository.profile.id,provider,Jsonb(details)))
            self.repository._event(db,'account:'+provider+':'+secrets.token_hex(12),'account.connected',
                {'provider':provider,'account':details.get('account') or details.get('store'),
                 'scopes':details.get('scopes',[])},person_id=actor.person_id)

    def begin(self,actor,provider,session_digest):
        if provider not in {'google','shopify'}:raise ValueError('Unknown provider')
        state,verifier=secrets.token_urlsafe(32),secrets.token_urlsafe(64)
        with self.repository.connection() as db:
            self.repository._person(db,actor,owner=True)
            db.execute('DELETE FROM ea_account_oauth_flow WHERE expires_at<now()')
            db.execute('''INSERT INTO ea_account_oauth_flow
                (digest,assistant_id,provider,owner_session_digest,verifier_sealed,expires_at)
                VALUES(%s,%s,%s,%s,%s,%s)''',(hashlib.sha256(state.encode()).hexdigest(),
                self.repository.profile.id,provider,session_digest,self.cipher.encrypt(verifier.encode()).decode(),
                datetime.now(timezone.utc)+timedelta(minutes=10)))
        return state,verifier

    def consume(self,provider,state,session_digest):
        with self.repository.connection() as db:
            row=db.execute('''DELETE FROM ea_account_oauth_flow WHERE digest=%s AND assistant_id=%s
                AND provider=%s AND owner_session_digest=%s AND expires_at>now() RETURNING verifier_sealed''',
                (hashlib.sha256(state.encode()).hexdigest(),self.repository.profile.id,provider,session_digest)).fetchone()
        if not row:raise ValueError('This connection request expired or was already used. Start again from Connections.')
        return self.cipher.decrypt(row['verifier_sealed'].encode()).decode()
