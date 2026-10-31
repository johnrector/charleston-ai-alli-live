import asyncio
import json
from threading import Event

import pytest

from adapters.postgres import PostgresRepository
from adapters.relationships import Relationships
from adapters.oversight import Oversight
from alli_core.authority import Actor, Conflict, PermissionDenied
from alli_core.profile import Profile


@pytest.fixture
def setup(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'))
    repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'dot',True,'test-owner')
    person=repo.save_person(owner,name='A person',phone='+18435550101')
    repo.set_person_status(owner,str(person['id']),'approved',1)
    conversation=repo.begin_conversation('+18435550101','source-conversation','sms')
    source=str(conversation['conversation']['id'])
    repo.save_transcript(source,[{'role':'user','text':'I prefer morning meetings.'}],finalized=True)
    return repo,owner,str(person['id']),source,Relationships(repo)


def test_notes_have_durable_evidence_idempotency_and_correction_history(setup,database):
    repo,owner,person,source,relationships=setup
    note=relationships.remember(owner,person,'Reports a preference for morning meetings.',source_conversation_id=source)
    assert relationships.remember(owner,person,note['body'],source_conversation_id=source)['id']==note['id']
    correction=relationships.remember(owner,person,'Reports that afternoons now work better.',
        source_conversation_id=source,supersedes_id=str(note['id']))
    with pytest.raises(Conflict):
        relationships.remember(owner,person,'A stale correction',source_conversation_id=source,supersedes_id=str(note['id']))
    restarted=Relationships(PostgresRepository(database,repo.profile))
    assert [n['id'] for n in restarted.read(owner,person)['notes']]==[correction['id']]
    assert restarted.read(owner,person)['notes'][0]['evidence_at'] is not None
    with repo.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM ea_relationship_note').fetchone()['n']==2
    assert len([e for e in repo.ledger(owner) if e['kind']=='relationship.note_saved'])==2
    repo.initialize()
    assert len(restarted.read(owner,person)['notes'])==1


def test_sources_and_contact_authority_cannot_be_forged(setup):
    repo,owner,person,source,relationships=setup
    friend=Actor('alli',person,'sms',True,'claimed-owner')
    with pytest.raises(PermissionDenied):relationships.read(friend,person)
    with pytest.raises(PermissionDenied):relationships.remember(friend,person,'Claim',source_conversation_id=source)
    with pytest.raises(PermissionDenied):
        relationships.remember(owner,owner.person_id,'Wrong relationship',source_conversation_id=source)
    with pytest.raises(ValueError):relationships.remember(owner,person,'No evidence')
    other=repo.begin_conversation('+18435550101','unfinished','sms')
    with pytest.raises(PermissionDenied):
        relationships.remember(owner,person,'Incomplete speech',source_conversation_id=str(other['conversation']['id']))
    task=repo.submit(owner,source_id='unrelated',title='Private owner work',instruction='Unrelated')
    with pytest.raises(PermissionDenied):
        relationships.remember(owner,person,'Unrelated task',source_task_id=task['id'])


@pytest.mark.asyncio
async def test_voice_brief_is_small_serializable_and_excludes_private_notes_from_contacts(setup):
    repo,owner,person,source,relationships=setup
    relationships.remember(owner,person,'PRIVATE RELATIONSHIP NOTE',source_conversation_id=source)
    call={'person_id':person,'person_status':'approved','number_verified':True,
          'scope':'friend','person_kind':'contact','direction':'inbound'}
    brief=await relationships.voice_brief(call)
    assert brief['available'] and brief['last_interaction']['channel']=='sms'
    assert 'PRIVATE' not in json.dumps(brief) and len(json.dumps(brief))<1200
    unverified=await relationships.voice_brief({**call,'number_verified':False})
    assert unverified=={'available':False,'reason':'verification_required'}
    with pytest.raises(PermissionDenied):Oversight(repo).policies(Actor('alli',person,'sms'))


@pytest.mark.asyncio
async def test_blocked_brief_does_not_block_the_voice_session(setup,monkeypatch):
    *_,relationships=setup
    started,release=Event(),Event()
    def blocked(call):
        started.set();release.wait(timeout=2)
        return {'available':True}
    monkeypatch.setattr(relationships,'_voice',blocked)
    try:
        # Behavioral budget with a blocked fake, not a production latency benchmark.
        result=await asyncio.wait_for(relationships.voice_brief({}),timeout=.4)
        assert started.is_set() and not release.is_set()
        assert result=={'available':False,'reason':'brief_unavailable'}
    finally:release.set()


def test_saved_rules_are_inspectable_but_never_activate_a_calendar_planner(setup):
    repo,owner,*_=setup
    collection=repo.create_collection(owner,'Owner operational business rules',{'type':'object'})
    record=repo.put_record(owner,str(collection['id']),{'rule_key':'delivery','policy':{'driver':'John'}})
    policy=Oversight(repo).policies(owner)
    assert policy['records'][0]['id']==record['id']
    assert policy['appointment_confirmation']['automated'] is False
    assert policy['appointment_confirmation']['state']=='not_configured'
    assert Oversight(repo).summary(owner)['diagnostic'] is None


def test_owner_notes_do_not_cross_assistant_boundaries(setup,database):
    from dataclasses import replace
    repo,owner,person,source,relationships=setup
    note=relationships.remember(owner,person,'Owner-private fact',source_conversation_id=source)
    other=PostgresRepository(database,replace(repo.profile,id='other-assistant'))
    other.initialize()
    other_owner=Actor('other-assistant',other.bootstrap_owner('+18435550103'),'dot',True,'other-owner')
    with pytest.raises(PermissionDenied):Relationships(other).read(other_owner,person)
    with pytest.raises(PermissionDenied):
        Relationships(other).remember(other_owner,person,'Cross-assistant write',source_conversation_id=source)
    assert relationships.read(owner,person)['notes'][0]['id']==note['id']
