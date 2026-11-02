"""Isolated DashScope SDK worker. Input JSON on stdin; no secrets on argv/logs."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0,str(_Path(__file__).resolve().parents[1]/'common'))
import project_paths
import base64,json,os,re,sys,threading,wave
from pathlib import Path
from project_paths import BASE, PROJECT, SRC, devices

def key():
    value=os.environ.get('DASHSCOPE_API_KEY','')
    if value:return value
    p=BASE/'private/secrets.env'
    if not p.exists():raise RuntimeError('missing_credentials')
    if p.stat().st_mode & 0o077:raise RuntimeError('credentials_permissions')
    for line in p.read_text().splitlines():
        if line.startswith('DASHSCOPE_API_KEY='):return line.split('=',1)[1].strip().strip('"\'')
    raise RuntimeError('missing_credentials')

def synth(request):
    os.sched_setaffinity(0,{min(os.sched_getaffinity(0))});os.nice(10)
    import dashscope
    from dashscope.audio.qwen_tts_realtime import QwenTtsRealtime,QwenTtsRealtimeCallback,AudioFormat
    dashscope.api_key=key()
    class Callback(QwenTtsRealtimeCallback):
        def __init__(self):
            self.done=threading.Event();self.audio=bytearray();self.error=None
        def on_event(self,event):
            kind=event.get('type')
            if kind=='response.audio.delta':
                self.audio.extend(base64.b64decode(event['delta'],validate=True))
                if len(self.audio)>24000*2*120:
                    self.error='audio_too_large';self.done.set()
            elif kind=='error':
                self.error=re.sub('[^a-zA-Z0-9_.-]','_',str(event.get('error',{}).get('code','service_error')))[:80]
                self.done.set()
            elif kind=='response.done':
                status=event.get('response',{}).get('status')
                if status in ('failed','cancelled'):self.error='response_'+status
                self.done.set()
        def on_close(self,*args):
            if not self.done.is_set():self.error='connection_closed';self.done.set()
    cfg=request['config']; cb=Callback()
    if cfg['url'] not in ('wss://dashscope.aliyuncs.com/api-ws/v1/realtime','wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime'):
        raise RuntimeError('unsupported_endpoint')
    client=QwenTtsRealtime(model=cfg['model'],callback=cb,url=cfg['url'])
    try:
        client.connect()
        client.update_session(voice=cfg['voice'],response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,mode='commit',language_type=cfg.get('language','Chinese'))
        client.append_text(request['text']);client.commit()
        if not cb.done.wait(float(cfg['request_timeout_seconds'])):raise RuntimeError('response_timeout')
        if cb.error:raise RuntimeError(cb.error)
        if not cb.audio or len(cb.audio)%2:raise RuntimeError('invalid_audio')
        out=Path(request['output']);temp=out.with_suffix('.%d.tmp.wav'%os.getpid())
        with wave.open(str(temp),'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(24000);f.writeframes(cb.audio)
        with temp.open('rb') as f:os.fsync(f.fileno())
        temp.replace(out)
    finally:client.close()

if __name__=='__main__':
    try:synth(json.load(sys.stdin));print(json.dumps({'ok':True}))
    except Exception as exc:
        # Never serialize exception details: SDK errors may contain request headers.
        http_status=getattr(exc,'status_code',None)
        safe=str(exc) if isinstance(exc,RuntimeError) and re.fullmatch('[a-zA-Z0-9_.-]{1,80}',str(exc)) else type(exc).__name__
        if isinstance(http_status,int):safe='HTTP_'+str(http_status)
        print(json.dumps({'ok':False,'error':safe}));sys.exit(1)
