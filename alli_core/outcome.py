"""A finished model turn is not necessarily finished delegated work."""
from typing import Literal
from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True)
class FinalizedOutcome:
    state: str
    summary: str


class Outcome(BaseModel):
    model_config=ConfigDict(extra='forbid')
    status: Literal['completed','partial','waiting']
    summary: str
    remaining: list[str]
    notify: bool

    @classmethod
    def format(cls):
        # Agents API's format schema differs from Responses: no name/strict.
        return {'type':'json_schema','schema':cls.model_json_schema()}
