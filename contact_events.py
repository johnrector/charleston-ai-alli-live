"""A caller-supplied name is never authority over an existing calendar event."""
import communication_store as store

def require_individual_meeting(event):
    """A participant's permission over their slot is not permission over a group."""
    import os
    owners={os.getenv('GOOGLE_OWNER_EMAIL','jsrector@gmail.com').casefold()}
    owners.update(x.strip().casefold() for x in os.getenv('ALLI_OWNER_EMAIL_ALIASES','').split(',') if x.strip())
    guests={a.get('email','').casefold() for a in event.get('attendees',[])
        if a.get('email') and not a.get('self') and not a.get('resource')
        and a.get('email','').casefold() not in owners and a.get('responseStatus')!='declined'}
    organizer=event.get('organizer',{})
    if organizer.get('email') and not organizer.get('self') and organizer['email'].casefold() not in owners:
        guests.add(organizer['email'].casefold())
    if len(guests)>1:raise ValueError('This meeting includes other people. John needs to review a change that affects the group.')

def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS alli_contact_event (
      phone text NOT NULL,event_id text NOT NULL,source text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(phone,event_id))''')
    # Preserve verified creation receipts; do not infer ownership from names.
    db.execute('''INSERT INTO alli_contact_event(phone,event_id,source)
      SELECT payload->>'phone',payload->'result'->>'event_id','verified_creation_receipt'
      FROM alli_communication_update WHERE payload->>'action'='create_meeting'
      AND payload->'result'->>'ok'='true' AND payload->'result'->>'event_id' IS NOT NULL
      AND payload->>'phone' IS NOT NULL ON CONFLICT DO NOTHING''')

def bind(phone,event_id,source='created_for_contact'):
    phone=store.normalize_phone(phone)
    if not phone or not event_id:raise ValueError('A contact number and event ID are required')
    with store.connection() as db:
        db.execute('INSERT INTO alli_contact_event(phone,event_id,source) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(phone,event_id,source))

def belongs(event,phone,email=''):
    # email is bound by the server to John's stored contact or authorized mission,
    # never an address supplied as an ownership claim during this conversation.
    if email and any(a.get('email','').casefold()==email.casefold() for a in event.get('attendees',[])):return True
    if not phone:return False
    with store.connection() as db:
        return bool(db.execute('SELECT 1 FROM alli_contact_event WHERE phone=%s AND event_id=%s',(store.normalize_phone(phone),event['id'])).fetchone())

def check_capacity(db,phone,email):
    """Run inside the global calendar mutation lock, including pending bookings."""
    from datetime import datetime,timedelta
    import google_integration as g
    bindings={r[0] for r in db.execute('SELECT event_id FROM alli_contact_event WHERE phone=%s',(phone,)).fetchall()}
    now=datetime.now(g.TZ)
    params=dict(timeMin=now.isoformat(),timeMax=(now+timedelta(days=90)).isoformat(),singleEvents=True,maxResults=250)
    count=0
    for _ in range(4):
        page=g.api('GET','calendar/v3/calendars/primary/events',params=params)
        for event in page.get('items',[]):
            if event.get('status')=='confirmed' and (event.get('id') in bindings or any(a.get('email','').casefold()==email.casefold() and a.get('responseStatus')!='declined' for a in event.get('attendees',[]))):count+=1
        if count>=3:raise ValueError('This contact already has 3 upcoming meetings. John can review any additional appointment.')
        if not page.get('nextPageToken'):return
        params['pageToken']=page['nextPageToken']
    raise ValueError('Calendar capacity could not be fully checked; John can review this booking')
