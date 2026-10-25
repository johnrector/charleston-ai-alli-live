from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone

import pytest

from adapters.deliveries import DeliveryRepository
from adapters.postgres import PostgresRepository
from alli_core.authority import Actor,Conflict,PermissionDenied
from alli_core.profile import Profile
from adapters.errors import InvalidInput


@pytest.fixture
def delivery_setup(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'))
    repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'dot',True,'owner-test-proof')
    recipient=repo.save_person(owner,name='Test recipient',phone='+18435550101')
    task=repo.submit(owner,source_id='owner-text',title='Send a text',instruction='Send the requested message')
    return repo,owner,str(recipient['id']),task,DeliveryRepository(repo)


def test_oversized_unicode_is_rejected_before_outbox_or_schedule(delivery_setup):
    repo,owner,recipient,task,outbox=delivery_setup
    with pytest.raises(InvalidInput):
        outbox.queue_text(task['id'],recipient,'“'*1100,'oversized',
                          at=datetime.now(timezone.utc)+timedelta(hours=1))
    assert outbox.read(owner)==[]
    with repo.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM ea_commitment').fetchone()['n']==0


def test_scheduled_text_survives_new_repository_without_early_delivery(delivery_setup):
    repo,owner,recipient,task,outbox=delivery_setup
    due=datetime.now(timezone.utc)+timedelta(hours=1)
    delivery=outbox.queue_text(task['id'],recipient,'Happy birthday!','birthday-test',at=due)
    reopened=DeliveryRepository(repo)
    assert reopened.claim_due() is None
    assert reopened.read(owner)[0]['due_at']==due
    assert repo.recognize('+18435550101')['status']=='known'  # sending is not approval
    duplicate=reopened.queue_text(task['id'],recipient,'Happy birthday!','birthday-test',at=due)
    assert duplicate['id']==delivery['id']
    with pytest.raises(Conflict):
        reopened.queue_text(task['id'],recipient,'Happy birthday!','birthday-test',at=due+timedelta(hours=1))
    cancelled=reopened.cancel(owner,delivery['commitment_id'],1)
    assert cancelled['state']=='cancelled'
    assert reopened.read(owner)[0]['state']=='cancelled'


def test_only_one_sender_can_claim_and_crash_never_resends(delivery_setup):
    repo,owner,recipient,task,outbox=delivery_setup
    delivery=outbox.queue_text(task['id'],recipient,'One message','unique-message')
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims=list(pool.map(lambda _:DeliveryRepository(repo).claim_due(),range(6)))
    claimed=[row for row in claims if row]
    assert len(claimed)==1 and claimed[0]['id']==delivery['id']
    assert DeliveryRepository(repo).claim_due() is None
    assert outbox.read(owner)[0]['state']=='attempting'


def test_provider_callback_before_send_reply_and_out_of_order_status(delivery_setup):
    repo,owner,recipient,task,outbox=delivery_setup
    delivery=outbox.queue_text(task['id'],recipient,'Test','callback-race',at=datetime.now(timezone.utc))
    outbox.claim_due()
    outbox.receipt(delivery['id'],'SM-test','delivered',{'status':'delivered'})
    late=outbox.receipt(delivery['id'],'SM-test','accepted',{'status':'accepted'})
    assert late['state']=='delivered'
    assert late['receipt']['status']=='delivered'
    with repo.connection() as db:
        promise=db.execute('SELECT state FROM ea_commitment WHERE id=%s',(delivery['commitment_id'],)).fetchone()
    assert promise['state']=='completed'
    with pytest.raises(Conflict):outbox.receipt(delivery['id'],'SM-other','delivered',{})
    with pytest.raises(Conflict):outbox.cancel(owner,delivery['commitment_id'],1)


def test_suspended_recipient_is_not_contacted_after_restart(delivery_setup):
    repo,owner,recipient,task,outbox=delivery_setup
    outbox.queue_text(task['id'],recipient,'Do not send after suspension','suspend-test')
    person=repo.recognize('+18435550101')
    repo.set_person_status(owner,recipient,'suspended',person['revision'])
    assert DeliveryRepository(repo).claim_due() is None
    assert outbox.read(owner)[0]['state']=='cancelled'


def test_friend_reports_only_go_to_requester_and_do_not_grant_broadcast(delivery_setup):
    repo,owner,recipient,_,outbox=delivery_setup
    person=repo.recognize('+18435550101')
    repo.set_person_status(owner,recipient,'approved',person['revision'])
    friend=Actor('alli',recipient,'sms')
    task=repo.submit(friend,source_id='friend-work',title='Public research',instruction='Research a public topic')
    with pytest.raises(PermissionDenied):
        outbox.queue_text(task['id'],owner.person_id,'Unrequested message','friend-broadcast')
    repo.record_outcome(task['id'],'completed','Finished the research',turn_id='turn-test')
    result=outbox.queue_result(task['id'],'Finished the research','turn-test')
    assert result['destination']=='+18435550101'
    assert outbox.queue_result(task['id'],'Finished the research','turn-test')['id']==result['id']
    assert outbox.claim_due()['id']==result['id']
    with pytest.raises(PermissionDenied):outbox.read(friend)
