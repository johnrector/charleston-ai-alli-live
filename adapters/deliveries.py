"""Durable delivery intent and receipts; no reasoning or provider calls."""
from datetime import datetime, timezone,timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from alli_core.authority import Conflict, PermissionDenied


def instant(value):
    if isinstance(value,str):
        value=datetime.fromisoformat(value.replace('Z','+00:00'))
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Supply an explicit time zone')
    return value.astimezone(timezone.utc)


class DeliveryRepository:
    def __init__(self,repository):
        self.repository=repository
        self.assistant_id=repository.profile.id

    def _enqueue(self,db,task,person_id,body,source_id,due,*,scheduled=False):
        if not isinstance(body,str) or not 1<=len(body)<=1600:
            raise ValueError('A text must contain between 1 and 1600 characters')
        prior=db.execute('SELECT * FROM ea_delivery WHERE assistant_id=%s AND source_id=%s',
                         (self.assistant_id,source_id)).fetchone()
        if prior:
            if (str(prior['person_id'])!=person_id or prior['body']!=body or prior['task_id']!=task['id']
                    or scheduled and prior['due_at']!=due):
                raise Conflict('This delivery source is already bound to another message')
            return prior
        recipient=db.execute('''SELECT p.status,e.address FROM ea_person p JOIN ea_endpoint e
            ON e.person_id=p.id AND e.assistant_id=p.assistant_id
            WHERE p.assistant_id=%s AND p.id=%s AND e.kind='phone' AND e.active''',
            (self.assistant_id,person_id)).fetchone()
        if not recipient or recipient['status'] in {'suspended','deleted'}:
            raise PermissionDenied('The recipient is unavailable')
        identifier=uuid4().hex
        commitment=None
        if scheduled:
            commitment=identifier
            db.execute('''INSERT INTO ea_commitment(id,assistant_id,person_id,task_id,kind,instruction,due_at)
                VALUES(%s,%s,%s,%s,'sms',%s,%s)''',
                (identifier,self.assistant_id,person_id,task['id'],body,due))
        row=db.execute('''INSERT INTO ea_delivery(id,assistant_id,source_id,person_id,task_id,
            commitment_id,channel,destination,body,due_at,expires_at) VALUES(%s,%s,%s,%s,%s,%s,'sms',%s,%s,%s,%s)
            RETURNING *''',(identifier,self.assistant_id,source_id,person_id,task['id'],commitment,
                           recipient['address'],body,due,due+timedelta(minutes=15) if scheduled else None)).fetchone()
        self.repository._event(db,'delivery:'+identifier+':queued','delivery.queued',
            {'delivery_id':identifier,'due_at':due.isoformat(),'scheduled':scheduled},
            person_id=person_id,task_id=task['id'])
        return row

    def queue_text(self,task_id,person_id,body,source_id,*,at=None):
        due=instant(at) if at else datetime.now(timezone.utc)
        if at and not -60<=(due-datetime.now(timezone.utc)).total_seconds()<=366*86400:
            raise ValueError('Schedule within the next year, with an explicit timezone')
        with self.repository.connection() as db:
            task=self.repository._task(db,task_id,lock=True)
            if not task or not self.repository._permitted(db,task_id,writes=False) or task['scope']!='owner':
                raise PermissionDenied('Sending messages to other people requires owner authority')
            return self._enqueue(db,task,str(person_id),body,source_id,due,scheduled=at is not None)

    def queue_result(self,task_id,body,turn_id):
        """Application-generated follow-through, always to the actual requester."""
        with self.repository.connection() as db:
            task=self.repository._task(db,task_id,lock=True)
            if not task or task['person_status']!='approved' or task['state'] not in {'completed','partial','failed','waiting'}:
                raise PermissionDenied('No reportable assignment outcome')
            return self._enqueue(db,task,str(task['person_id']),body,
                'result:'+task_id+':'+turn_id,datetime.now(timezone.utc))

    def contact_notice(self,person_id):
        """An owner-requested notification, never executing the stranger's text."""
        source='contact-notice:'+str(person_id)
        with self.repository.connection() as db:
            person=db.execute('''SELECT p.name,p.status,e.address FROM ea_person p JOIN ea_endpoint e
                ON e.person_id=p.id AND e.assistant_id=p.assistant_id WHERE p.assistant_id=%s AND p.id=%s
                AND e.kind='phone' AND e.active''',(self.assistant_id,person_id)).fetchone()
            owner=db.execute('''SELECT p.id,e.address FROM ea_person p JOIN ea_endpoint e
                ON e.person_id=p.id AND e.assistant_id=p.assistant_id WHERE p.assistant_id=%s
                AND p.kind='owner' AND e.kind='phone' AND e.active''',(self.assistant_id,)).fetchone()
            if not person or not owner or person['status']!='known':return None
            identifier=uuid4().hex
            body=('Someone new contacted me'+(' — '+person['name'] if person['name'] else '')+
                ' at '+person['address']+'. Their request is saved. You can review and approve them in People; '
                'I have not committed to anything for them.')
            row=db.execute('''INSERT INTO ea_delivery(id,assistant_id,source_id,person_id,channel,destination,body)
                VALUES(%s,%s,%s,%s,'sms',%s,%s) ON CONFLICT(assistant_id,source_id) DO NOTHING RETURNING *''',
                (identifier,self.assistant_id,source,owner['id'],owner['address'],body)).fetchone()
            if row:
                self.repository._event(db,'delivery:'+identifier+':queued','delivery.contact_notice',
                    {'delivery_id':identifier,'contact_id':str(person_id)},person_id=owner['id'])
            return row

    def guest_acknowledgment(self,task_id):
        with self.repository.connection() as db:
            task=self.repository._task(db,task_id,lock=True)
            if not task or task['state']!='needs_owner' or task['person_status']!='known':return None
            body=("I'm "+self.repository.profile.name+', '+self.repository.profile.principal+
                "'s AI assistant. Since you're not on the approved list yet, I'll check with "
                +self.repository.profile.principal+' before committing. Your request is saved.')
            return self._enqueue(db,task,str(task['person_id']),body,'guest-ack:'+task_id,
                                 datetime.now(timezone.utc))

    def read(self,actor,*,task_id=None,limit=100):
        with self.repository.connection() as db:
            self.repository._person(db,actor,owner=True)
            return db.execute('''SELECT * FROM ea_delivery WHERE assistant_id=%s
                AND (%s::text IS NULL OR task_id=%s) ORDER BY created_at DESC LIMIT %s''',
                (self.assistant_id,task_id,task_id,min(max(limit,1),500))).fetchall()

    def pending(self,task_id):
        with self.repository.connection() as db:
            return bool(db.execute('''SELECT 1 FROM ea_delivery WHERE assistant_id=%s AND task_id=%s
                AND source_id NOT LIKE 'result:%%' AND due_at<=now() AND state IN ('queued','attempting')
                LIMIT 1''',(self.assistant_id,task_id)).fetchone())

    def unforwarded(self,limit=50):
        with self.repository.connection() as db:
            return db.execute('''SELECT * FROM ea_delivery WHERE assistant_id=%s
                AND state IN ('accepted','delivered','failed','uncertain')
                AND forwarded_state IS DISTINCT FROM state AND task_id IS NOT NULL
                AND source_id NOT LIKE 'result:%%' ORDER BY updated_at LIMIT %s''',
                (self.assistant_id,min(max(limit,1),100))).fetchall()

    def forwarded(self,delivery_id,state):
        with self.repository.connection() as db:
            db.execute('''UPDATE ea_delivery SET forwarded_state=%s WHERE assistant_id=%s
                AND id=%s AND state=%s''',(state,self.assistant_id,delivery_id,state))

    def cancel(self,actor,commitment_id,revision):
        with self.repository.connection() as db:
            self.repository._person(db,actor,owner=True)
            delivery=db.execute('''SELECT * FROM ea_delivery WHERE assistant_id=%s AND commitment_id=%s
                FOR UPDATE''',(self.assistant_id,commitment_id)).fetchone()
            promise=db.execute('''SELECT * FROM ea_commitment WHERE assistant_id=%s AND id=%s
                FOR UPDATE''',(self.assistant_id,commitment_id)).fetchone()
            if not promise or promise['revision']!=revision:
                raise Conflict('Commitment changed or is unavailable')
            if not delivery or delivery['state'] not in {'queued','cancelled'}:
                raise Conflict('Delivery has already started; it cannot safely be cancelled')
            db.execute("UPDATE ea_delivery SET state='cancelled',updated_at=now() WHERE id=%s",(delivery['id'],))
            promise=db.execute('''UPDATE ea_commitment SET state='cancelled',revision=revision+1,
                updated_at=now() WHERE id=%s RETURNING *''',(commitment_id,)).fetchone()
            self.repository._event(db,'commitment:'+commitment_id+':'+str(promise['revision']),
                'commitment.cancelled',{'delivery_id':delivery['id']},
                person_id=promise['person_id'],task_id=promise['task_id'])
            return promise

    def claim_due(self):
        with self.repository.connection() as db:
            while True:
                row=db.execute('''SELECT * FROM ea_delivery WHERE assistant_id=%s AND state='queued'
                    AND due_at<=now() ORDER BY due_at,id LIMIT 1 FOR UPDATE SKIP LOCKED''',
                    (self.assistant_id,)).fetchone()
                if not row:return None
                task=self.repository._task(db,row['task_id']) if row['task_id'] else None
                person=db.execute('SELECT status,kind FROM ea_person WHERE assistant_id=%s AND id=%s',
                                  (self.assistant_id,row['person_id'])).fetchone()
                owner=bool(task and task['scope']=='owner' and task['person_kind']=='owner'
                    and task['authority'].get('verified_owner') and task['authority'].get('proof_id'))
                own_report=bool(task and row['source_id'].startswith('result:'+task['id']+':')
                    and task['person_id']==row['person_id'] and person and person['status']=='approved')
                allowed=bool(task and task['state']!='cancelled' and task['person_status']=='approved'
                    and person and person['status'] not in {'suspended','deleted'} and (owner or own_report))
                if task and row['source_id']=='guest-ack:'+task['id']:
                    allowed=bool(task['state']=='needs_owner' and task['person_status']=='known'
                        and row['person_id']==task['person_id'])
                if not task and person and person['kind']=='owner' and person['status']=='approved':
                    # This prefix is generated only by contact_notice, which is
                    # not a model-accessible tool. An audit event backs it.
                    allowed=bool(row['source_id'].startswith('contact-notice:') and db.execute('''SELECT 1
                        FROM ea_ledger WHERE assistant_id=%s AND source_id=%s AND kind='delivery.contact_notice' ''',
                        (self.assistant_id,'delivery:'+row['id']+':queued')).fetchone())
                expired=row['expires_at'] is not None and row['expires_at']<=datetime.now(timezone.utc)
                state='failed' if expired else 'attempting' if allowed else 'cancelled'
                row=db.execute('UPDATE ea_delivery SET state=%s,updated_at=now() WHERE id=%s RETURNING *',
                               (state,row['id'])).fetchone()
                if row['commitment_id']:
                    db.execute('UPDATE ea_commitment SET state=%s,updated_at=now() WHERE id=%s',
                               ('queued' if state=='attempting' else 'blocked' if expired else 'cancelled',row['commitment_id']))
                if expired:
                    db.execute("UPDATE ea_delivery SET receipt=%s WHERE id=%s",(Jsonb({'ok':False,'not_attempted':True,'reason':'delivery_window_missed'}),row['id']))
                self.repository._event(db,'delivery:'+row['id']+':'+state,'delivery.'+state,
                    {'delivery_id':row['id']},person_id=row['person_id'],task_id=row['task_id'])
                if state=='attempting':return row

    def receipt(self,delivery_id,provider_id,state,receipt):
        if state not in {'accepted','delivered','failed','uncertain'}:
            raise ValueError('Invalid delivery receipt')
        with self.repository.connection() as db:
            row=db.execute('SELECT * FROM ea_delivery WHERE assistant_id=%s AND id=%s FOR UPDATE',
                           (self.assistant_id,delivery_id)).fetchone()
            if not row or row['state'] in {'queued','cancelled'}:
                raise Conflict('Delivery was not attempted')
            if row['provider_id'] and row['provider_id']!=provider_id:
                raise Conflict('Provider identifier does not match this delivery')
            # Signed provider callbacks may precede the send API reply and may
            # arrive out of order. Never downgrade delivery to mere acceptance.
            current=row['state']
            incoming=state
            if current in {'delivered','failed'}:
                state=current
            saved_receipt=receipt if current not in {'delivered','failed'} or incoming==current else {}
            row=db.execute('''UPDATE ea_delivery SET provider_id=coalesce(provider_id,%s),state=%s,
                receipt=coalesce(receipt,'{}'::jsonb) || %s,updated_at=now() WHERE id=%s RETURNING *''',
                (provider_id,state,Jsonb(saved_receipt),delivery_id)).fetchone()
            if row['commitment_id'] and state in {'delivered','failed','uncertain'}:
                db.execute('UPDATE ea_commitment SET state=%s,updated_at=now() WHERE id=%s',
                           ('completed' if state=='delivered' else 'blocked',row['commitment_id']))
            self.repository._event(db,'delivery:'+delivery_id+':receipt:'+incoming,'delivery.receipt',
                {'delivery_id':delivery_id,'provider_id':provider_id,'receipt':receipt,
                 'reported_state':incoming,'canonical_state':state},
                person_id=row['person_id'],task_id=row['task_id'])
            return row
