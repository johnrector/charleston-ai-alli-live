"""Phone transport only: signed callbacks, G.711 media, calls and messages."""
import asyncio
import base64
from uuid import uuid4
from datetime import datetime,timezone
from urllib.parse import urlsplit, urlunsplit

from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse


class Carrier:
    def __init__(self,client,*,account_sid,auth_token,phone,base_url):
        origin=urlsplit(base_url)
        if origin.scheme!='https' or not origin.hostname or origin.username or origin.password:
            raise ValueError('A public HTTPS service origin is required')
        self.client,self.account_sid,self.phone=client,account_sid,phone
        self.base_url=base_url.rstrip('/')
        self.validator=RequestValidator(auth_token)

    def public_url(self,path,query='',*,websocket=False):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Expected an absolute service path')
        base=urlsplit(self.base_url)
        return urlunsplit(('wss' if websocket else 'https',base.netloc,path,query,''))

    def validate(self,path,params,signature,query='',*,websocket=False):
        if not websocket and params.get('AccountSid') != self.account_sid:
            return False
        url=self.public_url(path,query,websocket=websocket)
        return self.validator.validate(url,params,signature)

    def stream_twiml(self,media_path):
        response=VoiceResponse()
        response.connect().stream(url=self.public_url(media_path,websocket=True))
        return str(response)

    async def dial(self,to,*,answer_path,status_path,voicemail_path=None):
        if to == self.phone:
            raise ValueError('The assistant cannot call her own service number')
        # Acoustic readiness runs asynchronously, so a human connects to Live
        # immediately. It does not authorize a message or act as a voice agent.
        detection=({'machine_detection':'DetectMessageEnd','async_amd':'true',
            'async_amd_status_callback':self.public_url(voicemail_path),
            'async_amd_status_callback_method':'POST'} if voicemail_path else {})
        call=await asyncio.to_thread(self.client.calls.create,to=to,from_=self.phone,
            url=self.public_url(answer_path),method='POST',timeout=60,
            status_callback=self.public_url(status_path),status_callback_method='POST',
            status_callback_event=['initiated','ringing','answered','completed'],**detection)
        return {'ok':True,'provider':'twilio','call_sid':call.sid,
                'status':call.status,'to':call.to,'from':call._from}

    async def send(self,to,body,*,status_path):
        from .sms import validate
        validate(body)
        if to == self.phone:
            raise ValueError('The assistant cannot message her own service number')
        message=await asyncio.to_thread(self.client.messages.create,to=to,from_=self.phone,
            body=body,status_callback=self.public_url(status_path))
        return {'ok':True,'provider':'twilio','message_sid':message.sid,'status':message.status,
                'to':message.to,'from':message.from_}

    async def call_details(self,sid):
        call=await asyncio.to_thread(self.client.calls(sid).fetch)
        return {'call_sid':call.sid,'status':call.status,'duration':call.duration,
                'to':call.to,'from':call._from}

    async def end(self,sid):
        # Transport deadline only: an uncertain completion is never retried.
        async with asyncio.timeout(10):
            call=await asyncio.to_thread(self.client.calls(sid).update,status='completed')
        return {'ok':True,'provider':'twilio','call_sid':call.sid,'status':call.status}

    async def active(self):
        queries=[asyncio.to_thread(self.client.calls.list,status=status,limit=100,**{endpoint:self.phone})
                 for status in ('queued','ringing','in-progress') for endpoint in ('to','from_')]
        return {call.sid for calls in await asyncio.gather(*queries) for call in calls}


