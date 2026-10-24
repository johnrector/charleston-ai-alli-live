from dataclasses import dataclass,field
import os
from pathlib import Path

from alli_core.profile import Profile


@dataclass(frozen=True)
class Settings:
    base_url:str
    owner_phone:str
    phone:str
    account_sid:str
    database_url:str=field(repr=False)
    auth_token:str=field(repr=False)
    session_secret:str=field(repr=False)
    google_key:str=field(repr=False)
    owner_email:str=''
    sender_email:str=''
    background:bool=True
    native_probe:bool=False
    release:str=''

    @classmethod
    def from_environment(cls):
        domain=os.environ['TWILIO_VOICE_PUBLIC_DOMAIN'].removeprefix('https://').rstrip('/')
        return cls(base_url='https://'+domain,owner_phone=os.environ['ALLI_OWNER_PHONE'],
            phone=os.environ['TWILIO_PHONE_NUMBER'],account_sid=os.environ['TWILIO_ACCOUNT_SID'],
            database_url=os.environ['DATABASE_URL'],auth_token=os.environ['TWILIO_AUTH_TOKEN'],
            session_secret=os.environ['DEMO_KEY'],google_key=os.environ['GOOGLE_TOKEN_ENCRYPTION_KEY'],
            owner_email=os.environ['GOOGLE_OWNER_EMAIL'],
            sender_email=os.environ.get('GOOGLE_SENDER_EMAIL',os.environ['GOOGLE_OWNER_EMAIL']),
            native_probe=os.environ.get('ALLI_RUN_STARTUP_PROBE','').lower() in {'1','true'},
            release=os.environ.get('RENDER_GIT_COMMIT',''))


def load_profile():
    return Profile.load(Path(__file__).resolve().parents[1]/'profiles/alli.toml')
