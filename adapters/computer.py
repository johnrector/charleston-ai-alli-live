"""Owner-only computer assignments and an outbound, signed Mac executor lease."""
import asyncio
import base64
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import time

from cryptography.hazmat.primitives.serialization import load_pem_public_key

from alli_core.authority import PermissionDenied
from alli_core.execution import Execution


class Computer:
    def __init__(self,repository,api,configuration):
        self.repository,self.api,self.config=repository,api,configuration
        self.public=load_pem_public_key(configuration['public_key'].encode())

    @classmethod
    def configured(cls,repository,api):
        configured=os.environ.get('ALLI_MAC_CONFIG_JSON')
        path=Path.home()/'.config/alli/mac.json'
        data=json.loads(configured) if configured else json.loads(path.read_text()) if path.is_file() else None
        return cls(repository,api,data) if data else None

    def queue(self,actor,task_id,instruction,source_id,plugins=()):
        catalog=self.config.get('plugins',{})
        if set(plugins)-set(catalog):raise ValueError('Use names from the configured Mac plugin catalog')
        with self.repository.connection() as db:
            self.repository._person(db,actor,owner=True)
            parent=self.repository._task(db,task_id)
            if (not parent or not self.repository._permitted(db,task_id,writes=False)
                or parent['context'].get('internal_execution') or parent['authority'].get('allowed_tools') is not None):
                raise PermissionDenied('Only an unrestricted owner assignment can delegate computer work')
        child=self.repository.submit(actor,source_id='computer:'+source_id[:180],title='Mac: '+parent['title'][:170],
            instruction=instruction,context={'internal_execution':True,'execution':'self_hosted','plugins':sorted(set(plugins))})
        with self.repository.connection() as db:
            db.execute('UPDATE ea_task SET parent_id=%s WHERE id=%s',(task_id,child['id']))
            db.execute('''UPDATE ea_task SET context=jsonb_set(context,'{pending_computer_work}',
                coalesce(context->'pending_computer_work','[]'::jsonb)||to_jsonb(%s::text)),updated_at=now() WHERE id=%s''',
                (child['id'],task_id))
        return {'ok':True,'task_id':child['id'],'state':'queued',
                'note':'Cloud-managed work waits for the Mac executor if it is offline. No completion is claimed.'}

    def online(self):
        with self.repository.connection() as db:
            return bool(db.execute('SELECT 1 FROM ea_device WHERE assistant_id=%s AND id=%s AND last_seen>now()-interval \'90 seconds\'',
                (self.repository.profile.id,self.config['id'])).fetchone())

    def execution(self,task):
        roots=tuple(self.config.get('plugins',{})[name] for name in task['context'].get('plugins',[]))
        return Execution('self_hosted',workspace=self.config['workspace'],plugin_roots=roots)

    def authenticate(self,body,headers,path):
        stamp,nonce=headers.get('x-alli-time',''),headers.get('x-alli-nonce','')
        if len(body)>4096 or not stamp.isdigit() or abs(time.time()-int(stamp))>60 or not 24<=len(nonce)<=128:
            raise PermissionDenied('Invalid device request')
        try:
            data=json.loads(body)
            if data!={'device_id':self.config['id']}:raise ValueError('Unknown device')
            message='POST\n'+path+'\n'+stamp+'\n'+nonce+'\n'+hashlib.sha256(body).hexdigest()
            self.public.verify(base64.b64decode(headers.get('x-alli-signature',''),validate=True),message.encode())
        except Exception as error:raise PermissionDenied('Device signature is invalid') from error
        with self.repository.connection() as db:
            db.execute('DELETE FROM ea_device_nonce WHERE expires_at<now()')
            row=db.execute('''INSERT INTO ea_device_nonce(assistant_id,nonce,expires_at)
                VALUES(%s,%s,now()+interval '2 minutes') ON CONFLICT DO NOTHING RETURNING nonce''',
                (self.repository.profile.id,nonce)).fetchone()
            if not row:raise PermissionDenied('Device request was already used')
            db.execute('''INSERT INTO ea_device(assistant_id,id) VALUES(%s,%s) ON CONFLICT(assistant_id,id)
                DO UPDATE SET last_seen=now()''',(self.repository.profile.id,self.config['id']))

    async def lease(self):
        tasks=await asyncio.to_thread(self.repository.active_runs)
        jobs=[]
        for task in tasks:
            if not task['context'].get('internal_execution') or not task['session_id'] or task['scope']!='owner':continue
            session=await self.api.beta.agents.sessions.retrieve(task['session_id'])
            if (session.status not in {'in_progress','requires_action','idle'} or session.environment.type!='self_hosted'
                or session.metadata.get('task_id')!=task['id']):continue
            jobs.append({'session_id':session.id,'environment_id':session.environment.id,
                         'remote_url':session.environment.remote_url,'workspace':self.config['workspace']})
        return {'jobs':jobs[:2]}

    async def forward(self):
        def finished():
            with self.repository.connection() as db:
                return db.execute('''SELECT t.*,r.turn_id FROM ea_task t JOIN ea_agent_run r ON r.task_id=t.id
                    WHERE t.assistant_id=%s AND t.parent_id IS NOT NULL
                    AND t.state IN ('completed','partial','failed') AND t.context->>'internal_execution'='true'
                    AND t.context->>'forwarded_turn' IS DISTINCT FROM r.turn_id''',(self.repository.profile.id,)).fetchall()
        for task in await asyncio.to_thread(finished):
            await asyncio.to_thread(self.repository.stage_input,task['parent_id'],
                'computer:'+task['id']+':'+task['turn_id'],
                'Computer work receipt for your existing assignment, not new authorization: '+
                json.dumps({'task_id':task['id'],'state':task['state'],'summary':task['result']})+
                '\nUse the actual result to finish the original assignment. Do not repeat completed computer work.',role='developer')
            def mark():
                with self.repository.connection() as db:
                    db.execute('''UPDATE ea_task SET context=jsonb_set(context,'{pending_computer_work}',
                        coalesce(context->'pending_computer_work','[]'::jsonb)-%s) WHERE id=%s''',(task['id'],task['parent_id']))
                    db.execute("UPDATE ea_task SET context=jsonb_set(context,'{forwarded_turn}',to_jsonb(%s::text)) WHERE id=%s",(task['turn_id'],task['id']))
            await asyncio.to_thread(mark)
