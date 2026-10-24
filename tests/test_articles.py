import asyncio
from types import SimpleNamespace
import pytest
import article_tasks as intake
import wordpress_publish as publisher
import work_agent

@pytest.mark.parametrize('owner,group,blog,expected',[(True,False,True,True),(False,False,True,False),(True,True,True,False),(True,False,False,False)])
def test_article_destination_is_server_bound(monkeypatch,owner,group,blog,expected):
    captured={}
    monkeypatch.setattr(intake.jobs,'owner',lambda phone:phone=='owner')
    def create(*a,**kw):captured.update(kw);return dict(ok=True,work_id='TEST',state='queued')
    monkeypatch.setattr(intake.jobs,'create',create)
    result=intake.create('owner','John','A Person','',blog,'request1',authorized_owner=owner,shared_group=group,original='Research and publish A Person')
    assert captured['owner_scope'] is expected
    assert captured['context']['wordpress_authoring'] is expected
    assert result['destination']==('johnrector.me' if expected else 'expiring_result')
    assert not any(k in captured['context'] for k in ('prior_work','private_contacts','account_data'))

@pytest.mark.parametrize('scope,phone,group,assigned',[('friend','owner',False,True),('owner','friend',False,True),('owner','owner',True,True),('owner','owner',False,False)])
def test_publisher_rejects_friend_forgery_group_and_unassigned(monkeypatch,scope,phone,group,assigned):
    monkeypatch.setattr(publisher.jobs,'owner',lambda p:p=='owner')
    with pytest.raises(ValueError):publisher.owner_only(dict(scope=scope,phone=phone,context=dict(group_delegation=group,wordpress_authoring=assigned)))

@pytest.mark.parametrize('html',['<button onclick="alert(1)">x</button>','<iframe src="https://example.com"></iframe>','<a href="javascript:alert(1)">x</a>','<form>private data</form>'])
def test_publisher_rejects_active_content(html):
    with pytest.raises(ValueError):publisher.safe_source(html)

def test_script_free_html_controls_are_supported():
    publisher.safe_source('<style>.switch:checked ~ .answer{display:block}</style><input class="switch" type="checkbox"><label>Explore</label><div class="answer">Real article</div>')

def test_public_article_tools_cannot_expose_accounts():
    job=dict(scope='friend',context=dict(public_article=True))
    assert [t['name'] for t in work_agent.functions(job)]==['ask_for_clarification','article_artifacts','article_verify']

def test_public_article_backend_rejects_undeclared_private_tools(monkeypatch):
    job=dict(id='ARTICLE',scope='owner',context=dict(public_article=True,wordpress_authoring=True))
    monkeypatch.setattr(work_agent.jobs,'read',lambda _:dict(state='running'))
    call=SimpleNamespace(name='run_connected_tool',arguments='{}',call_id='test')
    with pytest.raises(ValueError,match='public research'):asyncio.run(work_agent.execute(job,call))

def test_visual_qa_requires_real_desktop_and_phone_proofs():
    with pytest.raises(ValueError,match='real desktop'):asyncio.run(publisher.visual({}, {}, ['invented'], 'audit',None,phase='preflight'))

def test_friend_article_reader_is_sandboxed_and_expires(monkeypatch):
    from datetime import datetime,timedelta,timezone
    from fastapi import FastAPI,HTTPException
    from fastapi.testclient import TestClient
    import work_routes as routes
    import work_store as jobs
    monkeypatch.setenv('DEMO_KEY','test-only-artifact-secret')
    monkeypatch.setattr(__import__('security_controls'),'blocked',lambda _:False)
    job=dict(id='ARTICLE',phone='+18435550100',scope='friend',state='completed',private_result=False,created_at=(datetime.now(timezone.utc)-timedelta(days=12)).isoformat(),completed_at=datetime.now(timezone.utc).isoformat(),title='Designed article',result='Your finished article',sources=[])
    artifact=dict(id='HTML',filename='article.html',content_type='text/html',content=b'<style>.art{color:navy}</style><article class="art"><h1>Art direction</h1></article>')
    monkeypatch.setattr(jobs,'read',lambda *a,**kw:job)
    monkeypatch.setattr(routes,'files',lambda *a,**kw:[artifact])
    def deny(_):raise HTTPException(401,'Sign in')
    monkeypatch.setattr(routes.dashboard,'authenticate',deny)
    app=FastAPI();routes.install_work(app);client=TestClient(app)
    link='/work/ARTICLE/'+jobs.result_token(job)
    page=client.get(link);assert page.status_code==200 and 'sandbox="allow-popups"' in page.text and 'allow-scripts' not in page.text
    inline=client.get(link+'/files/HTML?view=article')
    assert inline.status_code==200 and inline.headers['content-disposition'].startswith('inline')
    assert "default-src 'none'" in inline.headers['content-security-policy'] and 'allow-same-origin' not in inline.headers['content-security-policy']
    assert client.get('/dashboard/api/work/ARTICLE/files/HTML?view=article').status_code==401
    job['completed_at']=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
    assert client.get(link).status_code==404 and client.get(link+'/files/HTML?view=article').status_code==404

def test_personal_html_engine_preserves_art_and_lifts_metadata():
    import wordpress_html as engine
    source='<style>.unique{display:grid;color:#f4ebda;background:#163c33}.toggle:checked~section{display:grid}</style><article class="unique"><h1>A designed experience</h1><input class="toggle" type="checkbox"><section>Explore the idea</section></article>'
    output=engine.protect_custom_html(source)
    assert source in output
    assert '<template shadowrootmode="open">' in output
    assert 'data-codex-theme-canvas' in output and 'data-codex-theme-firewall' in output
    with pytest.raises(RuntimeError,match='cannot contain scripts'):engine.protect_custom_html(source+'<script>alert(1)</script>')
    with pytest.raises(RuntimeError,match='scoped CSS'):engine.protect_custom_html('<article>Generic content</article>')
