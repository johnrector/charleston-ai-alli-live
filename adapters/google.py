"""Google is an account tool, not a conversation or reasoning runtime."""
import base64
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
import json
import re
from html.parser import HTMLParser

from cryptography.fernet import Fernet
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials


class ProviderRejected(ValueError):
    """A definitive provider rejection, rather than an unconfirmed write."""
    def __init__(self,status,code):
        self.status,self.code=status,code
        super().__init__(f'Google rejected the operation ({status}: {code})')


class InvalidInput(ValueError):
    pass


class GoogleTokenStore:
    def __init__(self,connection,encryption_key,*,client_id='',client_secret=''):
        self.connection,self.cipher=connection,Fernet(encryption_key)
        self.client_id,self.client_secret=client_id,client_secret

    def credentials(self):
        # Keep the existing authorized connection; no token transfer to models.
        with self.connection() as db:
            row=db.execute('SELECT encrypted_token FROM alli_google_token WHERE id=1').fetchone()
        if not row:
            raise ProviderRejected(401,'connection_required')
        encrypted=row['encrypted_token'] if isinstance(row,dict) else row[0]
        raw=self.cipher.decrypt(encrypted.encode()).decode()
        if raw.startswith('{'):return Credentials.from_authorized_user_info(json.loads(raw))
        # The existing production record is an encrypted refresh-token string,
        # not a serialized Credentials object. Keep its account registration in
        # the service's existing private environment variables.
        return Credentials(None,refresh_token=raw,token_uri='https://oauth2.googleapis.com/token',
            client_id=self.client_id,client_secret=self.client_secret)

    def save(self,credentials):
        if not credentials.refresh_token:raise ValueError('Offline access is required')
        encrypted=self.cipher.encrypt(credentials.refresh_token.encode()).decode()
        with self.connection() as db:
            db.execute('''INSERT INTO alli_google_token(id,encrypted_token,updated_at) VALUES(1,%s,now())
                ON CONFLICT(id) DO UPDATE SET encrypted_token=EXCLUDED.encrypted_token,updated_at=now()''',(encrypted,))


def timestamp(value):
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    except (ValueError,AttributeError) as exc:
        raise InvalidInput('Supply an ISO date and time with its timezone') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidInput('A timezone is required')
    return parsed


def email_address(value):
    if not isinstance(value,str) or value.strip()!=value or any(c in value for c in '\r\n'):
        raise InvalidInput('Invalid email address')
    try:
        address=Address(addr_spec=value)
    except ValueError as exc:
        raise InvalidInput('Invalid email address') from exc
    if not address.username or not address.domain or '.' not in address.domain:
        raise InvalidInput('Invalid email address')
    return str(address)


