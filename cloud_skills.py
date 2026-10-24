"""Private skill snapshots mounted into OpenAI's hosted execution environment."""
import asyncio
import hashlib
import io
import json
from pathlib import Path,PurePosixPath
import zipfile
from psycopg.types.json import Jsonb
import communication_store as store

ROOT=Path(__file__).parent/'work_skills'
_lock=asyncio.Lock()

def validate_bundle(data):
    if len(data)>20*1024*1024:raise ValueError('Skill archive must be under 20 MB')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names=z.namelist()
        if len(names)>10000 or sum(i.file_size for i in z.infolist())>80*1024*1024:raise ValueError('Skill archive exceeds expanded limits')
        if len(names)!=len(set(names)):raise ValueError('Duplicate archive paths')
        if any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts or '\\' in n or not n.startswith('john-skills/') for n in names):
            raise ValueError('Use a single john-skills folder without traversal paths')
        if any((i.external_attr>>16)&0o170000==0o120000 for i in z.infolist()):raise ValueError('Symlinks are not allowed')
        if 'john-skills/SKILL.md' not in names or 'john-skills/catalog.json' not in names:raise ValueError('Skill manifest and catalog are required')
        catalog=json.loads(z.read('john-skills/catalog.json'))
        if not isinstance(catalog,list) or len(catalog)>1000:raise ValueError('Invalid skill catalog')
        for entry in catalog:
            if not isinstance(entry,dict) or not all(isinstance(entry.get(k),str) for k in ('name','description','path')):raise ValueError('Invalid skill entry')
            if 'john-skills/'+entry['path'] not in names:raise ValueError('Skill entrypoint missing')
    return catalog

def import_bundle(data):
    catalog=validate_bundle(data);digest=hashlib.sha256(data).hexdigest()
    store.initialize()
    with store.connection() as db:
        db.execute('''INSERT INTO alli_work_skill(id,digest,bundle,catalog) VALUES(1,%s,%s,%s)
          ON CONFLICT(id) DO UPDATE SET digest=EXCLUDED.digest,bundle=EXCLUDED.bundle,catalog=EXCLUDED.catalog,
          provider_id=NULL,provider_version=NULL,updated_at=now() WHERE alli_work_skill.digest<>EXCLUDED.digest''',(digest,data,Jsonb(catalog)))
    return dict(ok=True,skill_count=len(catalog),digest=digest)

def snapshot():
    store.initialize()
    with store.connection() as db:
        row=db.execute('SELECT digest,bundle,catalog,provider_id,provider_version FROM alli_work_skill WHERE id=1').fetchone()
    if row:return dict(zip(('digest','bundle','catalog','provider_id','provider_version'),row))
    content=ROOT.joinpath('SKILL.md').read_bytes()
    catalog=[dict(name='alli-cloud-work',description='Research, writing, analysis and finished documents',path='SKILL.md')]
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('john-skills/SKILL.md',content);z.writestr('john-skills/catalog.json',json.dumps(catalog))
    import_bundle(buf.getvalue());return snapshot()

def catalog():
    value=snapshot()
    return dict(skills=value['catalog'],digest=value['digest'],hosted_version=value['provider_version'])

async def attachment(client):
    from openai import NotFoundError
    async with _lock:
        row=await asyncio.to_thread(snapshot)
        if not row['provider_id']:
            skill=await client.skills.create(files=('john-skills.zip',bytes(row['bundle']),'application/zip'))
            row['provider_id']=skill.id;row['provider_version']=str(skill.latest_version)
            with store.connection() as db:
                db.execute('UPDATE alli_work_skill SET provider_id=%s,provider_version=%s WHERE id=1 AND digest=%s',
                  (row['provider_id'],row['provider_version'],row['digest']))
        for attempt in range(6):
            try:
                await client.skills.versions.retrieve(row['provider_version'],skill_id=row['provider_id']);break
            except NotFoundError:
                if attempt==5:raise
                await asyncio.sleep(min(2**attempt,8))
        return dict(type='skill_reference',skill_id=row['provider_id'],version=row['provider_version'])
