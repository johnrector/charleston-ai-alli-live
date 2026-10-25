"""A finished model turn is not necessarily finished delegated work."""
from typing import Literal
from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class FinalizedOutcome:
    state: str
    summary: str


class Outcome(BaseModel):
    model_config=ConfigDict(extra='forbid')
    status: Literal['completed','partial','waiting']
    summary: str
    remaining: list[str]
    notify: bool = Field(description=(
        'Deliver the summary to the requester. For incoming SMS this is the '
        'conversational reply, including greetings and questions, not merely a '
        'work-completion alert. True unless the requester explicitly asked for '
        'silence or the reply was already sent through a verified message action. '
        'For phone calls with no outstanding request, or internal/read-only '
        'checks that require no notification, use false.'))

    @classmethod
    def format(cls):
        # Agents API's format schema differs from Responses: no name/strict.
        return {'type':'json_schema','schema':cls.model_json_schema()}
