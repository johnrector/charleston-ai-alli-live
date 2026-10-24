"""Phone transport only: signed callbacks, G.711 media, calls and messages."""
import asyncio
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

    async def dial(self,to,*,answer_path,status_path):
        if to == self.phone:
            raise ValueError('The assistant cannot call her own service number')
        # OpenAI handles the conversation, screening and voicemail. No AMD,
        # ConversationRelay, Twilio speech, or provider-side conversational agent.
        call=await asyncio.to_thread(self.client.calls.create,to=to,from_=self.phone,
            url=self.public_url(answer_path),method='POST',timeout=60,
            status_callback=self.public_url(status_path),status_callback_method='POST',
            status_callback_event=['initiated','ringing','answered','completed'])
        return {'ok':True,'provider':'twilio','call_sid':call.sid,
                'status':call.status,'to':call.to,'from':call._from}

    async def send(self,to,body,*,status_path):
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
                    return
                if event.get('event')=='media':
                    media=event['media']
                    if media.get('track') not in {None,'inbound'}:
                        continue
                    payload=media['payload']
                    if not isinstance(payload,str) or len(payload)>65536:
                        raise ValueError('Invalid audio packet')
                    yield payload
        except WebSocketDisconnect:
            return

    async def play(self,base64_audio):
        if self.closed:
            return
        if not self.stream_sid:
            raise ValueError('Media handshake required')
        await self.socket.send_json({'event':'media','streamSid':self.stream_sid,
                                     'media':{'payload':base64_audio}})

    async def close(self):
        if not self.closed:
            self.closed=True
            try:
                await self.socket.close()
            except (RuntimeError,WebSocketDisconnect):
                pass
