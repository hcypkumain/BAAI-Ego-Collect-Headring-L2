"""Bounded background cloud cache with retry; compatible with system Python 3.8."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0,str(_Path(__file__).resolve().parents[1]/'common'))
import project_paths
import hashlib,json,os,subprocess,threading,time
from pathlib import Path
import yaml
from project_paths import BASE, PROJECT, SRC, devices
class CloudCache:
    def __init__(self):
        self.config=yaml.safe_load((BASE/'configs/speech.yaml').read_text())
        self.cache=BASE/'runtime'/'speech_cloud';self.cache.mkdir(parents=True,exist_ok=True)
        self.condition=threading.Condition();self.pending={};self.closed=False;self.process=None
        self.next_try=0.;self.failures=0;self.errors={}
        self.thread=threading.Thread(target=self.work,daemon=True);self.thread.start()
    def path(self,text):
        spec={k:self.config.get(k) for k in ('model','voice','url','language')};spec['text']=text
        return self.cache/(hashlib.sha256(json.dumps(spec,ensure_ascii=False,sort_keys=True).encode()).hexdigest()+'.wav')
    def request(self,text,wait=0):
        p=self.path(text)
        if p.exists():return p
        if not self.config.get('enabled'):return None
        with self.condition:
            if len(self.pending)<256:self.pending.setdefault(text,p)
            self.condition.notify_all()
            deadline=time.monotonic()+wait
            while not p.exists() and not self.closed and time.monotonic()<deadline:
                self.condition.wait(max(.01,deadline-time.monotonic()))
        return p if p.exists() else None
    def last_error(self,text):
        with self.condition:return self.errors.get(text)

    def work(self):
        while True:
            with self.condition:
                if self.closed:return
                if not self.pending:
                    self.condition.wait();continue
                if time.monotonic()<self.next_try:
                    self.condition.wait(self.next_try-time.monotonic());continue
                text,p=next(iter(self.pending.items()))
            result={'ok':False,'error':'worker_failure'}
            try:
                proc=subprocess.Popen([str(BASE/'.venvs/framework/bin/python'),str(SRC/'audio/cloud_tts.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=dict(os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1'))
                with self.condition:
                    self.process=proc
                    if self.closed:proc.kill()
                try:
                    output,_=proc.communicate(json.dumps({'config':self.config,'text':text,'output':str(p)}),timeout=float(self.config['request_timeout_seconds']))
                    for line in reversed(output.splitlines()):
                        try:result=json.loads(line);break
                        except ValueError:pass
                except subprocess.TimeoutExpired:
                    proc.kill();proc.communicate();result={'ok':False,'error':'total_timeout'}
            except Exception:pass
            with self.condition:
                self.process=None
                if result.get('ok') and p.exists():
                    self.pending.pop(text,None);self.errors.pop(text,None);self.failures=0;self.next_try=0
                else:
                    self.errors[text]=result.get('error','worker_failure')
                    self.failures+=1
                    delay=min(float(self.config['retry_max_seconds']),float(self.config['retry_initial_seconds'])*2**min(self.failures-1,8))
                    self.next_try=time.monotonic()+delay
                status={'pid':os.getpid(),'cloud_available':bool(result.get('ok')),'error':result.get('error'),'failures':self.failures,'pending':len(self.pending),'retry_in_seconds':max(0,self.next_try-time.monotonic()),'time':time.time()}
                temp=BASE/'runtime'/('speech_status.%d.%d.tmp'%(os.getpid(),threading.get_ident()));temp.write_text(json.dumps(status));temp.replace(BASE/'runtime'/'speech_status.json')
                self.condition.notify_all()
    def close(self):
        with self.condition:
            self.closed=True
            if self.process and self.process.poll() is None:self.process.kill()
            self.condition.notify_all()
        self.thread.join(timeout=2)
