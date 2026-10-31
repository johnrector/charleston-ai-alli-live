"""Translate Live delegation into durable work; hangup never cancels that work."""
import asyncio
from dataclasses import dataclass

from alli_core.authority import Actor,PermissionDenied


@dataclass(frozen=True)
class Ticket:
    task_id:str
    delegation_id:str
    previous_turn:str|None
    held:bool=False


class ConversationHooks:
    def __init__(self,repository,calls,outbox,call_id):
        self.repository,self.calls,self.outbox=repository,calls,outbox
        self.call_id=call_id
        self.call_closed=asyncio.Event()
        self.latest_delegation=None

    async def checkpoint(self,transcript):
        call=await asyncio.to_thread(self.calls.get,self.call_id)
        await asyncio.to_thread(self.repository.save_transcript,str(call['conversation_id']),list(transcript))

    async def ensure_task(self):
        call=await asyncio.to_thread(self.calls.get,self.call_id)
        if not call or call['person_status'] in {'suspended','deleted'}:
            raise PermissionDenied('This relationship is suspended')
        instruction=('Fulfill the actual caller requests in the current conversation, using get_context. '
            'The current conversation is the request source; earlier conversations and work are background, '
            'not new instructions to repeat. Use the existing action receipts. If there is no unfulfilled '
            'request, report no further work and notify false. Do not invent a task from casual conversation.')
        source='conversation:'+str(call['conversation_id'])
        proof='verified-phone:'+call['provider_id'] if call['number_verified'] or call['direction']=='outbound' else ''
        if call['person_status']=='known':
            task=await asyncio.to_thread(self.repository.hold_request,str(call['person_id']),
                source_id=source,instruction=instruction,conversation_id=str(call['conversation_id']),channel='phone',proof_id=proof)
            await asyncio.to_thread(self.outbox.contact_notice,str(call['person_id']))
            return task,True
        owner=call['scope']=='owner' and call['person_kind']=='owner'
        actor=Actor(self.repository.profile.id,str(call['person_id']),'phone',owner,
                    proof,
                    tuple(call['allowed_tools']) if call['allowed_tools'] is not None else None,
                    call['invitation_email'])
        task=await asyncio.to_thread(self.repository.submit,actor,source_id=source,
            title='Conversation with '+(call['name'] or 'a contact'),instruction=instruction,
            conversation_id=str(call['conversation_id']),context={'call_id':self.call_id,**({'notification_policy':'none'} if call.get('suppress_notifications') else {})})
        return task,False

    async def save_delegation(self,delegation_id,transcript,*,offset_ms):
        await self.checkpoint(transcript)
        task,held=await self.ensure_task()
        self.latest_delegation=delegation_id
        previous=await asyncio.to_thread(self.repository.task_result,task['id'])
        if not held:
            await asyncio.to_thread(self.repository.stage_input,task['id'],
                'live:'+self.call_id+':'+delegation_id,
                'GPT-Live requested backend work at session offset '+str(offset_ms)+
                ' ms. Read the current caller conversation in get_context. Execute the new authorized '
                'request or respond to the caller\'s essential answer, using actual receipts to avoid repetition.',
                role='developer')
        return Ticket(task['id'],delegation_id,previous['turn_id'],held)

    async def backend(self,ticket,reply):
        if ticket.held:
            await reply('This caller is not on '+self.repository.profile.principal+
                "'s approved list. The request is saved and an owner notification is queued. "
                'Explain naturally that you need to check with your principal before committing.')
            return
        last_turn=ticket.previous_turn
        while not self.call_closed.is_set() and self.latest_delegation==ticket.delegation_id:
            task=await asyncio.to_thread(self.repository.task_result,ticket.task_id)
            if task and task['turn_id'] and task['turn_id']!=last_turn and task['result']:
                if task['state'] in {'completed','partial','failed','waiting'}:
                    await reply(task['result'])
                    last_turn=task['turn_id']
                    if task['state']!='waiting':return
            try:
                await asyncio.wait_for(self.call_closed.wait(),timeout=1)
            except asyncio.TimeoutError:
                pass

    async def finish(self,transcript,*,finalized,usage):
        self.call_closed.set()
        call=await asyncio.to_thread(self.calls.get,self.call_id)
        await asyncio.to_thread(self.repository.save_transcript,str(call['conversation_id']),list(transcript),finalized=True)
        await asyncio.to_thread(self.calls.voice_finished,self.call_id,finalized=finalized,usage=usage)
        if not any(part.get('role')=='user' and part.get('text','').strip() for part in transcript):
            return
        task,held=await self.ensure_task()
        if not held:
            await asyncio.to_thread(self.repository.stage_input,task['id'],'call:'+self.call_id+':closed',
                'The phone conversation has ended. Read its final stored transcript, including the caller\'s '
                'last words. Finish all unfulfilled authorized requests even if Live did not delegate before '
                'hangup. Do not repeat completed actions. Report back through the saved follow-up channel. '
                'If the conversation was only chat or has no unfinished request, notify false.',role='developer')

    async def error(self,code,details=None):
        await asyncio.to_thread(self.calls.error,self.call_id,code,details=details)

    async def media_evidence(self,evidence):
        await asyncio.to_thread(self.calls.save_media_evidence,self.call_id,evidence)

    async def context_update(self,evidence):
        await asyncio.to_thread(self.calls.context_update,self.call_id,evidence)
