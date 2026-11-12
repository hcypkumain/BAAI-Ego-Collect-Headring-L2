"""Local Unix socket client. No credentials transit the web process."""
import json, os, socket, stat, struct
from pathlib import Path

def call(state,request,on_event=lambda *_:None,timeout=8):
    path=Path(state)/'broker.sock'
    info=path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600: raise ValueError('unsafe broker socket')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(timeout); s.connect(str(path))
        uid=struct.unpack('3i',s.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]
        if uid!=os.getuid(): raise ValueError('invalid broker owner')
        s.sendall(json.dumps(request,separators=(',',':')).encode()+b'\n')
        with s.makefile('rb') as stream:
            while True:
                raw=stream.readline(4097)
                if not raw or len(raw)>4096: raise ValueError('invalid broker response')
                value=json.loads(raw)
                if 'event' in value: on_event(value['event']); continue
                return value

def status(state):
    try: return call(state,{'operation':'status'},timeout=1)
    except (OSError,ValueError): return {'unlocked':False,'targets':[],'expires_at':None}