class MediaChannel:
    """Twilio supplies and plays bytes. OpenAI owns timing and interruptions."""
    def __init__(self,websocket,*,account_sid,call_sid):
        self.socket,self.account_sid,self.call_sid=websocket,account_sid,call_sid
        self.stream_sid=None
        self.closed=False
        self.output_lock=asyncio.Lock()
        self.playback_epoch=0
        self.marks={}
        self.on_inbound_audio=None
        self.allow_live_output=None
        self.statistics={'inbound_packets':0,'inbound_base64_chars':0,'live_output_packets':0,
            'finite_audio_bytes':0,'clears_sent':0,'marks_sent':0,'marks_acknowledged':0,'marks_invalidated':0}
        self.last_finite_playback={}

    def evidence(self):
        return {**self.statistics,'last_finite_playback':dict(self.last_finite_playback)}

    def cancel_marks(self):
        self.playback_epoch+=1
        self.statistics['marks_invalidated']+=len(self.marks)
        for _,future in self.marks.values():
            if not future.done():future.set_result(False)
        self.marks.clear()

    async def clear(self):
        async with self.output_lock:
            self.cancel_marks()
            if not self.closed and self.stream_sid:
                await self.socket.send_json({'event':'clear','streamSid':self.stream_sid})
                self.statistics['clears_sent']+=1

    async def play_finite(self,audio,*,permitted=lambda:True):
        if not isinstance(audio,bytes) or not audio:return False
        async with self.output_lock:
            if self.closed or not self.stream_sid or not permitted():return False
            self.cancel_marks()
            await self.socket.send_json({'event':'clear','streamSid':self.stream_sid})
            self.statistics['clears_sent']+=1
            name='voicemail_'+uuid4().hex
            future=asyncio.get_running_loop().create_future()
            epoch=self.playback_epoch
            self.marks[name]=(epoch,future)
            try:
                for offset in range(0,len(audio),4000):
                    if not permitted():self.cancel_marks();return False
                    await self.socket.send_json({'event':'media','streamSid':self.stream_sid,
                        'media':{'payload':base64.b64encode(audio[offset:offset+4000]).decode()}})
                    self.statistics['finite_audio_bytes']+=len(audio[offset:offset+4000])
                if not permitted():self.cancel_marks();return False
                await self.socket.send_json({'event':'mark','streamSid':self.stream_sid,'mark':{'name':name}})
                self.statistics['marks_sent']+=1
            except Exception:
                self.cancel_marks();raise
        # This is a media-sized transport deadline, not a silence/hangup timer.
        # Timeout is never treated as played and never authorizes hangup.
        try:
            played=await asyncio.wait_for(asyncio.shield(future),len(audio)/8000+10)
            if played and epoch==self.playback_epoch and not self.closed:
                self.last_finite_playback={'mark_name':name,'epoch':epoch,'audio_bytes':len(audio),
                    'acknowledged_at':datetime.now(timezone.utc).isoformat(),'played':True}
                return True
            return False
        except asyncio.TimeoutError:
            self.marks.pop(name,None)
            if not future.done():future.set_result(False)
            return False

    async def handshake(self):
        async with asyncio.timeout(10):
            while True:
                event=await self.socket.receive_json()
                if event.get('event')=='connected':
                    continue
                if event.get('event')!='start':
                    raise ValueError('Expected media start')
                start=event['start']
                if start.get('accountSid')!=self.account_sid or start.get('callSid')!=self.call_sid:
                    raise ValueError('Media identity mismatch')
                if start.get('mediaFormat')!={'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}:
                    raise ValueError('Unsupported phone audio format')
                self.stream_sid=start['streamSid']
                return

    async def audio(self):
        if not self.stream_sid:
            raise ValueError('Media handshake required')
        try:
            while not self.closed:
                event=await self.socket.receive_json()
                if event.get('streamSid') not in {None,self.stream_sid}:
                    raise ValueError('Media stream mismatch')
                if event.get('event')=='stop':
                    self.cancel_marks();return
                if event.get('event')=='mark':
                    item=self.marks.pop(event.get('mark',{}).get('name'),None)
                    if item:
                        epoch,future=item
                        if not future.done():
                            valid=epoch==self.playback_epoch and not self.closed
                            if valid:self.statistics['marks_acknowledged']+=1
                            future.set_result(valid)
                    continue
                if event.get('event')=='media':
                    media=event['media']
                    if media.get('track') not in {None,'inbound'}:
                        continue
                    payload=media['payload']
                    if not isinstance(payload,str) or len(payload)>65536:
                        raise ValueError('Invalid audio packet')
                    self.statistics['inbound_packets']+=1
                    self.statistics['inbound_base64_chars']+=len(payload)
                    if self.on_inbound_audio is not None:
                        await self.on_inbound_audio(payload)
                    yield payload
        except WebSocketDisconnect:
            self.cancel_marks();return

    async def play(self,base64_audio):
        if self.closed:
            return
        if not self.stream_sid:
            raise ValueError('Media handshake required')
        async with self.output_lock:
            if not self.closed and (self.allow_live_output is None or self.allow_live_output()):
                await self.socket.send_json({'event':'media','streamSid':self.stream_sid,
                                             'media':{'payload':base64_audio}})
                self.statistics['live_output_packets']+=1

    async def close(self):
        if not self.closed:
            self.closed=True
            self.cancel_marks()
            try:
                await self.socket.close()
            except (RuntimeError,WebSocketDisconnect):
                pass
