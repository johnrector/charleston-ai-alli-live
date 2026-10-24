"""Persistent owner sessions. Deploying does not invalidate a browser cookie."""
from dataclasses import dataclass,field
from datetime import datetime,timedelta,timezone
import hashlib
import hmac
import secrets

from alli_core.authority import Actor


class RateLimited(ValueError):
    pass


@dataclass(frozen=True)
class Challenge:
    token:str=field(repr=False)
    code:str=field(repr=False)


class OwnerSessions:
    def __init__(self,repository,secret,*,lifetime_seconds=43200):
        if not secret or not 3600<=lifetime_seconds<=7*86400:
            raise ValueError('Configure a session secret and supported lifetime')
        self.repository=repository
        self.secret=secret.encode()
        self.lifetime=lifetime_seconds

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def code_digest(self,token,code):
        return hmac.new(self.secret,('owner-code:'+token+':'+code).encode(),hashlib.sha256).hexdigest()

    def csrf(self,token):
        return hmac.new(self.secret,('csrf:'+token).encode(),hashlib.sha256).hexdigest()

    def start(self):
        token=secrets.token_urlsafe(32)
        code=f'{secrets.randbelow(1000000):06}'
        with self.repository.connection() as db:
            db.execute('SELECT id FROM ea_assistant WHERE id=%s FOR UPDATE',(self.repository.profile.id,))
            recent=db.execute('''SELECT count(*) AS total,max(created_at) AS latest FROM ea_auth_challenge
                WHERE assistant_id=%s AND created_at>now()-interval '15 minutes' ''',
                (self.repository.profile.id,)).fetchone()
            now=datetime.now(timezone.utc)
            if recent['total']>=3 or recent['latest'] and (now-recent['latest']).total_seconds()<60:
                raise RateLimited('A code was recently requested. You can still enter a code already received.')
            db.execute('''INSERT INTO ea_auth_challenge(digest,assistant_id,code_digest,expires_at)
                VALUES(%s,%s,%s,now()+interval '10 minutes')''',
                (self.digest(token),self.repository.profile.id,self.code_digest(token,code)))
        return Challenge(token,code)

    def verify(self,challenge_token,code):
        if not isinstance(code,str) or len(code)!=6 or not code.isascii() or not code.isdigit():
            return None
        session=secrets.token_urlsafe(32)
        with self.repository.connection() as db:
            row=db.execute('''SELECT * FROM ea_auth_challenge WHERE assistant_id=%s AND digest=%s
                AND expires_at>now() AND NOT consumed FOR UPDATE''',
                (self.repository.profile.id,self.digest(challenge_token))).fetchone()
            if not row or row['attempts']>=5:return None
            db.execute('UPDATE ea_auth_challenge SET attempts=attempts+1 WHERE digest=%s',(row['digest'],))
            if not hmac.compare_digest(row['code_digest'],self.code_digest(challenge_token,code)):
                return None
            person=db.execute("SELECT id FROM ea_person WHERE assistant_id=%s AND kind='owner' AND status='approved'",
                              (self.repository.profile.id,)).fetchone()
            if not person:return None
            db.execute('UPDATE ea_auth_challenge SET consumed=true WHERE digest=%s',(row['digest'],))
            expires=datetime.now(timezone.utc)+timedelta(seconds=self.lifetime)
            db.execute('''INSERT INTO ea_owner_session(digest,assistant_id,person_id,expires_at)
                VALUES(%s,%s,%s,%s)''',(self.digest(session),self.repository.profile.id,person['id'],expires))
            self.repository._event(db,'session:'+self.digest(session)+':created','owner.signed_in',
                {'expires_at':expires.isoformat()},person_id=person['id'])
        return session

    def actor(self,token):
        if not token or not isinstance(token,str) or len(token)>200:return None
        digest=self.digest(token)
        with self.repository.connection() as db:
            row=db.execute('''SELECT s.person_id FROM ea_owner_session s JOIN ea_person p
                ON p.id=s.person_id AND p.assistant_id=s.assistant_id WHERE s.assistant_id=%s
                AND s.digest=%s AND s.expires_at>now() AND NOT s.revoked
                AND p.kind='owner' AND p.status='approved' ''',(self.repository.profile.id,digest)).fetchone()
        return Actor(self.repository.profile.id,str(row['person_id']),'dashboard',True,
                     'owner-session:'+digest[:24]) if row else None

    def revoke(self,token):
        with self.repository.connection() as db:
            db.execute('UPDATE ea_owner_session SET revoked=true WHERE assistant_id=%s AND digest=%s',
                       (self.repository.profile.id,self.digest(token)))
