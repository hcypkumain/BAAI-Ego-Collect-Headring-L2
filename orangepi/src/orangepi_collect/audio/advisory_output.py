"""Best-effort headphone/speaker output for a single background voice worker.

Construction/status/request_volume do no file or command I/O. Audio failures
are results, never collector exceptions. Skip/cancel always beat completion,
errors and fallback. No Bluetooth connects or default-device changes.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import selectors
import stat
import struct
import subprocess
import sys
import threading
import time

SERVER = 'unix:/tmp/pulse-socket'
HEADPHONE = 'bluez_sink.41_42_98_47_58_B5.a2dp_sink'
SPEAKER = 'alsa_output.usb-Timesintelli_USB_Audio_Device_223483-00.mono-fallback'
PLAYER = '/opt/opi-headphone-audio/guarded-paplay'
PROBE_SECONDS = .5
VOLUME_SECONDS = .4
PLAY_GRACE = 3.0
MAX_PLAY_SECONDS = 120.0
_SINK = re.compile(r'[A-Za-z_][A-Za-z0-9_.-]{0,254}\Z')
_SERVER = re.compile(r'(?:unix:)?/[A-Za-z0-9_./-]{1,250}\Z')
_ADDRESS = re.compile(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\Z')
_SELECTION_LIMIT = 4096
_NATIVE_REASONS = frozenset((
    'requested sink is unavailable or stream moved', 'audio connection lost',
    'audio stream lost', 'audio drain failed', 'could not drain audio stream',
    'audio write failed', 'audio playback timeout', 'audio stream ready timeout',
    'audio server ready timeout', 'audio drain ended without success',
    'WAV data read failed', 'unsupported or invalid PCM WAV',
    'compatible libpulse.so.0 unavailable', 'could not connect to explicit sink',
    'could not connect to explicit audio server',
))


@dataclass(frozen=True)
class _Result:
    code: object = None
    out: bytes = b''
    err: bytes = b''
    error: str = ''
    truncated: bool = False


class _Job:
    """Nonblocking POSIX child; bounded stdout/diagnostics, no extra thread."""
    def __init__(self, argv, env, timeout):
        self.process = None; self.selector = None; self.result = None
        self.out = bytearray(); self.err = bytearray(); self.truncated = False
        self.deadline = time.monotonic()+max(.01, timeout)
        try:
            self.process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.selector = selectors.DefaultSelector()
            for stream, name in ((self.process.stdout, 'out'), (self.process.stderr, 'err')):
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, selectors.EVENT_READ, name)
        except Exception:
            self.stop()
            self.result = _Result(error='spawn_failed')

    def _read(self, stream, name):
        for _ in range(4):
            try: chunk = os.read(stream.fileno(), 4096)
            except BlockingIOError: break
            if not chunk:
                try: self.selector.unregister(stream)
                except Exception: pass
                break
            if name == 'out':
                room = max(0, 65536-len(self.out)); self.out.extend(chunk[:room])
                self.truncated |= len(chunk)>room
            else:
                self.err.extend(chunk)
                if len(self.err)>4096: del self.err[:-4096]

    def poll(self):
        if self.result is not None: return self.result
        try:
            for key, _ in self.selector.select(0): self._read(key.fileobj, key.data)
            code = self.process.poll()
            if code is not None:
                self._read(self.process.stdout, 'out'); self._read(self.process.stderr, 'err')
                self.result = _Result(code, bytes(self.out), bytes(self.err), truncated=self.truncated)
                self._close()
            elif time.monotonic()>=self.deadline:
                self.stop(); self.result = _Result(error='timeout')
        except Exception:
            self.stop(); self.result = _Result(error='process_io_failed')
        return self.result

    def _close(self):
        if self.selector is not None:
            try: self.selector.close()
            except Exception: pass
            self.selector = None
        if self.process is not None:
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    try: stream.close()
                    except Exception: pass

    def stop(self):
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                    try: self.process.wait(timeout=.05)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        try: self.process.wait(timeout=.05)
                        except subprocess.TimeoutExpired: pass
            except Exception: pass
        self._close()

    def terminate_nowait(self):
        try:
            if self.process is not None and self.process.poll() is None:self.process.terminate()
        except Exception:pass


def _volume(value):
    if type(value) is not int or not 0<=value<=120:
        raise ValueError('音量须为 0 到 120 的整数')
    return value


def _action(cancelled, skipped):
    try:
        if skipped(): return 'skipped'
        if cancelled(): return 'cancelled'
    except Exception:
        return 'cancelled'
    return None


def _duration(path):
    """Read bounded RIFF metadata only; native still validates exact PCM data."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2); size=f.tell(); f.seek(0)
            header=f.read(12)
            if len(header)!=12 or header[:4]!=b'RIFF' or header[8:]!=b'WAVE': return None
            end=struct.unpack('<I',header[4:8])[0]+8
            if end!=size or size>64*1024*1024+65536: return None
            pos=12; rate=channels=bits=0; data_size=None
            for _ in range(1024):
                if pos==end: break
                if pos+8>end: return None
                f.seek(pos); chunk=f.read(8)
                if len(chunk)!=8: return None
                length=struct.unpack('<I',chunk[4:])[0]; next_pos=pos+8+length+(length&1)
                if next_pos>end: return None
                if chunk[:4]==b'fmt ':
                    if rate or length<16: return None
                    raw=f.read(16); fmt,channels,rate,_,align,bits=struct.unpack('<HHIIHH',raw)
                    if fmt not in (1,0xfffe) or channels not in (1,2) or not 8000<=rate<=96000 or bits!=16 or align!=channels*2: return None
                elif chunk[:4]==b'data':
                    if data_size is not None: return None
                    data_size=length
                pos=next_pos
            if pos!=end or not rate or not data_size or data_size%(channels*2): return None
            return data_size/(rate*channels*2)
    except Exception:
        return None


