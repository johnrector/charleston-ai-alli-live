import os,secrets
from contextlib import contextmanager
import communication_store as store
import security_controls as security,owner_access,work_store as jobs,scheduled_sms as sms
store.initialize()
original=store.connection
owner_before=os.environ['ALLI_OWNER_PHONE']
friend='+18435550110';fake_owner='+18435550111';tag='qa-'+secrets.token_hex(8)
with original() as db:
    with db.transaction(force_rollback=True):
        assert not db.execute('SELECT 1 FROM alli_contact_name WHERE phone=ANY(%s)',([friend,fake_owner],)).fetchone()
        @contextmanager
        def nested():
            with db.transaction():yield db
        store.connection=nested;os.environ['ALLI_OWNER_PHONE']=fake_owner
        try:
            store.save_contact(friend,'Temporary security fixture','fixture@example.com')
            assert store.context_for(friend)['contact']['source']=='owner_saved_contact'
            owner_access.grant_sms(db)
            assert owner_access.authorized(fake_owner,'sms')
            sid='CA'+secrets.token_hex(16)
            db.execute("INSERT INTO alli_owner_access(channel,binding,expires_at) VALUES('voice',%s,now()+interval '5 minutes')",(sid,))
            assert owner_access.authorized(fake_owner,'voice',sid)
            assert not owner_access.authorized(fake_owner,'voice','CA'+'0'*32)
            assert not owner_access.authorized(friend,'voice',sid)
            work=jobs.create(friend,'Temporary security fixture','Temporary rollback fixture','Write a brief checklist',tag,notify=False)
            text=sms.enqueue(sms.TextMessage(request_id=tag,phone=friend,recipient_name='Temporary security fixture',message='Rollback-only fixture',send_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc)+__import__('datetime').timedelta(minutes=5)))
            import promises
            promise=promises.create('Future rollback-only birthday',phone=friend,contact_name='Temporary security fixture',kind='text',due_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc)+__import__('datetime').timedelta(days=8),response='Happy birthday!',dedupe=tag+'promise')
            security.set_block(friend,True,'Rollback-only QA')
            assert promises.read_row(db,promise['promise']['id'])['state']=='cancelled'
            assert jobs.read(work['work_id'])['state']=='cancelled'
            assert sms.get(tag)['status']=='cancelled'
            assert not jobs.allowed(db,friend)
            security.set_block(friend,False)
            assert jobs.allowed(db,friend)
            assert promises.read_row(db,promise['promise']['id'])['state']=='cancelled'
            assert jobs.read(work['work_id'])['state']=='cancelled'
            security.delete_contact(friend)
            assert store.context_for(friend)['contact'] is None
            assert db.execute('SELECT deleted_at IS NOT NULL FROM alli_contact_name WHERE phone=%s',(friend,)).fetchone()[0]
            security.set_block(friend,False)
            assert store.context_for(friend)['contact']['name']=='Temporary security fixture'
            for n in range(4):security.calendar_write(friend,tag+str(n))
            try:security.calendar_write(friend,tag+'daily-stop');raise AssertionError('Daily limit did not stop')
            except security.Limited:pass
            db.execute("UPDATE alli_security_usage SET created_at=now()-interval '2 days' WHERE phone=%s AND kind='calendar_write'",(friend,))
            for n in range(4,8):security.calendar_write(friend,tag+str(n))
            db.execute("UPDATE alli_security_usage SET created_at=now()-interval '1 day' WHERE phone=%s AND kind='calendar_write' AND reference IN (%s,%s)",(friend,friend+':'+tag+'4',friend+':'+tag+'5'))
            try:security.calendar_write(friend,tag+'weekly-stop');raise AssertionError('Weekly limit did not stop')
            except security.Limited:pass
            print('PASS: owner possession/call binding; job/message cancellation; suspend/reactivate; recoverable delete/restore; daily and weekly booking ceilings')
        finally:
            store.connection=original;os.environ['ALLI_OWNER_PHONE']=owner_before
    assert not db.execute('SELECT 1 FROM alli_contact_name WHERE phone=ANY(%s)',([friend,fake_owner],)).fetchone()
    assert not db.execute('SELECT 1 FROM alli_outbound_sms WHERE request_id=%s',(tag,)).fetchone()
    assert not db.execute('SELECT 1 FROM alli_work WHERE dedupe=%s',(tag,)).fetchone()
print('PASS: outer transaction rolled back; no fixture, queued text or work persisted')
