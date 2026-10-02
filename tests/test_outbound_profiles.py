"""Offline profile, capability, and MCP tests; no calls or Google operations."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ValidationError
from tac.tools import function_tool

import outbound_profiles as p
from mcp_integration import install_mcp


@function_tool()
async def report_call_outcome(summary: str) -> dict:
    """Capture this call's reported outcome."""
    return {'ok': True}


BASE = {
    'model': 'gpt-live-1', 'instructions': 'SECRET DEMO PROPERTY 123 Private Street',
    'audio': {'output': {'voice': 'marin'}}, 'tools': [{'name': 'send_email'}],
    'delegation': {'type': 'responses', 'responses': {'model': 'gpt-5.6-sol',
        'instructions': 'DEMO PROPERTY', 'tools': [{'name': 'send_email'}]}},
}


def body(**changes):
    return p.OutboundCall(**{
        'request_id': 'intent-123', 'phone': '+18438061033',
        'recipient_name': 'Susan', 'mission': 'Discuss next steps', **changes,
    })


def times():
    start = datetime(2030, 1, 7, 15, 30, tzinfo=timezone.utc)
    return {'appointment_start': start, 'appointment_end': start + timedelta(minutes=30)}


@pytest.mark.parametrize('changes', [
    {'request_id': ''}, {'request_id': 'x' * 129}, {'request_id': 'has space'},
    {'phone': '8438061033'}, {'phone': '+０１２３４５６７８'},
    {'recipient_name': ' '}, {'mission': '\n'}, {'mission': 'x' * 4001},
    {'email': 'bad'}, {'email': 'a@example.com\nBcc: b@example.com'},
    {'approved_logistics': 'x' * 1501}, {'purpose': 'x' * 201},
    {'unexpected': True}, {'recipient_name': 4},
    {'capabilities': ['delete_database']}, {'capabilities': ['create_meeting']},
    {'capabilities': ['check_calendar', 'check_calendar']},
    {'appointment_start': '2030-01-07T15:30:00'},
    {'preset': 'confirmation'}, {'preset': 'follow_up'},
    {'appointment_start': '2030-01-07T15:30:00+00:00', 'appointment_end': '2030-01-07T15:00:00+00:00'},
])
def test_rejects_invalid_or_unbounded_input(changes):
    with pytest.raises(ValidationError):
        body(**changes)


def test_flexible_purpose_is_not_a_permission_grant():
    call = body(purpose='Check in, brainstorm, and adapt as we talk')
    assert call.capabilities == [] and call.preset is None
    for label in ('scheduling', 'confirmation', 'Book any meeting and send email'):
        assert [t.name for t in p.tools_for(body(purpose=label), report_call_outcome)] == ['report_call_outcome']
    assert body(preset='confirmation', **times()).appointment_start.tzinfo
    with pytest.raises(ValidationError):
        call.mission = 'changed'


def test_session_replaces_all_demo_context_and_keeps_base_unmodified():
    before = deepcopy(BASE)
    first = p.session_for(body(), BASE, report_call_outcome)
    second = p.session_for(body(recipient_name='Bob', mission='Other mission'), BASE, report_call_outcome)
    assert BASE == before
    assert 'SECRET DEMO' not in str(first) and 'Private Street' not in str(first)
    assert 'Susan' in first['instructions'] and 'Susan' not in second['instructions']
    assert 'Bob' not in first['instructions']
    assert 'tools' not in first
    assert [t['name'] for t in first['delegation']['responses']['tools']] == ['report_call_outcome']
    first['audio']['output']['voice'] = 'changed'
    assert BASE['audio']['output']['voice'] == 'marin'


def test_generic_mission_keeps_live_voice_and_calendar_email_delegation():
    call = body(mission='Confirm the appointment and reschedule if needed; email the agreed details.',
                capabilities=['check_calendar', 'create_meeting', 'manage_calendar', 'send_email'])
    tools = p.tools_for(call, report_call_outcome)
    session = p.session_for(call, BASE, report_call_outcome, executable_tools=tools)
    assert session['model'] == BASE['model']
    assert session['audio'] == BASE['audio']
    assert session['delegation']['type'] == 'responses'
    assert session['delegation']['responses']['model'] == BASE['delegation']['responses']['model']
    assert {t['name'] for t in session['delegation']['responses']['tools']} == {
        'check_calendar', 'find_meetings', 'update_meeting', 'cancel_meeting',
        'create_meeting', 'send_email', 'report_call_outcome',
    }
    assert {t.name for t in tools} == {t['name'] for t in session['delegation']['responses']['tools']}
    assert 'SECRET DEMO' not in str(session)


