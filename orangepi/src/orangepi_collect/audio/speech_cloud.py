"""Bounded, content-addressed cloud speech cache for the advisory voice worker."""
import fcntl
from collections import OrderedDict
import hashlib
import json
import os
import stat
import subprocess
import sys as _sys
import threading
import time
import wave
from pathlib import Path

_sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'common'))
import project_paths
from project_paths import BASE, PROJECT, SRC

CACHE_SCHEMA=2
OUTPUT_FORMAT='pcm_s16le_24000_mono_wav'
MAX_PENDING=256
MAX_TEXT=512
MAX_WAV_BYTES=8*1024*1024
MAX_DURATION=120
MAX_ATTEMPTS=3
PERMANENT_ERRORS=frozenset(('missing_credentials','credentials_permissions','unsupported_endpoint','invalid_audio','audio_too_large','HTTP_401','HTTP_403'))


class CloudCache:
    def __init__(self,base=None,config=None,runner=None):
        self.base=Path(base) if base is not None else Path(BASE)
        config_path=self.base/'configs/speech.yaml'
        if config is None:
            import yaml
            config=yaml.safe_load(config_path.read_text()) or {}
        self.config=config
        self.cache=self.base/'runtime/speech_cloud';self.cache.mkdir(parents=True,exist_ok=True)
        self.condition=threading.Condition();self.pending=OrderedDict();self.inflight=set();self.closed=False
        self.process=None;self.runner=runner;self.retry_at={};self.attempts={};self.errors={};self.rows={}
        self._last_generation_ok=None
        self.thread=threading.Thread(target=self.work,name='cloud-speech-cache',daemon=True);self.thread.start()

    def _spec(self,text):
        return {'cache_schema':CACHE_SCHEMA,'format':OUTPUT_FORMAT,
            'model':self.config.get('model'),'voice':self.config.get('voice'),
            'url':self.config.get('url'),'language':self.config.get('language'),'text':text}

    def digest(self,text):
        return hashlib.sha256(json.dumps(self._spec(text),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()

    def path(self,text):return self.cache/(self.digest(text)+'.wav')

    def _legacy_path(self,text):
        spec={key:self.config.get(key) for key in ('model','voice','url','language')};spec['text']=text
        digest=hashlib.sha256(json.dumps(spec,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
        return self.cache/(digest+'.wav')

    @staticmethod
    def valid_wav(path):
        try:
            info=path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size<44 or info.st_size>MAX_WAV_BYTES:return False
            with wave.open(str(path),'rb') as audio:
                channels,width,rate,frames=audio.getnchannels(),audio.getsampwidth(),audio.getframerate(),audio.getnframes()
                if channels!=1 or width!=2 or rate!=24000 or frames<=0 or frames/rate>MAX_DURATION:return False
                raw=audio.readframes(frames)
                return len(raw)==frames*channels*width
        except Exception:return False

    def _quarantine(self,path):
        try:
            target=path.with_name(path.stem+'.corrupt-'+str(os.getpid())+'.wav')
            if target.exists():target.unlink()
            os.replace(str(path),str(target))
            old=sorted(path.parent.glob(path.stem+'.corrupt-*.wav'),key=lambda x:x.stat().st_mtime,reverse=True)
            for stale in old[2:]:
                try:stale.unlink()
                except OSError:pass
        except OSError:
            try:path.unlink()
            except OSError:pass

    def lookup(self,text):
        path=self.path(text)
        if self.valid_wav(path):return path
        legacy=self._legacy_path(text)
        if legacy!=path and self.valid_wav(legacy):return legacy
        return None

    def _id(self,text,prompt_id):
        if isinstance(prompt_id,str) and prompt_id:return prompt_id[:96]
        return 'dynamic_'+self.digest(text)[:16]

    def _status_snapshot(self):
        with self.condition:
            rows=sorted((dict(prompt_id=row['prompt_id'],state=row['state'],error=row.get('error')) for row in self.rows.values()),key=lambda x:x['prompt_id'])
            pending=len(set(self.pending)|self.inflight)
            failed=sum(row['state']=='error' for row in rows)
            ready=sum(row['state']=='ready' for row in rows)
            last_error=next((row.get('error') for row in reversed(rows) if row['state']=='error'),None)
            retry_seconds=max(0,min(self.retry_at.values(),default=time.monotonic())-time.monotonic())
            generation_ok=self._last_generation_ok
        return dict(schema_version=CACHE_SCHEMA,pid=os.getpid(),cloud_available=generation_ok,
            last_generation_ok=generation_ok,error=last_error,failures=failed,pending=pending,retry_in_seconds=retry_seconds,
            cache_ready=bool(ready),cached=ready,total=len(rows),items=rows,time=time.time())

    def _write_status(self):
        try:
            path=self.base/'runtime/speech_status.json';path.parent.mkdir(parents=True,exist_ok=True)
            temp=path.with_name(path.name+'.%d.%d.tmp'%(os.getpid(),threading.get_ident()))
            temp.write_text(json.dumps(self._status_snapshot(),ensure_ascii=False,separators=(',',':')))
            os.replace(str(temp),str(path))
        except OSError:pass

    def request(self,text,wait=0,priority='normal',prompt_id=None,force=False):
        if not isinstance(text,str) or not text or len(text)>MAX_TEXT:return None
        target=self.path(text);hit=self.lookup(text);key=self.digest(text)
        if prompt_id is None and key in self.rows:prompt_id=self.rows[key]['prompt_id']
        identifier=self._id(text,prompt_id)
        if hit and not force:
            with self.condition:
                self.rows[key]=dict(prompt_id=identifier,state='ready',error=None)
                self.pending.pop(key,None);self.errors.pop(key,None);self.condition.notify_all()
            self._write_status();return hit
        if self.config.get('enabled') is False:
            with self.condition:self.rows[key]=dict(prompt_id=identifier,state='error',error='disabled')
            self._write_status();return None
        with self.condition:
            if self.closed:return None
            previous=self.errors.get(key)
            if force:
                self.errors.pop(key,None);self.attempts.pop(key,None);self.retry_at.pop(key,None);previous=None
            if (previous in PERMANENT_ERRORS or self.attempts.get(key,0)>=MAX_ATTEMPTS) and not force:return None
            if key not in self.pending and key not in self.inflight and len(self.pending)+len(self.inflight)>=MAX_PENDING:return None
            self.rows[key]=dict(prompt_id=identifier,state='pending',error=None)
            self.pending.setdefault(key,dict(text=text,path=target,prompt_id=identifier,priority=priority,force=force))
            if priority=='urgent' and key in self.pending:self.pending.move_to_end(key,last=False)
            self.condition.notify_all()
            deadline=time.monotonic()+max(0,float(wait))
            while wait>0 and not self.closed and time.monotonic()<deadline:
                self.condition.wait(min(.1,deadline-time.monotonic()))
                if self.valid_wav(target):break
                if key not in self.pending and key not in self.inflight and self.errors.get(key):break
        return self.lookup(text)

    def warmup(self,prompts):
        count=0
        rows=prompts.items() if isinstance(prompts,dict) else prompts
        for prompt_id,text in rows:
            if self.request(text,wait=0,priority='warm',prompt_id=prompt_id) or self.config.get('enabled') is not False:count+=1
        return count

    def last_error(self,text):return self.errors.get(self.digest(text))

    def terminal_error(self,text):
        key=self.digest(text)
        with self.condition:
            error=self.errors.get(key)
            if key not in self.pending and key not in self.inflight and error:return error
            return None

    def _acquire_key_lock(self,path):
        lockpath=path.with_suffix(path.suffix+'.lock')
        flags=os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0)
        try:fd=os.open(str(lockpath),flags,0o600)
        except OSError:return None
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);return fd
        except (BlockingIOError,OSError):
            os.close(fd);return None

    def _run_generation(self,text,path):
        if self.runner:return self.runner(text,path)
        try:
            proc=subprocess.Popen([str(self.base/'.venvs/framework/bin/python'),str(SRC/'audio/cloud_tts.py')],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,
                env=dict(os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1'))
            with self.condition:
                if self.closed:
                    proc.kill()
                    try:proc.communicate(timeout=2)
                    except Exception:pass
                    return {'ok':False,'error':'cancelled'}
                self.process=proc
            try:
                output,_=proc.communicate(json.dumps({'config':self.config,'text':text,'output':str(path)}),
                    timeout=float(self.config.get('request_timeout_seconds',35)))
            except subprocess.TimeoutExpired:
                proc.kill();proc.communicate();return {'ok':False,'error':'total_timeout'}
            for line in reversed(output.splitlines()):
                try:return json.loads(line)
                except ValueError:pass
            return {'ok':False,'error':'worker_failure'}
        except Exception:return {'ok':False,'error':'worker_failure'}
        finally:
            with self.condition:self.process=None

    def _eligible(self):
        now=time.monotonic()
        for key,row in self.pending.items():
            if key not in self.inflight and self.retry_at.get(key,0)<=now:return key,row
        return None,None

    def work(self):
        while True:
            with self.condition:
                while not self.closed:
                    key,row=self._eligible()
                    if row is not None:break
                    delay=min((value-time.monotonic() for value in self.retry_at.values() if value>time.monotonic()),default=.5)
                    self.condition.wait(max(.02,min(.5,delay)))
                if self.closed:return
                self.inflight.add(key);self.rows[key]=dict(prompt_id=row['prompt_id'],state='generating',error=None)
            self._write_status()
            fd=self._acquire_key_lock(row['path']);result={'ok':False,'error':'lock_busy'}
            try:
                if fd is None:pass
                elif self.valid_wav(row['path']) and not row.get('force'):result={'ok':True,'cached':True}
                else:
                    if row['path'].exists() or row['path'].is_symlink():self._quarantine(row['path'])
                    result=self._run_generation(row['text'],row['path'])
                    if result.get('ok') and not self.valid_wav(row['path']):result={'ok':False,'error':'invalid_audio'}
            finally:
                if fd is not None:
                    try:fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
                    except OSError:pass
            with self.condition:
                self.inflight.discard(key)
                if result.get('ok'):
                    if not result.get('cached'):self._last_generation_ok=True
                    self.pending.pop(key,None);self.retry_at.pop(key,None);self.errors.pop(key,None);self.attempts.pop(key,None)
                    self.rows[key]=dict(prompt_id=row['prompt_id'],state='ready',error=None)
                elif result.get('error')=='lock_busy':
                    self.rows[key]=dict(prompt_id=row['prompt_id'],state='pending',error=None)
                    self.retry_at[key]=time.monotonic()+.2
                    self.pending.move_to_end(key)
                else:
                    if result.get('error')!='lock_busy':self._last_generation_ok=False
                    error=result.get('error','worker_failure');count=self.attempts.get(key,0)+1;self.attempts[key]=count;self.errors[key]=error
                    permanent=error in PERMANENT_ERRORS or count>=MAX_ATTEMPTS
                    if permanent:
                        self.pending.pop(key,None);self.retry_at.pop(key,None)
                        self.rows[key]=dict(prompt_id=row['prompt_id'],state='error',error=error)
                    else:
                        self.rows[key]=dict(prompt_id=row['prompt_id'],state='pending',error=error)
                        initial=max(.05,float(self.config.get('retry_initial_seconds',1)))
                        maximum=max(initial,float(self.config.get('retry_max_seconds',30)))
                        self.retry_at[key]=time.monotonic()+min(maximum,initial*2**(count-1))
                        self.pending.move_to_end(key)
                self.condition.notify_all()
            self._write_status()

    def manifest(self):return self._status_snapshot()['items']

    def close(self):
        with self.condition:
            self.closed=True;process=self.process
            if process is not None and process.poll() is None:
                try:process.kill()
                except Exception:pass
            self.condition.notify_all()
        if threading.current_thread() is not self.thread:self.thread.join(timeout=2)
