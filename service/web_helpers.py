import html
from pathlib import Path
from urllib.parse import parse_qsl,urlsplit

from fastapi import HTTPException
from starlette.responses import HTMLResponse


def escape(value):
    return html.escape(str(value),quote=True)


def safe_return(value):
    if (not value or not (value.startswith('/dashboard') or value.startswith('/mcp-owner/consent?'))
            or value.startswith('//') or '\\' in value):
        return '/dashboard'
    parsed=urlsplit(value)
    return value if not parsed.netloc and not parsed.scheme else '/dashboard'


async def form(request,maximum=65536):
    if request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':
        raise HTTPException(415,'Expected a form submission')
    body=bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>maximum:raise HTTPException(413,'Request is too large')
    pairs=parse_qsl(body.decode('utf-8'),keep_blank_values=True,max_num_fields=100)
    # Parameter ambiguity is unsafe for signed provider callbacks and auth forms.
    if len({key for key,_ in pairs})!=len(pairs):raise HTTPException(400,'Repeated form parameter')
    return dict(pairs)


DASHBOARD_STYLE = Path(__file__).with_name('dashboard.css').read_text()


def page(title,content,*,status=200):
    navigation=[('Today','/dashboard','◌'),('People','/dashboard/people','◎'),
        ('Calls & work','/dashboard/promises','◷'),('Rules','/dashboard/policies','≡'),
        ('Health','/dashboard/health','♡'),('Activity','/dashboard/activity','↗')]
    selected={'Promises':'Calls & work','Work':'Calls & work','Connections':'Health'}.get(title,title)
    nav=''.join('<a href="'+url+'"'+(' aria-current="page"' if label==selected else '')+
        '><span aria-hidden="true">'+icon+'</span>'+label+'</a>' for label,url,icon in navigation)
    document=('<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>'+escape(title)+' · Alli</title>'
        '<style>'+DASHBOARD_STYLE+'</style></head><body><div class="shell"><aside class="sidebar">'
        '<a class="brand" href="/dashboard"><span class="brand-mark" aria-hidden="true">a</span>alli</a>'
        '<p class="brand-caption">John’s private space</p><nav class="nav" aria-label="Main">'+nav+'</nav>'
        '<div class="sidebar-foot"><div class="owner"><span class="avatar">JR</span>'
        '<div>John Rector<small>Private owner access</small></div></div>'
        '<p>Conversation with continuity.<br>Work with receipts.</p></div></aside>'
        '<div class="workspace"><main class="main">'+content+'</main></div></div></body></html>')
    return HTMLResponse(document,status_code=status,headers={
        'Cache-Control':'no-store','Referrer-Policy':'same-origin','X-Content-Type-Options':'nosniff',
        'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