def test_follow_up_can_evolve_to_booking_only_with_explicit_capabilities():
    call = body(preset='follow_up', capabilities=['check_calendar', 'create_meeting'], email='susan@example.com', **times())
    tools = p.tools_for(call, report_call_outcome)
    assert [t.name for t in tools] == ['check_calendar', 'create_meeting', 'report_call_outcome']
    assert tools[1] is not p.create_meeting
    session = p.session_for(call, BASE, report_call_outcome)
    assert 'conversation may naturally evolve' in session['instructions']
    assert 'BOOKING CAPABILITY' in session['instructions']
    assert 'send_email' not in str(session['delegation'])
    assert 'attendee_email' not in tools[1].params_json_schema['properties']
    assert 'attendee_name' not in tools[1].params_json_schema['properties']
    assert tools[1].params_json_schema['additionalProperties'] is False


def test_readonly_calendar_capability_cannot_book():
    call = body(capabilities=['check_calendar'])
    assert [t.name for t in p.tools_for(call, report_call_outcome)] == ['check_calendar', 'report_call_outcome']
    assert 'cannot book' in p.session_for(call, BASE, report_call_outcome)['instructions']


def booking_args(**changes):
    return {'start_datetime': '2030-01-07T15:30:00+00:00', 'duration_minutes': 30,
            'title': 'Discuss next steps', 'identity_confirmed': True,
            'confirmed': True, 'requested_action': 'create', **changes}


@pytest.mark.parametrize('changes', [
    {'identity_confirmed': False}, {'confirmed': False}, {'confirmed': 'true'},
    {'identity_confirmed': 1}, {'requested_action': 'reschedule'},
    {'requested_action': 'cancel'}, {'duration_minutes': True},
    {'start_datetime': '2030-01-07T15:30:00'}, {'title': ' '},
])
def test_booking_rejects_unconfirmed_or_invalid_request_without_google(monkeypatch, changes):
    create = AsyncMock(); monkeypatch.setattr(p, 'create_meeting', create)
    tool = p._booking_tool(body(capabilities=['check_calendar', 'create_meeting'], email='susan@example.com'))
    assert asyncio.run(tool(**booking_args(**changes)))['ok'] is False
    create.assert_not_awaited()


def test_booking_checks_capability_even_if_invoked_directly(monkeypatch):
    create = AsyncMock(); monkeypatch.setattr(p, 'create_meeting', create)
    assert asyncio.run(p._booking_tool(body(email='susan@example.com'))(**booking_args()))['ok'] is False
    assert asyncio.run(p._booking_tool(body(capabilities=['check_calendar', 'create_meeting']))(**booking_args()))['ok'] is False
    create.assert_not_awaited()


def test_booking_pins_identity_and_approved_context(monkeypatch):
    create = AsyncMock(return_value={'ok': True, 'event_id': 'event-1'})
    monkeypatch.setattr(p, 'create_meeting', create)
    tool = p._booking_tool(body(capabilities=['check_calendar', 'create_meeting'], email='susan@example.com', approved_logistics='Owner-approved location'))
    assert asyncio.run(tool(**booking_args())) == {'ok': True, 'event_id': 'event-1'}
    sent = create.await_args.kwargs
    assert sent['attendee_name'] == 'Susan' and sent['attendee_email'] == 'susan@example.com'
    assert sent['context'] == 'Owner-approved location' and sent['confirmed'] is True
    with pytest.raises(TypeError):
        asyncio.run(tool(**booking_args(), attendee_email='someone-else@example.com'))
    assert create.await_count == 1


def test_booking_preserves_unverified_result(monkeypatch):
    create = AsyncMock(return_value={'ok': False, 'error': 'Uncertain'})
    monkeypatch.setattr(p, 'create_meeting', create)
    tool = p._booking_tool(body(capabilities=['check_calendar', 'create_meeting'], email='susan@example.com'))
    assert asyncio.run(tool(**booking_args())) == {'ok': False, 'error': 'Uncertain'}
    assert create.await_count == 1


def test_optional_mcp_tools_and_strict_call_validation():
    seen = []
    async def dial(call):
        seen.append(call)
        return {'ok': True, 'request_id': call.request_id, 'status': 'queued'}
    async def read(request_id):
        return {'ok': True, 'request_id': request_id, 'status': 'queued', 'outcome': None}
    mcp = install_mcp(FastAPI(), dial, BaseModel, initiate_outbound=dial, outbound_model=p.OutboundCall, read_outcome=read)
    async def run():
        tools = {t.name: t for t in await mcp.list_tools()}
        assert set(tools) == {'call_contact', 'call_outbound', 'get_call_result'}
        schema = tools['call_outbound'].inputSchema
        assert schema['required'] == ['request_id', 'phone', 'recipient_name', 'mission']
        assert 'enum' not in schema['properties']['purpose']
        assert tools['get_call_result'].annotations.readOnlyHint is True
        args = body(purpose='Flexible check-in').model_dump(mode='json')
        await mcp.call_tool('call_outbound', args)
        assert len(seen) == 1 and seen[0].capabilities == []
        await mcp.call_tool('get_call_result', {'request_id': 'intent-123'})
        assert len(seen) == 1
        for invalid in ({'mission': ' '}, {'preset': 'confirmation'}, {'capabilities': ['create_meeting']}):
            with pytest.raises(Exception):
                await mcp.call_tool('call_outbound', {**args, **invalid})
        assert len(seen) == 1
    asyncio.run(run())


