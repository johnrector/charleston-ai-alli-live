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
        return {'records':records,'appointment_confirmation':self.appointment_policy(actor)}

    def summary(self, actor):
        with self.repository.connection() as db:
            self.repository._person(db, actor, owner=True)
            assistant = self.repository.profile.id
            work = db.execute('''SELECT id,title,state,left(result,700) AS result,updated_at FROM ea_task
                WHERE assistant_id=%s AND state IN ('needs_owner','partial','failed')
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
        return {'work':work, 'calls':calls, 'diagnostic':diagnostic,
                'appointment_policy':self.appointment_policy(actor),'coverage':self.coverage(actor)}

    def appointment_policy(self,actor):
        with self.repository.connection() as db:
            self.repository._person(db,actor,owner=True)
            row=db.execute("""SELECT r.body FROM ea_record r JOIN ea_collection c ON c.id=r.collection_id
                AND c.assistant_id=r.assistant_id WHERE r.assistant_id=%s AND r.deleted_at IS NULL
                AND c.name='Operating policies' AND r.body->>'rule_key'='appointment_confirmation_v1'
                ORDER BY r.updated_at DESC LIMIT 1""",(self.repository.profile.id,)).fetchone()
            schedule=db.execute("SELECT id,state,provider_id FROM ea_schedule WHERE assistant_id=%s AND source_id='policy:appointment-planner:v1'",(self.repository.profile.id,)).fetchone()
        enabled=bool(row and row['body'].get('enabled'))
        active=bool(enabled and schedule and schedule['state']=='active' and schedule['provider_id'])
        return {'state':'enabled' if active else 'schedule_missing' if enabled else 'disabled' if row else 'not_configured',
            'automated':active,'planner':schedule,
            'reason':'One day before at 11 a.m.; preparation, exclusions and pre-dial checks apply.' if active else
                'No active native calendar-to-call planner. Calendar coverage is not confirmed.'}

    def coverage(self,actor):
        with self.repository.connection() as db:
            self.repository._person(db,actor,owner=True)
            snapshot=db.execute('SELECT * FROM ea_calendar_coverage WHERE assistant_id=%s',(self.repository.profile.id,)).fetchone()
            if not snapshot:return None
            intents=db.execute("""SELECT i.id,i.state,i.reason,i.due_at,i.schedule_id,p.state AS preparation_state,
                t.state AS preparation_task_state,s.state AS schedule_state,s.provider_id,c.state AS call_state,
                c.carrier_status,c.duration_seconds FROM ea_appointment_intent i LEFT JOIN ea_person_preparation p
                ON p.id=i.preparation_id AND p.assistant_id=i.assistant_id LEFT JOIN ea_task t ON t.id=p.task_id
                LEFT JOIN ea_schedule s ON s.id=i.schedule_id AND s.assistant_id=i.assistant_id
                LEFT JOIN ea_phone_call c ON c.id=i.call_id AND c.assistant_id=i.assistant_id
                WHERE i.assistant_id=%s""",(self.repository.profile.id,)).fetchall()
        by_id={r['id']:r for r in intents}
        events=[]
        for event in snapshot['events']:
            intent=by_id.get(event.get('appointment_id'))
            if intent:
                state=intent['state'];reason=intent['reason']
                if state=='scheduled' and (intent['schedule_state']!='active' or not intent['provider_id']):
                    state='execution pending' if intent['schedule_state']=='completed' else 'schedule missing'
                if state=='planned':state='missing'
                if intent['preparation_state']=='blocked' or intent['preparation_task_state'] in {'failed','partial','cancelled'}:
                    state='blocked';reason=reason or 'People preparation did not complete'
                events.append({**event,**intent,'state':state,'reason':reason})
            else:events.append(event)
        return {**snapshot,'events':events}
