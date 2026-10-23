import asyncio,json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import reminder_reasoning as r
import outbound_profiles as profiles


def test_return_visit_context_uses_high_flagship_and_saves_reasoned_question(monkeypatch):
    context=dict(prior_scheduled_meetings=[{'location':'Charleston AI'}]*3,calendar_history_complete=True,recent_conversation_history=[{'body':'See you Friday'}])
    monkeypatch.setattr(r,'context_for',lambda _:context)
    create=AsyncMock(return_value=SimpleNamespace(output_text=json.dumps({'question':"What would you like John to focus on in this session?"})))
    client=SimpleNamespace(responses=SimpleNamespace(create=create))
    result=asyncio.run(r.draft_followup(dict(message='Facts. Reply here if anything changes.'),client))
    assert result['mode']=='contextual' and 'focus on' in result['message'] and 'directions' not in result['message']
    assert create.call_args.kwargs['model']=='gpt-6-astra'
    assert create.call_args.kwargs['reasoning']=={'effort':'high'}
    assert json.loads(create.call_args.kwargs['input'])==context
    assert 'not proof of attendance' in create.call_args.kwargs['instructions']


def test_draft_failure_keeps_facts_without_canned_questions(monkeypatch):
    monkeypatch.setattr(r,'context_for',lambda _: {})
    client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=TimeoutError())))
    result=asyncio.run(r.draft_followup(dict(message='Verified facts. Reply here if anything changes.'),client))
    assert result['mode']=='facts_only' and 'directions' not in result['message']


def test_session_preserves_high_reasoning_in_voice_delegation():
    from test_outbound_profiles import body
    base={'model':'gpt-live-1','delegation':{'responses':{'model':'gpt-6-astra','reasoning':{'effort':'high'}}}}
    from tac.tools import function_tool
    @function_tool(description='Record test outcome')
    async def result(summary:str)->dict:return {'ok':True}
    session=profiles.session_for(body(),base,result)
    assert session['model']=='gpt-live-1'
    assert session['delegation']['responses']['model']=='gpt-6-astra'
    assert session['delegation']['responses']['reasoning']=={'effort':'high'}
