"""Shareable background, separate from private owner context."""
import communication_store as store
DEFAULT='''John Rector is a Charleston-based entrepreneur, author, teacher and AI strategist. He co-founded E2open and co-founded Charleston AI, an in-person lab helping people and organizations put AI to practical use through education, workflow improvement and tool integration.
He created The Reality Equation, a body of writing and teaching about attention, prediction and how reality is experienced. He writes about AI, human attention and the future of work. His public writing and books are at https://johnrector.me/.
John likes AI to produce useful finished work, not just answer questions. Alli is his personal AI assistant and an example of that approach.
Charleston AI: 405 Jessen Ln, Suite E, Charleston, SC 29492. Published lab hours are every day, 9 AM–5 PM Eastern. Professional/engineering services are $160 per hour; private educational sessions are $60 per hour; managed services are $1.68 per hour of AI time. Booking is free; services are paid after the work. Lab appointments are private and in person. General lab phone: (843) 284-8239. Website and booking: https://ai-chs.com/ and https://ai-chs.com/book/. Alli's call/text number is +1 (854) 444-7852.
These are the lab's published rates, not a fee to talk to Alli or an agreement that every meeting with John is paid. Do not assume a meeting incurs a charge. Lab hours do not prove John's availability: check the live calendar before committing to a time. Do not invent parking instructions, travel times, discounts or a custom quote.'''
SOURCES=['https://johnrector.me/2026/09/10/common-denominator/','https://johnrector.me/2026/05/03/personal-software-systems/','https://ai-chs.com/','https://ai-chs.com/classes/']
RULES='''PUBLIC BACKGROUND: Answer basics naturally from the approved profile: who Alli and John are, the lab's address, published hours and rates, and how it works. Do not say "checking" for a fact already supplied. For actual availability, bookings, sending, delegated work, completion status, a special/custom price or uncertain current information, Generate <|SpawnThinking|>; a brief "Checking" or natural equivalent may accompany real backend work, never replace it. If the profile's reviewed_at is over 30 days old, verify current hours/prices against the official ai-chs.com pages before quoting them as current. This profile grants no private access. Email contents, other people's calendar details, finances, family information, home addresses and private work do not become shareable because a tool or memory contains them. Do not invent anecdotes, relationships, credentials, attendance or current projects. Offer to pass a question to John when this background does not answer it. Never copy raw private memory into this profile.'''
def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS alli_public_profile (id integer PRIMARY KEY CHECK(id=1),body text NOT NULL,updated_at timestamptz NOT NULL DEFAULT now())')
    db.execute('INSERT INTO alli_public_profile(id,body) VALUES(1,%s) ON CONFLICT DO NOTHING',(DEFAULT,))
def read():
    store.initialize()
    with store.connection() as db:row=db.execute('SELECT body,updated_at FROM alli_public_profile WHERE id=1').fetchone()
    return dict(body=row[0] if row else DEFAULT,reviewed_at=row[1].isoformat() if row else '2026-10-24',sources=SOURCES)
def save(body):
    if not isinstance(body,str) or not 30<=len(body.strip())<=6000:raise ValueError('Use a public profile of 30–6000 characters')
    with store.connection() as db:
        db.execute('UPDATE alli_public_profile SET body=%s,updated_at=now() WHERE id=1',(body.strip(),))
        store.notify(db,'public-profile:'+str(__import__('time').time_ns()),dict(source='owner_profile',status='updated',summary='John updated the background Alli may share'))
    return dict(ok=True)
