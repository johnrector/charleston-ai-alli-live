from datetime import datetime,timedelta,timezone
from unittest.mock import Mock

import pytest

from adapters.calendar import CalendarService
from adapters.google import InvalidInput
from adapters.postgres import PostgresRepository
from alli_core.authority import Actor,PermissionDenied
from alli_core.profile import Profile


def setup(database,proof):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'));repo.initialize()
    owner=Actor('alli',repo.bootstrap_owner('+18435550100'),'sms',True,'signed-sms:owner')
    person=repo.save_person(owner,name='A friend',phone='+18435550101',email='friend@example.com')
    repo.set_person_status(owner,str(person['id']),'approved',person['revision'])
    friend=Actor('alli',str(person['id']),'phone',False,proof)
    task=repo.submit(friend,source_id='friend-meeting',title='Meeting',instruction='Arrange my own meeting')
    google=Mock();google.create_event.return_value={'ok':True,'event_id':'actual-provider-event'}
    return repo,task,google,CalendarService(repo,google)


def dates(hour=15):
    day=datetime.now(timezone.utc)+timedelta(days=2)
    start=day.replace(hour=hour,minute=0,second=0,microsecond=0)
    return {'start':start.isoformat(),'end':(start+timedelta(minutes=15)).isoformat()}


def test_verified_friend_can_book_but_cannot_exceed_daily_ceiling(database):
    repo,task,google,calendar=setup(database,'verified-phone:actual-callback')
    for i in range(3):assert calendar.book(task['id'],action_key=str(i),title='My meeting',**dates())['ok']
    with pytest.raises(PermissionDenied):calendar.book(task['id'],action_key='fourth',title='Too many meetings',**dates())
    assert google.create_event.call_count==3
    assert google.create_event.call_args.kwargs['attendee_emails']==('friend@example.com',)


def test_number_recognition_does_not_expose_private_meetings_or_commit_booking(database):
    _,task,google,calendar=setup(database,'')
    with pytest.raises(PermissionDenied):calendar.lookup(task['id'],**dates())
    with pytest.raises(PermissionDenied):calendar.book(task['id'],action_key='one',title='My meeting',**dates())
    google.events.assert_not_called();google.create_event.assert_not_called()
