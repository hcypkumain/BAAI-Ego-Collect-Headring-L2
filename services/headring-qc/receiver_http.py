"""Authenticated, same-origin manual receiver control endpoints."""
import hmac
from http.cookies import SimpleCookie,CookieError
import json

from receiver_state import Conflict

def token_from(handler):
    try:
        raw=handler.headers.get('Cookie','')
        if len(raw)>4096:return ''
        cookie=SimpleCookie();cookie.load(raw)
        return cookie['headring_operator'].value if 'headring_operator' in cookie else ''
    except CookieError:return ''

def send(handler,status,value,cookie=None):
    data=json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
    handler.send_response(status);handler.send_header('Content-Type','application/json; charset=utf-8')
    handler.send_header('Content-Length',str(len(data)));handler.send_header('Cache-Control','no-store')
    handler.send_header('X-Content-Type-Options','nosniff')
    if cookie:handler.send_header('Set-Cookie',cookie)
    handler.end_headers();handler.wfile.write(data)

def get(handler,receiver):
    path=handler.path.split('?')[0]
    if path not in ('/api/receive/status','/api/receive/auth'):return False
    token=token_from(handler);authenticated=receiver.store.authorized(token)
    if path=='/api/receive/auth':value=dict(authenticated=authenticated,csrf=receiver.store.csrf(token) if authenticated else None)
    else:value=receiver.snapshot();value['authenticated']=authenticated
    send(handler,200,value);return True

def post(handler,receiver):
    path=handler.path.split('?')[0]
    if path not in ('/api/receive/login','/api/receive/control'):return False
    try:
        origin=handler.headers.get('Origin');host=handler.headers.get('Host')
        if not host or origin!='http://'+host:raise PermissionError('Same-origin operator request required')
        if handler.headers.get('Transfer-Encoding'):raise ValueError('Chunked control bodies not accepted')
        length=handler.headers.get('Content-Length','')
        if not length.isdecimal() or not 1<=int(length)<=4096:raise ValueError('Invalid control body length')
        if handler.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('JSON control body required')
        raw=handler.rfile.read(int(length))
        if len(raw)!=int(length):raise ValueError('Incomplete control body')
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('Invalid control object')
        if path=='/api/receive/login':
            if set(value)!={'key'}:raise ValueError('Invalid login fields')
            token,csrf=receiver.store.login(value['key'],handler.client_address[0])
            send(handler,200,dict(authenticated=True,csrf=csrf),
                'headring_operator='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400')
        else:
            token=token_from(handler)
            if not receiver.store.authorized(token):raise PermissionError('Operator login required')
            supplied=handler.headers.get('X-CSRF-Token','')
            if not hmac.compare_digest(supplied,receiver.store.csrf(token)):raise PermissionError('Control CSRF token rejected')
            if set(value)!={'receiving','revision','operation_id'}:raise ValueError('Invalid control fields')
            result=receiver.store.set_control(value['receiving'],value['revision'],value['operation_id'])
            receiver.wake.set();send(handler,200,result)
    except Conflict as error:send(handler,409,dict(error='stale_control',message=str(error)))
    except PermissionError as error:send(handler,403,dict(error='operator_required',message=str(error)))
    except (ValueError,TypeError) as error:send(handler,400,dict(error='invalid_control',message=str(error)))
    return True
