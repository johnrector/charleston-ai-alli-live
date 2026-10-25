import pytest
from pydantic import ValidationError

from adapters.errors import InvalidInput
from adapters.sms import segments, validate, result_message
from alli_core.outcome import Outcome


@pytest.mark.parametrize('body,count', [
    ('A'*160, 1), ('A'*161, 2), ('^'*80, 1), ('^'*81, 2),
    ('é'*160, 1), ('“'*70, 1), ('“'*71, 2), ('😀'*35, 1), ('😀'*36, 2),
])
def test_actual_encoding_not_python_character_count(body, count):
    assert segments(body) == count


def test_budget_includes_gsm_extension_and_surrogate_pairs():
    validate('A'*1224)
    validate('“'*536)
    validate('😀'*268)
    for body in ('A'*1225, '^'*613, '“'*537, '😀'*269, '\ud800', '', '   '):
        with pytest.raises(InvalidInput):
            validate(body)


def test_long_result_keeps_authored_notice_and_private_owner_url():
    full = 'A detailed update with typographic punctuation. “Verified.” '*30
    url = 'https://example.com/dashboard/work/123'
    body = result_message(full, 'The research is complete; one question remains.', owner_url=url)
    assert body == 'The research is complete; one question remains.\nFull update: '+url
    validate(body)
    assert result_message('Hi Blake!', '', owner_url=None) == 'Hi Blake!'
    assert result_message(full, 'Your research is ready.', owner_url=None) == 'Your research is ready.'


def test_older_sessions_and_oversized_notices_remain_sendable_without_false_success():
    full='“'*1100
    for notice in ('', '😀'*1000):
        owner=result_message(full, notice, owner_url='https://example.com/dashboard/work/123')
        friend=result_message(full, notice, owner_url=None)
        validate(owner); validate(friend)
        assert 'dashboard' in owner and 'dashboard' not in friend
        assert 'completed' not in owner and 'completed' not in friend


def test_outcome_supports_legacy_reports_and_bounds_new_notice():
    old=dict(status='completed', summary='Full useful result', remaining=[], notify=True)
    assert Outcome(**old).notification == ''
    schema=Outcome.format()['schema']
    assert set(schema['required'])==set(schema['properties'])
    assert 'default' not in schema['properties']['notification']
    assert Outcome(**old, notification='The result is ready.').summary == old['summary']
    with pytest.raises(ValidationError):
        Outcome(**old, notification='A'*161)
