"""Advisory speech: one bounded daemon voice worker, never an acquisition gate.

The module imports only the standard library. Manifest, router, cache imports,
cloud generation and playback all happen in the voice worker. Owner callbacks
are coalesced and delivered only by tick() on the calling/main thread.
"""
from collections import deque
import hashlib
import importlib
import math
import os
from pathlib import Path
import threading
import time

BASE = Path(__file__).parents[3]
QUEUE_LIMIT = 8
TEXT_LIMIT = 512
DEFAULT_TTL = 30.0


class CloudVoiceError(RuntimeError):
    """Compatibility import only; advisory interfaces never raise this."""


class PromptSkipped(Exception):
    """Compatibility import only."""


def _default_router(base, environment):
    return importlib.import_module('advisory_output').AudioRouter(base, environment=environment)


def _default_cache():
    return importlib.import_module('speech_cloud').CloudCache()


class Speaker:
    def __init__(self, base=None, router_factory=None, cache_factory=None):
        self.base = Path(base) if base is not None else BASE
        self.assets = self.base / 'assets/cloud_voice'
        self.env = os.environ.copy()
        self.silent = False
        self.realtime = False
        self.cancelled = lambda: False
        self.skip_requested = lambda: False
        self.on_prompt = lambda active: None
        self.last_text = ''
        self._router_factory = router_factory or _default_router
        self._cache_factory = cache_factory or _default_cache
        self._condition = threading.Condition()
        self._queue = deque()
        self._generation = 0
        self._event = threading.Event()
        self._closed = False
        self._cancel_flag = self._skip_flag = False
        self._prompt_event = None
        self._router = self._cache = None
        self._cache_generation = -1
        self._retired = deque()
        self._assets_valid = False
        self._pending_volume = None
        self._volume_revision = 0
        self._status = dict(warning='', output='none', current_sink='', speaking=False,
                            last_outcome='deferred', volume_percent=None,volume_requested=None,
                            volume_applied=None,volume_state='unknown',volume_sink='',volume_error='',volume_generation=0)
        self._thread = threading.Thread(target=self._work, name='advisory-voice', daemon=True)
        try: self._thread.start()
        except Exception:
            self._closed = True
            self._status.update(warning='语音后台暂不可用，采集不受影响。', last_outcome='failed')
        else:self._enqueue('warm_fixed',None,1800)

    def snapshot(self):
        with self._condition:
            result = dict(self._status)
            result.update(queue_size=len(self._queue), generation=self._generation)
            return result

    @property
    def status(self):
        return self.snapshot()

    @property
    def last_say_outcome(self):
        return self.snapshot()['last_outcome']

    def _publish(self, generation, **changes):
        with self._condition:
            if generation != self._generation or self._closed:
                return
            reported=changes.get('volume_generation')
            if type(reported) is int and reported<self._volume_revision:
                for key in ('volume_percent','volume_requested','volume_state','volume_error','volume_generation'):
                    changes.pop(key,None)
            if 'speaking' in changes and bool(changes['speaking']) != self._status['speaking']:
                self._prompt_event = (generation, bool(changes['speaking']))
            self._status.update(changes)

    def _enqueue(self, kind, value, ttl, front=False):
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
            with self._condition:
                self._status.update(warning='语音提示有效期无效，采集不受影响。', last_outcome='failed')
            return
        with self._condition:
            if self._closed:
                return
            if self.silent:
                self._status['last_outcome'] = 'deferred'
                return
            warning = ''
            if len(self._queue) == QUEUE_LIMIT:
                self._queue.popleft()
                warning = '语音队列已满，已丢弃较早提示。'
            item=(self._generation, self._event, time.monotonic()+float(ttl), kind, value)
            self._queue.appendleft(item) if front else self._queue.append(item)
            self._status.update(last_outcome='queued', warning=warning)
            self._condition.notify_all()

    def say(self, text, ttl=DEFAULT_TTL):
        if not isinstance(text, str):
            with self._condition:
                self._status.update(warning='语音提示无效，采集不受影响。', last_outcome='failed')
            return
        self.last_text = text[:TEXT_LIMIT]
        self._enqueue('text', self.last_text, ttl)

    def speak_prompt(self, text, ttl=DEFAULT_TTL):
        self.say(text, ttl)

    def prefetch(self, prompts, ttl=300.0, priority='warm'):
        rows=[]
        if isinstance(prompts,dict):rows=list(prompts.items())
        else:
            for index,item in enumerate(prompts or ()):
                if isinstance(item,(tuple,list)) and len(item)==2:rows.append((item[0],item[1]))
                else:rows.append(('dynamic_%d'%index,item))
        clean=[]
        for name,text in rows:
            if isinstance(name,str) and isinstance(text,str) and text and len(text)<=TEXT_LIMIT:
                clean.append((name[:96],text))
        if clean:self._enqueue('prefetch',(clean,'urgent' if priority=='urgent' else 'warm'),ttl,front=priority=='urgent')
        return len(clean)

    def play(self, path, ttl=DEFAULT_TTL):
        if not isinstance(path, (str, Path)):
            return
        self._enqueue('file', str(path)[:2048], ttl)

    def safety(self, name):
        # Legacy fatal/timeout/API-error clips can falsely claim capture stopped.
        # They are advisory notices only; never synthesize or play them here.
        self._enqueue('notice', '', DEFAULT_TTL)

    def speak_error(self, code):
        self._enqueue('notice', '', DEFAULT_TTL)

    @staticmethod
    def _terminate(process):
        if process is not None:
            try: process.terminate()
            except Exception: pass

    def _cancel(self, closing):
        with self._condition:
            if self._closed:
                return
            self._closed = bool(closing)
            self._event.set()
            self._generation += 1
            self._event = threading.Event()
            warm=[item for item in self._queue if item[3] in ('prefetch','warm_fixed')] if not closing else []
            self._queue.clear()
            for _,_,deadline,kind,value in warm:
                self._queue.append((self._generation,self._event,deadline,kind,value))
            self._cancel_flag = self._skip_flag = False
            self._prompt_event = (self._generation, False)
            self._status.update(speaking=False, current_sink='', last_outcome='cancelled')
            router, cache = self._router, self._cache
            if cache is not None and closing:
                self._cache = None
                self._retired.append(cache)
            if not closing:cache=None
            self._condition.notify_all()
        # Snapshot handles under the short lifecycle lock; terminate outside it.
        if router is not None:
            try: router.cancel_active()
            except Exception: pass
        if cache is not None and closing:
            try: cache.closed = True  # CloudCache checks this before registering a spawned child.
            except Exception: pass
            self._terminate(getattr(cache, 'process', None))

    def interrupt(self):
        self._cancel(False)

    def close(self):
        self._cancel(True)  # no main-thread join

    def set_volume(self, percent):
        if type(percent) is not int or not 0 <= percent <= 120:
            raise ValueError('音量应在 0 到 120 之间')
        with self._condition:
            if self._closed:
                return
            self._volume_revision+=1;self._pending_volume=(percent,self._volume_revision)
            self._status.update(volume_percent=percent,volume_requested=percent,volume_state='pending',volume_error='',volume_generation=self._volume_revision)
            self._condition.notify_all()

    def tick(self):
        # Legacy callbacks may touch controller state. Evaluate them only here.
        with self._condition:
            generation = self._generation
            if self._closed:
                return
        try:
            cancel = bool(self.cancelled())
            skip = bool(self.skip_requested())
        except Exception:
            cancel, skip = True, False
            self._publish(generation, warning='语音状态回调失败，采集不受影响。')
        with self._condition:
            if generation != self._generation or self._closed:
                return
            self._cancel_flag, self._skip_flag = cancel, skip
            event, self._prompt_event = self._prompt_event, None
        if event is not None and event[0] == generation:
            try: self.on_prompt(event[1])
            except Exception:
                self._publish(generation, warning='语音提示回调失败，采集不受影响。')

    def _reason(self, job, expiry=True):
        generation, event, deadline = job[:3]
        if event.is_set() or self._closed or generation != self._generation or self._cancel_flag:
            return 'cancelled'
        if self.silent:
            return 'deferred'
        if self._skip_flag:
            return 'skipped'
        if expiry and time.monotonic() >= deadline:
            return 'deferred'
        return ''

    def _sync_router(self, job):
        router = self._router
        if router is None:
            return
        try: info = router.status
        except Exception: return
        output = info.get('route', 'none')
        if output not in ('headphones', 'speaker', 'none', 'disabled'):
            output = 'none'
        playing = info.get('state') == 'playing'
        sink = info.get('active_sink', '') if playing else ''
        if not isinstance(sink, str) or len(sink) > 255:
            sink = ''
        warning = '音频配置异常，采集不受影响。' if info.get('config_degraded') else ''
        if output == 'speaker' and playing:
            warning = '耳机暂不可用，使用扬声器播报。'
        self._publish(job[0], output=output, current_sink=sink, speaking=playing,
                      last_outcome='playing' if playing else self.snapshot()['last_outcome'], warning=warning,
                      **self._volume_changes(job[0],info))

    def _volume_changes(self, generation, info):
        returned=info.get('volume_generation',0)
        with self._condition:
            if generation!=self._generation or self._closed:return {}
            current=self._status.get('volume_generation',0)
            if ((self._pending_volume is not None and self._pending_volume[1]>returned) or current>returned):
                return {key:info[key] for key in ('volume_applied','volume_sink') if key in info}
        return dict(volume_percent=info.get('volume_requested'),volume_requested=info.get('volume_requested'),
            volume_applied=info.get('volume_applied'),volume_state=info.get('volume_state','unknown'),
            volume_sink=info.get('volume_sink',''),volume_error=info.get('volume_error',''),volume_generation=returned)

    def _poll_cancel(self, job):
        self._submit_volume()
        self._sync_router(job)
        return bool(self._reason(job, expiry=False) in ('cancelled', 'deferred'))

    def _check_assets(self, job):
        if self._assets_valid:
            return True
        import json
        try:
            with (self.assets/'manifest.json').open('rb') as handle:
                raw = handle.read(32769)
            if len(raw) > 32768:
                return False
            manifest = json.loads(raw.decode('utf-8'))
            names = ['wait','fatal','api_error','timeout','unknown']+['char_'+c for c in '0123456789abcdefghijklmnopqrstuvwxyz']+['char_underscore','char_dash','char_dot']
            for name in names:
                if self._reason(job):
                    return False
                digest = hashlib.sha256(); size = 0
                with (self.assets/(name+'.wav')).open('rb') as handle:
                    while True:
                        part = handle.read(65536)
                        if not part: break
                        size += len(part)
                        if size > 8388608 or self._reason(job): return False
                        digest.update(part)
                if digest.hexdigest() != manifest[name]['sha256']:
                    return False
            self._assets_valid = True
            return True
        except Exception:
            return False

    def _get_router(self):
        with self._condition:
            router = self._router
        if router is None:
            router = self._router_factory(self.base, self.env)
            with self._condition:
                if not self._closed: self._router = router
            if self._closed:
                try: router.cancel_active()
                except Exception: pass
        return router

    def _submit_volume(self):
        # The voice worker is the sole router submitter, including its play
        # polling callback. Main setters only merge the latest pending value.
        with self._condition:
            router = self._router
            if router is None or self._closed: return
            pending, self._pending_volume = self._pending_volume, None
        if pending is not None:
            value,revision=pending
            try:
                try:router.request_volume(value,request_id=revision)
                except TypeError:router.request_volume(value)
            except Exception:
                self._publish(self._generation, warning='音量暂未应用，采集不受影响。')

    def _get_cache(self, job):
        with self._condition:
            cache = self._cache
        if cache is None:
            cache = self._cache_factory()
            with self._condition:
                if not self._closed:
                    self._cache, self._cache_generation = cache, job[0]
                else:
                    self._retired.append(cache)
        return cache

    def _cleanup_cache(self):
        with self._condition:
            retired = list(self._retired); self._retired.clear()
            if self._closed and self._cache is not None:
                retired.append(self._cache); self._cache = None
        for cache in retired:
            try: cache.close()
            except Exception: pass

    def _flush_volume(self):
        with self._condition:
            pending = self._pending_volume is not None
            router = self._router
        if pending and router is None:
            router = self._get_router()
        self._submit_volume()
        if router is not None:
            router.flush_volume()
            try:
                info=router.status;generation=self.snapshot()['generation'];state=info.get('volume_state','unknown')
                changes=self._volume_changes(generation,info)
                if state in ('failed','unavailable'):changes['warning']='音量未能应用，采集不受影响。'
                self._publish(generation,**changes)
            except Exception:pass

    def _perform(self, job):
        generation, event, deadline, kind, value = job
        if kind in ('prefetch','warm_fixed'):
            if self._closed or time.monotonic()>=deadline:return
            cache=self._get_cache(job)
            try:
                if kind=='warm_fixed':
                    from speech_prompts import fixed_prompts
                    rows=fixed_prompts().items();priority='warm'
                else:rows,priority=value
                for prompt_id,text in rows:cache.request(text,wait=0,priority=priority,prompt_id=prompt_id)
            except Exception:pass
            return
        reason = self._reason(job)
        if reason:
            self._publish(generation, last_outcome=reason)
            return
        if kind == 'notice':
            self._publish(generation, warning='语音提示暂不可用，采集不受影响。', last_outcome='deferred')
            return
        if not self._check_assets(job):
            if not self._reason(job):
                self._publish(generation, warning='语音资源校验失败，已跳过提示；采集继续。', last_outcome='failed')
            return
        router = self._get_router()
        if self._reason(job): return
        if kind == 'text':
            cache = self._get_cache(job)
            path = None
            while not self._reason(job):
                self._submit_volume()
                path = cache.request(value, wait=min(.1, max(0, deadline-time.monotonic())),priority='urgent')
                if path: break
                if getattr(cache, 'config', {}).get('enabled') is False: break
                terminal=getattr(cache,'terminal_error',lambda _text:None)(value)
                if terminal:
                    self._publish(generation,warning='语音合成失败（'+str(terminal)[:80]+'），本次提示已跳过；采集继续。',last_outcome='failed')
                    return
                self._publish(generation, warning='语音正在准备或重试，采集继续。')
                event.wait(.01)
            if not path or self._reason(job):
                self._publish(generation, warning='语音未及时生成，本次提示已跳过。', last_outcome=self._reason(job) or 'deferred')
                return
        else:
            path = value
        if self._reason(job): return
        result = router.play(path, cancelled=lambda:self._poll_cancel(job),
                             skipped=lambda:self._reason(job, expiry=False)=='skipped')
        reason = self._reason(job, expiry=False)
        outcome = reason or {'played':'played','skipped':'skipped','cancelled':'cancelled',
                             'disabled':'disabled'}.get(result, 'failed')
        self._sync_router(job)
        warning = '' if outcome in ('played','skipped','cancelled','disabled') else '语音提示暂不可用，采集继续。'
        self._publish(generation, speaking=False, current_sink='', last_outcome=outcome, warning=warning)

    def _work(self):
        try:
            while True:
                self._cleanup_cache()
                with self._condition:
                    if self._closed: break
                    if not self._queue and self._pending_volume is None:
                        self._condition.wait(.1)
                    if self._closed: break
                    job = self._queue.popleft() if self._queue else None
                    generation = self._generation
                try:
                    self._flush_volume()
                    if job is not None: self._perform(job)
                except Exception:
                    self._publish(job[0] if job else generation, speaking=False, current_sink='',
                                  warning='语音服务暂不可用，采集不受影响。', last_outcome='failed')
        finally:
            self._cleanup_cache()
            router = self._router
            if router is not None:
                try: router.cancel_active()
                except Exception: pass
