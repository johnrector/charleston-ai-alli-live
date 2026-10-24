"""Manual integration probe: signed media bridge, real OpenAI, no PSTN call.

Requires an existing unexpired owner dashboard sign-in. This privileged QA
fixture creates one two-minute grant bound to a random synthetic CallSid,
removes it on exit, and sends completed work only to the configured owner.
Never expose this script as an HTTP endpoint or run it in automatic tests.
"""
import argparse,asyncio,base64,contextlib,json,os,secrets,time
from pathlib import Path
from xml.etree import ElementTree
from urllib.parse import urlsplit
import httpx,websockets
from twilio.request_validator import RequestValidator
from psycopg.types.json import Jsonb
import communication_store as store
import owner_access

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--send-result-to-owner',action='store_true');args=parser.parse_args()
    if not args.send_result_to_owner:parser.error('This test sends one finished-work text to the owner; pass --send-result-to-owner explicitly.')
    store.initialize()
    sid='CA'+secrets.token_hex(16);stream='MZ'+secrets.token_hex(16)
    phone=store.normalize_phone(os.environ['ALLI_OWNER_PHONE'])
    domain=os.getenv('TWILIO_VOICE_PUBLIC_DOMAIN','charleston-ai-alli-gpt-live.onrender.com')
    root='https://'+domain;voice_url=root+'/inbound/voice'
    signer=RequestValidator(os.environ['TWILIO_AUTH_TOKEN'])
    form=dict(AccountSid=os.environ['TWILIO_ACCOUNT_SID'],To=os.environ['TWILIO_PHONE_NUMBER'],From=phone,CallSid=sid,CallStatus='in-progress',StirVerstat='TN-Validation-Passed-A')
    with store.connection() as db:
        assert db.execute('SELECT 1 FROM alli_dashboard_session WHERE expires_at>now() LIMIT 1').fetchone(),'An actual owner dashboard sign-in is required first'
        db.execute("INSERT INTO alli_owner_access(channel,binding,expires_at) VALUES('voice',%s,now()+interval '2 minutes')",(sid,))
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            r=await http.post(voice_url,data=form,headers={'X-Twilio-Signature':signer.compute_signature(voice_url,form)})
        assert r.status_code==200,'Voice webhook HTTP '+str(r.status_code)
        tag=ElementTree.fromstring(r.text).find('.//Stream')
        assert tag is not None,'Voice admission did not produce a media stream'
        url=tag.attrib['url'];assert urlsplit(url).hostname==domain
        with store.connection() as db:
            db.execute('UPDATE alli_inbound SET owner_verified=true,context=context||%s WHERE sid=%s',(Jsonb(dict(qa_synthetic=True,qa_description='Signed synthetic media bridge; no physical phone call')),sid))
        output_bytes=0
        async with websockets.connect(url,additional_headers={'X-Twilio-Signature':signer.compute_signature(url,{})},max_size=None,open_timeout=20) as ws:
            async def read():
                nonlocal output_bytes
                try:
                    async for message in ws:
                        event=json.loads(message)
                        if event.get('event')=='media':output_bytes+=len(base64.b64decode(event['media']['payload']))
                except websockets.ConnectionClosed:pass
            reader=asyncio.create_task(read())
            await ws.send(json.dumps(dict(event='start',sequenceNumber='1',start=dict(accountSid=form['AccountSid'],callSid=sid,streamSid=stream,tracks=['inbound'],mediaFormat=dict(encoding='audio/x-mulaw',sampleRate=8000,channels=1),customParameters={}))))
            audio=(Path(__file__).resolve().parents[1]/'tests/fixtures/phone-hangup.ulaw').read_bytes()+b'\xff'*4000
            start=time.monotonic()
            for n,offset in enumerate(range(0,len(audio),160)):
                await asyncio.sleep(max(0,start+n*.02-time.monotonic()))
                await ws.send(json.dumps(dict(event='media',sequenceNumber=str(n+2),streamSid=stream,media=dict(track='inbound',chunk=str(n+1),timestamp=str(n*20),payload=base64.b64encode(audio[offset:offset+160]).decode()))))
            await ws.send(json.dumps(dict(event='stop',streamSid=stream,stop=dict(accountSid=form['AccountSid'],callSid=sid))))
            print('HANGUP SENT',sid,'input_seconds',round(len(audio)/8000,1),flush=True)
            with contextlib.suppress(asyncio.TimeoutError):await asyncio.wait_for(reader,15)
        await asyncio.sleep(2)
        with store.connection() as db:
            row=db.execute('SELECT status,transcript,followthrough_ready FROM alli_inbound WHERE sid=%s',(sid,)).fetchone()
            jobs=db.execute("SELECT id,title,state,internal FROM alli_work WHERE context->>'source_sid'=%s OR context->>'call_recovery_sid'=%s ORDER BY created_at",(sid,sid)).fetchall()
        assert output_bytes>0,'No actual OpenAI voice audio returned'
        assert row and row[1] and row[2],'The signed voice session did not persist and finalize its transcript'
        print('VOICE PASS',json.dumps(dict(sid=sid,status=row[0],audio_output_bytes=output_bytes,transcript_fragments=len(row[1]),caller_excerpt=''.join(t.get('text','') for t in row[1] if t.get('role')=='user')[:650],assistant_excerpt=''.join(t.get('text','') for t in row[1] if t.get('role')=='assistant')[:650],work=[dict(id=w[0],title=w[1],state=w[2],internal=w[3]) for w in jobs])),flush=True)
    finally:owner_access.end_call(sid)

if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as exc:
        print('PROBE FAILED',type(exc).__name__,str(exc)[:160],flush=True)
        raise SystemExit(1)
