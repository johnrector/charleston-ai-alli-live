"""One-time useful-record seed. No old runtime or failed work is replayed."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from .deliveries import DeliveryRepository


def seed_existing_records(repository):
    with repository.connection() as db:
        if db.execute('SELECT 1 FROM ea_ledger WHERE assistant_id=%s AND source_id=%s',
                      (repository.profile.id,'bootstrap:contacts-v1')).fetchone():return
        exists=lambda table:bool(db.execute('SELECT to_regclass(%s) AS name',(table,)).fetchone()['name'])
        contacts=db.execute('SELECT phone,name,email,source,deleted_at FROM alli_contact_name').fetchall() if exists('alli_contact_name') else []
        blocks={row['phone'] for row in db.execute('SELECT phone FROM alli_security_block').fetchall()} if exists('alli_security_block') else set()
        # Only explicitly approved sources qualify; a self-introduction does not.
        approved={'owner_saved_contact','apple_favorite'}
        for contact in contacts:
            try:phone=repository.normalize_phone(contact['phone'])
            except ValueError:continue
            if db.execute('SELECT 1 FROM ea_endpoint WHERE assistant_id=%s AND address=%s',
                          (repository.profile.id,phone)).fetchone():continue
            trusted=contact['source'] in approved
            if contact['source']=='owner_supplied_mission' and exists('alli_appointment_dispatch'):
                names=db.execute("SELECT count(DISTINCT lower(trim(context->>'recipient_name'))) AS total FROM alli_appointment_dispatch WHERE context->>'phone'=%s AND status<>'blocked'",(phone,)).fetchone()['total']
                trusted=names==1
            status='deleted' if contact['deleted_at'] else 'suspended' if phone in blocks else 'approved' if trusted else 'known'
            identifier=uuid4()
            db.execute('''INSERT INTO ea_person(id,assistant_id,name,email,status,source)
                VALUES(%s,%s,%s,%s,%s,%s)''',
                (identifier,repository.profile.id,contact['name'] or '',contact['email'] or '',status,'seed:'+(contact['source'] or 'legacy')))
            db.execute("INSERT INTO ea_endpoint(assistant_id,person_id,kind,address) VALUES(%s,%s,'phone',%s)",
                       (repository.profile.id,identifier,phone))
    seeds=json.loads(os.environ.get('ALLI_SEED_CONTACTS_JSON','[]'))
    if seeds:
        owner=repository.owner_actor('owner-supplied-seed')
        for seed in seeds:
            contact=repository.recognize(seed['phone'])
            if contact and contact['status'] in {'suspended','deleted'}:continue
            if not contact or not contact['name']:
                contact=repository.save_person(owner,name=seed['name'],phone=seed['phone'],
                    email=contact['email'] if contact else '',person_id=str(contact['id']) if contact else None,
                    revision=contact['revision'] if contact else None)
            if contact['status']=='known':repository.set_person_status(owner,str(contact['id']),'approved',contact['revision'])
    preserve_birthday(repository)
    with repository.connection() as db:
        repository._event(db,'bootstrap:contacts-v1','bootstrap.contacts',{'count':len(contacts)})


def preserve_birthday(repository):
    """Retain the one explicitly authorized real future promise, exactly once."""
    with repository.connection() as db:
        if not db.execute("SELECT to_regclass('alli_promise') AS name").fetchone()['name']:return
        promise=db.execute('SELECT * FROM alli_promise WHERE id=%s',('260D9C67CF8B',)).fetchone()
        if not promise or promise['kind']!='text' or promise['state'] not in {'ready','queued'}:return
        if not promise['due_at'] or promise['due_at']<=datetime.now(timezone.utc):return
        if promise['outbound_id']:
            prior=db.execute('SELECT status,message_sid FROM alli_outbound_sms WHERE request_id=%s',
                             (promise['outbound_id'],)).fetchone()
            if prior and (prior['message_sid'] or prior['status']!='scheduled'):
                raise RuntimeError('Existing birthday delivery needs reconciliation before cutover')
    owner=repository.owner_actor('owner-authorized-birthday:260D9C67CF8B')
    contact=repository.recognize(promise['phone'])
    if contact and contact['status']=='known' and promise['contact_name']:
        contact=repository.save_person(owner,name=promise['contact_name'],phone=promise['phone'],email=contact['email'],person_id=str(contact['id']),revision=contact['revision'])
        contact=repository.set_person_status(owner,str(contact['id']),'approved',contact['revision'])
    if not contact or contact['status']!='approved':
        raise RuntimeError('Preserve birthday recipient approval before cutover')
    task=repository.submit(owner,source_id='preserve:260D9C67CF8B',title=promise['title'],
        instruction='Preserve the existing owner-authorized birthday text; do not send another copy.',
        context={'bootstrap_saved_intent':True})
    DeliveryRepository(repository).queue_text(task['id'],str(contact['id']),promise['response'],
        'preserve:260D9C67CF8B',at=promise['due_at'])
    repository.record_outcome(task['id'],'completed','Birthday text remains scheduled for November 1 at 9 AM Eastern.',
                              turn_id='preserved-birthday',notify=False)
    with repository.connection() as db:
        if promise['outbound_id']:
            db.execute("UPDATE alli_outbound_sms SET status='cancelled',updated_at=now() WHERE request_id=%s AND status='scheduled' AND message_sid IS NULL",(promise['outbound_id'],))
        db.execute("UPDATE alli_promise SET state='cancelled',completion_note='Preserved once in the clean executive ledger; do not replay',updated_at=now() WHERE id=%s",(promise['id'],))
        repository._event(db,'bootstrap:birthday','bootstrap.commitment_preserved',{'legacy_id':promise['id']},task_id=task['id'])
