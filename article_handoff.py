"""Cloud-agent handoff support: permissions, tool transport and artifact delivery.

The hosted agent owns composition, Publisher skill execution and publication QA.
Render never composes or transforms the WordPress post on this path.
"""
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from psycopg.types.json import Jsonb
import communication_store as store
import work_store as jobs
from reasoning_config import MODEL, REASONING

NAMES={'list_work_artifacts','inspect_work_images','read_public_resource'}
OPERATIONS={'posts.list','posts.get','posts.create','posts.update','media.get','media.create','categories.list','tags.list'}
HOSTS={'johnrector.me','johnrector.files.wordpress.com','fonts.googleapis.com','fonts.gstatic.com','s0.wp.com','s1.wp.com','s2.wp.com','i0.wp.com','i1.wp.com','i2.wp.com'}

def enabled(job):
    return bool((job.get('context') or {}).get('cloud_article_handoff'))

def wordpress_tool(name):
    return name.lower().replace('-','_').endswith('wpcom_mcp_content_authoring')

def permitted(name,job):
    c=job.get('context') or {}
    return bool(enabled(job) and c.get('wordpress_authoring') and job.get('scope')=='owner'
      and jobs.owner(job.get('phone','')) and not c.get('group_delegation')
      and c.get('delegation_source') and wordpress_tool(name))

def validate(name,args,job):
    if not permitted(name,job):raise ValueError('Only John’s authenticated article assignment can use the WordPress plugin')
    if args.get('wpcom_site')!='johnrector.me':raise ValueError('This assignment is limited to johnrector.me')
    if args.get('action') not in {'list','describe','execute'}:raise ValueError('Unsupported WordPress plugin action')
    op=args.get('operation')
    if op and op not in OPERATIONS:raise ValueError('This plugin operation is outside the article assignment')
    if args.get('action')=='execute' and op not in OPERATIONS:raise ValueError('An allowed article operation is required')
    params=args.get('params') or {}
    if not isinstance(params,dict):raise ValueError('Plugin parameters must be an object')
    if (job.get('context') or {}).get('qa_dry_run_only') and op in {'posts.create','posts.update','media.create'}:
        raise ValueError('This diagnostic assignment forbids WordPress writes')
    if op in {'posts.create','posts.update','media.create'} and params.get('user_confirmed') is not True:
        raise ValueError('Use user_confirmed=true only for the actual owner-delegated publication')

def read_only(name,args):
    return wordpress_tool(name) and (args.get('action') in {'list','describe'} or args.get('operation') in {'posts.list','posts.get','media.get','categories.list','tags.list'})

def schemas(schema):
    return [schema('list_work_artifacts','Cache actual files from this job’s cloud container. Optionally select a returned HTML file ID as the primary deliverable.',dict(primary_file_id=dict(type='string')),()),
      schema('inspect_work_images','Ask an independent cloud reasoning model to inspect actual cached images. Supply the question and image IDs. The authoring agent decides and performs repairs.',dict(question=dict(type='string'),image_ids=dict(type='array',items=dict(type='string'))),('question','image_ids')),
      schema('read_public_resource','Download a public WordPress page, image, stylesheet or font into this job’s cloud container for the agent to inspect. No cookies or private endpoints. Returns actual path, URL, status and hash.',dict(url=dict(type='string')),('url',))]

def artifact(job,fid):
    with store.connection() as db:
        row=db.execute('SELECT filename,content_type,content FROM alli_work_file WHERE work_id=%s AND id=%s',(job['id'],fid)).fetchone()
    if not row:raise ValueError('File is not an artifact of this assignment')
    return dict(filename=row[0],content_type=row[1],content=bytes(row[2]))

async def execute(job,call,args,client):
    if not enabled(job):raise ValueError('Cloud handoff is not enabled for this assignment')
    if call.name=='list_work_artifacts':
        from article_studio import import_files
        result=await import_files(job,client)
        if args.get('primary_file_id'):
            item=artifact(job,args['primary_file_id'])
            if item['content_type']!='text/html':raise ValueError('The article deliverable must be HTML')
            job['context']['primary_artifact_id']=args['primary_file_id']
            with store.connection() as db:db.execute('UPDATE alli_work SET context=%s WHERE id=%s',(Jsonb(job['context']),job['id']))
        return result
    if call.name=='read_public_resource':
        url=args['url']
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as http:
            for _ in range(4):
                u=urlsplit(url)
                if u.scheme!='https' or u.hostname not in HOSTS or u.username or u.password or u.port not in (None,443) or any(p in u.path.lower() for p in ('wp-admin','wp-json','xmlrpc')):
                    raise ValueError('Use public site/media/font URLs; private endpoints are unavailable')
                async with http.stream('GET',url) as response:
                    if response.is_redirect:
                        from urllib.parse import urljoin
                        url=urljoin(url,response.headers['location']);continue
                    response.raise_for_status();data=bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data)>8*1024*1024:raise ValueError('Public resource exceeds 8 MB')
                    mime=response.headers.get('content-type','application/octet-stream').split(';')[0]
                    suffix=Path(u.path).suffix[:10] or ('.html' if 'html' in mime else '.bin')
                    filename='public-'+hashlib.sha256(url.encode()).hexdigest()[:12]+suffix
                    saved=await client.containers.files.create(job['context']['wordpress_container_id'],file=(filename,bytes(data),mime))
                    return dict(url=url,status=response.status_code,path=getattr(saved,'path',''),file_id=saved.id,content_type=mime,sha256=hashlib.sha256(data).hexdigest())
        raise ValueError('Too many public redirects')
    if call.name=='inspect_work_images':
        ids=args['image_ids']
        if not 1<=len(ids)<=6:raise ValueError('Inspect one to six actual images')
        uploaded=[]
        try:
            content=[dict(type='input_text',text=args['question'][:6000])]
            for fid in ids:
                item=artifact(job,fid)
                if item['content_type'] not in {'image/png','image/jpeg','image/webp'}:raise ValueError('Supply actual image artifacts')
                f=await client.files.create(file=(item['filename'],item['content'],item['content_type']),purpose='vision');uploaded.append(f.id)
                content.append(dict(type='input_image',file_id=f.id,detail='high'))
            result=await client.responses.create(model=MODEL,reasoning=dict(REASONING),input=[dict(role='user',content=content)],max_output_tokens=3000)
            with store.connection() as db:jobs.event(db,job['id'],'artifact_review',dict(image_ids=ids,response_id=result.id,review=result.output_text))
            return dict(review=result.output_text,response_id=result.id)
        finally:
            for fid in uploaded:
                try:await client.files.delete(fid)
                except Exception:pass
    raise ValueError('Unknown handoff support tool')
