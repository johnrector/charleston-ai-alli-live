import html
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


def page(title,content,*,status=200):
    document='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>'''+escape(title)+''' · Alli</title><style>
    :root{color-scheme:light;font:16px/1.55 system-ui;color:#203e31;background:#f6f5ee}*{box-sizing:border-box}
    body{max-width:1100px;margin:auto;padding:28px}header{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap}
    a{color:#285a44}nav{display:flex;gap:22px;flex-wrap:wrap}h1,h2{font-family:Georgia,serif;line-height:1.13}h1{font-size:clamp(32px,6vw,58px)}
    main{margin-top:36px}section,article{background:#fffef9;border:1px solid #dedfd3;border-radius:8px;padding:24px;margin:18px 0}
    input,textarea,select,button{font:inherit;max-width:100%;padding:11px;border:1px solid #b5c0b5;border-radius:4px}
    input,textarea{width:100%}label{display:block;margin:14px 0}button,.button{background:#285a44;color:white;cursor:pointer;padding:12px 20px;border:0;display:inline-block;text-decoration:none}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,290px),1fr));gap:18px}.grid article{margin:0}
    .muted{color:#667166}.error{color:#a03725}pre{white-space:pre-wrap;overflow-wrap:anywhere}.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
    </style><header><strong>alli · John's private space</strong><nav><a href="/dashboard">Activity</a><a href="/dashboard/people">People</a><a href="/dashboard/work">Work</a><a href="/dashboard/promises">Promises</a><a href="/dashboard/connections">Connections</a></nav></header><main>'''+content+'''</main></html>'''
    return HTMLResponse(document,status_code=status,headers={
        'Cache-Control':'no-store','Referrer-Policy':'same-origin','X-Content-Type-Options':'nosniff',
        'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
