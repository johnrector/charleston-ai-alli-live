import asyncio
import importlib
from unittest.mock import AsyncMock
from types import SimpleNamespace


def test_two_calls_keep_their_own_context(monkeypatch):
    for key,value in {'TWILIO_ACCOUNT_SID':'AC'+'0'*32,'TWILIO_AUTH_TOKEN':'test','TWILIO_API_KEY':'SK'+'0'*32,'TWILIO_API_SECRET':'test','TWILIO_PHONE_NUMBER':'+15555550100','TWILIO_VOICE_PUBLIC_DOMAIN':'example.com','OPENAI_API_KEY':'test','DEMO_KEY':'test-admin'}.items():
        monkeypatch.setenv(key,value)
    app=importlib.import_module('app')
    captured=[]
    async def initiate(options):
        captured.append(options)
        await asyncio.sleep(0)
        return SimpleNamespace(call_sid='CA-test')
    monkeypatch.setattr(app.voice_channel,'initiate_outbound_conversation',initiate)
    async def calls():
        return await asyncio.gather(
            app.demo_call(app.DemoCall(to='+15555550101',name='Susan',email='susan@example.com',mission='Sell Mount Pleasant house'),'test-admin'),
            app.demo_call(app.DemoCall(to='+15555550102',name='Bob',email='bob@example.com',mission='Different mission'),'test-admin'))
    assert all(r['ok'] for r in asyncio.run(calls()))
    assert 'susan@example.com' in captured[0].session_config['instructions']
    assert 'susan@example.com' not in captured[1].session_config['instructions']
    assert 'bob@example.com' not in captured[0].session_config['instructions']
    assert 'susan@example.com' not in app.SESSION_CONFIG['instructions']
    assert 'Mount Pleasant' not in captured[1].session_config['instructions']
    assert 'Waterway Boulevard' not in str(captured[0].session_config)
    assert captured[0].session_config['model'] == 'gpt-live-1'
    assert captured[0].session_config['delegation']['type'] == 'responses'
    assert captured[0].session_config['delegation']['responses']['model'] == 'gpt-5.6-sol'
    assert [t['name'] for t in captured[0].session_config['delegation']['responses']['tools']]==['check_calendar','create_meeting','send_email']