class AudioRouter:
    def __init__(self, base, environment=None):
        self._init_degraded=False
        try:self._base=Path(base)
        except Exception:self._base=None;self._init_degraded=True
        try:self._env=dict(os.environ if environment is None else environment)
        except Exception:self._env=dict(os.environ);self._init_degraded=True
        self._lock=threading.Lock(); self._loaded=False; self._enabled=True
        self._jobs=set();self._cancel_version=0;self._operation_version=0
        self._server=SERVER; self._headphone=HEADPHONE; self._speaker=SPEAKER
        self._desired=None; self._version=0; self._request_id=0; self._pending=False
        self._active=''; self._playing=''; self._route=''; self._applied={}
        self._volume_job=None; self._volume_meta=None; self._volume_block=None; self._next_volume_try=0
        self._job_factory=_Job; self._last_log=None
        self._state={'policy':'advisory','enabled':True,'state':'idle','active_sink':'',
                     'active_route':'','last_result':'unavailable','last_error':'',
                     'volume_percent':None,'volume_requested':None,'volume_applied':None,'volume_generation':0,
                     'volume_state':'unknown','volume_sink':'','volume_error':'',
                     'config_loaded':False,'config_degraded':False,
                     'route':'none','legacy_config':False,'headphone_address':'',
                     'selection_source':'legacy','selection_degraded':False}

    @property
    def status(self):
        with self._lock: return dict(self._state)

    def _update(self, **values):
        with self._lock: self._state.update(values)

    def _begin_operation(self):
        with self._lock:self._operation_version=self._cancel_version

    def _decision(self, cancelled, skipped):
        # External callbacks may themselves read status. Never call under lock.
        action=_action(cancelled,skipped)
        if action:return action
        with self._lock:invalid=self._operation_version!=self._cancel_version
        return 'cancelled' if invalid else None

    def cancel_active(self):
        with self._lock:
            self._cancel_version+=1
            jobs=tuple(self._jobs)
        for job in jobs:
            try:job.terminate_nowait()
            except Exception:pass

    def _new_job(self, argv, env, timeout):
        job=self._job_factory(argv,env,timeout)
        with self._lock:
            self._jobs.add(job)
            invalid=self._operation_version!=self._cancel_version
        if invalid:
            try:job.terminate_nowait()
            except Exception:pass
        return job

    def _poll_job(self, job):
        result=job.poll()
        if result is not None:
            with self._lock:self._jobs.discard(job)
        return result

    def _stop_job(self, job):
        try:job.stop()
        finally:
            with self._lock:self._jobs.discard(job)

    def _log(self, event, route='', code=None, error='', err=b''):
        reason=''
        for line in err[-4096:].decode('ascii','replace').splitlines():
            if line.startswith('guarded-paplay: '):
                candidate=line[len('guarded-paplay: '):].split(' [',1)[0]
                if candidate in _NATIVE_REASONS: reason=candidate;break
        key=(event,route,code,error,reason); now=time.monotonic()
        if self._last_log and self._last_log[0]==key and now-self._last_log[1]<5: return
        self._last_log=(key,now)
        try: print('AUDIO_ADVISORY '+json.dumps({'event':event,'route':route,'code':code,'error':error,'native':reason},sort_keys=True),file=sys.stderr,flush=True)
        except Exception: pass

    def _ensure_config(self):
        if self._loaded: return
        degraded=self._init_degraded; value={}
        try:
            with (self._base/'configs/audio_output.json').open('rb') as f: raw=f.read(4097)
            if len(raw)>4096: raise ValueError()
            def unique(pairs):
                result={}
                for k,v in pairs:
                    if k in result: raise ValueError()
                    result[k]=v
                return result
            value=json.loads(raw.decode('utf-8'),object_pairs_hook=unique)
            if not isinstance(value,dict): raise ValueError()
        except Exception:
            value={};degraded=True
        legacy='policy' not in value
        explicit_advisory=value.get('policy')=='advisory'
        enabled=value.get('enabled',True) if explicit_advisory else True
        if type(enabled) is not bool: enabled=True;degraded=True
        if not legacy and not explicit_advisory:degraded=True
        server=value.get('server',SERVER)
        if not isinstance(server,str) or not _SERVER.fullmatch(server): server=SERVER;degraded=True
        headphone=value.get('headphone_sink',value.get('sink',HEADPHONE))
        speaker=value.get('speaker_sink',SPEAKER)
        if not isinstance(headphone,str) or not _SINK.fullmatch(headphone): headphone=HEADPHONE;degraded=True
        if not isinstance(speaker,str) or not _SINK.fullmatch(speaker) or speaker==headphone: speaker=SPEAKER;degraded=True
        if value.get('player',PLAYER)!=PLAYER: degraded=True
        desired=None
        if 'volume_percent' in value:
            try: desired=_volume(value['volume_percent'])
            except ValueError: degraded=True
        self._server,self._headphone,self._speaker,self._enabled=server,headphone,speaker,enabled
        with self._lock:
            if not self._version:
                self._desired=desired
                self._pending=desired is not None
            self._state.update(enabled=enabled,volume_percent=self._desired,volume_requested=self._desired,
                volume_state='pending' if desired is not None and enabled else 'disabled' if not enabled else 'unknown',
                config_loaded=True,config_degraded=degraded,state='idle' if enabled else 'disabled',
                route='none' if enabled else 'disabled',legacy_config=legacy)
        self._loaded=True
        if degraded:self._log('config_degraded')

    def _refresh_headphone_selection(self):
        """Read only in the voice worker, once per bounded sink probe.

        Missing initial selection preserves the legacy fixed output. After a
        successful read, failed or malformed reads preserve the last valid
        choice; explicit null is the only way to select speaker-only output.
        Never accept a supplied sink name or a path from this JSON.
        """
        descriptor = None
        try:
            path = self._base/'configs/headphone_selection.json'
            flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
            descriptor = os.open(str(path), flags)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _SELECTION_LIMIT:
                raise ValueError('invalid selection file')
            raw = os.read(descriptor, _SELECTION_LIMIT+1)
            if len(raw) > _SELECTION_LIMIT:
                raise ValueError('selection file too large')
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError('duplicate selection key')
                    value[key] = item
                return value
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
            if not isinstance(value, dict) or set(value) != {'version', 'address'}:
                raise ValueError('invalid selection fields')
            if type(value['version']) is not int or value['version'] != 1:
                raise ValueError('unsupported selection version')
            address = value['address']
            if address is not None and (not isinstance(address, str) or not _ADDRESS.fullmatch(address)):
                raise ValueError('invalid headphone address')
            sink = 'bluez_sink.'+address.replace(':', '_')+'.a2dp_sink' if address is not None else ''
            self._headphone = sink
            self._update(headphone_address=address or '',
                         selection_source='selected' if address is not None else 'speaker',
                         selection_degraded=False)
        except FileNotFoundError:
            # A deleted file must not undo an earlier explicit speaker choice.
            if self.status['selection_source'] != 'legacy':
                self._update(selection_degraded=True)
        except Exception:
            self._update(selection_degraded=True)
        finally:
            if descriptor is not None:
                try: os.close(descriptor)
                except Exception: pass

    def _environment(self, sink=''):
        env=dict(self._env);env['PULSE_SERVER']=self._server
        if sink:env['PULSE_SINK']=sink
        else:env.pop('PULSE_SINK',None)
        return env

    def _finish(self, result, error=''):
        self._stop_volume_job()
        self._playing=''
        self._update(last_result=result,last_error=error,state='disabled' if result=='disabled' else 'idle',active_sink=self._active,active_route=self._route,route='disabled' if result=='disabled' else self._route or 'none')
        return result

    def _wait(self, job, cancelled, skipped, volume=False):
        try:
            while True:
                action=self._decision(cancelled,skipped)
                if action:self._stop_job(job);return None,action
                if volume:self._pump_volume(cancelled,skipped)
                action=self._decision(cancelled,skipped)
                if action:self._stop_job(job);return None,action
                result=self._poll_job(job)
                # A button always wins over completion/errors/fallback.
                action=self._decision(cancelled,skipped)
                if action:self._stop_job(job);return None,action
                if result is not None:return result,None
                time.sleep(.02)
        except Exception:
            self._stop_job(job)
            return _Result(error='process_io_failed'),self._decision(cancelled,skipped)

    def _probe(self, cancelled, skipped):
        self._refresh_headphone_selection()
        action=self._decision(cancelled,skipped)
        if action:return set(),action
        self._update(state='probing')
        job=self._new_job(['pactl','--server',self._server,'list','short','sinks'],self._environment(),PROBE_SECONDS)
        result,action=self._wait(job,cancelled,skipped)
        if action:return set(),action
        if result.error or result.code or result.truncated:
            self._log('probe_failed',code=result.code,error=result.error or 'probe_failed')
            return set(),None
        found=set()
        for line in result.out.decode('utf-8','replace').splitlines():
            fields=line.split()
            if len(fields)>=2 and fields[1] in (self._headphone,self._speaker):found.add(fields[1])
        return found,None

    def request_volume(self, percent, request_id=None):
        percent=_volume(percent)
        with self._lock:
            self._desired=percent;self._version+=1;self._pending=True
            self._request_id=request_id if type(request_id) is int and request_id>=0 else self._request_id+1
            self._next_volume_try=0;self._volume_block=None
            self._state.update(volume_percent=percent,volume_requested=percent,volume_generation=self._request_id,volume_state='pending',volume_error='')

    def _stop_volume_job(self):
        if self._volume_job is not None:self._stop_job(self._volume_job)
        self._volume_job=None;self._volume_meta=None

    def _pump_volume(self, cancelled=lambda:False, skipped=lambda:False):
        if self._decision(cancelled,skipped):self._stop_volume_job();return
        if self._volume_job is not None:
            result=self._poll_job(self._volume_job)
            if self._decision(cancelled,skipped):self._stop_volume_job();return
            if result is None:return
            sink,percent,version,phase=self._volume_meta
            success=not result.error and result.code==0
            if success and phase=='set':
                self._volume_meta=(sink,percent,version,'readback')
                self._update(volume_state='reading_back')
                self._volume_job=self._new_job(['pactl','--server',self._server,'get-sink-volume',sink],self._environment(sink),VOLUME_SECONDS)
                return
            if success and phase=='readback':success=self._readback_matches(result.out,percent)
            if success:
                self._applied[sink]=(percent,version)
                with self._lock:
                    if self._version==version:
                        self._state.update(volume_applied=percent,volume_sink=sink,volume_state='applied',volume_error='')
            else:
                self._volume_block=(sink,version);self._log('volume_failed',code=result.code,error=result.error or 'volume_failed')
                with self._lock:
                    if self._version==version:self._state.update(volume_state='failed',volume_error='sink_readback_mismatch' if phase=='readback' and not result.error and result.code==0 else 'sink_apply_failed')
            with self._lock:
                if success and self._version==version:self._pending=False
            self._volume_job=None;self._volume_meta=None
        sink=self._playing
        if not sink:return
        with self._lock:percent,version,pending=self._desired,self._version,self._pending
        if percent is None:return
        if self._volume_block==(sink,version):return
        if not pending and self._applied.get(sink)==(percent,version):return
        self._volume_meta=(sink,percent,version,'set')
        with self._lock:
            if self._version==version:self._state.update(volume_state='applying',volume_error='')
        self._volume_job=self._new_job(['pactl','--server',self._server,'set-sink-volume',sink,str(percent)+'%'],self._environment(sink),VOLUME_SECONDS)

    @staticmethod
    def _readback_matches(output, percent):
        try:
            values=[float(value) for value in re.findall(rb'/\s*(\d+(?:\.\d+)?)%',output)]
            return bool(values) and all(abs(value-percent)<=0.6 for value in values)
        except (TypeError,ValueError):return False

    def _prepare_volume(self, cancelled, skipped):
        self._pump_volume(cancelled,skipped)
        until=time.monotonic()+2*VOLUME_SECONDS+.1
        while self._volume_job is not None and time.monotonic()<until:
            action=self._decision(cancelled,skipped)
            if action:self._stop_volume_job();return action,False
            self._pump_volume(cancelled,skipped)
            if self._volume_job is not None:time.sleep(.02)
        if self._volume_job is not None:
            phase=self._volume_meta[3] if self._volume_meta else 'set';self._stop_volume_job()
            with self._lock:self._state.update(volume_state='failed',volume_error='sink_'+phase+'_timeout')
        with self._lock:desired,version=self._desired,self._version
        return self._decision(cancelled,skipped),desired is None or self._applied.get(self._playing)==(desired,version)

    def flush_volume(self):
        try:
            self._begin_operation()
            self._ensure_config()
            if not self._enabled:
                self._update(volume_state='disabled',volume_error='')
                return False
            with self._lock:pending=self._pending;next_try=self._next_volume_try
            if not pending:return True
            if time.monotonic()<next_try:return False
            until=time.monotonic()+PROBE_SECONDS+2*VOLUME_SECONDS+.2
            found,_=self._probe(lambda:False,lambda:False)
            targets=[]
            for sink in (self._active,self._headphone,self._speaker):
                if sink and sink in found and sink not in targets:targets.append(sink)
            for sink in targets:
                if time.monotonic()>=until:break
                self._playing=sink;self._volume_block=None
                self._pump_volume()
                while self._volume_job is not None and time.monotonic()<until:
                    self._pump_volume();time.sleep(.02)
                if self._volume_job is not None:
                    phase=self._volume_meta[3] if self._volume_meta else 'set';self._stop_volume_job()
                    with self._lock:self._state.update(volume_state='failed',volume_error='sink_'+phase+'_timeout')
                    break
                with self._lock:pending=self._pending
                if not pending:self._playing='';return True
            with self._lock:
                self._next_volume_try=time.monotonic()+1
                if self._pending and not targets and self._state.get('volume_state')!='failed':self._state.update(volume_state='unavailable',volume_error='sink_unavailable')
            self._playing='';return False
        except Exception:
            self._stop_volume_job();self._playing='';self._log('volume_failed',error='internal_error');return False
        finally:
            self._playing=''
            self._update(state='idle' if self._enabled else 'disabled')

    def set_volume(self, percent):
        self.request_volume(percent)
        return self.flush_volume()

    def play(self, path, cancelled=lambda:False, skipped=lambda:False):
        try:
            self._begin_operation()
            action=self._decision(cancelled,skipped)
            if action:return self._finish(action)
            self._ensure_config()
            if not self._enabled:return self._finish('disabled')
            duration=_duration(path)
            action=self._decision(cancelled,skipped)
            if action:return self._finish(action)
            if duration is None:return self._finish('failed','invalid_wav')
            timeout=min(MAX_PLAY_SECONDS,max(.1,duration)+PLAY_GRACE)
            found,action=self._probe(cancelled,skipped)
            if action:return self._finish(action)
            if not found:return self._finish('unavailable','no_output')
            attempted=set()
            for name,sink in (('headphones',self._headphone),('speaker',self._speaker)):
                action=self._decision(cancelled,skipped)
                if action:return self._finish(action)
                if sink not in found or sink in attempted:continue
                attempted.add(sink);self._stop_volume_job();self._playing=sink;self._volume_block=None
                if sink!=self._active:self._applied.pop(sink,None)
                action,volume_ready=self._prepare_volume(cancelled,skipped)
                if action:return self._finish(action)
                if not volume_ready:
                    self._log('volume_failed',name,error='preplay_volume_not_applied')
                    action=self._decision(cancelled,skipped)
                    if action:return self._finish(action)
                    if name=='headphones':
                        found,action=self._probe(cancelled,skipped)
                        if action:return self._finish(action)
                    continue
                self._update(state='playing',active_sink=sink,active_route=name,route=name)
                job=self._new_job([PLAYER,'--server',self._server,'--sink',sink,str(path)],self._environment(sink),timeout)
                result,action=self._wait(job,cancelled,skipped,volume=True)
                if action:return self._finish(action)
                if not result.error and result.code==0:
                    self._active=sink;self._route=name
                    return self._finish('played')
                self._log('play_failed',name,result.code,result.error or 'native_failed',result.err)
                action=self._decision(cancelled,skipped)
                if action:return self._finish(action)
                if name=='headphones':
                    found,action=self._probe(cancelled,skipped)
                    if action:return self._finish(action)
            return self._finish('failed','outputs_failed')
        except Exception:
            action=self._decision(cancelled,skipped)
            if action:return self._finish(action)
            self._log('play_failed',error='internal_error')
            return self._finish('failed','internal_error')
