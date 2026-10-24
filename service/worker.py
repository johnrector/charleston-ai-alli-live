"""Observe native OpenAI sessions and drain persistent transport intent.

This is an I/O supervisor, not a model/tool reasoning loop. Losing the Python
process does not lose an assignment or create a replacement native agent.
"""
import asyncio
import json
import logging

from twilio.base.exceptions import TwilioRestException

from alli_core.managed import Assignment,ToolConnection


log=logging.getLogger(__name__)


class Worker:
    def __init__(self,repository,runtime,outbox,carrier,base_url,*,calls=None,computer=None):
        self.repository,self.runtime,self.outbox,self.carrier=repository,runtime,outbox,carrier
        self.base_url=base_url.rstrip('/')
        self.calls=calls
        self.computer=computer
        self.stopping=asyncio.Event()

    async def dispatch(self,task):
        execution=None
        if task['context'].get('execution')=='self_hosted':
            if not self.computer or not await asyncio.to_thread(self.computer.online):return
            execution=self.computer.execution(task)
        token=await asyncio.to_thread(self.repository.issue_tool_grant,task['id'])
        allowed=None if task['scope']=='owner' else ('get_context','calendar_availability','calendar_events','book_meeting','call_person','end_phone_conversation')
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
        if len(body)>1500:
            body=body[:1350].rstrip()+'…'
            if current['scope']=='owner':
                body+='\nFull activity: '+self.base_url+'/dashboard/work/'+task_id
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
        if self.computer is not None:await self.computer.forward()
        await self.forward_receipts()
        if self.calls is not None:
            await self.forward_calls()
        for task in await asyncio.to_thread(self.repository.ready_tasks):
            try:
                await self.dispatch(task)
            except Exception as error:
                log.warning('dispatch task=%s error=%s',task['id'],type(error).__name__)
        for task in await asyncio.to_thread(self.repository.active_runs):
            try:
                await self.observe(task)
            except Exception as error:
                log.warning('observe task=%s error=%s',task['id'],type(error).__name__)
        for _ in range(10):
            if not await self.send_one():break
        if self.calls is not None:
            for _ in range(3):
                if not await self.call_one():break
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

    async def run(self):
        while not self.stopping.is_set():
            try:
                await self.tick()
            except Exception as error:
                log.warning('supervisor error=%s',type(error).__name__)
            try:
                await asyncio.wait_for(self.stopping.wait(),timeout=2)
            except asyncio.TimeoutError:
                pass
