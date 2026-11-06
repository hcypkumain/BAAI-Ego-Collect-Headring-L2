"""Small boot page, released only when the real dashboard is ready to bind."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time

_lock = threading.RLock()
_server = None
_thread = None
_path = None
_state = {}
_handoff_guard = None

PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>头环正在启动</title>
<style>body{font:18px/1.7 system-ui,sans-serif;margin:0;padding:12vh 7vw;background:#f3f6fa;color:#17243b}main{max-width:560px;margin:auto;background:white;border-radius:22px;padding:30px;box-shadow:0 8px 30px #17243b12}h1{font-size:26px;margin:12px 0}p{margin:12px 0}.hint{font-size:15px;color:#627087}.dot{width:14px;height:14px;background:#3479ed;border-radius:50%;animation:pulse 1s infinite}@keyframes pulse{50%{opacity:.3}}@media(prefers-color-scheme:dark){body{background:#111827;color:#e7edf7}main{background:#1e293b}.hint{color:#b5c3d8}}</style>
<main><div class="dot"></div><h1>头环正在启动</h1><p id="message">手机已连接，正在准备采集程序。</p><p class="hint">准备完成后会自动进入采集页面，请保持数据线连接。</p><p class="hint">已等待 <span id="elapsed">0</span> 秒</p></main>
<script>
async function update(){
 const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),1500);
 try{
  const response=await fetch('/api/startup',{cache:'no-store',signal:abort.signal});
  if(response.headers.get('X-HeadRing-Startup')!=='1'){location.reload();return;}
  const state=await response.json();document.getElementById('message').textContent=state.message;
  document.getElementById('elapsed').textContent=Math.floor(state.elapsed_seconds);
 }catch(error){document.getElementById('message').textContent='正在进入采集界面，请稍候。';}
 finally{clearTimeout(timer);setTimeout(update,500);}
}update();
</script></html>'''.encode('utf-8')


def _write():
    if _path is None:
        return
    temporary = _path.with_suffix('.new')
    temporary.write_text(json.dumps(_state, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(_path)


def phase(name, message):
    with _lock:
        if not _state:
            return
        now = time.monotonic()
        _state.update(phase=name, message=message, updated_monotonic=now)
        _state.setdefault('events', []).append({'phase':name, 'monotonic':now})
        _write()
        print('STARTUP '+json.dumps({'phase':name,'boot_seconds':round(now,3)}, ensure_ascii=False), flush=True)


def snapshot():
    with _lock:
        return dict(phase=_state.get('phase','starting'),
                    message=_state.get('message','正在准备采集程序。'),
                    elapsed_seconds=max(0,time.monotonic()-_state.get('started_monotonic',time.monotonic())))


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, kind):
        self.send_response(code)
        self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-HeadRing-Startup','1')
        self.send_header('Retry-After','1')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        route = self.path.split('?',1)[0]
        if route in ('/','/phone','/phone/'):
            self._send(200,PAGE,'text/html; charset=utf-8')
        elif route == '/api/startup':
            self._send(200,json.dumps(snapshot(),ensure_ascii=False).encode(),'application/json; charset=utf-8')
        else:
            self._send(503,b'{"error":"starting","retry_after":1}','application/json')

    do_HEAD = do_GET

    def do_POST(self):
        self._send(503,b'{"error":"not_ready"}','application/json')


def begin(base, settings):
    global _server, _thread, _path, _state
    with _lock:
        if _server is not None:
            raise RuntimeError('Startup page already running')
        server = _Server((settings.get('host','0.0.0.0'),int(settings.get('port',8765))),_Handler)
        now=time.monotonic()
        try:boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:boot_id=None
        _state=dict(started_monotonic=now,boot_id=boot_id,port=server.server_port,events=[])
        runtime=Path(base)/'runtime';runtime.mkdir(parents=True,exist_ok=True)
        _path=runtime/'startup_status.json'
        _server=server
        _thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},daemon=True)
        _thread.start()
        phase('web_ready','手机已连接，正在准备采集设备。')
        return server.server_port


def guard_handoff(callback):
    global _handoff_guard
    with _lock:
        _handoff_guard=callback


def handoff(wait=True):
    """Do not hold the state lock while waiting for a request thread to finish."""
    global _server, _thread, _handoff_guard
    with _lock:
        guard=_handoff_guard if wait and _server is not None else None
    if guard is not None:
        guard()
    with _lock:
        server, thread = _server, _thread
        _server = _thread = None
        _handoff_guard = None
    if server is not None:
        server.shutdown();server.server_close();thread.join(2)


def live():
    phase('dashboard_ready','采集界面已就绪。')


def close():
    global _path, _state
    handoff(wait=False)
    with _lock:
        _path=None;_state={}
