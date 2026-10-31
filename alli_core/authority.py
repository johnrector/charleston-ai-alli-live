"""Trusted application identity. Never deserialize an Actor from model arguments."""
from dataclasses import dataclass


def friend_tools(*,scheduled=False):
    base=('get_context','prepare_interaction','calendar_availability','calendar_events','book_meeting','call_person',
          'end_phone_conversation','scheduled_work')
    return base if scheduled else (*base,'schedule_work','manage_scheduled_work')


@dataclass(frozen=True)
class Actor:
    assistant_id: str
    person_id: str
    channel: str
    verified_owner: bool = False
    proof_id: str = ''
    allowed_tools: tuple[str,...] | None = None
    invitation_email: str = ''


class PermissionDenied(ValueError):
    pass


class Conflict(ValueError):
    pass
