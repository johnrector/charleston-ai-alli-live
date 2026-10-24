"""Observe OpenAI's primary Live event stream; leave audio and turn timing intact.

The pinned TAC provider supplies transport. This narrow provider extension
retains OpenAI transcript timing and enforces application usage budgets.
"""
import asyncio
import logging
from tac.channels.voice.media_streams.gpt_live.config import GPTLiveProviderConfig
from tac.channels.voice.media_streams.gpt_live.provider import GPTLiveProvider
import security_controls as security
import communication_store as store

log=logging.getLogger('alli.live')

def audit(sid,phone,rid,status):
    with store.connection() as db:
        store.notify(db,'live:'+str(rid)+':'+status,dict(source='voice_delegation',sid=sid,phone=phone,response_id=rid,status=status,summary='OpenAI reasoning backend '+status+' a call step'))

def capture(metadata,event):
    kind=event.get('type')
    if kind not in ('session.input_transcript.delta','session.output_transcript.delta'):return
    text=event.get('delta')
    if not isinstance(text,str):return
    fragments=metadata.setdefault('live_transcript_fragments',[])
    if len(fragments)>=20000 or metadata.get('captured_characters',0)+len(text)>200000:
        metadata['transcript_truncated']=True;return
    fragments.append(dict(role='user' if kind=='session.input_transcript.delta' else 'assistant',text=text,
      start_ms=event.get('start_ms'),end_ms=event.get('end_ms')))
    metadata['captured_characters']=metadata.get('captured_characters',0)+len(text)

class AlliLiveProvider(GPTLiveProvider):
    async def _dispatch_model_event(self,conv_id,session,event):
        capture(session.metadata,event)
        inner=event.get('event') or {}
        if event.get('type')=='response.event':
            response=inner.get('response') or {};rid=response.get('id')
            ref='voice:'+str(session.call_sid)+':'+str(rid)
            if inner.get('type')=='response.created' and rid:
                try:
                    await asyncio.to_thread(security.model,self.config.bound_phone,ref,8192)
                except security.Limited:
                    session.metadata['budget_blocked']=True
                    await self._model_send(conv_id,dict(type='session.instructions.append',delegation_id=None,event_id='budget_notice',content='This call reached its application usage ceiling. Briefly explain that John can review it in his dashboard. Do not claim further work started.'))
                    await self._model_send(conv_id,dict(type='session.close'))
                    return
                await asyncio.to_thread(audit,session.call_sid,self.config.bound_phone,rid,'started')
            if inner.get('type') in ('response.completed','response.failed','response.incomplete') and rid:
                await asyncio.to_thread(security.settle_model,ref,(response.get('usage') or {}).get('output_tokens'))
                await asyncio.to_thread(audit,session.call_sid,self.config.bound_phone,rid,inner['type'].split('.')[-1])
            if session.metadata.get('budget_blocked') and inner.get('type')=='response.output_item.done':return
        await super()._dispatch_model_event(conv_id,session,event)

class AlliLiveConfig(GPTLiveProviderConfig):
    bound_phone:str
    def create_provider(self,channel,tac_config):return AlliLiveProvider(channel,tac_config,self)

VOICE_RULES='''You are Alli, John Rector's personal AI assistant. Speak warmly, naturally and concisely; use a little wit when it fits, then be useful. Open with the supplied greeting, then listen. Never ask saved friends to repeat an identity ritual.
Backchannel policy: Use moderate, unobtrusive backchannels in a personal conversation. In a group, listen quietly and avoid competing with participants.
Interruption policy: Stop your answer and listen when interrupted. A spoken interruption does not itself cancel an accepted assignment.
Delegation policy:
Backend tools: live calendar availability, this contact's permitted appointments, messages and reminders, durable cloud research and finished files, owner-authorized account work, group notes and individual action items. Only the configured tools are actually available; application permissions apply.
Generate <|SpawnThinking|> when: a request needs one of these capabilities, careful reasoning, a corrected task, or a real completion check. When a group is introduced, enter group-note mode with the backend immediately, announce note-taking and respect objections. At a request for respective action items, ask the backend to send them to approved participants. Offer to take on feasible work; execute only once delegated.
Do not generate <|SpawnThinking|> when: a greeting, brief clarification, passing joke without an assignment, or a still-current verified result suffices. A playful imperative is still an assignment: after a short quip, request useful permitted work and deliver a finished plan, draft or file. Do not wait for the caller to say "yes, start" again.
Generate <|SpawnThinking|> before any claim that depends on backend work. A spoken promise is not a saved task. Never guess an outcome. Say queued, completed and delivered accurately.
In a group, respond when addressed or when your contribution is useful; otherwise keep listening. Use explicitly introduced names and assignments; do not identify people from voices. Private owner tools are unavailable on a shared line; John can delegate that work by text. Do not repeat private earlier context to a group.
Friends may request useful research, planning and files. Their bills, taxes, spending and private accounts are not John's. Offer real help with humor, without pretending to pay or physically do chores. A friend's restaurant reservation needs John's approval before committing.
Use only the shareable public background for general questions about John. Unknown contacts need John's approval for calendar access or work. Recognition is a greeting, not private owner authorization. Follow the actual verification result without recurring approval for routine tasks.'''
