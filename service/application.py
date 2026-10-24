"""Thin HTTP/media boundary around OpenAI-managed executive work."""
import asyncio
from contextlib import asynccontextmanager,suppress
import logging
import hmac
import json
from urllib.parse import urlencode

from fastapi import FastAPI,HTTPException,Request,WebSocket
from starlette.responses import Response,RedirectResponse,JSONResponse

from adapters.calls import CallRepository
from adapters.deliveries import DeliveryRepository
from adapters.executive_mcp import create_tool_server,tool_http_app
from adapters.owner_sessions import OwnerSessions
from adapters.owner_oauth import OwnerOAuth
from adapters.owner_mcp import create_owner_server,owner_http_app
from adapters.events import Events
from adapters.computer import Computer
from adapters.bootstrap import seed_existing_records,preserve_birthday
from adapters.telephony import MediaChannel
from alli_core.authority import Actor,Conflict,PermissionDenied
from alli_core.live import LiveConversation
from alli_core.managed import ManagedRuntime
from alli_core.prompts import voice_instructions
from .conversations import ConversationHooks
from .dashboard import dashboard_router,SESSION
from .web_helpers import form,page,escape
from .worker import Worker


log=logging.getLogger(__name__)


class ToolSurface:
    def __init__(self,app,*,tools,owner):
        self.app,self.tools,self.owner=app,tools,owner
        self.owner_paths={route.path for route in owner.routes if hasattr(route,'path')}

    async def __call__(self,scope,receive,send):
        if scope['type']=='http' and scope['path'].startswith('/executive-tools'):
            await self.tools(scope,receive,send)
        elif scope['type']=='http' and scope['path'] in self.owner_paths:
            await self.owner(scope,receive,send)
        else:
            await self.app(scope,receive,send)


