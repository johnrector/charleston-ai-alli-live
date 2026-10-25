"""Production entrypoint. Models reason at OpenAI; this service handles I/O."""
import os
import logging

from openai import AsyncOpenAI
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from adapters.google import GoogleAccount,GoogleTokenStore
from adapters.postgres import PostgresRepository
from adapters.telephony import Carrier
from service.application import create_application
from service.settings import Settings,load_profile


def build_application():
    settings=Settings.from_environment()
    profile=load_profile()
    repository=PostgresRepository(settings.database_url,profile)
    username=os.environ.get('TWILIO_API_KEY') or settings.account_sid
    password=os.environ.get('TWILIO_API_SECRET') if os.environ.get('TWILIO_API_KEY') else settings.auth_token
    carrier=Carrier(Client(username,password,account_sid=settings.account_sid,
        http_client=TwilioHttpClient(timeout=15,max_retries=0)),account_sid=settings.account_sid,
        auth_token=settings.auth_token,phone=settings.phone,base_url=settings.base_url)
    logging.getLogger('twilio.http_client').setLevel(logging.WARNING)
    google=GoogleAccount(GoogleTokenStore(repository.connection,settings.google_key.encode(),
        client_id=os.environ['GOOGLE_CLIENT_ID'],client_secret=os.environ['GOOGLE_CLIENT_SECRET']),profile,
        sender_email=os.environ.get('GOOGLE_SENDER_EMAIL') or profile.email_sender or settings.sender_email,
        owner_email=settings.owner_email)
    return create_application(settings,repository,carrier,google,AsyncOpenAI(timeout=30,max_retries=0),
        shopify_setup=os.environ.get('ALLI_SHOPIFY_APP_SETUP',''))


if __name__=='__main__':
    import uvicorn
    uvicorn.run(build_application(),host='0.0.0.0',port=int(os.environ.get('PORT','8000')),
                log_level='warning',access_log=False)
