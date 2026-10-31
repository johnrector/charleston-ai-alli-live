"""Owner-only read models. No scheduling, account calls or paid health probes."""
class Oversight:
    def __init__(self, repository):
        self.repository = repository

    def policies(self, actor):
        with self.repository.connection() as db:
            self.repository._person(db, actor, owner=True)
            records = db.execute('''SELECT r.id,r.body,r.revision,r.updated_at,c.name AS collection
                FROM ea_record r JOIN ea_collection c ON c.id=r.collection_id AND c.assistant_id=r.assistant_id
                WHERE r.assistant_id=%s AND r.deleted_at IS NULL
                AND (r.body ? 'rule_key' OR lower(c.name) IN
                    ('owner operational business rules','operating policies'))
                ORDER BY r.updated_at DESC,r.id LIMIT 100''', (self.repository.profile.id,)).fetchall()
        return {'records':records, 'appointment_confirmation':{
            'state':'not_configured', 'automated':False,
            'reason':'No calendar-to-call planner is enabled. Saved guidance alone does not activate calls.'}}

    def summary(self, actor):
        with self.repository.connection() as db:
            self.repository._person(db, actor, owner=True)
            assistant = self.repository.profile.id
            work = db.execute('''SELECT id,title,state,left(result,700) AS result,updated_at FROM ea_task
                WHERE assistant_id=%s AND state IN ('queued','working','waiting','needs_owner','partial','failed')
                AND NOT context @> '{"native_schedule_setup":true}'::jsonb
                ORDER BY updated_at DESC LIMIT 8''', (assistant,)).fetchall()
            calls = db.execute('''SELECT c.id,c.parent_task_id,c.state,c.carrier_status,c.direction,
                c.duration_seconds,c.native_finalized,v.finalized AS transcript_finalized,c.created_at,p.name FROM ea_phone_call c
                JOIN ea_person p ON p.id=c.person_id AND p.assistant_id=c.assistant_id
                JOIN ea_conversation v ON v.id=c.conversation_id AND v.assistant_id=c.assistant_id
                WHERE c.assistant_id=%s ORDER BY CASE WHEN c.direction='outbound' AND
                    c.state IN ('queued','attempting','accepted','uncertain') THEN 0 ELSE 1 END,
                    c.created_at DESC LIMIT 20''', (assistant,)).fetchall()
            diagnostic = db.execute('''SELECT request_id,release,started_at,finished_at,result FROM ea_diagnostic_run
                WHERE assistant_id=%s ORDER BY check_date DESC LIMIT 1''', (assistant,)).fetchone()
        return {'work':work, 'calls':calls, 'diagnostic':diagnostic}
