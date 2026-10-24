"""The agent uses native plugin tools; Render cannot author or promote friends."""
import asyncio,json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import article_handoff as handoff
import work_agent
import phone_tool_policy as policy

NATIVE='wordpress-com.wpcom-mcp-content-authoring'

def owner():return dict(id='HANDOFF',scope='owner',phone='john',instruction='Publish the finished article',approval=None,context=dict(cloud_article_handoff=True,public_article=True,wordpress_authoring=True,article_destination='johnrector.me',delegation_source='Publish this article to my WordPress site.'))

@pytest.fixture(autouse=True)
def identity(monkeypatch):monkeypatch.setattr(handoff.jobs,'owner',lambda p:p=='john')

def test_only_native_article_plugin_is_exposed(monkeypatch):
    job=owner();names={x['name'] for x in work_agent.functions(job)}
    assert {'find_connected_tools','run_connected_tool','list_work_artifacts','inspect_work_images'}<=names
    assert not any(n.startswith('wordpress_') for n in names)
    assert policy.allowed(NATIVE,job)
    assert not handoff.permitted('gmail.read_email',job)
    monkeypatch.setattr(work_agent.runtime,'state','connected')
    monkeypatch.setattr(work_agent.runtime,'tools',{NATIVE:dict(name=NATIVE,description='WordPress article authoring',inputSchema={}), 'gmail.read_email':dict(name='gmail.read_email',description='Private email',inputSchema={})})
    assert [x['name'] for x in work_agent.find_tools('WordPress',job=job)['tools']]==[NATIVE]
    assert work_agent.find_tools('email',job=job)['tools']==[]

@pytest.mark.parametrize('change',[dict(scope='friend'),dict(phone='other'),dict(context=dict(cloud_article_handoff=True,wordpress_authoring=True,group_delegation=True,delegation_source='publish'))])
def test_friend_and_group_cannot_publish(change):
    job=owner();job.update(change)
    with pytest.raises(ValueError):policy.validate(NATIVE,dict(action='execute',operation='posts.create',wpcom_site='johnrector.me',params=dict(user_confirmed=True)),job)

@pytest.mark.parametrize('args',[dict(action='execute',operation='posts.create',wpcom_site='another-site.com',params=dict(user_confirmed=True)),dict(action='execute',operation='users.update',wpcom_site='johnrector.me',params=dict(user_confirmed=True)),dict(action='execute',operation='posts.create',wpcom_site='johnrector.me',params={})])
def test_site_and_operation_and_confirmation_are_bound(args):
    with pytest.raises(ValueError):policy.validate(NATIVE,args,owner())

def test_reading_a_write_schema_is_not_publishing():
    args=dict(action='describe',operation='posts.create',wpcom_site='johnrector.me')
    policy.validate(NATIVE,args,owner())
    assert handoff.read_only(NATIVE,args)
    args['action']='execute'
    with pytest.raises(ValueError):policy.validate(NATIVE,args,owner())

def test_native_plugin_receives_agent_authored_html_unchanged(monkeypatch):
    job=owner();job['state']='running';monkeypatch.setattr(work_agent.runtime,'state','connected');monkeypatch.setattr(work_agent.jobs,'read',lambda _:job);args=dict(action='execute',operation='posts.create',wpcom_site='johnrector.me',params=dict(user_confirmed=True,content='<article>Exact cloud-agent authored HTML</article>',status='publish'))
    receipt=dict(id=123,link='https://johnrector.me/test/',status='publish')
    tool=dict(name=NATIVE,description='Native WordPress tool',annotations=dict(readOnlyHint=False))
    monkeypatch.setattr(work_agent.runtime,'tools',{NATIVE:tool})
    invoke=AsyncMock(return_value=receipt);monkeypatch.setattr(work_agent.runtime,'invoke',invoke)
    monkeypatch.setattr(work_agent,'delegated_authority',AsyncMock(return_value=dict(allowed=True,reason='Explicit owner publication')))
    monkeypatch.setattr(__import__('security_controls'),'reserve',lambda *a:None)
    monkeypatch.setattr(work_agent.jobs,'event',lambda *a:None)
    @contextmanager
    def connection():yield SimpleNamespace(execute=lambda *a:SimpleNamespace(fetchone=lambda:None))
    monkeypatch.setattr(work_agent.store,'connection',connection)
    call=SimpleNamespace(name='run_connected_tool',call_id='test',arguments=json.dumps(dict(tool_name=NATIVE,arguments_json=json.dumps(args))))
    assert asyncio.run(work_agent.execute(job,call,object()))==receipt
    invoke.assert_awaited_once_with(NATIVE,args,job=job)
    assert job['context']['publication_receipt']==dict(post_id=123,url='https://johnrector.me/test/',status='publish',source='native_wordpress_plugin')


def test_owner_identity_does_not_depend_on_publication_destination(monkeypatch):
    import article_tasks
    seen={}
    monkeypatch.setattr(article_tasks.jobs,'create',lambda *a,**kw:seen.update(kw) or dict(ok=True,work_id='OWNER',state='queued'))
    monkeypatch.setattr(article_tasks.jobs,'clean',lambda x,*a,**kw:x)
    article_tasks.create('john','Misleading subject name','Blake Miller','Public profile',False,'owner-preview',authorized_owner=True,original='Write a standalone article about Blake')
    assert seen['owner_scope'] is True and seen['context']['wordpress_authoring'] is False
    job=owner();job['context']=seen['context']
    routing=handoff.envelope(job)
    assert routing['requester_role']=='owner' and routing['requester_name']=='John Rector'
    assert routing['article_subject']=='Blake Miller' and routing['destination']=='expiring_result'
    assert routing['wordpress_permitted'] is False
    assert not {'find_connected_tools','run_connected_tool'} & {t['name'] for t in work_agent.functions(job)}
