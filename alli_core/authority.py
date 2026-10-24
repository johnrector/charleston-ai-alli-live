"""Trusted application identity. Never deserialize an Actor from model arguments."""
from dataclasses import dataclass


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
