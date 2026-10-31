"""Durable operational state, isolated from OpenAI, channels and provider clients."""
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import phonenumbers
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg.types.json import Jsonb
from jsonschema import Draft202012Validator

from alli_core.authority import Actor, Conflict, PermissionDenied
from alli_core.outcome import FinalizedOutcome


class PostgresRepository:
    def __init__(self, dsn, profile, *, friend_active=3, friend_daily=10):
        self.dsn, self.profile = dsn, profile
        self.friend_active, self.friend_daily = friend_active, friend_daily
        self.pool=None

    def open(self,*,max_size=4):
        if self.pool is not None:return
        pool=ConnectionPool(self.dsn,open=False,min_size=1,max_size=max_size,timeout=10,
            kwargs={'row_factory':dict_row,'connect_timeout':10},
            check=ConnectionPool.check_connection,name=self.profile.id+'-postgres')
        try:pool.open(wait=True,timeout=10)
        except BaseException:
            pool.close()
            raise
        self.pool=pool

    def close(self):
        if self.pool is not None:
            self.pool.close()
            self.pool=None

    def connection(self):
        if self.pool is not None:return self.pool.connection()
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10)

    def initialize(self):
        schema = Path(__file__).resolve().parents[1] / 'persistence/schema.sql'
        with self.connection() as db:
            db.execute(schema.read_text())
            db.execute('''INSERT INTO ea_assistant(id,name,principal,timezone) VALUES(%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,principal=EXCLUDED.principal,
                timezone=EXCLUDED.timezone''', (self.profile.id, self.profile.name,
                    self.profile.principal, self.profile.timezone))
            if (not db.execute('SELECT 1 FROM ea_ledger LIMIT 1').fetchone()
                    and db.execute("SELECT to_regclass('alli_communication_update') AS name").fetchone()['name']):
                maximum=db.execute('SELECT coalesce(max(id),0) AS value FROM alli_communication_update').fetchone()['value']
                if maximum:
                    # Leave room for the old service's final in-flight feed
                    # records during the one-time clean-runtime cutover.
                    db.execute("SELECT setval(pg_get_serial_sequence('ea_ledger','id'),%s,true)",(maximum+1000000,))

    @staticmethod
    def normalize_phone(value):
        try:
            number = phonenumbers.parse(value, 'US')
            if not phonenumbers.is_possible_number(number):
                raise ValueError('Invalid phone number')
            return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException as exc:
            raise ValueError('Invalid phone number') from exc

    def _event(self, db, source, kind, facts, *, person_id=None, task_id=None):
        # Serialize this assistant's feed commits, so a cursor never skips an
        # earlier allocated ID that commits after a later one.
        lock = int.from_bytes(hashlib.sha256(self.profile.id.encode()).digest()[:8], 'big', signed=True)
        db.execute('SELECT pg_advisory_xact_lock(%s)', (lock,))
        db.execute('''INSERT INTO ea_ledger(assistant_id,person_id,task_id,source_id,kind,facts)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(assistant_id,source_id) DO NOTHING''',
            (self.profile.id, person_id, task_id, source, kind, Jsonb(facts)))

    def _person(self, db, actor, *, owner=False):
        if actor.assistant_id != self.profile.id:
            raise PermissionDenied('Wrong assistant')
        person = db.execute('SELECT * FROM ea_person WHERE assistant_id=%s AND id=%s',
                            (self.profile.id, actor.person_id)).fetchone()
        if not person or person['status'] != 'approved':
            raise PermissionDenied('This person is not approved')
        if owner and not (person['kind'] == 'owner' and actor.verified_owner and actor.proof_id):
            raise PermissionDenied('Owner authorization required')
        return person

    def bootstrap_owner(self, phone):
        """Provisioning only. Returns an identity, never an authenticated session."""
        phone = self.normalize_phone(phone)
        with self.connection() as db:
            db.execute('SELECT id FROM ea_assistant WHERE id=%s FOR UPDATE', (self.profile.id,))
            person = db.execute("SELECT id FROM ea_person WHERE assistant_id=%s AND kind='owner'",
                                (self.profile.id,)).fetchone()
            if person:
                return str(person['id'])
            person_id = str(uuid4())
            db.execute('''INSERT INTO ea_person(id,assistant_id,name,kind,status,source)
                VALUES(%s,%s,%s,'owner','approved','owner_configuration')''',
                (person_id, self.profile.id, self.profile.principal))
            db.execute('''INSERT INTO ea_endpoint(assistant_id,person_id,kind,address)
                VALUES(%s,%s,'phone',%s)''', (self.profile.id, person_id, phone))
            self._event(db, 'owner:configured', 'owner.configured', {}, person_id=person_id)
            return person_id

    def recognize(self, phone):
        phone = self.normalize_phone(phone)
        with self.connection() as db:
            return db.execute('''SELECT p.*,e.id AS endpoint_id,e.address FROM ea_person p
                JOIN ea_endpoint e ON e.person_id=p.id AND e.assistant_id=p.assistant_id
                WHERE p.assistant_id=%s AND e.kind='phone' AND e.address=%s AND e.active''',
                (self.profile.id, phone)).fetchone()

    def save_person(self, actor, *, name, phone, email='', person_id=None, revision=None, attributes=None):
        phone = self.normalize_phone(phone)
        name = ' '.join(name.split())
        if not 1 <= len(name) <= 120 or len(email) > 254:
            raise ValueError('Supply a name and a valid contact address')
        if attributes is not None:
            if not isinstance(attributes, dict):
                raise ValueError('Contact details must be an object')
            self._validate_json(attributes)
        with self.connection() as db:
            self._person(db, actor, owner=True)
            if person_id:
                row = db.execute('''SELECT * FROM ea_person WHERE assistant_id=%s AND id=%s
                    FOR UPDATE''', (self.profile.id, person_id)).fetchone()
                if not row or row['kind'] == 'owner':
                    raise PermissionDenied('Select a contact')
                if row['revision'] != revision:
                    raise Conflict('Contact changed; read it again')
                # Keep approval/suspension separate from editing the address book.
                db.execute('''UPDATE ea_person SET name=%s,email=%s,attributes=coalesce(%s,attributes),
                    revision=revision+1,updated_at=now() WHERE id=%s''',
                    (name, email, Jsonb(attributes) if attributes is not None else None, person_id))
                db.execute("UPDATE ea_endpoint SET active=false WHERE assistant_id=%s AND person_id=%s AND kind='phone'",
                           (self.profile.id, person_id))
            else:
                person_id = str(uuid4())
                db.execute('''INSERT INTO ea_person(id,assistant_id,name,email,attributes,source)
                    VALUES(%s,%s,%s,%s,%s,'owner_address_book')''',
                    (person_id, self.profile.id, name, email, Jsonb(attributes or {})))
            db.execute('''INSERT INTO ea_endpoint(assistant_id,person_id,kind,address)
                VALUES(%s,%s,'phone',%s)''', (self.profile.id, person_id, phone))
            row = db.execute('SELECT * FROM ea_person WHERE id=%s', (person_id,)).fetchone()
            self._event(db, 'person:'+str(person_id)+':'+str(row['revision']), 'person.saved',
                        {'name': name, 'email': email, 'phone': phone, 'attributes': attributes,
                         'actor': actor.person_id}, person_id=person_id)
            return row

    def set_person_status(self, actor, person_id, status, revision):
        if status not in {'known', 'approved', 'suspended', 'deleted'}:
            raise ValueError('Invalid contact status')
        with self.connection() as db:
            self._person(db, actor, owner=True)
            row = db.execute('''SELECT * FROM ea_person WHERE assistant_id=%s AND id=%s FOR UPDATE''',
                             (self.profile.id, person_id)).fetchone()
            if not row or row['kind'] == 'owner':
                raise PermissionDenied('Select a contact')
            if row['revision'] != revision:
                raise Conflict('Contact changed; read it again')
            updated = db.execute('''UPDATE ea_person SET status=%s,revision=revision+1,updated_at=now()
                WHERE id=%s RETURNING *''', (status, person_id)).fetchone()
            if status != 'approved':
                # Restoration never replays work cancelled during suspension.
                db.execute('''UPDATE ea_task SET state='cancelled',updated_at=now()
                    WHERE assistant_id=%s AND person_id=%s AND state IN ('queued','working','waiting','needs_owner')''',
                    (self.profile.id, person_id))
                db.execute('''UPDATE ea_schedule SET state='cancelled',control_state='contact_cancel',
                    revision=revision+1,updated_at=now() WHERE assistant_id=%s AND person_id=%s
                    AND state NOT IN ('cancelled','rejected','completed')''',(self.profile.id,person_id))
            elif row['status']=='known':
                # Approval releases bounded recent requests in their existing
                # friend scope; it never grants private owner access.
                db.execute('''UPDATE ea_task SET state='queued',updated_at=now() WHERE id IN (
                    SELECT id FROM ea_task WHERE assistant_id=%s AND person_id=%s AND state='needs_owner'
                    AND created_at>now()-interval '7 days' ORDER BY created_at DESC LIMIT %s)''',
                    (self.profile.id,person_id,self.friend_active))
            if status!='approved' or row['status']=='known':
                db.execute('''UPDATE ea_delivery SET state='cancelled',updated_at=now()
                    WHERE assistant_id=%s AND person_id=%s AND state='queued' ''', (self.profile.id, person_id))
                db.execute('''UPDATE ea_commitment SET state='cancelled',updated_at=now()
                    WHERE assistant_id=%s AND person_id=%s AND state IN ('planned','queued')''',
                    (self.profile.id, person_id))
            self._event(db, 'person:'+str(person_id)+':'+str(updated['revision']), 'person.status',
                        {'status': status, 'actor': actor.person_id}, person_id=person_id)
            return updated

    def list_people(self, actor, *, limit=100):
        with self.connection() as db:
            self._person(db, actor, owner=True)
            return db.execute('''SELECT p.*,activity.last_activity,(SELECT address FROM ea_endpoint e
                WHERE e.assistant_id=p.assistant_id AND e.person_id=p.id AND e.kind='phone' AND e.active LIMIT 1) AS phone
                FROM ea_person p LEFT JOIN LATERAL
                (SELECT max(started_at) AS last_activity FROM ea_conversation c
                 WHERE c.assistant_id=p.assistant_id AND c.person_id=p.id) activity ON true
                WHERE p.assistant_id=%s ORDER BY activity.last_activity DESC NULLS LAST,p.name,p.id LIMIT %s''',
                (self.profile.id, min(max(limit, 1), 500))).fetchall()

    def owner_tasks(self,actor,limit=100):
        with self.connection() as db:
            self._person(db,actor,owner=True)
            return db.execute('SELECT * FROM ea_task WHERE assistant_id=%s ORDER BY updated_at DESC LIMIT %s',
                              (self.profile.id,min(max(limit,1),500))).fetchall()

    def owner_actor(self,proof_id,channel='dot'):
        if not proof_id:raise PermissionDenied('Owner proof is required')
        with self.connection() as db:
            person=db.execute("SELECT id FROM ea_person WHERE assistant_id=%s AND kind='owner' AND status='approved'",
                              (self.profile.id,)).fetchone()
            if not person:raise PermissionDenied('Owner is not configured')
            return Actor(self.profile.id,str(person['id']),channel,True,proof_id)

    def record_dot_activity(self,actor,request_id,title,status,details,evidence):
        if not 1<=len(request_id)<=128 or not 1<=len(title)<=180 or len(details)>3000:
            raise ValueError('Invalid activity')
        if status not in {'queued','working','completed','blocked','cancelled'}:
            raise ValueError('Invalid activity state')
        self._validate_json(evidence)
        with self.connection() as db:
            self._person(db,actor,owner=True)
            self._event(db,'dot:'+request_id,'dot.reported',{'title':title,'status':status,'details':details,
                'evidence':evidence,'reported_by':'owner_authenticated_dot','independently_verified':False},person_id=actor.person_id)
            return {'ok':True,'request_id':request_id,'status':status,'recorded':True,'independently_verified':False}

    def owner_task(self,actor,task_id):
        with self.connection() as db:
            self._person(db,actor,owner=True)
            return self._task(db,task_id)

    def owner_commitments(self,actor,limit=100):
        with self.connection() as db:
            self._person(db,actor,owner=True)
            return db.execute('''SELECT c.*,p.name AS person_name FROM ea_commitment c
                JOIN ea_person p ON p.assistant_id=c.assistant_id AND p.id=c.person_id WHERE c.assistant_id=%s
                ORDER BY CASE WHEN c.state IN ('planned','queued','blocked') THEN 0 ELSE 1 END,
                    CASE WHEN c.state IN ('planned','queued','blocked') THEN c.due_at END ASC NULLS LAST,
                    c.due_at DESC LIMIT %s''',
                              (self.profile.id,min(max(limit,1),500))).fetchall()

    def recent_ledger(self,actor,*,person_id=None,task_id=None,limit=100):
        with self.connection() as db:
            self._person(db,actor,owner=True)
            rows=db.execute('''SELECT * FROM ea_activity WHERE assistant_id=%s
                AND (%s::uuid IS NULL OR person_id=%s::uuid) AND (%s::text IS NULL OR task_id=%s)
                ORDER BY id DESC LIMIT %s''',(self.profile.id,person_id,person_id,task_id,task_id,
                                             min(max(limit,1),500))).fetchall()
            return list(reversed(rows))

    @staticmethod
    def _validate_json(value, *, schema=False):
        encoded = json.dumps(value, allow_nan=False)
        if len(encoded.encode()) > 131072:
            raise ValueError('Record is too large')
        def walk(item, depth=0):
            if depth > 32:
                raise ValueError('Record nesting is too deep')
            if isinstance(item, dict):
                if schema and any(key in item and not str(item[key]).startswith('#') for key in ('$ref','$dynamicRef','$recursiveRef')):
                    raise ValueError('External schema references are unsupported')
                for child in item.values(): walk(child, depth+1)
            elif isinstance(item, list):
                for child in item: walk(child, depth+1)
        walk(value)

    def create_collection(self, actor, name, definition):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('Supply a collection name')
        self._validate_json(definition, schema=True)
        Draft202012Validator.check_schema(definition)
        if definition.get('type') != 'object':
            raise ValueError('A collection contains object records')
        with self.connection() as db:
            self._person(db, actor, owner=True)
            row = db.execute('''INSERT INTO ea_collection(assistant_id,name,definition) VALUES(%s,%s,%s)
                ON CONFLICT(assistant_id,name) DO NOTHING RETURNING *''',
                (self.profile.id, name.strip(), Jsonb(definition))).fetchone()
            if not row:
                row = db.execute('SELECT * FROM ea_collection WHERE assistant_id=%s AND name=%s',
                                 (self.profile.id, name.strip())).fetchone()
                if row['definition'] != definition:
                    raise Conflict('A collection with a different definition already exists')
            self._event(db, 'collection:'+str(row['id']), 'collection.created',
                        {'collection_id': str(row['id']), 'name': row['name'], 'actor': actor.person_id})
            return row

    def collections(self, actor):
        with self.connection() as db:
            self._person(db, actor, owner=True)
            return db.execute('SELECT * FROM ea_collection WHERE assistant_id=%s ORDER BY name,id',
                              (self.profile.id,)).fetchall()

    def put_record(self, actor, collection_id, body, *, record_id=None, revision=None):
        self._validate_json(body)
        with self.connection() as db:
            self._person(db, actor, owner=True)
            collection = db.execute('SELECT definition FROM ea_collection WHERE assistant_id=%s AND id=%s',
                                    (self.profile.id, collection_id)).fetchone()
            if not collection:
                raise PermissionDenied('Unknown collection')
            Draft202012Validator(collection['definition']).validate(body)
            if record_id:
                row = db.execute('''UPDATE ea_record SET body=%s,revision=revision+1,updated_at=now()
                    WHERE assistant_id=%s AND collection_id=%s AND id=%s AND revision=%s AND deleted_at IS NULL
                    RETURNING *''', (Jsonb(body), self.profile.id, collection_id, record_id, revision)).fetchone()
                if not row:
                    raise Conflict('Record changed or is unavailable; read it again')
            else:
                row = db.execute('''INSERT INTO ea_record(assistant_id,collection_id,body)
                    VALUES(%s,%s,%s) RETURNING *''', (self.profile.id, collection_id, Jsonb(body))).fetchone()
            self._event(db, 'record:'+str(row['id'])+':'+str(row['revision']), 'record.saved',
                        {'record_id': str(row['id']), 'revision': row['revision'],
                         'body': body, 'actor': actor.person_id})
            return row

    def records(self, actor, collection_id, *, include_deleted=False, limit=100):
        with self.connection() as db:
            self._person(db, actor, owner=True)
            return db.execute('''SELECT * FROM ea_record WHERE assistant_id=%s AND collection_id=%s
                AND (%s OR deleted_at IS NULL) ORDER BY updated_at DESC,id LIMIT %s''',
                (self.profile.id, collection_id, include_deleted, min(max(limit, 1), 500))).fetchall()

    def delete_record(self, actor, record_id, revision, *, restore=False):
        with self.connection() as db:
            self._person(db, actor, owner=True)
            row = db.execute('''UPDATE ea_record SET deleted_at=CASE WHEN %s THEN NULL ELSE now() END,
                revision=revision+1,updated_at=now() WHERE assistant_id=%s AND id=%s AND revision=%s
                RETURNING *''', (restore, self.profile.id, record_id, revision)).fetchone()
            if not row:
                raise Conflict('Record changed or is unavailable; read it again')
            self._event(db, 'record:'+str(row['id'])+':'+str(row['revision']),
                        'record.restored' if restore else 'record.deleted',
                        {'record_id': str(row['id']), 'actor': actor.person_id})
            return row

    def ledger(self, actor, *, after=0, person_id=None, limit=100):
        with self.connection() as db:
            self._person(db, actor, owner=True)
            return db.execute('''SELECT * FROM ea_activity WHERE assistant_id=%s AND id>%s
                AND (%s::uuid IS NULL OR person_id=%s::uuid) ORDER BY id LIMIT %s''',
                (self.profile.id, after, person_id, person_id, min(max(limit, 1), 500))).fetchall()

    def submit(self, actor, *, source_id, title, instruction, context=None, conversation_id=None,reservation_id=None,transaction=None):
        if not 1 <= len(source_id) <= 200 or not 1 <= len(instruction) <= 200000 or not 1 <= len(title) <= 180:
            raise ValueError('Invalid assignment')
        with (self.connection() if transaction is None else nullcontext(transaction)) as db:
            person = self._person(db, actor)
            db.execute('SELECT id FROM ea_person WHERE id=%s FOR UPDATE', (actor.person_id,))
            person = self._person(db, actor)
            prior = db.execute('SELECT * FROM ea_task WHERE assistant_id=%s AND source_id=%s',
                               (self.profile.id, source_id)).fetchone()
            if prior:
                if str(prior['person_id']) != actor.person_id or prior['instruction'] != instruction:
                    raise Conflict('The source is already bound to a different assignment')
                return prior
            owner = person['kind'] == 'owner' and actor.verified_owner and bool(actor.proof_id)
            if conversation_id:
                conversation=db.execute('''SELECT person_id,scope FROM ea_conversation
                    WHERE assistant_id=%s AND id=%s''',(self.profile.id,conversation_id)).fetchone()
                if not conversation or not owner and (str(conversation['person_id'])!=actor.person_id
                        or conversation['scope']=='owner'):
                    raise PermissionDenied('Conversation does not belong to this requester')
            if reservation_id:
                reservation=db.execute('''SELECT authority FROM ea_schedule WHERE assistant_id=%s AND id=%s
                    AND person_id=%s AND state IN ('active','completed')''',
                    (self.profile.id,reservation_id,actor.person_id)).fetchone()
                if not reservation or bool(reservation['authority'].get('verified_owner'))!=owner:
                    raise PermissionDenied('Invalid standing-work reservation')
            if not owner and not reservation_id:
                conversational=bool((context or {}).get('text_frontend'))
                count = db.execute('''SELECT count(*) FILTER(WHERE state IN ('queued','working','waiting','needs_owner')) AS active,
                    count(*) FILTER(WHERE created_at>now()-interval '24 hours') AS recent
                    FROM ea_task WHERE assistant_id=%s AND person_id=%s
                    AND (context @> '{"text_frontend":true}'::jsonb)=%s''',
                    (self.profile.id, actor.person_id,conversational)).fetchone()
                if count['active'] >= self.friend_active or count['recent'] >= self.friend_daily*(10 if conversational else 1):
                    raise PermissionDenied('Please finish existing assignments before adding more work')
            task_id = uuid4().hex
            row = db.execute('''INSERT INTO ea_task(id,assistant_id,person_id,conversation_id,source_id,title,
                instruction,scope,authority,context) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                (task_id, self.profile.id, actor.person_id, conversation_id, source_id, title, instruction,
                 'owner' if owner else 'friend', Jsonb({'channel': actor.channel,
                     'verified_owner': owner, 'proof_id': actor.proof_id,
                     'allowed_tools':list(actor.allowed_tools) if actor.allowed_tools is not None else None,
                     'invitation_email':actor.invitation_email}),
                 Jsonb(context or {}))).fetchone()
            db.execute('INSERT INTO ea_agent_run(task_id) VALUES(%s)', (task_id,))
            self._event(db, 'task:'+task_id+':accepted', 'task.accepted',
                        {'title': title, 'scope': row['scope']}, person_id=actor.person_id, task_id=task_id)
            return row

    def hold_request(self,person_id,*,source_id,instruction,conversation_id,channel,proof_id=''):
        """Record an unapproved person's request without executable authority."""
        if channel not in {'phone','sms'} or not 1<=len(instruction)<=200000:
            raise ValueError('Invalid contact request')
        with self.connection() as db:
            person=db.execute('SELECT * FROM ea_person WHERE assistant_id=%s AND id=%s FOR UPDATE',
                              (self.profile.id,person_id)).fetchone()
            if not person or person['status']!='known' or person['kind']!='contact':
                raise PermissionDenied('This request cannot enter contact review')
            conversation=db.execute('''SELECT person_id,channel FROM ea_conversation
                WHERE assistant_id=%s AND id=%s''',(self.profile.id,conversation_id)).fetchone()
            if not conversation or str(conversation['person_id'])!=str(person_id) or conversation['channel']!=channel:
                raise PermissionDenied('Conversation does not belong to this requester')
            prior=db.execute('SELECT * FROM ea_task WHERE assistant_id=%s AND source_id=%s',
                             (self.profile.id,source_id)).fetchone()
            if prior:
                if str(prior['person_id'])!=str(person_id) or prior['instruction']!=instruction:
                    raise Conflict('Request source is already used')
                return prior
            count=db.execute('''SELECT count(*) AS total FROM ea_task WHERE assistant_id=%s AND person_id=%s
                AND created_at>now()-interval '24 hours' ''',(self.profile.id,person_id)).fetchone()['total']
            if count>=self.friend_daily:raise PermissionDenied('Too many pending requests')
            identifier=uuid4().hex
            row=db.execute('''INSERT INTO ea_task(id,assistant_id,person_id,conversation_id,source_id,title,
                instruction,scope,authority,state) VALUES(%s,%s,%s,%s,%s,'Contact approval request',
                %s,'friend',%s,'needs_owner') RETURNING *''',
                (identifier,self.profile.id,person_id,conversation_id,source_id,instruction,
                 Jsonb({'channel':channel,'verified_owner':False,'proof_id':proof_id}))).fetchone()
            db.execute('INSERT INTO ea_agent_run(task_id) VALUES(%s)',(identifier,))
            self._event(db,'task:'+identifier+':held','task.approval_required',{},
                        person_id=person_id,task_id=identifier)
            return row

    def _task(self, db, task_id, *, lock=False):
        return db.execute('''SELECT t.*,p.status AS person_status,p.kind AS person_kind FROM ea_task t
            JOIN ea_person p ON p.id=t.person_id AND p.assistant_id=t.assistant_id
            WHERE t.assistant_id=%s AND t.id=%s''' + (' FOR UPDATE OF t' if lock else ''),
            (self.profile.id, task_id)).fetchone()

    def _permitted(self, db, task_id, *, writes):
        task = self._task(db, task_id)
        if not task or task['person_status'] != 'approved' or task['state'] not in {'queued','working','waiting'}:
            return False
        if task['scope'] == 'owner' and not (task['person_kind'] == 'owner' and
                task['authority'].get('verified_owner') and task['authority'].get('proof_id')):
            return False
        if writes and db.execute('''SELECT 1 FROM ea_action WHERE task_id=%s AND writes
                AND (state IN ('attempting','uncertain') OR receipt->>'uncertain'='true') LIMIT 1''',
                (task_id,)).fetchone():
            return False
        return True

    def permitted(self, task_id, *, writes):
        with self.connection() as db:
            return self._permitted(db, task_id, writes=writes)

    def session_id(self, task_id):
        with self.connection() as db:
            row = db.execute('''SELECT r.session_id FROM ea_agent_run r JOIN ea_task t ON t.id=r.task_id
                WHERE t.assistant_id=%s AND t.id=%s''', (self.profile.id, task_id)).fetchone()
            if not row:
                raise ValueError('Unknown assignment')
            return row['session_id']

    def claim_dispatch(self, task_id):
        with self.connection() as db:
            task=self._task(db,task_id)
            if not task:return False
            db.execute('SELECT id FROM ea_person WHERE id=%s FOR UPDATE',(task['person_id'],))
            task = self._task(db, task_id, lock=True)
            if not task or not self._permitted(db, task_id, writes=False):
                return False
            if task['scope']=='friend':
                active=db.execute('''SELECT count(*) AS count FROM ea_task WHERE assistant_id=%s
                    AND person_id=%s AND id<>%s AND state IN ('working','waiting')
                    AND NOT context @> '{"text_frontend":true}'::jsonb
                    AND NOT context @> '{"native_schedule_setup":true}'::jsonb''',
                    (self.profile.id,task['person_id'],task_id)).fetchone()['count']
                if active>=self.friend_active:return False
            row = db.execute('''UPDATE ea_agent_run SET dispatch_state='creating',updated_at=now()
                WHERE task_id=%s AND dispatch_state='ready' AND session_id IS NULL RETURNING task_id''',
                (task_id,)).fetchone()
            if not row:
                return False
            db.execute("UPDATE ea_task SET state='working',updated_at=now() WHERE id=%s", (task_id,))
            self._event(db, 'task:'+task_id+':dispatch', 'task.dispatching', {},
                        person_id=task['person_id'], task_id=task_id)
            return True

    def bind_session(self, task_id, session_id):
        with self.connection() as db:
            task = self._task(db, task_id, lock=True)
            if not task:
                raise ValueError('Unknown assignment')
            row = db.execute('''UPDATE ea_agent_run SET session_id=%s,dispatch_state='bound',updated_at=now()
                WHERE task_id=%s AND (session_id IS NULL OR session_id=%s) RETURNING task_id''',
                (session_id, task_id, session_id)).fetchone()
            if not row:
                raise Conflict('Assignment is already bound to another Agent session')
            self._event(db, 'task:'+task_id+':session', 'task.session_bound',
                        {'session_id': session_id}, person_id=task['person_id'], task_id=task_id)

    def dispatch_uncertain(self, task_id):
        with self.connection() as db:
            task = self._task(db, task_id, lock=True)
            if not task:
                raise ValueError('Unknown assignment')
            db.execute('''UPDATE ea_agent_run SET dispatch_state='uncertain',updated_at=now()
                WHERE task_id=%s AND session_id IS NULL''', (task_id,))
            self._event(db, 'task:'+task_id+':dispatch-uncertain', 'task.dispatch_uncertain', {},
                        person_id=task['person_id'], task_id=task_id)

    def claim_action(self, task_id, call_id, tool, arguments, writes):
        intent = hashlib.sha256(json.dumps([tool, arguments] if writes else ['read', call_id],
                               sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        with self.connection() as db:
            task = self._task(db, task_id, lock=True)
            if not task:
                return {'state': 'denied'}
            allowed=task['authority'].get('allowed_tools')
            if allowed is not None and tool not in allowed:
                return {'state':'denied'}
            prior = db.execute('''SELECT * FROM ea_action WHERE task_id=%s AND (call_id=%s OR intent=%s)''',
                               (task_id, call_id, intent)).fetchone()
            if prior:
                if prior['tool'] != tool or prior['arguments'] != arguments:
                    return {'state': 'conflict'}
                if prior['state'] == 'done':
                    return {'state': 'done', 'result': prior['receipt']}
                if prior['writes']:
                    return {'state': 'uncertain'}
                return {'state': 'claimed'}
            if not self._permitted(db, task_id, writes=writes):
                return {'state': 'denied'}
            db.execute('''INSERT INTO ea_action(task_id,call_id,tool,arguments,intent,writes)
                VALUES(%s,%s,%s,%s,%s,%s)''', (task_id, call_id, tool, Jsonb(arguments), intent, writes))
            self._event(db, 'action:'+task_id+':'+call_id+':attempt', 'action.attempting',
                        {'call_id': call_id, 'tool': tool, 'writes': writes},
                        person_id=task['person_id'], task_id=task_id)
            return {'state': 'claimed'}

    def save_action(self, task_id, call_id, result):
        with self.connection() as db:
            task = self._task(db, task_id, lock=True)
            if not task:
                raise ValueError('Unknown assignment')
            state = 'uncertain' if result.get('uncertain') else 'done'
            row = db.execute('''UPDATE ea_action SET state=%s,receipt=%s,updated_at=now()
                WHERE task_id=%s AND call_id=%s AND state='attempting' RETURNING call_id''',
                (state, Jsonb(result), task_id, call_id)).fetchone()
            if not row:
                prior = db.execute('SELECT receipt FROM ea_action WHERE task_id=%s AND call_id=%s',
                                   (task_id, call_id)).fetchone()
                if not prior or prior['receipt'] != result:
                    raise Conflict('Action already has a different receipt')
            operation=db.execute('SELECT tool,writes FROM ea_action WHERE task_id=%s AND call_id=%s',
                                 (task_id,call_id)).fetchone()
            audit=result if operation['writes'] else {
                'ok':result.get('ok'),'error':result.get('error'),'lookup_data_omitted':True}
            self._event(db, 'action:'+task_id+':'+call_id+':receipt', 'action.receipt',
                        {'call_id':call_id,'tool':operation['tool'],'read':not operation['writes'],
                         'receipt':audit},person_id=task['person_id'],task_id=task_id)

    def record_outcome(self, task_id, status, text, *, turn_id, remaining=(), notify=True,provider_error=None,notification=''):
        if status not in {'completed','partial','waiting','failed','cancelled'}:
            raise ValueError('Only a terminal turn can have an outcome')
        with self.connection() as db:
            task = self._task(db, task_id, lock=True)
            if not task:
                raise ValueError('Unknown assignment')
            run = db.execute('SELECT turn_id FROM ea_agent_run WHERE task_id=%s', (task_id,)).fetchone()
            if run['turn_id'] == turn_id or task['state'] == 'cancelled':
                return FinalizedOutcome(task['state'],task['result'])
            receipts = db.execute('SELECT * FROM ea_action WHERE task_id=%s AND writes', (task_id,)).fetchall()
            unresolved = any(r['state'] != 'done' or not (r['receipt'] or {}).get('ok') for r in receipts)
            pending = bool(task['context'].get('pending_calls') or task['context'].get('pending_computer_work'))
            delivery=db.execute('''SELECT state FROM ea_delivery WHERE task_id=%s
                AND source_id NOT LIKE 'result:%%' AND due_at<=now()
                AND state IN ('queued','attempting','uncertain') LIMIT 1''',(task_id,)).fetchone()
            pending = pending or bool(delivery)
            if delivery and delivery['state']=='uncertain':
                unresolved=True
            if db.execute("SELECT 1 FROM ea_phone_call WHERE parent_task_id=%s AND state='uncertain' LIMIT 1",
                          (task_id,)).fetchone():
                unresolved=True
            if db.execute('''SELECT 1 FROM ea_phone_call c JOIN ea_conversation v ON v.id=c.conversation_id
                WHERE c.parent_task_id=%s AND c.state='finished'
                AND (c.carrier_status<>'completed' OR jsonb_array_length(v.transcript)=0) LIMIT 1''',(task_id,)).fetchone():
                unresolved=True
            if db.execute("SELECT 1 FROM ea_delivery WHERE task_id=%s AND source_id NOT LIKE 'result:%%' AND state='failed' LIMIT 1",(task_id,)).fetchone():
                unresolved=True
            state = 'partial' if unresolved else status
            if status=='failed':
                # An unavailable model must not erase work already completed or
                # the verified result returned by its computer assignment.
                evidence=[]
                if task['result'] and task['result']!=text:
                    evidence.append('Earlier saved status: '+task['result'][:6000])
                children=db.execute('''SELECT t.title,t.state,left(t.result,6000) AS result FROM ea_task t
                    WHERE t.assistant_id=%s AND t.id<>%s AND (t.parent_id=%s OR EXISTS
                        (SELECT 1 FROM ea_phone_call c WHERE c.parent_task_id=%s
                            AND c.conversation_id=t.conversation_id))
                    AND t.state IN ('completed','partial') AND t.result<>''
                    ORDER BY t.created_at LIMIT 5''',(self.profile.id,task_id,task_id,task_id)).fetchall()
                evidence.extend(child['title']+': '+child['result'] for child in children)
                if evidence:
                    state='partial'
                    text='\n\n'.join([text,*evidence])
                if provider_error and task['scope']!='owner':
                    text='The AI service could not finish the remaining work. John needs to resolve its connection. Completed work and receipts are retained.'
                    remaining=('Service connection needs John’s attention.',)
            if unresolved and status == 'completed':
                text = 'Some requested actions have no verified success receipt. The work and actual receipts are saved for review.'
            if status == 'completed' and pending:
                state = 'waiting'
                text = 'Some delegated work is still pending. Its outcome has not been confirmed yet.'
            if status == 'completed' and task['context'].get('needs_input'):
                state = 'waiting'
            db.execute('''UPDATE ea_task SET state=%s,result=%s,updated_at=now(),context=context || %s,
                completed_at=CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s''',
                (state, text, Jsonb({'remaining':list(remaining),'notify_requested':notify,
                                   'notification':notification if state==status else ''}),
                 state not in {'waiting','working'}, task_id))
            db.execute('UPDATE ea_agent_run SET turn_id=%s,updated_at=now() WHERE task_id=%s', (turn_id, task_id))
            self._event(db, 'task:'+task_id+':turn:'+turn_id, 'task.'+state,
                        {'summary': text, 'remaining':list(remaining), 'notify_requested':notify,
                         'conversation_reply':bool(task['context'].get('text_frontend')),
                         'turn_id': turn_id, 'unresolved_actions': unresolved,
                         'provider_error':provider_error},
                        person_id=task['person_id'], task_id=task_id)
            return FinalizedOutcome(state,text)

    def execution_allowed(self, task_id, kind):
        if kind not in {'none','openai_hosted','self_hosted'}:
            return False
        with self.connection() as db:
            task = self._task(db, task_id)
            return bool(task and self._permitted(db, task_id, writes=False) and
                        (kind != 'self_hosted' or task['scope'] == 'owner'
                         and task['authority'].get('allowed_tools') is None))

    def issue_tool_grant(self, task_id, *, lifetime_seconds=86400):
        import secrets
        from datetime import datetime, timedelta, timezone
        if not 60 <= lifetime_seconds <= 604800:
            raise ValueError('Invalid grant lifetime')
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        expires = datetime.now(timezone.utc)+timedelta(seconds=lifetime_seconds)
        with self.connection() as db:
            self._task(db, task_id, lock=True)
            if not self._permitted(db, task_id, writes=False):
                raise PermissionDenied('Assignment is not active')
            db.execute('''INSERT INTO ea_tool_grant(digest,assistant_id,task_id,expires_at)
                VALUES(%s,%s,%s,%s)''', (digest, self.profile.id, task_id, expires))
        return token

    def resolve_tool_grant(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connection() as db:
            row = db.execute('''SELECT task_id,expires_at FROM ea_tool_grant WHERE assistant_id=%s
                AND digest=%s AND NOT revoked AND expires_at>now()''', (self.profile.id, digest)).fetchone()
            if not row or not self._permitted(db, row['task_id'], writes=False):
                return None
            task = self._task(db, row['task_id'])
            return {'task_id': task['id'], 'actor':Actor(self.profile.id, str(task['person_id']),
                task['authority']['channel'], task['scope']=='owner', task['authority'].get('proof_id',''),
                tuple(task['authority']['allowed_tools']) if task['authority'].get('allowed_tools') is not None else None,
                task['authority'].get('invitation_email','')),
                'expires_at':int(row['expires_at'].timestamp())}

    def begin_conversation(self,phone,source_id,channel,*,owner_verified=False):
        phone=self.normalize_phone(phone)
        if channel not in {'phone','sms','dot','dashboard'}:
            raise ValueError('Invalid channel')
        with self.connection() as db:
            # The phone endpoint, not a claimed name, selects the relationship.
            db.execute('SELECT id FROM ea_assistant WHERE id=%s FOR UPDATE',(self.profile.id,))
            person=db.execute('''SELECT p.* FROM ea_person p JOIN ea_endpoint e
                ON e.assistant_id=p.assistant_id AND e.person_id=p.id
                WHERE p.assistant_id=%s AND e.kind='phone' AND e.address=%s AND e.active''',
                (self.profile.id,phone)).fetchone()
            if not person:
                person=db.execute('''INSERT INTO ea_person(assistant_id,source)
                    VALUES(%s,'incoming_contact') RETURNING *''',(self.profile.id,)).fetchone()
                db.execute('''INSERT INTO ea_endpoint(assistant_id,person_id,kind,address)
                    VALUES(%s,%s,'phone',%s)''',(self.profile.id,person['id'],phone))
                self._event(db,'person:'+str(person['id'])+':introduced','person.introduced',
                            {'phone':phone},person_id=person['id'])
            scope='owner' if person['kind']=='owner' and owner_verified else (
                'friend' if person['status']=='approved' else 'guest')
            row=db.execute('''INSERT INTO ea_conversation(assistant_id,person_id,source_id,channel,scope)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(assistant_id,source_id) DO NOTHING RETURNING *''',
                (self.profile.id,person['id'],source_id,channel,scope)).fetchone()
            if not row:
                row=db.execute('SELECT * FROM ea_conversation WHERE assistant_id=%s AND source_id=%s',
                               (self.profile.id,source_id)).fetchone()
                if row['person_id']!=person['id'] or row['channel']!=channel:
                    raise Conflict('Conversation source is already bound to another person')
            self._event(db,'conversation:'+str(row['id'])+':started','conversation.started',
                        {'channel':channel,'scope':scope},person_id=person['id'])
            return {'conversation':row,'person':person,'phone':phone}

    def save_transcript(self,conversation_id,transcript,*,finalized=False):
        if not isinstance(transcript,list) or len(json.dumps(transcript,allow_nan=False).encode())>8*1024*1024:
            raise ValueError('Invalid or oversized conversation transcript')
        with self.connection() as db:
            row=db.execute('''UPDATE ea_conversation SET transcript=%s,finalized=finalized OR %s,
                ended_at=CASE WHEN %s THEN coalesce(ended_at,now()) ELSE ended_at END
                WHERE assistant_id=%s AND id=%s RETURNING person_id''',
                (Jsonb(transcript),finalized,finalized,self.profile.id,conversation_id)).fetchone()
            if not row:raise ValueError('Unknown conversation')
            if finalized:
                self._event(db,'conversation:'+str(conversation_id)+':closed','conversation.closed',
                            {'finalized':True},person_id=row['person_id'])

    def task_context(self,task_id):
        from datetime import datetime,timezone
        with self.connection() as db:
            if not self._permitted(db,task_id,writes=False):
                raise PermissionDenied('Assignment is unavailable')
            task=self._task(db,task_id)
            person=db.execute('SELECT name,email FROM ea_person WHERE id=%s',(task['person_id'],)).fetchone()
            conversation=db.execute('''SELECT id,channel,transcript,finalized,started_at FROM ea_conversation
                WHERE assistant_id=%s AND id=%s AND (%s OR person_id=%s AND scope<>'owner')''',
                (self.profile.id,task['conversation_id'],task['scope']=='owner',task['person_id'])).fetchone() if task['conversation_id'] else None
            # Never give a public/unverified task private owner transcripts.
            history=db.execute('''SELECT channel,transcript,started_at FROM ea_conversation
                WHERE assistant_id=%s AND person_id=%s AND (%s OR scope<>'owner')
                ORDER BY started_at DESC LIMIT 8''',
                (self.profile.id,task['person_id'],task['scope']=='owner')).fetchall()
            budget=24000
            excerpts=[]
            for item in history:
                text='\n'.join(str(part.get('role',''))+
                               (' [delivery '+str(part['delivery'])+']' if part.get('delivery') and part['delivery']!='delivered' else '')+
                               ': '+str(part.get('text',''))
                               for part in item['transcript'] if isinstance(part,dict))
                excerpt=text[-min(6000,budget):] if budget else ''
                if not excerpt:continue
                excerpts.append({'channel':item['channel'],'started_at':item['started_at'].isoformat(),
                                 'excerpt':excerpt,'truncated':len(excerpt)<len(text)})
                budget-=len(excerpt)
            # Discover persistent owner knowledge independently of recent task
            # history, without copying unrelated private record bodies into it.
            collections=[]
            if task['scope']=='owner':
                collections=[{'collection_id':str(row['id']),'name':row['name'],
                    'active_records':row['active_records']} for row in db.execute('''
                    SELECT c.id,c.name,count(r.id) AS active_records FROM ea_collection c
                    LEFT JOIN ea_record r ON r.collection_id=c.id AND r.assistant_id=c.assistant_id
                        AND r.deleted_at IS NULL
                    WHERE c.assistant_id=%s GROUP BY c.id,c.name ORDER BY c.name,c.id''',
                    (self.profile.id,)).fetchall()]
            return {'assistant':self.profile.name,'principal':self.profile.principal,
                'approved_public_facts':self.profile.public_facts,'facts_reviewed_at':self.profile.facts_reviewed_at,
                'requester':{'id':str(task['person_id']),'name':person['name'],'email':person['email']},
                'scope':task['scope'],'received_at':task['context'].get('requested_at') or task['created_at'].isoformat(),
                'scheduled_occurrence':{k:task['context'][k] for k in
                    ('native_schedule_id','native_invocation_id','scheduled_at','schedule_revision') if k in task['context']},
                'contact_channel_verified':task['authority'].get('proof_id','').startswith(('signed-sms:','verified-phone:')),
                'now':datetime.now(timezone.utc).isoformat(),'timezone':self.profile.timezone,
                'instruction':task['instruction'],'remaining':task['context'].get('remaining',[]),
                'current_conversation':conversation,
                'recent_conversations':excerpts,
                'saved_record_collections':collections,
                'action_receipts':db.execute('''SELECT task_id,tool,state,arguments,receipt FROM ea_action a
                    WHERE a.writes AND (a.task_id=%s OR EXISTS
                        (SELECT 1 FROM ea_task t JOIN ea_phone_call c ON c.conversation_id=t.conversation_id
                         WHERE t.id=a.task_id AND c.parent_task_id=%s))
                    ORDER BY a.updated_at DESC LIMIT 50''',(task_id,task_id)).fetchall(),
                'delivery_receipts':db.execute('''SELECT id,channel,destination,state,provider_id,receipt,due_at
                    FROM ea_delivery WHERE assistant_id=%s AND task_id=%s ORDER BY created_at DESC LIMIT 30''',
                    (self.profile.id,task_id)).fetchall(),
                'saved_updates':[json.loads(row['body']) for row in db.execute('''SELECT body FROM ea_agent_input
                    WHERE task_id=%s AND sent_at IS NULL ORDER BY created_at LIMIT 30''',(task_id,)).fetchall()],
                'call_receipts':db.execute('''SELECT c.id,c.person_id,c.state,c.carrier_status,
                    c.provider_id,c.duration_seconds,c.ended_at,v.transcript,v.finalized,
                    (SELECT jsonb_build_object('body',m.body,'state',m.state,'reason',m.reason,'playback_state',m.playback_state,'hangup_state',m.hangup_state,'played_at',m.played_at)
                    FROM ea_call_voicemail m WHERE m.assistant_id=c.assistant_id AND m.call_id=c.id) AS voicemail_evidence
                    FROM ea_phone_call c JOIN ea_conversation v ON v.id=c.conversation_id
                    WHERE c.assistant_id=%s AND c.parent_task_id=%s ORDER BY c.created_at LIMIT 10''',
                    (self.profile.id,task_id)).fetchall(),
                'recent_work':db.execute('''SELECT id,title,state,left(result,2000) AS result,created_at
                    FROM ea_task WHERE assistant_id=%s AND person_id=%s AND id<>%s
                    AND (%s OR scope='friend') ORDER BY created_at DESC LIMIT 6''',
                    (self.profile.id,task['person_id'],task_id,task['scope']=='owner')).fetchall()}

    def ready_tasks(self,limit=10,*,exclude_sms=False):
        with self.connection() as db:
            return db.execute('''SELECT t.* FROM ea_task t JOIN ea_person p ON p.id=t.person_id
                WHERE t.assistant_id=%s AND t.state='queued' AND t.available_at<=now() AND p.status='approved'
                AND NOT t.context @> '{"native_schedule_setup":true}'::jsonb
                AND (NOT %s OR t.authority->>'channel'<>'sms' OR t.context @> '{"backend_only":true}'::jsonb)
                AND (t.scope='owner' OR (SELECT count(*) FROM ea_task running WHERE running.assistant_id=t.assistant_id
                    AND running.person_id=t.person_id AND running.state IN ('working','waiting')
                    AND NOT running.context @> '{"text_frontend":true}'::jsonb
                    AND NOT running.context @> '{"native_schedule_setup":true}'::jsonb)<%s)
                ORDER BY t.created_at LIMIT %s''',(self.profile.id,exclude_sms,self.friend_active,min(max(limit,1),50))).fetchall()

    def ready_text_tasks(self,limit=8):
        with self.connection() as db:
            return db.execute('''SELECT * FROM (
                SELECT DISTINCT ON(t.person_id) t.* FROM ea_task t JOIN ea_person p ON p.id=t.person_id
                WHERE t.assistant_id=%s AND t.state='queued' AND t.available_at<=now() AND p.status='approved'
                AND t.authority->>'channel'='sms' AND NOT t.context @> '{"backend_only":true}'::jsonb
                AND NOT t.context @> '{"native_schedule_setup":true}'::jsonb
                ORDER BY t.person_id,t.created_at) requests ORDER BY created_at LIMIT %s''',
                (self.profile.id,min(max(limit,1),8))).fetchall()

    def active_runs(self,limit=50):
        with self.connection() as db:
            return db.execute('''SELECT t.*,r.session_id,r.dispatch_state FROM ea_task t
                JOIN ea_agent_run r ON r.task_id=t.id WHERE t.assistant_id=%s
                AND t.state IN ('working','waiting')
                AND NOT t.context @> '{"native_schedule_setup":true}'::jsonb
                AND NOT t.context @> '{"text_frontend":true}'::jsonb
                ORDER BY r.updated_at LIMIT %s''',
                (self.profile.id,min(max(limit,1),100))).fetchall()

    def unforwarded_conversation_work(self):
        with self.connection() as db:
            return db.execute('''SELECT t.id,t.title,t.state,left(t.result,10000) AS result,
                r.turn_id,c.parent_task_id FROM ea_task t
                JOIN ea_agent_run r ON r.task_id=t.id
                JOIN ea_phone_call c ON c.conversation_id=t.conversation_id
                JOIN ea_conversation v ON v.id=c.conversation_id
                JOIN ea_task p ON p.id=c.parent_task_id
                WHERE t.assistant_id=%s AND t.source_id='conversation:'||c.conversation_id::text
                    AND t.id<>p.id AND c.state='finished' AND v.finalized
                    AND t.state IN ('completed','partial','failed') AND r.turn_id IS NOT NULL
                    AND p.state IN ('working','waiting') AND NOT EXISTS
                    (SELECT 1 FROM ea_agent_input i WHERE i.id=
                        'conversation-result:'||t.id||':'||r.turn_id)
                ORDER BY t.updated_at LIMIT 20''',(self.profile.id,)).fetchall()

    def stage_input(self,task_id,submission_id,text,*,role='user'):
        if role not in {'user','developer'} or not 1<=len(text)<=200000:
            raise ValueError('Invalid agent input')
        with self.connection() as db:
            task=self._task(db,task_id,lock=True)
            if not task or task['state']=='cancelled' or task['person_status']!='approved':
                raise PermissionDenied('Assignment is unavailable')
            prior=db.execute('SELECT task_id,body FROM ea_agent_input WHERE id=%s',(submission_id,)).fetchone()
            body=json.dumps({'role':role,'text':text},sort_keys=True)
            if prior:
                if prior['task_id']!=task_id or prior['body']!=body:
                    raise Conflict('Input identity is already used')
                return
            db.execute('INSERT INTO ea_agent_input(id,task_id,body) VALUES(%s,%s,%s)',(submission_id,task_id,body))
            run=db.execute('SELECT session_id,dispatch_state FROM ea_agent_run WHERE task_id=%s',(task_id,)).fetchone()
            state='queued' if run and run['session_id'] is None and run['dispatch_state']=='ready' else 'working'
            db.execute('UPDATE ea_task SET state=%s,completed_at=NULL,updated_at=now() WHERE id=%s',(state,task_id))
            self._event(db,'input:'+submission_id,'task.input_saved',{'role':role},
                        person_id=task['person_id'],task_id=task_id)

    def pending_inputs(self,task_id):
        with self.connection() as db:
            if not self._permitted(db,task_id,writes=False):return []
            return db.execute('SELECT * FROM ea_agent_input WHERE task_id=%s AND sent_at IS NULL ORDER BY created_at',
                              (task_id,)).fetchall()

    def input_sent(self,task_id,submission_id):
        with self.connection() as db:
            if not self._task(db,task_id):raise ValueError('Unknown assignment')
            db.execute('UPDATE ea_agent_input SET sent_at=coalesce(sent_at,now()) WHERE task_id=%s AND id=%s',
                       (task_id,submission_id))

    def task_result(self,task_id):
        with self.connection() as db:
            task=self._task(db,task_id)
            if not task:return None
            run=db.execute('SELECT turn_id FROM ea_agent_run WHERE task_id=%s',(task_id,)).fetchone()
            task['turn_id']=run['turn_id'] if run else None
            return task

    def reportable_tasks(self,limit=50):
        with self.connection() as db:
            return db.execute('''SELECT t.*,r.turn_id FROM ea_task t JOIN ea_agent_run r ON r.task_id=t.id
                JOIN ea_person p ON p.id=t.person_id AND p.assistant_id=t.assistant_id
                WHERE t.assistant_id=%s AND p.status='approved'
                AND t.state IN ('completed','partial','failed','waiting') AND t.result<>'' AND r.turn_id IS NOT NULL
                AND coalesce((t.context->>'notify_requested')::boolean,true)
                AND NOT EXISTS(SELECT 1 FROM ea_delivery d WHERE d.assistant_id=t.assistant_id
                    AND d.source_id='result:' || t.id || ':' || r.turn_id)
                ORDER BY t.updated_at LIMIT %s''',(self.profile.id,min(max(limit,1),100))).fetchall()
