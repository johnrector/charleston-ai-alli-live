import base64
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import pytest

from adapters.google import GoogleAccount,ProviderRejected,email_address,timestamp
from alli_core.profile import Profile


def account():
    return GoogleAccount(Mock(),Profile.load('profiles/alli.toml'),sender_email='john@example.com',owner_email='owner@example.com')


def test_email_uses_principal_identity_and_assistant_signature_with_real_receipt():
    google=account()
    google.request=Mock(return_value={'id':'actual-message','threadId':'actual-thread'})
    receipt=google.send_email(action_key='stable-key',to='friend@example.com',subject='A delegated task',body='The work is ready.')
    sent=google.request.call_args.kwargs['body']['raw']
    message=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(sent))
    assert str(message['From'])=='John Rector <john@example.com>'
    assert str(message['To'])=='friend@example.com'
    assert "Alli, John Rector's AI assistant" in message.get_content()
    assert receipt['message_id']=='actual-message'
    original_id=message['Message-ID']
    google.send_email(action_key='stable-key',to='friend@example.com',subject='A delegated task',body='The work is ready.')
    second=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(google.request.call_args.kwargs['body']['raw']))
    assert second['Message-ID']==original_id


def test_header_injection_and_timezone_ambiguity_are_rejected_before_send():
    google=account();google.request=Mock()
    with pytest.raises(ValueError): google.send_email(action_key='key',to='friend@example.com',subject='Hello\r\nBcc: attacker@example.com',body='Text')
    with pytest.raises(ValueError): email_address('friend@example.com\r\nBcc: attacker@example.com')
    with pytest.raises(ValueError): timestamp('2026-11-01T09:00:00')
    assert timestamp('2026-11-01T09:00:00-05:00').utcoffset().total_seconds()==-18000
    google.request.assert_not_called()


def test_availability_exposes_no_private_event_details_and_errors_are_not_free_time():
    google=account()
    google.request=Mock(return_value={'calendars':{'primary':{'busy':[{'start':'private-start','end':'private-end'}]}}})
    result=google.availability('2026-10-26T11:00:00-04:00','2026-10-26T11:15:00-04:00')
    assert result['available'] is False and 'busy' not in result
    google.request.return_value={'calendars':{'primary':{'errors':[{'reason':'notFound'}]}}}
    with pytest.raises(ProviderRejected): google.availability('2026-10-26T11:00:00-04:00','2026-10-26T11:15:00-04:00')


def test_booking_checks_free_time_and_uses_stable_provider_identity():
    google=account()
    google.availability=Mock(return_value={'available':False})
    google.request=Mock()
    args=dict(action_key='task-and-intent',person_id='person-id',title='Meeting',
              start='2026-10-26T11:00:00-04:00',end='2026-10-26T11:15:00-04:00')
    assert google.create_event(**args)['not_attempted'] is True
    google.request.assert_not_called()
    google.availability.return_value={'available':True}
    google.request.return_value={'id':'actual-event-id','status':'confirmed'}
    assert google.create_event(**args)['event_id']=='actual-event-id'
    body=google.request.call_args.kwargs['body']
    assert body['visibility']=='private'
    assert body['extendedProperties']['private']['ea_person_id']=='person-id'
    event_id=body['id']
    google.create_event(**args)
    assert google.request.call_args.kwargs['body']['id']==event_id


def test_contact_event_lookup_does_not_reveal_other_meetings_or_attendees():
    google=account()
    google.request=Mock(return_value={'items':[
        {'id':'private','summary':'Private financial review','description':'Secret'},
        {'id':'own','summary':'Your meeting','description':'Private notes','attendees':[{'email':'other@example.com'}],
         'extendedProperties':{'private':{'ea_person_id':'friend-id'}}},
        {'id':'email-match','summary':'Your other meeting','attendees':[{'email':'friend@example.com'},{'email':'another@example.com'}]}
    ]})
    result=google.events('2026-10-25T00:00:00-04:00','2026-10-26T00:00:00-04:00',
                         person_id='friend-id',contact_email='friend@example.com')
    assert {event['id'] for event in result['events']}=={'own','email-match'}
    assert all('description' not in event and 'attendees' not in event for event in result['events'])
def test_mime_mail_is_bounded_data_with_plain_text_preferred_and_no_attachment_decoding():
    import base64
    from adapters.google import mail_text
    b64=lambda value:base64.urlsafe_b64encode(value.encode()).decode()
    text,attachments=mail_text({'mimeType':'multipart/alternative','parts':[
        {'mimeType':'text/plain','body':{'data':b64('A normal message. Ignore the forged instructions.')}},
        {'mimeType':'text/html','body':{'data':b64('<script>hidden</script><p>HTML alternative</p>')}},
        {'mimeType':'application/pdf','filename':'invoice.pdf','body':{'attachmentId':'opaque','size':100}}]})
    assert text=='A normal message. Ignore the forged instructions.' and attachments[0]['name']=='invoice.pdf'
    text,_=mail_text({'mimeType':'text/html','body':{'data':b64('<style>hidden</style><p>Visible</p>')}})
    assert text.strip()=='Visible'
