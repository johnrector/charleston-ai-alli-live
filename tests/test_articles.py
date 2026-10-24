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

@pytest.mark.parametrize('html',['<button onclick="alert(1)">x</button>','<iframe src="https://example.com"></iframe>','<a href="javascript:alert(1)">x</a>','<form>private data</form>','<svg><a xlink:href="java&#10;script:alert(1)">x</a></svg>','<svg><set attributeName="href" to="javascript:alert(1)"></set></svg>','<a href="data:text/html,evil">x</a>','<meta http-equiv="refresh" content="0;url=https://example.com">'])
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
    job=dict(id='ARTICLE',phone='+18435550100',scope='friend',state='completed',private_result=False,created_at=(datetime.now(timezone.utc)-timedelta(days=12)).isoformat(),completed_at=datetime.now(timezone.utc).isoformat(),title='Designed article',result='Your finished article',sources=[],context=dict(article_source_id='HTML'))
    artifact=dict(id='HTML',filename='article.html',content_type='text/html',content=b'<style>.art{color:navy}</style><article class="art"><h1>Art direction</h1></article>')
    monkeypatch.setattr(jobs,'read',lambda *a,**kw:job)
    old=dict(artifact,id='OLD',filename='old-version.html')
    monkeypatch.setattr(routes,'files',lambda *a,**kw:[old,artifact])
    def deny(_):raise HTTPException(401,'Sign in')
    monkeypatch.setattr(routes.dashboard,'authenticate',deny)
    app=FastAPI();routes.install_work(app);client=TestClient(app)
    link='/work/ARTICLE/'+jobs.result_token(job)
    page=client.get(link);assert page.status_code==200 and '/files/HTML?view=article' in page.text and '/files/OLD?view=article' not in page.text and 'sandbox="allow-popups"' in page.text and 'allow-scripts' not in page.text
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

def test_skill_upload_waits_for_real_availability_without_reupload(monkeypatch):
    from unittest.mock import AsyncMock
    import httpx
    from openai import NotFoundError
    missing=NotFoundError('Skill version not yet available',response=httpx.Response(404,request=httpx.Request('GET','https://api.openai.com/v1/skills/test')),body={})
    client=SimpleNamespace(skills=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(id='skill_test',latest_version='1')),versions=SimpleNamespace(retrieve=AsyncMock(side_effect=[missing,SimpleNamespace()]))),containers=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(id='container_test'))))
    monkeypatch.setattr(publisher,'mark',lambda job,**values:job['context'].update(values))
    monkeypatch.setattr(publisher.asyncio,'sleep',AsyncMock())
    job=dict(id='TEST',context=dict(public_article=True,wordpress_authoring=True))
    assert asyncio.run(publisher.container(job,client))=='container_test'
    assert client.skills.create.await_count==1 and client.skills.versions.retrieve.await_count==2
    assert client.containers.create.call_args.kwargs['network_policy']=={'type':'disabled'}
    assert job['context']['wordpress_skill_id']=='skill_test'

def test_visual_failure_can_be_repaired_but_uncertain_publication_stops(monkeypatch):
    from unittest.mock import AsyncMock
    job=dict(id='TEST',scope='owner',phone='owner',context=dict(public_article=True,wordpress_authoring=True))
    monkeypatch.setattr(work_agent.jobs,'read',lambda _:dict(state='running'))
    monkeypatch.setattr(publisher.jobs,'owner',lambda p:p=='owner')
    call=SimpleNamespace(name='wordpress_publish',arguments='{}',call_id='test')
    monkeypatch.setattr(publisher,'execute',AsyncMock(side_effect=publisher.VisualValidationError('Heading clipped')))
    result=asyncio.run(work_agent.execute(job,call))
    assert result['ok'] is False and 'Heading clipped' in result['validation_error']
    monkeypatch.setattr(publisher,'execute',AsyncMock(side_effect=ValueError('An earlier WordPress write is uncertain')))
    with pytest.raises(ValueError,match='uncertain'):asyncio.run(work_agent.execute(job,call))

