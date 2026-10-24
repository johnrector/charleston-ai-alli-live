"""Public article design and actual visual verification, without CMS access."""
import asyncio,hashlib
import wordpress_publish as publisher

NAMES={'article_artifacts','article_verify'}
def schemas(schema):
    return [schema('article_artifacts','Import actual generated HTML, screenshots and DOM audit from this article’s isolated cloud container. Returns real file IDs. No account access.',{},()),
      schema('article_verify','Verify the exact standalone HTML plus real desktop/320/390, complete 390-full.png and heading screenshots and DOM audit. Repair and rerun on failure. Required before saying the article is finished.',dict(source_id=dict(type='string'),screenshot_ids=dict(type='array',items=dict(type='string')),audit_id=dict(type='string')),('source_id','screenshot_ids','audit_id'))]

async def import_files(job,client):
    from pathlib import Path
    import work_store as jobs
    cid=job.get('context',{}).get('wordpress_container_id')
    if not cid:raise ValueError('This article needs its own isolated authoring container')
    listing=await client.containers.files.list(cid,limit=100)
    current=[]
    for f in listing.data[:100]:
        path=getattr(f,'path','') or ''
        if not path.startswith('/mnt/data/') or Path(path).suffix.lower() not in {'.html','.png','.json','.jpg','.jpeg','.webp'}:continue
        content=await client.containers.files.content.retrieve(f.id,container_id=cid)
        digest=hashlib.sha256(content.content).hexdigest();key=cid+':'+f.id+':'+digest
        await asyncio.to_thread(jobs.cache_file,job['id'],key,Path(path).name,content.content)
        with publisher.store.connection() as db:
            row=db.execute('SELECT id FROM alli_work_file WHERE work_id=%s AND provider_key=%s',(job['id'],key)).fetchone()
        current.append(dict(id=row[0],filename=Path(path).name,source_path=path,sha256=digest))
    return dict(current=current,files=await asyncio.to_thread(__import__('work_routes').files,job['id']))

async def execute(job,call,args,client):
    if not job.get('context',{}).get('public_article') or job['context'].get('wordpress_authoring'):raise ValueError('This is only for bound public standalone article jobs')
    if call.name=='article_artifacts':return await import_files(job,client)
    if call.name!='article_verify':raise ValueError('Unknown article action')
    source=publisher.artifact(job,args['source_id'])
    if source['content_type']!='text/html':raise ValueError('Supply the actual finished HTML file')
    html=source['content'].decode('utf-8');publisher.safe_source(html);publisher.engine.separate_json_ld(html)
    if '<style' not in html.lower() or '<article' not in html.lower():raise ValueError('Deliver an original styled semantic HTML article')
    prepared=dict(source_hash=hashlib.sha256(source['content']).hexdigest(),post=dict(title=job['title']))
    await publisher.visual(job,prepared,args['screenshot_ids'],args['audit_id'],client,phase='article')
    publisher.mark(job,article_verified=True,article_source_id=args['source_id'],article_source_hash=prepared['source_hash'])
    return dict(ok=True,status='verified_article',source_id=args['source_id'],destination='seven_day_result',expires_days=7)
