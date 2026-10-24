import base64
import hashlib
import json
import time
from types import SimpleNamespace as NS

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
import pytest

from adapters.computer import Computer
from adapters.postgres import PostgresRepository
from alli_core.authority import Actor,PermissionDenied
from alli_core.profile import Profile


def setup(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'));repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'sms',True,'signed-sms:test')
    task=repo.submit(owner,source_id='owner-work',title='Local work',instruction='Use my local files')
    key=Ed25519PrivateKey.generate()
    computer=Computer(repo,NS(),{'id':'test-mac','workspace':'/workspace','plugins':{'test-plugin':'/plugins/test'},
        'public_key':key.public_key().public_bytes(Encoding.PEM,PublicFormat.SubjectPublicKeyInfo).decode()})
    return repo,owner,task,key,computer


def test_executor_lease_requires_device_key_and_rejects_replay(database):
    repo,owner,task,key,computer=setup(database)
    assert not computer.online()
    body=json.dumps({'device_id':'test-mac'}).encode();stamp=str(int(time.time()));nonce='n'*48
    message='POST\n/executor/lease\n'+stamp+'\n'+nonce+'\n'+hashlib.sha256(body).hexdigest()
    headers={'x-alli-time':stamp,'x-alli-nonce':nonce,'x-alli-signature':base64.b64encode(key.sign(message.encode())).decode()}
    computer.authenticate(body,headers,'/executor/lease')
    assert computer.online()
    with pytest.raises(PermissionDenied):computer.authenticate(body,headers,'/executor/lease')
    with pytest.raises(PermissionDenied):computer.authenticate(body,headers,'/different/path')


@pytest.mark.asyncio
async def test_computer_work_waits_offline_and_forwards_once_without_child_text(database):
    repo,owner,task,key,computer=setup(database)
    result=computer.queue(owner,task['id'],'Inspect a local file','stable',['test-plugin'])
    child=repo.task_result(result['task_id'])
    assert child['parent_id']==task['id'] and child['context']['internal_execution'] is True
    assert not computer.online() and computer.execution(child).configuration()['capability_directories']==['/plugins/test']
    with pytest.raises(ValueError):computer.queue(owner,task['id'],'Bad plugin','another',['unregistered'])
    contact=repo.save_person(owner,name='Friend',phone='+18435550101')
    repo.set_person_status(owner,str(contact['id']),'approved',contact['revision'])
    friend=Actor('alli',str(contact['id']),'sms',False,'signed-sms:friend')
    with pytest.raises(PermissionDenied):computer.queue(friend,task['id'],'Read owner files','malicious')
    repo.record_outcome(child['id'],'completed','File inspected; actual result.',turn_id='computer-turn',notify=False)
    await computer.forward();await computer.forward()
    assert repo.task_result(task['id'])['context']['pending_computer_work']==[]
    assert len(repo.pending_inputs(task['id']))==1
