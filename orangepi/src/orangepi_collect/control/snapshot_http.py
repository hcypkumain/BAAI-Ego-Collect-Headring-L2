"""Small dependency-injected route shared by the real Dashboard and tests."""
import json
from urllib.parse import urlsplit
from uuid import uuid4


def error(code):
    return dict(schema_version='1.0', error=dict(code=code, message=code,
                retryable=code == 'SNAPSHOT_UNAVAILABLE', retry_after_ms=None,
                correlation_id=str(uuid4())))


def snapshot_get(handler, publisher, authorized):
    url = urlsplit(handler.path)
    if url.path != '/api/v1/snapshot': return False
    headers = {}
    if not authorized(): status, body = 401, error('AUTH_REQUIRED')
    elif '?' in handler.path: status, body = 400, error('INVALID_REQUEST')
    else:
        item = publisher.read() if publisher is not None else None
        if item is None: status, body = 503, error('SNAPSHOT_UNAVAILABLE')
        else:
            headers = {'ETag': item.etag, 'X-Snapshot-Age-Ms': str(max(0, publisher.clock() - item.published_ms))}
            status, body = (304, b'') if handler.headers.get('If-None-Match') == item.etag else (200, item.body)
    if not isinstance(body, bytes): body = json.dumps(body, allow_nan=False).encode()
    handler.send_response(status)
    for key, value in dict(headers, **{'Content-Type': 'application/json', 'Content-Length': str(len(body)), 'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff'}).items(): handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)
    return True
