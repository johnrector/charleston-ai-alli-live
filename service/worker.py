"""Observe native OpenAI sessions and drain persistent transport intent.

This is an I/O supervisor, not a model/tool reasoning loop. Losing the Python
process does not lose an assignment or create a replacement native agent.
"""
import asyncio
import json
import logging
import httpx

from twilio.base.exceptions import TwilioRestException

from alli_core.managed import Assignment,ToolConnection
from alli_core.authority import friend_tools
from adapters.sms import result_message
from adapters.errors import InvalidInput


log=logging.getLogger(__name__)


class Worker:
    def __init__(self,repository,runtime,outbox,carrier,base_url,*,calls=None,computer=None,release='',scheduler=None,text_conversation=None,people_workflow=None):
        self.repository,self.runtime,self.outbox,self.carrier=repository,runtime,outbox,carrier
        self.base_url=base_url.rstrip('/')
        self.calls=calls
        self.computer=computer
        self.release=release
        self.scheduler=scheduler
        self.people_workflow=people_workflow
        self.text_conversation=text_conversation
        self.conversation_tasks={}
        self.stopping=asyncio.Event()

    async def dispatch(self,task):
        if task['context'].get('native_schedule_setup'):return
        if task['context'].get('appointment_planner'):return
        if (self.text_conversation is not None and task['authority'].get('channel')=='sms'
                and not task['context'].get('backend_only')):
            await self.text_conversation.handle(task)
            return
        execution=None
        if task['context'].get('execution')=='self_hosted':
            if not self.computer or not await asyncio.to_thread(self.computer.online):return
            execution=self.computer.execution(task)
        token=await asyncio.to_thread(self.repository.issue_tool_grant,task['id'])
        allowed=None if task['scope']=='owner' else friend_tools(scheduled=task['authority'].get('channel')=='scheduled')
        constraints=task['authority'].get('allowed_tools')
        if constraints is not None:
            allowed=tuple(constraints) if allowed is None else tuple(tool for tool in allowed if tool in constraints)
        connection=ToolConnection(self.base_url+'/executive-tools','Bearer '+token,allowed)
        assignment=Assignment(task['id'],task['instruction'],
            'First read get_context for the authenticated requester, received date and relevant history. '
            'Finish the delegated work using authorized tools; routine work needs no further confirmation.')
        if execution is None:await self.runtime.dispatch(assignment,connection)
        else:await self.runtime.dispatch(assignment,connection,execution)

    async def observe(self,task):
        if task['context'].get('appointment_planner'):return
        if task['context'].get('native_schedule_setup'):return
        if task['context'].get('text_frontend'):return
        if not task['session_id']:
            await self.runtime.recover(Assignment(task['id'],task['instruction'],''))
            return
        inputs=await asyncio.to_thread(self.repository.pending_inputs,task['id'])
        for saved in inputs:
            body=json.loads(saved['body'])
            await self.runtime.follow_up(task['session_id'],body['text'],saved['id'],role=body['role'])
            await asyncio.to_thread(self.repository.input_sent,task['id'],saved['id'])
        await self.runtime.inspect(task['id'])

    async def report(self,current):
        if current['context'].get('internal_execution'):return
        task_id=current['id']
        # A queued provider action is not an essential question for the caller.
        if current['state']=='waiting' and await asyncio.to_thread(self.outbox.pending,task_id):
            return
        if current['state']=='waiting' and (current['context'].get('pending_calls')
                or current['context'].get('pending_computer_work')):
            return
        body=current['result'].strip()
        if not body:return
        owner_url=(self.base_url+'/dashboard/work/'+task_id) if current['scope']=='owner' else None
        body=result_message(body,current['context'].get('notification'),owner_url=owner_url)
        await asyncio.to_thread(self.outbox.queue_result,task_id,body,current['turn_id'])

    async def provider_update(self,row):
        """Forward receipts as evidence, never as a new caller authorization."""
        task=await asyncio.to_thread(self.repository.task_result,row['task_id'])
        if not task or task['state'] in {'cancelled','needs_owner'} or row['source_id'].startswith(('result:','guest-ack:')):
            return
        state=row['state']
        if task['state'] not in {'working','waiting'} and state not in {'failed','uncertain'}:
            return
        evidence={key:row[key] for key in ('id','channel','destination','state','provider_id')}
        text=('Provider receipt for your existing assignment, not a new request: '+json.dumps(evidence,default=str)+
            '\nRead get_context for the saved receipt details and finish any remaining work. Do not repeat the send. '
            'Acceptance is not delivery or proof that a human read the message. '
            'Report a failure or uncertainty to the requester; do not silently retry.')
        await asyncio.to_thread(self.repository.stage_input,row['task_id'],
            'delivery:'+row['id']+':'+state,text,role='developer')

    async def send_one(self):
        row=await asyncio.to_thread(self.outbox.claim_due)
        if not row:return False
        receipt=None
        try:
            receipt=await self.carrier.send(row['destination'],row['body'],
                status_path='/transport/sms-status/'+row['id'])
        except InvalidInput:
            # Includes legacy queued messages: no carrier request was made.
            await asyncio.to_thread(self.outbox.receipt,row['id'],None,'failed',
                {'ok':False,'not_attempted':True,'reason':'sms_wire_budget_exceeded'})
        except TwilioRestException as error:
            definitive=400<=error.status<500 and error.status!=429
            await asyncio.to_thread(self.outbox.receipt,row['id'],None,
                'failed' if definitive else 'uncertain',
                {'ok':False,'http_status':error.status,'provider_code':error.code})
        except Exception as error:
            # A lost send reply may already represent a real carrier message.
            await asyncio.to_thread(self.outbox.receipt,row['id'],None,'uncertain',
                {'ok':False,'error_type':type(error).__name__})
        if receipt is not None:
            await asyncio.to_thread(self.outbox.receipt,row['id'],receipt['message_sid'],'accepted',receipt)
        return True

    async def tick(self):
        await self.work_tick()
        await self.transport_tick()

    async def work_tick(self):
        if self.text_conversation is not None:await self.text_conversation.recover()
        if self.computer is not None:await self.computer.forward()
        await self.forward_receipts()
        if self.calls is not None:
            await self.forward_calls()
            await self.forward_conversation_work()
        for task in await asyncio.to_thread(self.repository.ready_tasks,exclude_sms=self.text_conversation is not None):
            try:
                await self.dispatch(task)
            except Exception as error:
                log.warning('dispatch task=%s error=%s',task['id'],type(error).__name__)
        for task in await asyncio.to_thread(self.repository.active_runs):
            try:
                await self.observe(task)
            except Exception as error:
                log.warning('observe task=%s error=%s',task['id'],type(error).__name__)

    async def conversation_tick(self):
        for identifier,job in list(self.conversation_tasks.items()):
            if job.done():
                self.conversation_tasks.pop(identifier)
                if not job.cancelled() and job.exception():
                    log.warning('text_conversation task=%s error=%s',identifier,type(job.exception()).__name__)
        capacity=8-len(self.conversation_tasks)
        if capacity<=0:return
        for task in await asyncio.to_thread(self.repository.ready_text_tasks,capacity):
            if task['id'] not in self.conversation_tasks:
                self.conversation_tasks[task['id']]=asyncio.create_task(self.text_conversation.handle(task))

    async def planner_tick(self):
        task=await asyncio.to_thread(self.people_workflow.claim_planner)
        if not task:return
        actor=await asyncio.to_thread(self.repository.owner_actor,'native-planner:'+task['id'])
        try:
            result=await self.people_workflow.plan(actor,task['id'])
        except Exception as error:
            from datetime import datetime,timedelta,timezone
            checked=datetime.now(timezone.utc)
            await asyncio.to_thread(self.people_workflow.save_coverage,[],checked,checked+timedelta(days=8),state='unavailable',reason='Calendar/planner unavailable: '+type(error).__name__)
            await asyncio.to_thread(self.repository.record_outcome,task['id'],'failed','Planner did not complete; saved coverage is stale.',turn_id='planner-failed',notify=False)
            return
        await asyncio.to_thread(self.repository.record_outcome,task['id'],'completed',json.dumps(result,default=str),turn_id='planner',notify=False)

    async def maintenance_tick(self):
        await asyncio.to_thread(self.people_workflow.deliver_preparation_waiters)
        await asyncio.to_thread(self.people_workflow.refresh_conversations)

    async def call_tick(self):
        if self.calls is not None:
            for _ in range(3):
                if not await self.call_one():break

    async def transport_tick(self,*,include_calls=True):
        for _ in range(10):
            if not await self.send_one():break
        if include_calls:await self.call_tick()
        await self.forward_receipts()
        for task in await asyncio.to_thread(self.repository.reportable_tasks):
            try:
                await self.report(task)
            except Exception as error:
                log.warning('report task=%s error=%s',task['id'],type(error).__name__)

    async def forward_receipts(self):
        for receipt in await asyncio.to_thread(self.outbox.unforwarded):
            try:
                await self.provider_update(receipt)
                await asyncio.to_thread(self.outbox.forwarded,receipt['id'],receipt['state'])
            except Exception as error:
                log.warning('forward delivery=%s error=%s',receipt['id'],type(error).__name__)

    async def call_one(self):
        call=await asyncio.to_thread(self.calls.claim)
        if not call:return False
        if self.people_workflow is not None:
            try:
                permitted=await asyncio.to_thread(self.people_workflow.pre_dial,call)
            except Exception as error:
                permitted=False
                log.warning('pre_dial call=%s held=%s',call['id'],type(error).__name__)
            if permitted is None:
                await asyncio.to_thread(self.calls.defer_before_dial,call['id'])
                return False
            if not permitted:
                await asyncio.to_thread(self.calls.cancel_before_dial,call['id'],'Fresh appointment safety checks did not pass')
                return True
        try:
            receipt=await self.carrier.dial(call['expected_to'],
                answer_path='/transport/answer/'+call['id'],status_path='/transport/call-status/'+call['id'])
        except Exception as error:
            await asyncio.to_thread(self.calls.uncertain,call['id'],type(error).__name__)
            return True
        await asyncio.to_thread(self.calls.bind,call['id'],receipt['call_sid'],
            to=receipt['to'],from_=receipt['from'],status=receipt['status'])
        return True

    async def forward_calls(self):
        for call in await asyncio.to_thread(self.calls.unreported):
            try:
                evidence={key:call[key] for key in ('id','state','carrier_status','duration_seconds','finalized')}
                # Raw dialogue stays in get_context, bounded separately. A carrier
                # status or answering-machine guess cannot supply a human answer.
                text=('Phone transport/conversation evidence for your existing mission, not a new authorization: '+
                    json.dumps(evidence,default=str)+
                    '\nRead get_context for the actual call dialogue. Determine what the person actually said '
                    'and finish the original assignment. No transcript means no confirmed conversation. '
                    'Do not redial or claim a meeting was confirmed from a carrier status alone.')
                await asyncio.to_thread(self.repository.stage_input,call['parent_task_id'],
                    'call:'+call['id']+':'+call['state']+(':final' if call['finalized'] else ':provisional'),text,role='developer')
                await asyncio.to_thread(self.calls.reported,call['id'])
            except Exception as error:
                log.warning('forward call=%s error=%s',call['id'],type(error).__name__)

    async def forward_conversation_work(self):
        # A call can end before the work delegated by its participant finishes.
        # Relay that later result once to the existing calling mission as data.
        for work in await asyncio.to_thread(self.repository.unforwarded_conversation_work):
            try:
                await asyncio.to_thread(self.repository.stage_input,work['parent_task_id'],
                    'conversation-result:'+work['id']+':'+work['turn_id'],
                    'Saved outcome of the work delegated during your completed call. '
                    'This is application evidence, not a new authorization. Do not redial or repeat actions. '
                    'Update the original mission using the actual saved outcome: '+
                    json.dumps({key:work[key] for key in ('id','title','state','result')},default=str),
                    role='developer')
            except Exception as error:
                log.warning('forward conversation work=%s error=%s',work['id'],type(error).__name__)

    async def run(self):
        if not await self.wait_for_public_release():return
        # A slow/blocked reasoning provider cannot delay an already scheduled
        # text, call intent or completion notification. Both use durable claims.
        loops=[self._poll(self.work_tick,2),self._poll(lambda:self.transport_tick(include_calls=False),1)]
        if self.calls is not None:loops.append(self._poll(self.call_tick,1))
        if self.people_workflow is not None:
            loops.extend([self._poll(self.maintenance_tick,10),self._poll(self.planner_tick,2)])
        if self.scheduler is not None:loops.append(self._poll(self.scheduler.reconcile,30))
        if self.text_conversation is not None:loops.append(self._poll(self.conversation_tick,0.25))
        try:await asyncio.gather(*loops)
        finally:
            if self.conversation_tasks:await asyncio.gather(*self.conversation_tasks.values(),return_exceptions=True)

    async def wait_for_public_release(self):
        if not self.release:return True
        # Render starts the replacement before switching public MCP traffic.
        # Do not dispatch a new release's work into the previous release's tools.
        async with httpx.AsyncClient(timeout=5,follow_redirects=False) as client:
            while not self.stopping.is_set():
                try:
                    response=await client.get(self.base_url+'/healthz',headers={'Cache-Control':'no-cache'})
                    if response.status_code==200 and response.json().get('release')==self.release:
                        log.warning('public_release_ready=%s',self.release)
                        return True
                except (httpx.HTTPError,ValueError):
                    pass
                try:await asyncio.wait_for(self.stopping.wait(),timeout=1)
                except asyncio.TimeoutError:pass
        return False

    async def _poll(self,step,interval):
        while not self.stopping.is_set():
            try:
                await step()
            except Exception as error:
                log.warning('supervisor error=%s',type(error).__name__)
            try:
                await asyncio.wait_for(self.stopping.wait(),timeout=interval)
            except asyncio.TimeoutError:
                pass
