from datetime import datetime,timezone

from adapters.bootstrap import seed_existing_records,preserve_birthday
from adapters.deliveries import DeliveryRepository
from adapters.postgres import PostgresRepository
from alli_core.profile import Profile


def test_seed_keeps_trust_blocks_and_exact_real_birthday_without_replaying_work(database,monkeypatch):
    monkeypatch.setenv('ALLI_SEED_CONTACTS_JSON','[{"name":"Test saved contact","phone":"+18435550104"}]')
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'));repo.initialize();repo.bootstrap_owner('+18435550100')
    with repo.connection() as db:
        db.execute('CREATE TABLE alli_contact_name(phone text,name text,email text,source text,deleted_at timestamptz)')
        db.execute('CREATE TABLE alli_security_block(phone text)')
        db.execute('CREATE TABLE alli_promise(id text,phone text,contact_name text,title text,response text,kind text,state text,due_at timestamptz,outbound_id text,completion_note text,updated_at timestamptz)')
        db.execute("INSERT INTO alli_contact_name VALUES('+18435550101','Elizabeth Test','e@example.com','owner_saved_contact',NULL),('+18435550102','Blocked','','apple_favorite',NULL),('+18435550103','Stranger','','self_introduced',NULL)")
        db.execute("INSERT INTO alli_security_block VALUES('+18435550102')")
        db.execute("INSERT INTO alli_promise VALUES('260D9C67CF8B','+18435550101','Elizabeth Test','Happy birthday','Happy birthday!','text','ready','2026-11-01 14:00:00+00',NULL,'',now())")
    seed_existing_records(repo);seed_existing_records(repo);preserve_birthday(repo)
    owner=repo.owner_actor('test')
    assert repo.recognize('+18435550101')['status']=='approved'
    assert repo.recognize('+18435550102')['status']=='suspended'
    assert repo.recognize('+18435550103')['status']=='known'
    assert repo.recognize('+18435550104')['name']=='Test saved contact'
    deliveries=DeliveryRepository(repo).read(owner)
    assert len(deliveries)==1 and deliveries[0]['due_at']==datetime(2026,11,1,14,tzinfo=timezone.utc)
    assert deliveries[0]['body']=='Happy birthday!' and deliveries[0]['state']=='queued'
    assert repo.ready_tasks()==[]