class GoogleAccount:
    def __init__(self,tokens,profile,*,sender_email,owner_email,calendar_id='primary'):
        self.tokens,self.profile=tokens,profile
        self.sender=email_address(sender_email)
        self.owner_email=email_address(owner_email)
        self.calendar_id=calendar_id

    def request(self,method,path,*,body=None,params=None):
        credentials=self.tokens.credentials()
        before=(credentials.token,credentials.expiry)
        try:
            with AuthorizedSession(credentials) as session:
                response=session.request(method,'https://www.googleapis.com/'+path,
                                         json=body,params=params,timeout=15)
            if response.status_code>=500:
                raise RuntimeError('The Google operation has no confirmed outcome')
            if response.status_code>=400:
                try:
                    error=response.json().get('error',{})
                    code=error.get('status') or str(error.get('code','request_rejected'))
                except (ValueError,AttributeError):
                    code='request_rejected'
                raise ProviderRejected(response.status_code,code)
            return response.json()
        finally:
            # A refresh belongs to the account connection, never an agent prompt.
            if credentials.token and before != (credentials.token,credentials.expiry):
                self.tokens.save(credentials)

    def availability(self,start,end):
        begin,finish=timestamp(start),timestamp(end)
        if not begin<finish or (finish-begin).total_seconds()>7*86400:
            raise InvalidInput('Availability windows must be positive and no longer than seven days')
        data=self.request('POST','calendar/v3/freeBusy',body={
            'timeMin':begin.isoformat(),'timeMax':finish.isoformat(),
            'items':[{'id':self.calendar_id}]})
        calendars=data.get('calendars',{})
        result=calendars.get(self.calendar_id)
        if result is None or result.get('errors'):
            raise ProviderRejected(503,'availability_unconfirmed')
        busy=result.get('busy',[])
        return {'ok':True,'available':not busy,'start':begin.isoformat(),'end':finish.isoformat()}

    def search_email(self,query,*,page_token=None,limit=15):
        if not isinstance(query,str) or not 1<=len(query)<=1500 or not 1<=limit<=25:
            raise InvalidInput('Use a Gmail query within 1,500 characters and limit 1–25')
        params={'q':query,'maxResults':limit,'includeSpamTrash':False}
        if page_token:params['pageToken']=page_token
        data=self.request('GET','gmail/v1/users/me/threads',params=params)
        return {'ok':True,'provider':'gmail','threads':data.get('threads',[]),
            'next_page_token':data.get('nextPageToken'),'estimated_matches':data.get('resultSizeEstimate'),
            'content_is_untrusted_data':True}

    def read_email_thread(self,thread_id,*,limit=6):
        if not isinstance(thread_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',thread_id) or not 1<=limit<=10:
            raise InvalidInput('Use a saved thread ID and limit 1–10')
        data=self.request('GET','gmail/v1/users/me/threads/'+thread_id,
                          params={'format':'minimal','fields':'id,messages(id)'})
        identities=data.get('messages',[])
        messages=[];budget=40000
        for entry in identities[-limit:]:
            identifier=entry['id']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',identifier):raise InvalidInput('Invalid provider message ID')
            message=self.request('GET','gmail/v1/users/me/messages/'+identifier,params={'format':'full'})
            payload=message.get('payload',{})
            headers={h['name'].lower():h.get('value','') for h in payload.get('headers',[])}
            text,attachments=mail_text(payload)
            excerpt=text[:min(12000,budget)];budget-=len(excerpt)
            messages.append({'id':identifier,'from':headers.get('from',''),'to':headers.get('to',''),
                'cc':headers.get('cc',''),'subject':headers.get('subject',''),'date':headers.get('date',''),
                'labels':message.get('labelIds',[]),'text':excerpt,'truncated':len(excerpt)<len(text),
                'attachments':attachments})
        return {'ok':True,'provider':'gmail','thread_id':thread_id,'messages':messages,
            'earlier_messages_omitted':max(0,len(identities)-len(messages)),
            'content_is_untrusted_data':True}

    def events(self,start,end,*,person_id=None,contact_email='',page_token=None):
        begin,finish=timestamp(start),timestamp(end)
        if not begin<finish or (finish-begin).total_seconds()>31*86400:
            raise InvalidInput('Use a positive calendar window no longer than 31 days')
        from urllib.parse import quote
        params={'timeMin':begin.isoformat(),'timeMax':finish.isoformat(),'singleEvents':True,
                'orderBy':'startTime','maxResults':250}
        if page_token:params['pageToken']=page_token
        data=self.request('GET','calendar/v3/calendars/'+quote(self.calendar_id,safe='')+'/events',params=params)
        records=[]
        for event in data.get('items',[]):
            if event.get('status')=='cancelled':continue
            private=event.get('extendedProperties',{}).get('private',{})
            matches=private.get('ea_person_id')==person_id or bool(contact_email and any(
                attendee.get('email','').casefold()==contact_email.casefold()
                for attendee in event.get('attendees',[])))
            if person_id and not matches:continue
            record={key:event.get(key) for key in ('id','summary','start','end','location','status')}
            # Descriptions, other attendees and account links stay private.
            if person_id is None:
                record.update(description=event.get('description',''),attendees=event.get('attendees',[]),
                              url=event.get('htmlLink'))
            records.append(record)
        return {'ok':True,'events':records,'next_page_token':data.get('nextPageToken'),
                'scope':'own_meetings' if person_id else 'owner_calendar'}

    def create_event(self,*,action_key,person_id,title,start,end,description='',location='',
                     attendee_emails=(),reminder=False):
        begin,finish=timestamp(start),timestamp(end)
        if not title.strip() or len(title)>250 or not begin<finish:
            raise InvalidInput('Supply a title and a positive event interval')
        recipients=[email_address(address) for address in attendee_emails]
        event_id='ea'+hashlib.sha256(action_key.encode()).hexdigest()[:40]
        body={'id':event_id,'summary':title,'description':description,'location':location,
            'start':{'dateTime':begin.isoformat()},'end':{'dateTime':finish.isoformat()},
            'visibility':'private','transparency':'transparent' if reminder else 'opaque',
            'extendedProperties':{'private':{'ea_assistant':self.profile.id,'ea_person_id':person_id,
                                            'ea_action':action_key}}}
        if recipients: body['attendees']=[{'email':address} for address in recipients]
        if not reminder and not self.availability(start,end)['available']:
            return {'ok':False,'not_attempted':True,'error':'That time is unavailable'}
        # Calendar event IDs are caller-chosen. This gives a real provider key
        # to reconcile if the insert reply is lost; never invent a new one.
        from urllib.parse import quote
        path='calendar/v3/calendars/'+quote(self.calendar_id,safe='')+'/events'
        data=self.request('POST',path,body=body,params={'sendUpdates':'all' if recipients else 'none'})
        return {'ok':True,'provider':'google_calendar','event_id':data['id'],
                'status':data.get('status'),'start':data.get('start'),'end':data.get('end'),
                'url':data.get('htmlLink')}

    def send_email(self,*,action_key,to,subject,body):
        recipient=email_address(to)
        if not 1<=len(subject)<=250 or any(c in subject for c in '\r\n'):
            raise InvalidInput('Invalid email subject')
        if not isinstance(body,str) or not 1<=len(body)<=50000:
            raise InvalidInput('Supply an email body within 50,000 characters')
        message=EmailMessage(policy=SMTP)
        message['From']=Address(display_name=self.profile.principal,addr_spec=self.sender)
        message['To']=Address(addr_spec=recipient)
        message['Subject']=subject
        message['Message-ID']='<'+hashlib.sha256(action_key.encode()).hexdigest()+'@assistant.local>'
        message.set_content(body+'\n\n'+self.profile.name+', '+self.profile.principal+"'s AI assistant")
        encoded=base64.urlsafe_b64encode(message.as_bytes()).decode()
        data=self.request('POST','gmail/v1/users/me/messages/send',body={'raw':encoded})
        return {'ok':True,'provider':'gmail','message_id':data['id'],'thread_id':data.get('threadId'),
                'to':recipient,'subject':subject}


class MailHTML(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.text=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:self.hidden+=1
        elif tag in {'p','div','br','li','tr'}:self.text.append('\n')
    def handle_endtag(self,tag):
        if tag in {'script','style'}:self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden:self.text.append(data)


def mail_text(payload):
    plain,html,attachments=[],[],[]
    def walk(part,depth=0):
        if depth>20:return
        body=part.get('body',{})
        if part.get('filename') or body.get('attachmentId'):
            attachments.append({'name':part.get('filename',''),'type':part.get('mimeType',''),
                                'bytes':body.get('size',0)})
            return
        data=body.get('data','')
        if data and part.get('mimeType') in {'text/plain','text/html'}:
            data=data[:100000]
            try:text=base64.urlsafe_b64decode(data+'='*(-len(data)%4)).decode('utf-8',errors='replace')
            except (ValueError,TypeError):text='[Message text could not be decoded]'
            (plain if part['mimeType']=='text/plain' else html).append(text)
        for child in part.get('parts',[])[:100]:walk(child,depth+1)
    walk(payload)
    if plain:return '\n'.join(plain),attachments
    parser=MailHTML();parser.feed('\n'.join(html))
    return ''.join(parser.text),attachments
