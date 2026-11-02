"""Small, strict HTTP boundary for optional Pi headphone actions."""
import hmac
import re
from urllib.parse import urlsplit

from headphone_manager import unique_json

READ_PEERS = frozenset(('127.0.0.1', '::1', '192.168.250.1', '192.168.250.2'))
WRITE_PEERS = frozenset(('127.0.0.1', '::1', '192.168.250.2'))


def handle_headphones(handler, method):
    """Return False for unrelated existing preview routes. Never block on I/O."""
    if len(handler.path) > 1024:
        return False
    url = urlsplit(handler.path)
    if not url.path.startswith('/headphones'):
        return False
    if handler.path != '/headphones':
        handler.send(400, {'error': 'invalid_path'})
        return True
    # Reject ambiguous framing, auth or browser-originated mutations. No CORS
    # responses or GET mutations are implemented.
    seen = set()
    for key, value in handler.headers.items():
        name = key.lower()
        if name in seen or len(value) > 1024:
            handler.send(400, {'error': 'invalid_headers'})
            return True
        seen.add(name)
    if 'transfer-encoding' in seen:
        handler.send(400, {'error': 'invalid_framing'})
        return True
    if method == 'POST' and 'origin' in seen:
        handler.send(403, {'error': 'browser_action_forbidden'})
        return True
    token = handler.server.token
    if token:
        supplied = handler.headers.get('X-Access-Token', '')
        try:
            valid = (len(supplied) <= 511 and
                     hmac.compare_digest(supplied.encode('ascii'), token.encode('ascii')))
        except UnicodeEncodeError:
            valid = False
        if not valid:
            handler.send(401, {'error': 'unauthorized'})
            return True
    elif handler.client_address[0] not in (WRITE_PEERS if method == 'POST' else READ_PEERS):
        handler.send(403, {'error': 'peer_forbidden'})
        return True
    manager = getattr(handler.server, 'headphones', None)
    if manager is None:
        handler.send(503, {'error': 'headphone_service_unavailable'})
        return True
    if method == 'GET':
        if handler.headers.get('Content-Length', '0') != '0':
            handler.send(400, {'error': 'unexpected_body'})
        else:
            handler.send(200, manager.snapshot())
        return True
    content = handler.headers.get('Content-Type', '')
    if content.lower() not in ('application/json', 'application/json; charset=utf-8'):
        handler.send(415, {'error': 'json_required'})
        return True
    length = handler.headers.get('Content-Length', '')
    if not re.fullmatch(r'[1-9][0-9]{0,2}', length) or int(length) > 256:
        handler.send(400, {'error': 'invalid_length'})
        return True
    try:
        raw = handler.rfile.read(int(length))
        if len(raw) != int(length):
            raise ValueError()
        value = unique_json(raw)
        if not isinstance(value, dict) or not isinstance(value.get('action'), str):
            raise ValueError()
        expected = {'action', 'address'} if value['action'] == 'connect' else {'action'}
        if set(value) != expected:
            raise ValueError()
    except (ValueError, UnicodeError, OSError, TimeoutError):
        handler.send(400, {'error': 'invalid_body'})
        return True
    status, error = manager.submit(value['action'], value.get('address'))
    response = manager.snapshot()
    if error:
        response['error'] = error
    handler.send(status, response)
    return True