def create_application(settings,repository,carrier,google,api):
    calls=CallRepository(repository,settings.phone)
    outbox=DeliveryRepository(repository)
    sessions=OwnerSessions(repository,settings.session_secret)
    runtime=ManagedRuntime(api,repository.profile,repository)
    computer=Computer.configured(repository,api)
    worker=Worker(repository,runtime,outbox,carrier,settings.base_url,calls=calls,computer=computer)
    tools=tool_http_app(create_tool_server(repository,settings.base_url,
        google=google,deliveries=outbox,calls=calls,carrier=carrier,computer=computer),settings.base_url)
    oauth=OwnerOAuth(repository,settings.base_url,settings.session_secret,settings.google_key.encode())
    events=Events(repository,settings.google_key.encode(),settings.base_url)
    owner_tools=owner_http_app(create_owner_server(repository,calls,outbox,oauth,events),settings.base_url)
    background=set()

    def spawn(coroutine):
        task=asyncio.create_task(coroutine)
        background.add(task)
        def finished(done):
            background.discard(done)
            if not done.cancelled() and done.exception():
                log.warning('background error=%s',type(done.exception()).__name__)
        task.add_done_callback(finished)
        return task

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(repository.initialize)
        await asyncio.to_thread(repository.bootstrap_owner,settings.owner_phone)
        if settings.background and settings.native_probe:
            from tools.probe_native_openai import agent_probe,live_probe
            brain,voice=await asyncio.gather(agent_probe(),live_probe())
            verified=(brain.get('managed_agent')=='completed' and brain.get('survived_client_disconnect')
                and any(call['status']=='completed' and not call['error'] for call in brain.get('mcp_calls',[]))
                and voice.get('live_finalized') and not voice.get('error_codes'))
            log.warning('native_readiness=%s',json.dumps({'verified':verified,'brain':brain,'voice':voice}))
            if not verified:raise RuntimeError('Production native OpenAI readiness failed; do not cut over traffic')
        if settings.background:
            if await carrier.active():raise RuntimeError('Cutover deferred: the assistant has an active phone call')
            from datetime import datetime,timedelta,timezone
            begin=datetime.now(timezone.utc)+timedelta(days=1)
            finish=begin+timedelta(minutes=15)
            await asyncio.to_thread(google.availability,begin.isoformat(),finish.isoformat())
            await asyncio.to_thread(google.events,begin.isoformat(),finish.isoformat())
            log.warning('google_calendar_readiness=verified')
        await asyncio.to_thread(seed_existing_records,repository)
        await asyncio.to_thread(preserve_birthday,repository)
        async with tools.router.lifespan_context(tools),owner_tools.router.lifespan_context(owner_tools):
            supervisor=spawn(worker.run()) if settings.background else None
            notifier=spawn(events.run()) if settings.background else None
            try:
                yield
            finally:
                worker.stopping.set()
                events.stopping.set()
                if supervisor:
                    try:await asyncio.wait_for(asyncio.shield(supervisor),timeout=20)
                    except asyncio.TimeoutError:supervisor.cancel()
                if background:
                    done,pending=await asyncio.wait(background,timeout=10)
                    for task in pending:task.cancel()
                    await asyncio.gather(*pending,return_exceptions=True)
                await api.close()

    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.repository,app.state.worker=repository,worker
    app.state.calls,app.state.outbox,app.state.sessions,app.state.oauth=calls,outbox,sessions,oauth
    app.state.events=events
    app.add_middleware(ToolSurface,tools=tools,owner=owner_tools)
    app.include_router(dashboard_router(settings,repository,sessions,carrier,outbox))

    @app.exception_handler(PermissionDenied)
    async def denied(request:Request,error):
        return page('Access unavailable','<h1>This action is unavailable.</h1><p>Use the owner dashboard for private account access.</p>',status=403)

    @app.exception_handler(Conflict)
    async def conflict(request:Request,error):
        return page('Record changed','<h1>This record changed.</h1><p>Reload the page before trying again.</p>',status=409)

    @app.exception_handler(ValueError)
    async def invalid(request:Request,error):
        return page('Check the request','<h1>Check the supplied details.</h1><p>Use a valid phone, date, or current record revision.</p>',status=400)

    async def signed(request):
        data=await form(request)
        if not carrier.validate(request.url.path,data,request.headers.get('x-twilio-signature',''),request.url.query):
            raise HTTPException(403,'Invalid carrier signature')
        return data

    def hangup():
        return Response('<Response><Hangup/></Response>',media_type='application/xml')

    def media_response(call):
        return Response(carrier.stream_twiml('/transport/media/'+call['id']),media_type='application/xml')

    @app.get('/healthz')
    @app.get('/health')
    async def health():
        return {'ok':True,'runtime':'native_openai','configured_voice_model':repository.profile.voice_model,
                'configured_work_model':repository.profile.work_model,'reasoning':repository.profile.reasoning_effort}

    @app.post('/executor/lease')
    async def lease(request:Request):
        if computer is None:raise HTTPException(404)
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>4096:raise HTTPException(413,'Request is too large')
        await asyncio.to_thread(computer.authenticate,bytes(body),request.headers,request.url.path)
        return JSONResponse(await computer.lease(),headers={'Cache-Control':'no-store'})

    @app.get('/mcp-owner/consent')
    async def consent(request:Request):
        nonce=request.query_params.get('request','')
        if not await oauth.get('pending',nonce):raise HTTPException(400,'Connection request expired')
        token=request.cookies.get(SESSION,'')
        if not await asyncio.to_thread(sessions.actor,token):
            destination='/mcp-owner/consent?'+urlencode({'request':nonce})
            return RedirectResponse('/dashboard/login?'+urlencode({'next':destination}),303)
        body='<h1>Connect Alli to your Dot.</h1><p>Allow your owner-only assistant connection to delegate executive work, communicate, schedule, and read actual outcomes. Friends do not receive this access.</p>'
        body+='<form method="post"><input type="hidden" name="request" value="'+escape(nonce)+'"><input type="hidden" name="csrf" value="'+escape(sessions.csrf(token))+'"><button>Authorize my assistant connection</button></form>'
        return page('Connect your assistant',body)

    @app.post('/mcp-owner/consent')
    async def approve_connection(request:Request):
        token=request.cookies.get(SESSION,'')
        if request.headers.get('origin')!=settings.base_url or not await asyncio.to_thread(sessions.actor,token):
            raise HTTPException(403,'Owner sign-in required')
        data=await form(request)
        if not hmac.compare_digest(data.get('csrf',''),sessions.csrf(token)):
            raise HTTPException(403,'Reload the connection form')
        return RedirectResponse(await oauth.consent(data.get('request','')),303,
                                headers={'Referrer-Policy':'no-referrer','Cache-Control':'no-store'})

    @app.post('/inbound/voice')
    async def incoming_voice(request:Request):
        data=await signed(request)
        try:call=await asyncio.to_thread(calls.incoming,data)
        except PermissionDenied:return hangup()
        relationship=await asyncio.to_thread(calls.get,call['id'])
        if relationship['person_status'] in {'suspended','deleted'} or data['From']==settings.phone:
            return hangup()
        return media_response(call)

    @app.post('/transport/answer/{identifier}')
    async def answer(request:Request,identifier:str):
        data=await signed(request)
        call=await asyncio.to_thread(calls.bind,identifier,data['CallSid'],to=data['To'],from_=data['From'],
                                     status=data.get('CallStatus','in-progress'))
        current=await asyncio.to_thread(calls.get,identifier)
        if current['person_status'] in {'suspended','deleted'}:return hangup()
        return media_response(call)

    @app.post('/transport/call-status/{identifier}')
    async def call_status(request:Request,identifier:str):
        data=await signed(request)
        await asyncio.to_thread(calls.bind,identifier,data['CallSid'],to=data['To'],from_=data['From'],
            status=data['CallStatus'],duration=data.get('CallDuration'))
        return Response(status_code=204)

    @app.post('/inbound/voice-status')
    async def incoming_status(request:Request):
        data=await signed(request)
        call=await asyncio.to_thread(calls.by_provider,data['CallSid'])
        if call:
            await asyncio.to_thread(calls.bind,call['id'],data['CallSid'],to=data['To'],from_=data['From'],
                status=data['CallStatus'],duration=data.get('CallDuration'))
        return Response(status_code=204)

    @app.websocket('/transport/media/{identifier}')
    async def media(socket:WebSocket,identifier:str):
        if not carrier.validate(socket.url.path,{},socket.headers.get('x-twilio-signature',''),
                                socket.url.query,websocket=True):
            await socket.close(code=1008);return
        call=await asyncio.to_thread(calls.get,identifier)
        if not call or call['person_status'] in {'suspended','deleted'}:
            await socket.close(code=1008);return
        await socket.accept()
        channel=MediaChannel(socket,account_sid=settings.account_sid,call_sid=call['provider_id'])
        try:
            await channel.handshake()
            await asyncio.to_thread(calls.attach_media,identifier,call['provider_id'])
            hooks=ConversationHooks(repository,calls,outbox,identifier)
            conversation=LiveConversation(api,repository.profile,channel,hooks,hooks.backend,spawn)
            context={'caller':{'name':call['name'],'status':call['person_status']},
                'scope':call['scope'],'direction':call['direction'],'mission':call['instruction'],
                'principal':repository.profile.principal,'public_facts':repository.profile.public_facts,
                'facts_reviewed_at':repository.profile.facts_reviewed_at,'conversation_budget_seconds':call['max_seconds']}
            # An unknown contact cannot consume an unlimited conversation.
            limit=call['max_seconds']
            async with asyncio.timeout(limit):
                await conversation.run(instructions=voice_instructions(repository.profile),context=context)
        except Exception as error:
            import traceback
            frame=traceback.extract_tb(error.__traceback__)[-1]
            from pathlib import Path
            diagnostic=type(error).__name__+'@'+Path(frame.filename).name+':'+str(frame.lineno)
            if isinstance(error,AttributeError) and error.name:diagnostic+=':'+error.name
            with suppress(Exception):await asyncio.to_thread(calls.error,identifier,diagnostic)
            await channel.close()

    @app.post('/inbound/sms')
    async def incoming_text(request:Request):
        data=await signed(request)
        if repository.normalize_phone(data['To'])!=repository.normalize_phone(settings.phone):
            raise HTTPException(403,'Wrong SMS destination')
        if data['From']==settings.phone:return Response('<Response/>',media_type='application/xml')
        body=data.get('Body','').strip()
        if not 1<=len(body)<=4000:raise HTTPException(400,'A text message is required')
        relationship=await asyncio.to_thread(repository.begin_conversation,data['From'],
            'sms:'+data['MessageSid'],'sms',owner_verified=True)
        person,conversation=relationship['person'],relationship['conversation']
        if person['status'] in {'suspended','deleted'}:
            return Response('<Response/>',media_type='application/xml')
        await asyncio.to_thread(repository.save_transcript,str(conversation['id']),
                                [{'role':'user','text':body}],finalized=True)
        if person['status']=='known':
            task=await asyncio.to_thread(repository.hold_request,str(person['id']),source_id='sms:'+data['MessageSid'],
                instruction=body,conversation_id=str(conversation['id']),channel='sms',proof_id='signed-sms:'+data['MessageSid'])
            await asyncio.to_thread(outbox.contact_notice,str(person['id']))
            await asyncio.to_thread(outbox.guest_acknowledgment,task['id'])
        else:
            verified=person['kind']=='owner'
            actor=Actor(repository.profile.id,str(person['id']),'sms',verified,
                        'signed-sms:'+data['MessageSid'])
            await asyncio.to_thread(repository.submit,actor,source_id='sms:'+data['MessageSid'],
                title=' '.join(body.split())[:100],instruction=body,conversation_id=str(conversation['id']))
        return Response('<Response/>',media_type='application/xml')

    @app.post('/transport/sms-status/{identifier}')
    async def sms_status(request:Request,identifier:str):
        data=await signed(request)
        state=data.get('MessageStatus') or data.get('SmsStatus')
        mapping={'queued':'accepted','accepted':'accepted','sending':'accepted','sent':'accepted',
                 'delivered':'delivered','undelivered':'failed','failed':'failed'}
        if state not in mapping:raise HTTPException(400,'Unknown message status')
        await asyncio.to_thread(outbox.receipt,identifier,data.get('MessageSid') or data['SmsSid'],
            mapping[state],{'status':state,'error_code':data.get('ErrorCode')})
        return Response(status_code=204)

    @app.post('/transport/auth-status/{identifier}')
    async def auth_status(request:Request,identifier:str):
        await signed(request)
        return Response(status_code=204)

    return app
