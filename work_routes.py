"""Private owner controls and recipient-only, expiring result delivery links."""
import asyncio
import html
import re
from urllib.parse import quote
from fastapi import Depends,HTTPException,Request,Response
from fastapi.responses import HTMLResponse,RedirectResponse
from pydantic import BaseModel,ConfigDict,Field
import dashboard
import work_store as jobs
import work_agent
import cloud_skills
from cloud_runtime import runtime
import communication_store as store

class Input(BaseModel):
    model_config=ConfigDict(extra='forbid')
class NewWork(Input):
    title:str=Field(min_length=1,max_length=180)
    instruction:str=Field(min_length=1,max_length=6000)
    phone:str=Field(default='',max_length=30)
    intent_id:str=Field(min_length=16,max_length=64,pattern='^[A-Za-z0-9-]+$')
    notify:bool=True
class Change(Input):
    action:str=Field(pattern='^(approve|deny|answer|cancel|revoke_link)$')
    answer:str=Field(default='',max_length=6000)

def files(wid,*,content=False):
    with store.connection() as db:
        rows=db.execute('SELECT id,filename,content_type'+(',content' if content else '')+' FROM alli_work_file WHERE work_id=%s ORDER BY created_at,id',(wid,)).fetchall()
    return [dict(zip(('id','filename','content_type','content') if content else ('id','filename','content_type'),r)) for r in rows]

def details():
    rows=jobs.listing()
    for row in rows:
        internal=jobs.read(row['id'],internal=True)
        row['approval']=internal['approval'];row['files']=files(row['id'])
        row['result']=row['result'][:2000]
        if row['state'] in ('completed','partial','blocked') and row['result']:row['result_url']='/dashboard/work/'+row['id']
        if row['outbound_id']:row['delivery_status']=__import__('scheduled_sms').get(row['outbound_id'])['status']
    return dict(ok=True,work=rows,runtime=runtime.status(),skills=cloud_skills.catalog())

def link_text(text):
    # Text is data, not HTML. Only link actual HTTP(S) URL spans.
    # Files have verified download buttons; hosted-container paths are private
    # implementation details and do not work in the recipient's browser.
    text=re.sub(r'\[([^\]]+)\]\(sandbox:/[^)]+\)',r'\1',text)
    text=re.sub(r'[^]*','',text)
    result=[];last=0
    for match in re.finditer(r'https?://[^\s<>\]\)"\']+',text):
        result.append(html.escape(text[last:match.start()]))
        url=html.escape(match.group(),quote=True)
        result.append('<a rel="noreferrer noopener" href="'+url+'">'+html.escape(match.group())+'</a>');last=match.end()
    return ''.join(result)+html.escape(text[last:])