@pytest.mark.parametrize('fail_upload',[False,True])
def test_visual_review_streams_image_files_and_cleans_up(monkeypatch,fail_upload):
    import json,struct,contextlib
    from unittest.mock import AsyncMock,Mock
    import security_controls
    audit=dict(source_hash='exact',phase='article',viewports=[dict(width=w,overflow_px=0,split_words=0,clipped_headings=0,overlapping_headings=0) for w in (320,390,1440)])
    def png(name,w,h):return dict(filename=name,content=b'\x89PNG\r\n\x1a\n'+b'\0'*8+struct.pack('>II',w,h)+b'opaque-test-pixels')
    files={'audit':dict(content=json.dumps(audit).encode()),'phone320':png('320.png',320,844),'phone390':png('390.png',390,844),'desktop':png('1440.png',1440,900),'headings':png('headings.png',820,1000),'full':png('390-full.png',390,2200)}
    monkeypatch.setattr(publisher,'artifact',lambda job,fid:files[fid])
    monkeypatch.setattr(publisher.store,'connection',lambda:contextlib.nullcontext(Mock()))
    monkeypatch.setattr(security_controls,'model',lambda *a:None)
    monkeypatch.setattr(security_controls,'settle_model',lambda *a:None)
    created=[SimpleNamespace(id='image'+str(i)) for i in range(5)]
    if fail_upload:created=[created[0],RuntimeError('Upload failed')]
    client=SimpleNamespace(files=SimpleNamespace(create=AsyncMock(side_effect=created),delete=AsyncMock()),responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text='{"passed":true,"issues":[]}',usage=None))))
    args=(dict(id='TEST',phone='owner'),dict(source_hash='exact',post=dict(title='A designed article')),['phone320','phone390','desktop','headings','full'],'audit',client)
    if fail_upload:
        with pytest.raises(RuntimeError,match='Upload failed'):asyncio.run(publisher.visual(*args,phase='article'))
        assert client.files.delete.await_count==1 and not client.responses.create.called
    else:
        asyncio.run(publisher.visual(*args,phase='article'))
        content=client.responses.create.call_args.kwargs['input'][0]['content']
        images=[v for v in content if v['type']=='input_image']
        assert len(images)==5 and all('file_id' in v and 'image_url' not in v and '_artifact_id' not in v for v in images)
        assert client.files.delete.await_count==5

@pytest.mark.parametrize('memory,expected',[(536870912,1),(2147483648,2)])
def test_default_work_parallelism_respects_memory(monkeypatch,memory,expected):
    from pathlib import Path
    monkeypatch.delenv('ALLI_WORK_CONCURRENCY',raising=False)
    monkeypatch.setattr(Path,'read_text',lambda _:str(memory))
    assert work_agent.work_parallelism()==expected

def test_dashboard_middleware_preserves_sandboxed_article_design(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import work_routes as routes
    monkeypatch.setattr(routes.dashboard,'authenticate',lambda _:dict(method='cookie'))
    artifact=dict(id='HTML',filename='article.html',content_type='text/html',content=b'<style>.art{color:navy}</style><article class="art">Designed article</article>')
    monkeypatch.setattr(routes,'files',lambda *a,**kw:[artifact])
    app=FastAPI();routes.dashboard.install_dashboard(app,None,None,None);routes.install_work(app)
    client=TestClient(app)
    article=client.get('/dashboard/api/work/ARTICLE/files/HTML?view=article')
    csp=article.headers['content-security-policy']
    assert article.status_code==200 and "style-src 'unsafe-inline' https:" in csp
    assert "default-src 'none'" in csp and 'sandbox allow-popups' in csp and 'allow-scripts' not in csp and 'allow-same-origin' not in csp
    assert article.headers['x-frame-options']=='SAMEORIGIN' and "frame-ancestors 'self'" in csp
    dashboard=client.get('/dashboard')
    assert "style-src 'self'" in dashboard.headers['content-security-policy'] and dashboard.headers['x-frame-options']=='DENY'
