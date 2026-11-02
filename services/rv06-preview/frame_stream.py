"""Bounded length-prefixed JPEG records shared by local display streams."""
import json,struct,socket
MAX_FRAME=2*1024*1024
def packet(meta,raw):
    if not isinstance(raw,bytes) or not 0<len(raw)<=MAX_FRAME:raise ValueError('Invalid JPEG size')
    meta=dict(meta,bytes=len(raw));header=json.dumps(meta,separators=(',',':'),ensure_ascii=True).encode()
    if len(header)>2048:raise ValueError('Oversized frame metadata')
    return struct.pack('!I',len(header))+header+raw
def read_frame(handle):
    first=handle.read(4)
    if not first:return None
    if len(first)!=4:raise ValueError('Truncated frame prefix')
    size=struct.unpack('!I',first)[0]
    if not 2<=size<=2048:raise ValueError('Invalid frame metadata')
    header=handle.read(size)
    if len(header)!=size:raise ValueError('Truncated frame metadata')
    meta=json.loads(header);length=meta.get('bytes')
    if type(length) is not int or not 0<length<=MAX_FRAME:raise ValueError('Invalid frame size')
    raw=handle.read(length)
    if len(raw)!=length:raise ValueError('Truncated frame body')
    return meta,raw
def begin(handler):
    handler.connection.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,128*1024)
    handler.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
    handler.close_connection=True
    handler.send_response(200);handler.send_header('Content-Type','application/x-pi-jpeg-stream');handler.send_header('Connection','close');handler.send_header('Cache-Control','no-store');handler.send_header('X-Content-Type-Options','nosniff');handler.end_headers()
