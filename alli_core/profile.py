"""The executive assistant's identity is configuration, not infrastructure."""
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    principal: str
    organization: str
    timezone: str
    charter: str
    voice_model: str = 'gpt-live-1'
    work_model: str = 'gpt-6.1-sol'
    reasoning_effort: str = 'high'
    voice: str = 'marin'
    email_sender: str = ''
    public_facts: str = ''
    facts_reviewed_at: str = ''

    @classmethod
    def load(cls, path: str | Path):
        with Path(path).open('rb') as source:
            return cls(**tomllib.load(source))
