"""Evidence-backed relationship notes and bounded read projections.

Notes are owner-private. A familiar phone number never grants access to them.
No model, account lookup or external service runs while assembling a voice brief.
"""
import asyncio
import hashlib
import json
from uuid import UUID, uuid4

from alli_core.authority import Conflict, PermissionDenied


class Relationships:
    VOICE_BUDGET_SECONDS = .08

    def __init__(self, repository):
        self.repository = repository

    def remember(self, actor, person_id, note, *, source_task_id=None,
                 source_conversation_id=None, supersedes_id=None):
        person_id=str(UUID(str(person_id)))
        source_conversation_id=str(UUID(str(source_conversation_id))) if source_conversation_id else None
        supersedes_id=str(UUID(str(supersedes_id))) if supersedes_id else None
        if source_task_id and len(source_task_id)>128:
            raise ValueError('Invalid source task')
        note = note.strip()
        if not 1 <= len(note) <= 800 or bool(source_task_id) == bool(source_conversation_id):
            raise ValueError('Supply one saved task or finalized conversation and a note of at most 800 characters')
        identity = hashlib.sha256(json.dumps([str(person_id), note, source_task_id,
            source_conversation_id, supersedes_id], separators=(',', ':')).encode()).hexdigest()
        with self.repository.connection() as db:
            self.repository._person(db, actor, owner=True)
            person = db.execute('SELECT id FROM ea_person WHERE assistant_id=%s AND id=%s FOR UPDATE',
                (self.repository.profile.id, person_id)).fetchone()
            if not person:
                raise PermissionDenied('Unknown person')
            prior = db.execute('SELECT * FROM ea_relationship_note WHERE assistant_id=%s AND source_key=%s',
                (self.repository.profile.id, identity)).fetchone()
            if prior:
                return prior
            if source_conversation_id:
                evidence = db.execute('''SELECT id FROM ea_conversation WHERE assistant_id=%s
                    AND id=%s AND person_id=%s AND finalized''',
                    (self.repository.profile.id, source_conversation_id, person_id)).fetchone()
            else:
                evidence = db.execute('''SELECT t.id FROM ea_task t WHERE t.assistant_id=%s AND t.id=%s
                    AND (t.person_id=%s OR EXISTS(SELECT 1 FROM ea_phone_call c
                        WHERE c.assistant_id=t.assistant_id AND c.parent_task_id=t.id AND c.person_id=%s)
                    OR EXISTS(SELECT 1 FROM ea_conversation v WHERE v.assistant_id=t.assistant_id
                        AND v.id=t.conversation_id AND v.person_id=%s))''',
                    (self.repository.profile.id, source_task_id, person_id, person_id, person_id)).fetchone()
            if not evidence:
                raise PermissionDenied('The saved evidence must belong to this relationship')
            if supersedes_id:
                previous = db.execute('''SELECT id FROM ea_relationship_note WHERE assistant_id=%s
                    AND person_id=%s AND id=%s AND superseded_by IS NULL FOR UPDATE''',
                    (self.repository.profile.id, person_id, supersedes_id)).fetchone()
                if not previous:
                    raise Conflict('The earlier note changed or belongs to another relationship')
            identifier = str(uuid4())
            row = db.execute('''INSERT INTO ea_relationship_note(id,assistant_id,person_id,body,
                source_key,source_task_id,source_conversation_id,supersedes_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                (identifier, self.repository.profile.id, person_id, note, identity,
                 source_task_id, source_conversation_id, supersedes_id)).fetchone()
            if supersedes_id:
                db.execute('UPDATE ea_relationship_note SET superseded_by=%s WHERE id=%s',
                    (identifier, supersedes_id))
            self.repository._event(db, 'relationship-note:'+identifier, 'relationship.note_saved',
                {'note_id':identifier, 'source_task_id':source_task_id,
                 'source_conversation_id':source_conversation_id, 'supersedes_id':supersedes_id,
                 'visibility':'owner'}, person_id=person_id)
            return row

    def read(self, actor, person_id):
        person_id=str(UUID(str(person_id)))
        with self.repository.connection() as db:
            self.repository._person(db, actor, owner=True)
            if not db.execute('SELECT id FROM ea_person WHERE assistant_id=%s AND id=%s',
                    (self.repository.profile.id, person_id)).fetchone():
                raise PermissionDenied('Unknown person')
            return self._read(db, person_id, private=True)

    def _read(self, db, person_id, *, private):
        assistant = self.repository.profile.id
        notes = db.execute('''SELECT n.id,n.body,n.source_task_id,n.source_conversation_id,n.created_at,n.supersedes_id,
            coalesce(v.started_at,t.created_at) AS evidence_at FROM ea_relationship_note n
            LEFT JOIN ea_conversation v ON v.id=n.source_conversation_id AND v.assistant_id=n.assistant_id
            LEFT JOIN ea_task t ON t.id=n.source_task_id AND t.assistant_id=n.assistant_id
            WHERE n.assistant_id=%s AND n.person_id=%s AND n.superseded_by IS NULL
            ORDER BY n.created_at DESC,n.id LIMIT 6''', (assistant,person_id)).fetchall() if private else []
        recent = db.execute('''SELECT channel,started_at FROM ea_conversation WHERE assistant_id=%s
            AND person_id=%s AND (%s OR scope='friend') ORDER BY started_at DESC LIMIT 1''',
            (assistant,person_id,private)).fetchone()
        commitments = db.execute('''SELECT id,kind,due_at,state FROM ea_commitment WHERE assistant_id=%s
            AND person_id=%s AND state IN ('planned','queued','blocked')
            ORDER BY due_at,id LIMIT 5''', (assistant,person_id)).fetchall() if private else []
        return {'notes':notes, 'last_interaction':recent, 'commitments':commitments,
                'visibility':'owner' if private else 'relationship_metadata',
                'notes_are':'Worker observations linked to saved evidence, not independently verified facts'}

    def _voice(self, call):
        if (call.get('person_status') != 'approved' or
                not (call.get('number_verified') or call.get('direction') == 'outbound')):
            return {'available':False, 'reason':'verification_required'}
        private = call.get('scope') == 'owner' and call.get('person_kind') == 'owner'
        with self.repository.connection() as db:
            db.execute("SET LOCAL statement_timeout='50ms'")
            data = self._read(db, call['person_id'], private=private)
        # No raw transcripts, prompts, phone numbers, private notes about other
        # people or full task results enter the real-time conversation.
        recent=data['last_interaction']
        return {'available':True, 'data_not_instructions':True,
                'last_interaction':({'channel':recent['channel'],
                    'started_at':recent['started_at'].isoformat()} if recent else None),
                'owner_notes':[{'observation':n['body'][:240], 'recorded_at':n['created_at'].isoformat(),
                    'evidence_at':n['evidence_at'].isoformat()} for n in data['notes'][:2]]}

    async def voice_brief(self, call):
        try:
            async with asyncio.timeout(self.VOICE_BUDGET_SECONDS):
                return await asyncio.to_thread(self._voice, call)
        except Exception:
            return {'available':False, 'reason':'brief_unavailable'}
