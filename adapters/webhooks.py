"""Signed, pinned-public-address webhook delivery; never follows redirects."""
import base64
from datetime import datetime,timezone
import ipaddress
import json
import socket
from urllib.parse import urlsplit

import certifi
from standardwebhooks.webhooks import Webhook
import urllib3


class CallbackError(ValueError):
    def __init__(self,reason):
        self.reason=reason
        super().__init__('Callback endpoint could not be verified: '+reason)


def signing_key(secret):
    if not isinstance(secret,str) or not secret.startswith('whsec_'):
        raise CallbackError('invalid_signing_secret')
    try:
        key=base64.b64decode(secret[6:],validate=True)
    except (ValueError,TypeError) as error:
        raise CallbackError('invalid_signing_secret') from error
    if not 24<=len(key)<=64:raise CallbackError('invalid_signing_secret')
    return key


def public_address(value):
    address=ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address,ipaddress.IPv6Address):
        if address.ipv4_mapped and not public_address(str(address.ipv4_mapped)):return False
        if address.sixtofour and not public_address(str(address.sixtofour)):return False
        if address.teredo and not all(public_address(str(ip)) for ip in address.teredo):return False
    return True


class PublicWebhook:
    def __init__(self,*,resolver=socket.getaddrinfo,pool_factory=urllib3.HTTPSConnectionPool):
        self.resolver,self.pool_factory=resolver,pool_factory

    def destination(self,url):
        try:
            if not isinstance(url,str):raise CallbackError('unsafe_url')
            parsed=urlsplit(url)
            if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.fragment or parsed.port not in {None,443} or len(url)>2048 or '%' in parsed.hostname):
                raise CallbackError('unsafe_url')
            host=parsed.hostname.encode('idna').decode('ascii')
            addresses=list(dict.fromkeys(record[4][0] for record in self.resolver(host,443,type=socket.SOCK_STREAM)))
            if not addresses or not all(public_address(address) for address in addresses):
                raise CallbackError('non_public_address')
            return host,addresses,parsed.path or '/',parsed.query
        except CallbackError:raise
        except (ValueError,UnicodeError,socket.gaierror) as error:
            raise CallbackError('invalid_destination') from error

    def send(self,url,subscription_id,secrets,event_id,payload):
        body=json.dumps(payload,separators=(',',':'),ensure_ascii=False)
        if len(body.encode())>262144:raise CallbackError('payload_too_large')
        now=datetime.now(timezone.utc)
        signatures=[]
        for secret in secrets:
            signing_key(secret)
            signatures.append(Webhook(secret).sign(event_id,now,body))
        headers={'Content-Type':'application/json','webhook-id':event_id,
            'webhook-timestamp':str(int(now.timestamp())),'webhook-signature':' '.join(signatures),
            'X-MCP-Subscription-Id':subscription_id}
        host,addresses,path,query=self.destination(url)
        headers['Host']=host
        # The connection uses a validated numeric address. TLS verification and
        # SNI still use the original hostname, closing the DNS-rebinding gap.
        pool=self.pool_factory(addresses[0],port=443,server_hostname=host,assert_hostname=host,
            cert_reqs='CERT_REQUIRED',ca_certs=certifi.where(),timeout=urllib3.Timeout(connect=5,read=10),
            retries=False,maxsize=1)
        try:
            response=pool.request('POST',path+('?' + query if query else ''),body=body.encode(),
                headers=headers,redirect=False,retries=False,preload_content=False)
            raw=response.read(65537)
            try:data=json.loads(raw) if len(raw)<=65536 else None
            except (ValueError,UnicodeError):data=None
            return {'status':response.status,'body':data}
        except (urllib3.exceptions.HTTPError,OSError) as error:
            raise CallbackError('network_error') from error
        finally:
            pool.close()