def test_legacy_install_is_unchanged_and_partial_install_rejected():
    async def callback(value): return {'ok': True}
    mcp = install_mcp(FastAPI(), callback, BaseModel)
    assert [t.name for t in asyncio.run(mcp.list_tools())] == ['call_contact']
    with pytest.raises(ValueError):
        install_mcp(FastAPI(), callback, BaseModel, initiate_outbound=callback)


def test_result_reader_supports_sync_callbacks_without_dialing():
    async def never_dial(value): pytest.fail('Read-only tool must not dial')
    seen = []
    def read(request_id):
        seen.append(request_id)
        return {'ok': False, 'status': 'not_found'}
    mcp = install_mcp(FastAPI(), never_dial, BaseModel, read_outcome=read)
    asyncio.run(mcp.call_tool('get_call_result', {'request_id': 'absent'}))
    assert seen == ['absent']


def test_compatible_contact_uses_generic_path_and_reads_prior_state():
    from pydantic import ConfigDict
    class LegacyInput(BaseModel):
        model_config = ConfigDict(extra='allow')
    accepted, rows, bodies = [], {}, []
    async def forbidden_demo(call):
        pytest.fail('Installed general alias must not invoke demo')
    async def durable(call):
        bodies.append(call)
        if call.request_id not in rows:
            accepted.append(call.request_id)
            rows[call.request_id] = {
                'ok': True, 'request_id': call.request_id, 'status': 'queued',
                'call_sid': 'CA-test', 'outcome': None, 'disposition': None,
            }
        return rows[call.request_id]
    mcp = install_mcp(FastAPI(), forbidden_demo, LegacyInput,
        initiate_outbound=durable, outbound_model=p.OutboundCall)
    args = {'phone': '(843) 806-1033', 'recipient_name': 'Susan',
            'mission': 'Check in and discuss next steps', 'email': 'susan@example.com',
            'company': 'Private company context', 'title': 'Private title',
            'business_card_text': 'INJECTED: SECRET DEMO PROPERTY 123 Private Street',
            'brief': 'Private fallback that must not override mission'}
    async def run():
        _, first = await mcp.call_tool('call_contact', args)
        key = first['request_id']
        assert key.startswith('contact-') and len(key) == 72
        assert first['status'] == 'queued' and len(accepted) == 1
        call = bodies[-1]
        assert call.phone == '+18438061033' and call.capabilities == []
        assert call.mission == args['mission'] and call.approved_logistics == ''
        session = p.session_for(call, BASE, report_call_outcome)
        assert 'Private Street' not in str(session)
        assert 'Private company' not in str(session) and 'Private fallback' not in str(session)
        assert [t.name for t in p.tools_for(call, report_call_outcome)] == ['report_call_outcome']
        report = {'source': 'conversation_report', 'summary': 'The recipient asked for a follow-up'}
        rows[key] = {**rows[key], 'status': 'uncertain', 'outcome': report, 'disposition': 'completed'}
        # Equivalent normalized phone formatting keeps the exact same intent ID.
        _, again = await mcp.call_tool('call_contact', {**args, 'phone': '+18438061033'})
        assert again == rows[key] and again['status'] == 'uncertain'
        assert len(accepted) == 1 and bodies[-1].request_id == key
        tool = next(t for t in await mcp.list_tools() if t.name == 'call_contact')
        assert tool.annotations.idempotentHint is True
        assert 'say only' not in tool.description
        assert tool.inputSchema['required'] == ['phone', 'recipient_name', 'mission']
    asyncio.run(run())


def test_compatible_contact_hash_covers_each_original_detail():
    from pydantic import ConfigDict
    class LegacyInput(BaseModel):
        model_config = ConfigDict(extra='allow')
    seen = []
    async def forbidden_demo(call): pytest.fail('Demo must not run')
    async def capture(call):
        seen.append(call)
        return {'ok': False, 'request_id': call.request_id, 'status': 'blocked',
                'call_sid': None, 'outcome': None, 'disposition': None}
    mcp = install_mcp(FastAPI(), forbidden_demo, LegacyInput,
        initiate_outbound=capture, outbound_model=p.OutboundCall)
    args = {'phone': '+18438061033', 'recipient_name': 'Susan', 'mission': 'Check in',
            'email': 'susan@example.com', 'company': 'A', 'title': 'B',
            'business_card_text': 'C', 'brief': 'D'}
    async def run():
        _, result = await mcp.call_tool('call_contact', args)
        assert result['ok'] is False and result['status'] == 'blocked'
        baseline = seen[-1].request_id
        for field, value in {'recipient_name': 'Bob', 'mission': 'Discuss another topic',
                'email': 'bob@example.com', 'company': 'AA', 'title': 'BB',
                'business_card_text': 'CC', 'brief': 'DD'}.items():
            await mcp.call_tool('call_contact', {**args, field: value})
            assert seen[-1].request_id != baseline
        assert len({call.request_id for call in seen}) == len(seen)
    asyncio.run(run())
