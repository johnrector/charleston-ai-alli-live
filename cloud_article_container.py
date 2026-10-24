"""Mount the Publisher skill/resources in OpenAI's cloud authoring container.

This module packages bytes; it does not research, write, validate or publish posts.
"""
import asyncio,hashlib
from pathlib import Path
from psycopg.types.json import Jsonb
import communication_store as store
import wordpress_html as engine

def mark(job,**values):
    job['context']=dict(job.get('context') or {},**values)
    with store.connection() as db:db.execute('UPDATE alli_work SET context=%s WHERE id=%s',(Jsonb(job['context']),job['id']))

async def container(job,client):
    import io,zipfile
    kind='wordpress-publisher' if (job.get('context') or {}).get('wordpress_authoring') else 'article-studio'
    folder=Path(__file__).parent/'work_skills'/kind
    skill_source=(folder/'SKILL.md').read_bytes()
    renderer=(Path(__file__).parent/'work_skills'/'wordpress-publisher'/'render_article.py').read_bytes()
    html_engine=Path(engine.__file__).read_bytes()
    digest=hashlib.sha256(skill_source+b'\0'+renderer+b'\0'+html_engine).hexdigest()
    cid=(job.get('context') or {}).get('wordpress_container_id')
    if cid and (job.get('context') or {}).get('wordpress_bundle_digest')==digest:
        existing=await client.containers.retrieve(cid)
        if getattr(existing,'status','')!='expired':return cid
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('john-publisher/SKILL.md',skill_source)
        z.writestr('john-publisher/render_article.py',renderer)
        z.writestr('john-publisher/publisher_html.py',html_engine)
    from openai import NotFoundError
    skill_id=(job.get('context') or {}).get('wordpress_skill_id') if (job.get('context') or {}).get('wordpress_skill_digest')==digest else None
    version=(job.get('context') or {}).get('wordpress_skill_version')
    if not skill_id:
        skill=await client.skills.create(files=('john-publisher.zip',buf.getvalue(),'application/zip'))
        skill_id=skill.id;version=str(skill.latest_version)
        mark(job,wordpress_skill_id=skill_id,wordpress_skill_version=version,wordpress_skill_digest=digest)
    # Skill upload acknowledges storage before all authoring workers can see it.
    # Reuse the saved version and wait; never re-upload or weaken the sandbox.
    for attempt in range(7):
        try:
            await client.skills.versions.retrieve(version,skill_id=skill_id)
            created=await client.containers.create(name='alli-publisher-'+job['id'],memory_limit='1g',network_policy=dict(type='disabled'),
              skills=[dict(type='skill_reference',skill_id=skill_id,version=version)],expires_after=dict(anchor='last_active_at',minutes=20))
            break
        except NotFoundError:
            if attempt==6:raise
            await asyncio.sleep(min(2**attempt,8))
    if cid or (job.get("context") or {}).get("cloud_article_handoff"):
        with store.connection() as db:
            saved=db.execute("SELECT DISTINCT ON(filename) filename,content FROM alli_work_file WHERE work_id=%s AND content_type='text/html' ORDER BY filename,created_at DESC LIMIT 4",(job['id'],)).fetchall()
        for filename,content in saved:await client.containers.files.create(created.id,file=(filename,bytes(content),'text/html'))
    mark(job,wordpress_container_id=created.id,wordpress_bundle_digest=digest)
    return created.id

