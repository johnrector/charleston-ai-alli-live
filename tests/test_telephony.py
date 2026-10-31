import asyncio
from types import SimpleNamespace as NS
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest
from twilio.request_validator import RequestValidator

from adapters.telephony import Carrier,MediaChannel


def carrier():
    client=Mock()
    client.calls.create.return_value=NS(sid='CA-test',status='queued',to='+18435550101',_from='+18545550100')
    return Carrier(client,account_sid='AC-test',auth_token='test-signing-key',phone='+18545550100',base_url='https://alli.example.com')


def test_carrier_validates_the_configured_public_origin_and_all_parameters():
    transport=carrier()
    fields={'AccountSid':'AC-test','From':'+18435550101','To':'+18545550100','Body':'Do this work'}
    signature=RequestValidator('test-signing-key').compute_signature('https://alli.example.com/inbound/sms',fields)
    assert transport.validate('/inbound/sms',fields,signature)
    assert not transport.validate('/inbound/sms',{**fields,'From':'+18435550102'},signature)
    ws_signature=RequestValidator('test-signing-key').compute_signature('wss://alli.example.com/phone/media/call',{})
    assert transport.validate('/phone/media/call',{},ws_signature,websocket=True)
    assert not transport.validate('/phone/media/call',{},ws_signature)


def test_outgoing_call_is_transport_without_machine_detection_or_twilio_speech():
    transport=carrier()
    receipt=asyncio.run(transport.dial('+18435550101',answer_path='/phone/answer/call',status_path='/phone/status/call'))
    assert receipt['call_sid']=='CA-test' and receipt['from']=='+18545550100'
    arguments=transport.client.calls.create.call_args.kwargs
    assert arguments['timeout']==60
    assert 'machine_detection' not in arguments and 'async_amd' not in arguments
    assert 'twiml' not in arguments and 'record' not in arguments
    response=ElementTree.fromstring(transport.stream_twiml('/phone/media/call'))
    assert response.find('./Connect/Stream').attrib['url']=='wss://alli.example.com/phone/media/call'
    assert response.find('.//Say') is None and response.find('.//Gather') is None


def test_sms_receipt_uses_the_actual_sdk_message_resource():
    from twilio.rest.api.v2010.account.message import MessageInstance
    transport=carrier()
    transport.client.messages.create.return_value=MessageInstance(None,{
        'sid':'SM-test','status':'queued','to':'+18435550101','from':'+18545550100'},account_sid='AC-test')
    receipt=asyncio.run(transport.send('+18435550101','Finished work',status_path='/phone/sms-status/test'))
    assert receipt['message_sid']=='SM-test' and receipt['from']=='+18545550100'
    assert transport.client.messages.create.call_args.kwargs['body']=='Finished work'


def test_sms_wire_rejection_happens_before_carrier_api():
    from adapters.errors import InvalidInput
    transport=carrier()
    with pytest.raises(InvalidInput):
        asyncio.run(transport.send('+18435550101','“'*1100,status_path='/phone/sms-status/test'))
    transport.client.messages.create.assert_not_called()


class Socket:
    def __init__(self):self.incoming=asyncio.Queue();self.sent=[];self.closed=False
    async def receive_json(self):return await self.incoming.get()
    async def send_json(self,event):self.sent.append(event)
    async def close(self):self.closed=True


def start(call='CA-test'):
    return {'event':'start','start':{'accountSid':'AC-test','callSid':call,'streamSid':'MZ-test',
        'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}}


@pytest.mark.asyncio
async def test_media_preserves_pcmu_audio_and_binds_the_signed_call():
    socket=Socket();channel=MediaChannel(socket,account_sid='AC-test',call_sid='CA-test')
    await socket.incoming.put({'event':'connected'})
    await socket.incoming.put(start())
    await channel.handshake()
    await socket.incoming.put({'event':'media','streamSid':'MZ-test','media':{'track':'inbound','payload':'input-pcmu'}})
    await socket.incoming.put({'event':'stop','streamSid':'MZ-test'})
    assert [audio async for audio in channel.audio()]==['input-pcmu']
    await channel.play('output-pcmu')
    assert socket.sent==[{'event':'media','streamSid':'MZ-test','media':{'payload':'output-pcmu'}}]
    await channel.close()
    assert socket.closed
    wrong=Socket();await wrong.incoming.put(start('CA-someone-else'))
    with pytest.raises(ValueError,match='identity mismatch'):
        await MediaChannel(wrong,account_sid='AC-test',call_sid='CA-test').handshake()


def test_voicemail_acoustic_detection_is_asynchronous_and_bound_to_signed_callback_path():
    transport=carrier()
    asyncio.run(transport.dial('+18435550101',answer_path='/answer/test',status_path='/status/test',voicemail_path='/transport/amd/test'))
    args=transport.client.calls.create.call_args.kwargs
    assert args['machine_detection']=='DetectMessageEnd' and args['async_amd']=='true'
    assert args['async_amd_status_callback']=='https://alli.example.com/transport/amd/test'
    assert args['async_amd_status_callback_method']=='POST'
