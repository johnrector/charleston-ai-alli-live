"""Calendar permission and booking boundaries around the account adapter."""
from datetime import datetime,timedelta,timezone
import hashlib
from zoneinfo import ZoneInfo

from alli_core.authority import PermissionDenied
from .google import InvalidInput,timestamp


class CalendarService:
    def __init__(self,repository,google,*,friend_daily_bookings=3,friend_max_minutes=120):
        self.repository,self.google=repository,google
        self.daily,self.maximum=friend_daily_bookings,friend_max_minutes

    def lookup(self,task_id,start,end,*,page_token=None):
        with self.repository.connection() as db:
            task=self.repository._task(db,task_id)
            if not task or not self.repository._permitted(db,task_id,writes=False):
                raise PermissionDenied('Assignment is unavailable')
            if task['scope']=='friend' and not task['authority'].get('proof_id','').startswith(('signed-sms:','verified-phone:')):
                raise PermissionDenied('Use a verified callback to the saved phone or SMS for caller-specific meeting details; asking a name does not verify a number')
            person=db.execute('SELECT email FROM ea_person WHERE assistant_id=%s AND id=%s',
                              (self.repository.profile.id,task['person_id'])).fetchone()
        return self.google.events(start,end,page_token=page_token,
            person_id=None if task['scope']=='owner' else str(task['person_id']),
            contact_email=person['email'] if task['scope']=='friend' else '')

    def book(self,task_id,*,action_key,title,start,end,description='',location='',invitation_email=''):
        begin,finish=timestamp(start),timestamp(end)
        with self.repository.connection() as db:
            lock=int.from_bytes(hashlib.sha256(('calendar:'+self.repository.profile.id).encode()).digest()[:8],
                                'big',signed=True)
            db.execute('SELECT pg_advisory_xact_lock(%s)',(lock,))
            task=self.repository._task(db,task_id)
            if not task or not self.repository._permitted(db,task_id,writes=False):
                raise PermissionDenied('Assignment is unavailable')
            person=db.execute('SELECT email FROM ea_person WHERE assistant_id=%s AND id=%s',
                              (self.repository.profile.id,task['person_id'])).fetchone()
            bound=task['authority'].get('invitation_email','')
            if bound and invitation_email and bound.casefold()!=invitation_email.casefold():
                raise PermissionDenied('The owner-supplied invitation email cannot be substituted')
            email=bound or invitation_email or person['email']
            if task['scope']=='friend':
                if not task['authority'].get('proof_id','').startswith(('signed-sms:','verified-phone:')):
                    raise PermissionDenied('Use a verified callback or SMS before committing a meeting; public availability can still be checked')
                now=datetime.now(timezone.utc)
                if begin<now-timedelta(minutes=5) or begin>now+timedelta(days=90):
                    raise InvalidInput('Ask John about a meeting outside the next 90 days')
                if not begin<finish or (finish-begin).total_seconds()>self.maximum*60:
                    raise InvalidInput('Ask John about a meeting longer than two hours')
                local_start=begin.astimezone(ZoneInfo(self.repository.profile.timezone))
                local_end=finish.astimezone(ZoneInfo(self.repository.profile.timezone))
                if (local_start.date()!=local_end.date() or local_start.hour<9 or local_end.hour>17
                        or local_end.hour==17 and (local_end.minute or local_end.second)):
                    raise InvalidInput('Ask John before booking outside 9 AM–5 PM in his time zone')
                count=db.execute('''SELECT count(*) AS total FROM ea_booking WHERE assistant_id=%s AND person_id=%s
                    AND created_at>now()-interval '24 hours' ''',
                    (self.repository.profile.id,task['person_id'])).fetchone()['total']
                if count>=self.daily:raise PermissionDenied('The daily meeting limit has been reached; ask John for more')
                if not email:raise InvalidInput('An invitation email is needed to book your meeting')
            result=self.google.create_event(action_key=action_key,person_id=str(task['person_id']),
                title=title,start=start,end=end,description=description,location=location,
                attendee_emails=(email,) if email else ())
            if result.get('ok'):
                db.execute('''INSERT INTO ea_booking(action_key,assistant_id,person_id,task_id,event_id,starts_at,ends_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(action_key) DO NOTHING''',
                    (action_key,self.repository.profile.id,task['person_id'],task_id,result['event_id'],begin,finish))
            return result