def install_work(app):
    async def auth(request:Request):return await asyncio.to_thread(dashboard.authenticate,request)
    async def write(request:Request,identity=Depends(auth)):
        dashboard.write_guard(request,identity)
        return identity

    @app.get('/dashboard/api/work',dependencies=[Depends(auth)])
    async def work():return await asyncio.to_thread(details)

    @app.post('/dashboard/api/work',dependencies=[Depends(write)])
    async def create(body:NewWork):
        phone=store.normalize_phone(body.phone) if body.phone else __import__('promises').owner_phone()
        if not phone:raise HTTPException(422,'Valid recipient required')
        context=await asyncio.to_thread(store.context_for,phone)
        name=(context.get('contact') or {}).get('name') or 'Saved contact'
        try:
            return await asyncio.to_thread(jobs.create,phone,name,body.title,body.instruction,'dashboard:'+body.intent_id,
              owner_scope=jobs.owner(phone),notify=body.notify,context=await asyncio.to_thread(work_agent.recent_context,phone))
        except ValueError as exc:raise HTTPException(409,str(exc))

    @app.post('/dashboard/api/work/{wid}',dependencies=[Depends(write)])
    async def change(wid:str,body:Change):
        try:
            if body.action=='revoke_link':
                with store.connection() as db:
                    db.execute('UPDATE alli_work SET link_revoked=true WHERE id=%s',(wid,))
                    jobs.event(db,wid,'result_link_revoked')
                return dict(ok=True)
            if body.action in ('approve','deny'):return await asyncio.to_thread(work_agent.decision,wid,body.action=='approve')
            if body.action=='answer':return await asyncio.to_thread(work_agent.resume,wid,None,body.answer,john=True)
            return await asyncio.to_thread(jobs.cancel,wid)
        except ValueError as exc:raise HTTPException(409,str(exc))

    @app.post('/dashboard/api/cloud/login',dependencies=[Depends(write)])
    async def cloud_login():
        try:return dict(ok=True,login=await runtime.begin_login())
        except Exception:raise HTTPException(409,'Cloud account runtime is starting or needs attention; refresh shortly')

    @app.post('/dashboard/api/cloud/refresh',dependencies=[Depends(write)])
    async def cloud_refresh():return dict(ok=True,runtime=await runtime.refresh())

    @app.post('/dashboard/api/skills/import',dependencies=[Depends(write)])
    async def import_skills(request:Request):
        data=bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>20*1024*1024:raise HTTPException(413,'Skill archive exceeds 20 MB')
        try:return await asyncio.to_thread(cloud_skills.import_bundle,bytes(data))
        except Exception:raise HTTPException(422,'Invalid skill archive. Use the prepared john-skills ZIP with manifest, catalog and resources.')

    async def render_result(wid,token=None,*,admin=False,request=None):
        job=await asyncio.to_thread(jobs.read,wid)
        if not job or (not admin and not jobs.valid_token(job,token)) or job['state'] not in ('completed','partial','blocked') or not job['result']:raise HTTPException(404,'Result unavailable or link expired')
        if not admin and (job['scope']=='owner' or job.get('private_result')):
            try:await auth(request)
            except HTTPException:return RedirectResponse('/dashboard?result='+quote(wid,safe=''),status_code=303)
        artifacts=await asyncio.to_thread(files,wid)
        prefix='/dashboard/api/work/'+quote(wid,safe='')+'/files/' if admin else '/work/'+quote(wid,safe='')+'/'+quote(token,safe='')+'/files/'
        downloads=''
        internal=await asyncio.to_thread(jobs.read,wid,internal=True)
        preferred=(internal.get('context') or {}).get('primary_artifact_id') or (internal.get('context') or {}).get('article_source_id') or (internal.get('context') or {}).get('wordpress_source_id')
        article=next((f for f in artifacts if f['id']==preferred),None) or next((f for f in reversed(artifacts) if f['content_type']=='text/html' and f['filename'].lower().endswith(('.html','.htm'))),None)
        reader=('<a class="file" href="'+prefix+article['id']+'?view=article">Open article</a><a class="file" href="'+prefix+article['id']+'">Download HTML</a>') if article else ''
        if article:
            if admin:
                latest={}
                for f in artifacts:
                    if f['id']!=article['id']:latest[f['filename']]=f
                downloads='<details><summary>Supporting files</summary>'+''.join('<a class="file" href="'+prefix+f['id']+'">↓ '+html.escape(f['filename'])+'</a>' for f in latest.values())+'</details>' if latest else ''
        else:downloads=''.join('<a class="file" href="'+prefix+f['id']+'">↓ '+html.escape(f['filename'])+'</a>' for f in artifacts)

        sources=''.join('<li><a rel="noreferrer noopener" href="'+html.escape(s['url'],quote=True)+'">'+html.escape(s.get('title') or s['url'])+'</a></li>' for s in job['sources'] if s.get('url','').startswith(('https://','http://')))
        page='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>Alli · Your work</title><style>body{margin:0;background:#f5f4ed;color:#233b2e;font:16px/1.7 system-ui,sans-serif}main{max-width:850px;margin:auto;padding:36px 24px}header{font-size:14px;letter-spacing:2px}h1{font:42px/1.1 Georgia,serif}a{color:#244f3b;overflow-wrap:anywhere}.file{display:inline-block;margin:5px 10px 15px 0;padding:12px 18px;background:#244f3b;color:white;border-radius:6px;text-decoration:none}.article-reader{display:block;width:100%;height:85vh;min-height:500px;border:1px solid #dedfd5;border-radius:8px;background:white}.result{white-space:pre-wrap;overflow-wrap:anywhere;background:#fffef9;padding:24px;border:1px solid #dedfd5;border-radius:8px}.note{font-size:12px;color:#626e60}h2{font:28px Georgia,serif}</style><main><header>ALLI / JOHN RECTOR'S AI ASSISTANT</header><h1>'''+html.escape(job['title'])+'</h1>'+reader+downloads+'<div class="result">'+link_text(job['result'])+'</div>'+('<h2>Sources</h2><ul>'+sources+'</ul>' if sources else '')+'<p class="note">Reply to Alli by call or text for changes. This private link expires 14 days after completion. Anyone you share the link with can read this result.</p></main></html>'
        page=page.replace('expires 14 days','expires 7 days')
        if admin:page=page.replace('This private link expires 7 days after completion. Anyone you share the link with can read this result.','Owner view. Your sign-in is required; revoking a recipient link does not remove this activity record.')
        elif job['scope']=='owner' or job.get('private_result'):page=page.replace('Anyone you share the link with can read this result.','Your owner sign-in is required to read this result.')
        return HTMLResponse(page,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Robots-Tag':'noindex, nofollow','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY',
          'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-src 'self'; frame-ancestors 'none'"})

    @app.get('/work/{wid}/{token}',include_in_schema=False)
    async def result(wid:str,token:str,request:Request):return await render_result(wid,token,request=request)

    @app.get('/dashboard/work/{wid}',include_in_schema=False)
    async def owner_result(wid:str,request:Request):
        try:await auth(request)
        except HTTPException:return RedirectResponse('/dashboard?result='+quote(wid,safe=''),status_code=303)
        return await render_result(wid,admin=True)

    async def file_response(wid,fid,view=''):
        artifact=next((f for f in await asyncio.to_thread(files,wid,content=True) if f['id']==fid),None)
        if not artifact:raise HTTPException(404,'File unavailable')
        inline=view=='article' and artifact['content_type']=='text/html' and artifact['filename'].lower().endswith(('.html','.htm'))
        return Response(bytes(artifact['content']),media_type=artifact['content_type'],headers={
          'Content-Disposition':("inline; filename*=UTF-8''" if inline else "attachment; filename*=UTF-8''")+quote(artifact['filename'],safe=''),
          'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Robots-Tag':'noindex, nofollow','X-Content-Type-Options':'nosniff',
          'X-Frame-Options':'SAMEORIGIN' if inline else 'DENY',
          'Content-Security-Policy':("default-src 'none'; style-src 'unsafe-inline' https:; img-src https: data:; font-src https: data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox allow-popups" if inline else "default-src 'none'; sandbox")})

    @app.get('/dashboard/api/work/{wid}/files/{fid}',include_in_schema=False)
    async def owner_artifact(wid:str,fid:str,request:Request,view:str=''):
        try:await auth(request)
        except HTTPException:
            if view=='article':return RedirectResponse('/dashboard?result='+quote(wid,safe=''),status_code=303)
            raise
        return await file_response(wid,fid,view)

    @app.get('/work/{wid}/{token}/files/{fid}',include_in_schema=False)
    async def artifact(wid:str,token:str,fid:str,request:Request,view:str=''):
        job=await asyncio.to_thread(jobs.read,wid)
        if not jobs.valid_token(job,token) or job['state'] not in ('completed','partial','blocked') or not job['result']:raise HTTPException(404,'File unavailable')
        if job['scope']=='owner' or job.get('private_result'):
            try:await auth(request)
            except HTTPException:
                if view=='article':return RedirectResponse('/dashboard?result='+quote(wid,safe=''),status_code=303)
                raise
        return await file_response(wid,fid,view)
