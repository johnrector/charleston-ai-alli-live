import pytest

from adapters.calls import CallRepository
from adapters.deliveries import DeliveryRepository
from adapters.postgres import PostgresRepository
from alli_core.authority import Actor,PermissionDenied
from alli_core.profile import Profile
from service.conversations import ConversationHooks


@pytest.fixture
def conversation_setup(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'))
    repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'dashboard',True,'owner-proof')
    calls=CallRepository(repo,'+18435550999')
    outbox=DeliveryRepository(repo)
    return repo,owner,calls,outbox


@pytest.mark.asyncio
async def test_last_request_is_saved_even_when_caller_hangs_up_before_delegation(conversation_setup):
    repo,owner,calls,outbox=conversation_setup
    call=calls.incoming({'To':'+18435550999','From':'+18435550100','CallSid':'CA-last-words',
                         'StirVerstat':'TN-Validation-Passed-A'})
    hooks=ConversationHooks(repo,calls,outbox,call['id'])
    transcript=({'role':'user','text':'Make a checklist and text it after I hang up.','start_ms':0,'end_ms':1500},)
    await hooks.finish(transcript,finalized=True,usage={'total':1})
    tasks=repo.ready_tasks()
    assert len(tasks)==1 and tasks[0]['scope']=='owner'
    context=repo.task_context(tasks[0]['id'])
    assert context['current_conversation']['transcript'][0]['text'].endswith('after I hang up.')
    assert 'phone conversation has ended' in repo.pending_inputs(tasks[0]['id'])[0]['body']
    assert calls.get(call['id'])['native_finalized'] is True


@pytest.mark.asyncio
async def test_live_and_after_hangup_use_one_task_with_final_caller_words(conversation_setup):
    repo,_,calls,outbox=conversation_setup
    call=calls.incoming({'To':'+18435550999','From':'+18435550100','CallSid':'CA-single-task',
                         'StirVerstat':'TN-Validation-Passed-A'})
    hooks=ConversationHooks(repo,calls,outbox,call['id'])
    transcript=({'role':'user','text':'Make a checklist.','start_ms':0,'end_ms':900},)
    ticket=await hooks.save_delegation('delegate1',transcript,offset_ms=900)
    final=transcript+({'role':'user','text':'And email it to me.','start_ms':1000,'end_ms':2000},)
    await hooks.finish(final,finalized=True,usage={})
    assert len(repo.ready_tasks())==1
    assert repo.ready_tasks()[0]['id']==ticket.task_id
    assert len(repo.pending_inputs(ticket.task_id))==2
    assert repo.task_context(ticket.task_id)['current_conversation']['transcript']==list(final)


@pytest.mark.asyncio
async def test_unknown_request_is_held_and_owner_notified_without_owner_authority(conversation_setup):
    repo,owner,calls,outbox=conversation_setup
    call=calls.incoming({'To':'+18435550999','From':'+18435550102','CallSid':'CA-new-contact'})
    hooks=ConversationHooks(repo,calls,outbox,call['id'])
    ticket=await hooks.save_delegation('delegate1',({'role':'user','text':'Book a meeting'},),offset_ms=500)
    assert ticket.held
    assert repo.ready_tasks()==[]
    with pytest.raises(PermissionDenied):repo.issue_tool_grant(ticket.task_id)
    notice=outbox.claim_due()
    assert str(notice['person_id'])==owner.person_id and notice['task_id'] is None
    contact=repo.recognize('+18435550102')
    repo.set_person_status(owner,str(contact['id']),'approved',contact['revision'])
    assert repo.ready_tasks()[0]['id']==ticket.task_id
    assert repo.ready_tasks()[0]['scope']=='friend'
    assert repo.ready_tasks()[0]['authority']['verified_owner'] is False


def test_friend_cannot_attach_another_persons_private_conversation(conversation_setup):
    repo,owner,calls,_=conversation_setup
    private=calls.incoming({'To':'+18435550999','From':'+18435550100','CallSid':'CA-private',
                            'StirVerstat':'TN-Validation-Passed-A'})
    person=repo.save_person(owner,name='Friend',phone='+18435550103')
    repo.set_person_status(owner,str(person['id']),'approved',person['revision'])
    actor=Actor('alli',str(person['id']),'sms')
    with pytest.raises(PermissionDenied):
        repo.submit(actor,source_id='wrong-conversation',title='Read',instruction='Read this',
                    conversation_id=str(private['conversation_id']))
