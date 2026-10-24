"""Persistent phone identities and evidence, shared across service instances."""
import hashlib
from uuid import uuid4

from psycopg.types.json import Jsonb

from alli_core.authority import Conflict,PermissionDenied


TERMINAL={'completed','busy','failed','no-answer','canceled'}
STATUSES=TERMINAL|{'queued','initiated','ringing','in-progress'}


class CallRepository:
    def __init__(self,repository,phone,*,daily_per_person=3):
        self.repository=repository
        self.assistant_id=repository.profile.id
        self.phone=repository.normalize_phone(phone)
        self.daily_per_person=daily_per_person

    def incoming(self,form):
        """Only call after validating the complete carrier webhook signature."""
        if self.repository.normalize_phone(form['To'])!=self.phone:
            raise PermissionDenied('Wrong destination')
        verified=form.get('StirVerstat') in {'TN-Validation-Passed-A','TN-Validation-Passed-A-Passthrough'}
        relationship=self.repository.begin_conversation(form['From'],'phone:'+form['CallSid'],'phone',
                                                       owner_verified=verified)
        person=relationship['person']
        identifier=hashlib.sha256((self.assistant_id+':'+form['CallSid']).encode()).hexdigest()[:32]
        with self.repository.connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(%s)',(int(hashlib.sha256((self.assistant_id+':voice-admission').encode()).hexdigest()[:15],16),))
            prior=db.execute('SELECT * FROM ea_phone_call WHERE assistant_id=%s AND source_id=%s',
                (self.assistant_id,'inbound:'+form['CallSid'])).fetchone()
            if prior:return prior
            usage=db.execute('''SELECT count(*) AS calls,coalesce(sum(coalesce(duration_seconds,
                least(max_seconds,extract(epoch FROM coalesce(ended_at,now())-created_at)))),0) AS seconds
                FROM ea_phone_call WHERE assistant_id=%s AND person_id=%s AND created_at>now()-interval '24 hours' ''',
                (self.assistant_id,person['id'])).fetchone()
            overall=db.execute("SELECT count(*) AS calls FROM ea_phone_call WHERE assistant_id=%s AND created_at>now()-interval '24 hours'",(self.assistant_id,)).fetchone()['calls']
            owner=person['kind']=='owner' and verified
            daily,count,per_call=(7200,20,3600) if owner else (240,2,120) if person['status']=='known' else (3600,10,1800)
            maximum=min(per_call,int(daily-usage['seconds']))
            if usage['calls']>=count or maximum<15 or overall>=80:
                raise PermissionDenied('The phone conversation limit has been reached')
            row=db.execute('''INSERT INTO ea_phone_call(id,assistant_id,conversation_id,person_id,
                source_id,direction,expected_from,expected_to,state,provider_id,carrier_status,number_verified,max_seconds)
                VALUES(%s,%s,%s,%s,%s,'inbound',%s,%s,'accepted',%s,%s,%s,%s)
                ON CONFLICT(assistant_id,source_id) DO NOTHING RETURNING *''',
                (identifier,self.assistant_id,relationship['conversation']['id'],person['id'],
                 'inbound:'+form['CallSid'],relationship['phone'],self.phone,form['CallSid'],
                 form.get('CallStatus','in-progress'),verified,maximum)).fetchone()
            if not row:
                row=db.execute('SELECT * FROM ea_phone_call WHERE assistant_id=%s AND source_id=%s',
                               (self.assistant_id,'inbound:'+form['CallSid'])).fetchone()
            self.repository._event(db,'call:'+identifier+':received','call.received',
                {'call_id':identifier,'number_verified':verified},person_id=person['id'])
            return row

    def queue(self,task_id,person_id,instruction,source_id,*,allowed_tools=None,invitation_email=''):
        if not isinstance(instruction,str) or not 1<=len(instruction)<=12000:
            raise ValueError('Supply a concise call mission')
        with self.repository.connection() as db:
            task=self.repository._task(db,task_id,lock=True)
            if not task or not self.repository._permitted(db,task_id,writes=False):
                raise PermissionDenied('Assignment is unavailable')
            if task['scope']!='owner' and str(task['person_id'])!=person_id:
                raise PermissionDenied('This assignment can only request a callback to its own caller')
            prior=db.execute('''SELECT * FROM ea_phone_call WHERE assistant_id=%s
                AND parent_task_id=%s AND person_id=%s''',(self.assistant_id,task_id,person_id)).fetchone()
            if prior:
                return prior  # changing words never authorizes an automatic redial
            recipient=db.execute('''SELECT p.*,e.address FROM ea_person p JOIN ea_endpoint e
                ON e.assistant_id=p.assistant_id AND e.person_id=p.id WHERE p.assistant_id=%s
                AND p.id=%s AND e.active AND e.kind='phone' ''',(self.assistant_id,person_id)).fetchone()
            if not recipient or recipient['status'] in {'suspended','deleted'} or recipient['address']==self.phone:
                raise PermissionDenied('The recipient is unavailable')
            # Serialize the recipient budget, without taking contact/task locks
            # in the opposite order to suspension.
            budget_lock=int.from_bytes(hashlib.sha256(('call-budget:'+person_id).encode()).digest()[:8],
                                       'big',signed=True)
            db.execute('SELECT pg_advisory_xact_lock(%s)',(budget_lock,))
            count=db.execute('''SELECT count(*) AS total FROM ea_phone_call WHERE assistant_id=%s
                AND person_id=%s AND direction='outbound' AND created_at>now()-interval '24 hours'
                AND state<>'cancelled' ''',(self.assistant_id,person_id)).fetchone()['total']
            if count>=self.daily_per_person:
                raise PermissionDenied('The daily call limit for this person has been reached')
            identifier=uuid4().hex
            # A callback reaches the configured owner's actual number, rather
            # than trusting an unverified caller-ID claim on an inbound leg.
            scope='owner' if recipient['kind']=='owner' else 'friend'
            conversation=db.execute('''INSERT INTO ea_conversation(assistant_id,person_id,source_id,channel,scope)
                VALUES(%s,%s,%s,'phone',%s) RETURNING id''',
                (self.assistant_id,person_id,'outbound:'+identifier,scope)).fetchone()
            row=db.execute('''INSERT INTO ea_phone_call(id,assistant_id,conversation_id,person_id,parent_task_id,
                source_id,direction,instruction,expected_from,expected_to,state,allowed_tools,invitation_email)
                VALUES(%s,%s,%s,%s,%s,%s,'outbound',%s,%s,%s,'queued',%s,%s) RETURNING *''',
                (identifier,self.assistant_id,conversation['id'],person_id,task_id,source_id,
                 instruction,self.phone,recipient['address'],Jsonb(list(allowed_tools)) if allowed_tools is not None else None,
                 invitation_email)).fetchone()
            db.execute('''UPDATE ea_task SET context=jsonb_set(context,'{pending_calls}',
                coalesce(context->'pending_calls','[]'::jsonb) || %s),updated_at=now() WHERE id=%s''',
                (Jsonb([identifier]),task_id))
            self.repository._event(db,'call:'+identifier+':queued','call.queued',
                {'call_id':identifier,'instruction':instruction},person_id=person_id,task_id=task_id)
            return row

    def get(self,identifier):
        with self.repository.connection() as db:
            return db.execute('''SELECT c.*,v.transcript,v.finalized,v.scope,p.name,p.status AS person_status,
                p.kind AS person_kind FROM ea_phone_call c JOIN ea_conversation v ON v.id=c.conversation_id
                JOIN ea_person p ON p.id=c.person_id WHERE c.assistant_id=%s AND c.id=%s''',
                (self.assistant_id,identifier)).fetchone()

    def by_provider(self,provider_id):
        with self.repository.connection() as db:
            return db.execute('SELECT * FROM ea_phone_call WHERE assistant_id=%s AND provider_id=%s',
                              (self.assistant_id,provider_id)).fetchone()

    def by_request(self,source_id):
        with self.repository.connection() as db:
            row=db.execute('SELECT id FROM ea_phone_call WHERE assistant_id=%s AND source_id=%s',
                           (self.assistant_id,source_id)).fetchone()
        return self.get(row['id']) if row else None

    def current_for_task(self,task_id):
        with self.repository.connection() as db:
            task=self.repository._task(db,task_id)
            if not task or not self.repository._permitted(db,task_id,writes=False):
                raise PermissionDenied('Assignment is unavailable')
            row=db.execute('''SELECT * FROM ea_phone_call WHERE assistant_id=%s AND conversation_id=%s
                AND person_id=%s AND state='accepted' ''',
                (self.assistant_id,task['conversation_id'],task['person_id'])).fetchone()
            if not row:raise PermissionDenied('This task has no active conversation to close')
            return row

    def claim(self):
        with self.repository.connection() as db:
            while True:
                row=db.execute('''SELECT * FROM ea_phone_call WHERE assistant_id=%s AND state='queued'
                    ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED''',(self.assistant_id,)).fetchone()
                if not row:return None
                task=self.repository._task(db,row['parent_task_id'])
                recipient=db.execute('SELECT status FROM ea_person WHERE assistant_id=%s AND id=%s',
                                     (self.assistant_id,row['person_id'])).fetchone()
                allowed=bool(task and task['person_status']=='approved' and task['state']!='cancelled'
                    and recipient and recipient['status'] not in {'suspended','deleted'})
                state='attempting' if allowed else 'cancelled'
                row=db.execute('UPDATE ea_phone_call SET state=%s,updated_at=now() WHERE id=%s RETURNING *',
                               (state,row['id'])).fetchone()
                if allowed:return row

    def bind(self,identifier,provider_id,*,to,from_,status,duration=None):
        if status not in STATUSES:
            raise ValueError('Unknown carrier call status')
        with self.repository.connection() as db:
            row=db.execute('SELECT * FROM ea_phone_call WHERE assistant_id=%s AND id=%s FOR UPDATE',
                           (self.assistant_id,identifier)).fetchone()
            if not row or row['state'] in {'queued','cancelled'}:
                raise PermissionDenied('Call was not authorized for transport')
            if self.repository.normalize_phone(to)!=row['expected_to'] or self.repository.normalize_phone(from_)!=row['expected_from']:
                raise PermissionDenied('Call endpoints do not match')
            if row['provider_id'] and row['provider_id']!=provider_id:
                raise Conflict('Call is already bound to a different provider identifier')
            duration=int(duration) if duration not in {None,''} else None
            if duration is not None and duration<0:raise ValueError('Invalid call duration')
            already_ended=row['ended_at'] is not None
            ended=status in TERMINAL
            rank={'queued':0,'initiated':1,'ringing':2,'in-progress':3}
            effective=status if rank.get(status,4)>=rank.get(row['carrier_status'],-1) else row['carrier_status']
            row=db.execute('''UPDATE ea_phone_call SET provider_id=coalesce(provider_id,%s),
                carrier_status=CASE WHEN ended_at IS NULL THEN %s ELSE carrier_status END,
                state=CASE WHEN ended_at IS NOT NULL OR %s THEN 'finished' ELSE 'accepted' END,
                duration_seconds=coalesce(%s,duration_seconds),
                reported_at=CASE WHEN ended_at IS NULL AND %s THEN NULL ELSE reported_at END,
                ended_at=CASE WHEN %s THEN coalesce(ended_at,now()) ELSE ended_at END,
                updated_at=now() WHERE id=%s RETURNING *''',
                (provider_id,effective,ended,duration,ended,ended,identifier)).fetchone()
            self.repository._event(db,'call:'+identifier+':carrier:'+status,'call.carrier_status',
                {'call_id':identifier,'provider_id':provider_id,'status':status,'duration_seconds':duration,
                 'conversation_confirmed':False,'arrived_after_end':already_ended},
                person_id=row['person_id'],task_id=row['parent_task_id'])
            return row

    def attach_media(self,identifier,provider_id):
        with self.repository.connection() as db:
            row=db.execute('''UPDATE ea_phone_call SET media_connected_at=now(),updated_at=now()
                WHERE assistant_id=%s AND id=%s AND provider_id=%s AND media_connected_at IS NULL
                AND state='accepted' RETURNING *''',(self.assistant_id,identifier,provider_id)).fetchone()
            if not row:raise Conflict('This call cannot attach another media session')
            return row

    def uncertain(self,identifier,error_type):
        with self.repository.connection() as db:
            row=db.execute('''UPDATE ea_phone_call SET state='uncertain',updated_at=now()
                WHERE assistant_id=%s AND id=%s AND state='attempting' RETURNING *''',
                (self.assistant_id,identifier)).fetchone()
            if row:
                self.repository._event(db,'call:'+identifier+':uncertain','call.uncertain',
                    {'call_id':identifier,'error_type':error_type},person_id=row['person_id'],
                    task_id=row['parent_task_id'])

    def voice_finished(self,identifier,*,finalized,usage):
        with self.repository.connection() as db:
            row=db.execute('''UPDATE ea_phone_call SET native_finalized=%s,voice_usage=%s,updated_at=now()
                WHERE assistant_id=%s AND id=%s RETURNING *''',
                (finalized,Jsonb(usage),self.assistant_id,identifier)).fetchone()
            if not row:raise ValueError('Unknown call')
            self.repository._event(db,'call:'+identifier+':voice-closed','call.voice_closed',
                {'call_id':identifier,'native_finalized':finalized,'usage':usage},
                person_id=row['person_id'],task_id=row['parent_task_id'])

    def error(self,identifier,code):
        with self.repository.connection() as db:
            row=db.execute('SELECT person_id,parent_task_id FROM ea_phone_call WHERE assistant_id=%s AND id=%s',
                           (self.assistant_id,identifier)).fetchone()
            if not row:raise ValueError('Unknown call')
            self.repository._event(db,'call:'+identifier+':error:'+code,'call.error',
                {'call_id':identifier,'code':code},person_id=row['person_id'],task_id=row['parent_task_id'])

    def unreported(self):
        with self.repository.connection() as db:
            return db.execute('''SELECT c.*,v.transcript,v.finalized FROM ea_phone_call c
                JOIN ea_conversation v ON v.id=c.conversation_id WHERE c.assistant_id=%s
                AND c.parent_task_id IS NOT NULL AND (c.reported_at IS NULL OR v.finalized AND NOT c.reported_finalized)
                AND (c.state IN ('uncertain','cancelled') OR c.state='finished'
                    AND (v.finalized OR c.ended_at<now()-interval '20 seconds'))
                ORDER BY c.updated_at LIMIT 50''',(self.assistant_id,)).fetchall()

    def reported(self,identifier):
        with self.repository.connection() as db:
            row=db.execute('''SELECT parent_task_id FROM ea_phone_call WHERE assistant_id=%s AND id=%s
                FOR UPDATE''',(self.assistant_id,identifier)).fetchone()
            if not row:raise ValueError('Unknown call')
            db.execute('''UPDATE ea_phone_call SET reported_at=coalesce(reported_at,now()),reported_finalized=
                (SELECT finalized FROM ea_conversation WHERE id=ea_phone_call.conversation_id) WHERE id=%s''',(identifier,))
            if row['parent_task_id']:
                db.execute('''UPDATE ea_task SET context=jsonb_set(context,'{pending_calls}',
                    coalesce(context->'pending_calls','[]'::jsonb)-%s),updated_at=now() WHERE id=%s''',
                    (identifier,row['parent_task_id']))
