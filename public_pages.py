"""Public app identity and privacy disclosure for the Google consent screen."""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()
STYLE = '''body{margin:0;background:#f6f4ed;color:#183b32;font:18px/1.65 system-ui,sans-serif}
main{max-width:760px;margin:auto;padding:64px 24px}nav{display:flex;gap:24px;flex-wrap:wrap;margin-bottom:48px}
a{color:#155f4d;text-underline-offset:4px}h1{font:600 clamp(2rem,7vw,3.5rem)/1.1 Georgia,serif;letter-spacing:-.025em}
h2{font-size:1.25rem;margin-top:36px}p,li{max-width:68ch}small{font-size:.9rem}footer{border-top:1px solid #b5c6b8;margin-top:48px;padding-top:24px}
a:focus-visible{outline:3px solid #ad6000;outline-offset:4px}'''


def page(title, body):
    return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} | Charleston AI Alli</title>
<meta name="description" content="Alli, John Rector’s personal AI assistant with Charleston AI. App information and privacy.">
<style>{STYLE}</style></head><body><main><nav aria-label="Main"><a href="/">Charleston AI Alli</a><a href="/privacy">Privacy</a></nav>
{body}<footer><small>Charleston AI Alli · John Rector<br>Questions: <a href="mailto:jsrector@gmail.com">jsrector@gmail.com</a></small></footer>
</main></body></html>''', headers={'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                                'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})


@router.get('/', response_class=HTMLResponse, include_in_schema=False)
def home():
    return page('John’s personal assistant', '''<h1>Meet Alli.</h1>
<p>Charleston AI Alli is John Rector’s personal AI assistant. She helps with phone conversations, text messages, meeting availability, appointments and reminders.</p>
<h2>How the Google connection is used</h2>
<p>John connects his own Google account so Alli can check his calendar, arrange agreed meetings, create requested reminders and send email when he authorizes it. Google sign-in is restricted to John’s configured account by the service.</p>
<p>Friends interact with Alli by phone or text. They do not need to connect a Google account. Only John’s approved contacts may check availability or arrange their own meetings. New contacts need his approval first.</p>
<p>Alli is an AI assistant. Check a confirmed calendar event or message receipt for the result of an action. A conversation alone does not guarantee a booking or message delivery.</p>
<p>Read <a href="/privacy">how Alli handles information</a>, or learn about <a href="https://ai-chs.com/">Charleston AI</a>.</p>''')


@router.get('/privacy', response_class=HTMLResponse, include_in_schema=False)
def privacy():
    return page('Privacy', '''<h1>Alli’s privacy policy.</h1><p><small>Effective October 23, 2026</small></p>
<p>This policy covers Charleston AI Alli, John Rector’s personal phone, text and scheduling assistant. John operates this service with Charleston AI. It is separate from the Charleston AI website’s appointment-booking service.</p>
<h2>Information Alli processes</h2>
<p>Alli processes phone numbers, names, supplied email addresses, text messages, conversation transcripts and summaries, scheduling requests, contact approvals, and action and delivery records. Call audio is processed to conduct the conversation; transcripts may be retained. This policy does not promise that every call is recorded or fully transcribed.</p>
<p>With John’s Google authorization, Alli accesses his account email address, calendar availability and events on calendars he owns, including relevant meeting details and attendees. It can create, update or cancel authorized events, and send authorized email through Gmail. The requested Gmail permission is for sending; it does not grant access to read John’s inbox.</p>
<h2>Why the information is used</h2>
<p>The information is used to recognize contacts, carry on conversations, answer permitted scheduling questions, carry out agreed actions, request John’s approval for new contacts, and diagnose failed actions. Approved contacts receive their own meeting information and availability answers; approval does not grant them unrestricted access to John’s calendar or email.</p>
<h2>Service providers and sharing</h2>
<p>Google provides account authorization, Calendar and Gmail. Twilio handles telephone calls and messages. OpenAI processes relevant conversation and tool context to provide the AI assistant. Render hosts the application and its PostgreSQL database. Information needed for a requested call, message or calendar invitation is sent to its intended recipient.</p>
<p>Alli does not sell personal information, share it with advertisers, or use Google user data to train generalized AI models. Alli’s use and transfer of information received from Google APIs adheres to the <a href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a>, including its Limited Use requirements.</p>
<h2>Storage, access and retention</h2>
<p>Google refresh tokens are encrypted before storage in the service’s private Render database. Service credentials are kept on the server. Access to administration is restricted. Conversation, contact, approval and action records are stored for continuity and troubleshooting. There is currently no automatic deletion schedule for those records; the assistant’s shorter conversational recall window is not a deletion policy.</p>
<p>John and people he authorizes to maintain the service may review records for the requested work, support or security. Providers may retain operational records or backups under their own policies. No internet service can guarantee absolute security.</p>
<h2>Your choices</h2>
<p>Contact <a href="mailto:jsrector@gmail.com">jsrector@gmail.com</a> to ask about access, correction, deletion or stopping use of your contact details. Requests are handled by John; this page does not automatically delete records. Message opt-outs are handled by the messaging provider and applicable service controls.</p>
<p>John can revoke the Google connection through <a href="https://myaccount.google.com/connections">his Google Account connections</a>. Revoking access stops future authorized API use; it does not by itself delete records already stored by Alli. Ask John to remove those separately. Publishing the Google app does not grant other callers access to his account.</p>''')
