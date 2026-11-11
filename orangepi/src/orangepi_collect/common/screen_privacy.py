"""Fail-closed screen/capture handshake; no display or acquisition hardware access."""
import copy
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

POLICY = 'screen_black_before_capture_v1'
STOP_PROTOCOL = 'session-stop-v1'
CLIENT_ID = re.compile(r'^[A-Za-z0-9_.-]{1,96}$')


def boot_identity():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return None


def process_start_ticks(pid):
    if type(pid) is not int or pid <= 0:return None
    try:
        text=Path('/proc/%d/stat'%pid).read_text()
        fields=text[text.rindex(')')+2:].split()
        if fields[0] in ('Z','X'):return None
        return int(fields[19])
    except (OSError,ValueError,IndexError):return None


class ScreenPrivacy:
    def __init__(self, clock=time.monotonic, lease_seconds=5.0, settle_seconds=.25):
        self.clock = clock
        self.lease = float(lease_seconds)
        self.settle = float(settle_seconds)
        self.lock = threading.RLock()
        self.instance = secrets.token_hex(16)
        self.generation = 0
        self.clients = {}
        self.cohort = set()
        self.phase = 'idle'
        self.nonce = ''
        self.session = ''
        self.committed = False
        self.committed_ns = 0
        self.cancelled = False
        self.error = ''
        self.owner = {}
        self.start_proof = None
        self.last_stopped_target = None

    def display(self, value):
        if not isinstance(value, dict) or not CLIENT_ID.fullmatch(str(value.get('client_id', ''))):
            raise ValueError('无效的屏幕标识')
        identity = value['client_id']
        with self.lock:
            now = self.clock()
            if identity not in self.clients and len(self.clients) >= 16:
                stale = [k for k, v in self.clients.items() if now - v['seen'] > self.lease and k not in self.cohort]
                for key in stale:
                    self.clients.pop(key, None)
                if len(self.clients) >= 16:
                    raise ValueError('屏幕连接过多')
            client = self.clients.setdefault(identity, {'ack': '', 'ack_at': 0.0})
            if now-client.get('seen',now)>self.lease:
                client['ack']='';client['ack_at']=0.0
            client['seen'] = now
            client['stop_protocol'] = value.get('stop_protocol')
            if self.phase == 'arming' and not self.committed:
                self.cohort.add(identity)

    def acknowledge(self, value):
        if not isinstance(value, dict):
            raise ValueError('无效的黑屏确认')
        with self.lock:
            identity = value.get('client_id')
            client = self.clients.get(identity) if isinstance(identity, str) else None
            if value.get('stop_protocol') != STOP_PROTOCOL or not client or client.get('stop_protocol') != STOP_PROTOCOL:
                raise ValueError('页面版本已更新，请刷新后再开始')
            if (self.phase != 'arming' or self.committed or self.cancelled or not client
                    or value.get('nonce') != self.nonce or not self.nonce
                    or self.clock() - client['seen'] > self.lease or identity not in self.cohort):
                raise ValueError('黑屏确认已过期，请重新同步')
            if client['ack'] != self.nonce:
                client['ack'] = self.nonce
                client['ack_at'] = self.clock()

    def arm(self, session, worker_pid=None, adapter_pid=None, owner_run=None):
        if not isinstance(session, str) or not session:
            raise ValueError('缺少本段标识')
        with self.lock:
            if self.phase != 'idle':
                raise ValueError('上次采集的停止状态尚未确认')
            now = self.clock()
            self.clients = {k: v for k, v in self.clients.items() if now - v['seen'] <= self.lease}
            self.cohort = set(self.clients)
            self.session = session
            self.nonce = secrets.token_hex(24)
            self.phase = 'arming'
            self.committed = False
            self.committed_ns = 0
            self.cancelled = False
            self.error = ''
            self.owner={'worker_pid':worker_pid,'worker_start_ticks':process_start_ticks(worker_pid),
                        'adapter_pid':adapter_pid,'adapter_start_ticks':process_start_ticks(adapter_pid),
                        'owner_run':owner_run}
            self.start_proof=None
            self.last_stopped_target=None
            self.generation += 1

    def ready(self):
        with self.lock:
            now = self.clock()
            return (self.phase == 'arming' and not self.cancelled and not self.committed
                    and bool(self.cohort) and all(
                        k in self.clients and now - self.clients[k]['seen'] <= self.lease
                        and self.clients[k].get('stop_protocol') == STOP_PROTOCOL
                        and self.clients[k]['ack'] == self.nonce
                        and now - self.clients[k]['ack_at'] >= self.settle for k in self.cohort))

    def require_stop_protocol(self):
        with self.lock:
            now=self.clock()
            if any(now-v['seen'] <= self.lease and v.get('stop_protocol') != STOP_PROTOCOL for v in self.clients.values()):
                raise ValueError('页面版本已更新，请刷新所有连接的采集页面后再开始')

    def target_matches(self, target, allow_same_session=False, allow_stopped=False):
        with self.lock:
            if (not isinstance(target,dict) or set(target)!={'instance','session_id','nonce','generation'}
                    or target.get('instance')!=self.instance or type(target.get('generation')) is not int):return False
            active=(target.get('session_id')==self.session and target.get('nonce')==self.nonce and bool(self.nonce)
                    and (target['generation']==self.generation or
                         allow_same_session and 0<=target['generation']<self.generation))
            if active:return True
            stopped=self.last_stopped_target
            return bool(allow_stopped and self.phase=='idle' and isinstance(stopped,dict)
                and target.get('session_id')==stopped.get('session_id')
                and target.get('nonce')==stopped.get('nonce')
                and 0<=target['generation']<=stopped.get('generation',-1))

    def cancel_request(self, target):
        with self.lock:
            if not self.target_matches(target):raise ValueError('采集会话已变化，请刷新后重试')
            if self.phase == 'arming' and not self.committed:
                self.cancelled = True
                self.generation += 1
                return True
            return False

    def cancel_before_start(self):
        with self.lock:
            if self.committed:
                raise ValueError('已发送开始请求，必须确认停止后才能唤醒')
            self.phase = 'idle'
            self.nonce = ''
            self.cohort.clear()
            self.generation += 1

    def commit(self):
        with self.lock:
            if not self.ready():
                raise ValueError('触控屏尚未确认黑屏，未开始采集')
            if not boot_identity() or process_start_ticks(os.getpid()) is None:
                raise ValueError('无法确认当前控制进程，未开始采集')
            for role in ('worker','adapter'):
                pid,ticks=self.owner.get(role+'_pid'),self.owner.get(role+'_start_ticks')
                if type(pid) is not int or type(ticks) is not int or ticks<=0 or process_start_ticks(pid)!=ticks:
                    raise ValueError('采集进程已变化，未开始采集')
            self.committed = True
            self.committed_ns = time.monotonic_ns()
            self.generation += 1
            proof={'schema_version': 1, 'policy': POLICY, 'nonce': self.nonce,
                    'session_id': self.session, 'controller_pid': os.getpid(),
                    'controller_start_ticks':process_start_ticks(os.getpid()),
                    'boot_id': boot_identity(), 'committed_monotonic_ns': self.committed_ns,
                    'black_confirmed_monotonic_ns': int(max(self.clients[k]['ack_at'] for k in self.cohort)*1e9),
                    'display_ids': sorted(self.cohort), 'settle_seconds': self.settle,
                    'privacy_instance':self.instance,'privacy_generation':self.generation,
                    'display_acknowledged': True,**self.owner}
            self.start_proof=copy.deepcopy(proof)
            return proof

    def recording(self):
        with self.lock:
            if self.committed:
                if self.phase != 'recording':self.generation += 1
                self.phase = 'recording'

    def stopping(self, target=None, allow_same_session=True):
        with self.lock:
            if target is not None and not self.target_matches(target,allow_same_session=allow_same_session):
                raise ValueError('采集会话已变化，请刷新后重试')
            if self.committed:
                if self.phase != 'stopping':self.generation += 1
                self.phase = 'stopping'
            return {'instance':self.instance,'session_id':self.session,'nonce':self.nonce,'generation':self.generation}

    def fault(self, message):
        with self.lock:
            if self.committed:
                if self.phase != 'fault':self.generation += 1
                self.phase = 'fault'
                self.error = str(message)[:160]

    def confirm_stopped(self, value):
        with self.lock:
            stamp = value.get('stop_monotonic_ns') if isinstance(value, dict) else None
            if (not self.committed or type(stamp) is not int or stamp < self.committed_ns
                    or stamp>time.monotonic_ns()
                    or value.get('native_reaped') is not True
                    or type(value.get('native_exit_code')) is not int or value['native_exit_code'] < 0
                    or value.get('capture_finalized') is not True
                    or value.get('shutdown_complete') is not True
                    or (value.get('summary_status'),value['native_exit_code']==0) not in (('success',True),('error',False))
                    or type(value.get('finalized_monotonic_ns')) is not int
                    or not stamp <= value['finalized_monotonic_ns'] <= time.monotonic_ns()
                    or value.get('recording_active') is not False
                    or not self.start_proof or value.get('screen_privacy')!=self.start_proof
                    or value.get('worker_pid')!=self.start_proof.get('worker_pid')
                    or value.get('boot_id')!=self.start_proof.get('boot_id')):
                return False
            self.last_stopped_target={'instance':self.instance,'session_id':self.session,
                'nonce':self.nonce,'generation':self.generation}
            self.phase = 'idle'
            self.nonce = ''
            self.committed = False
            self.error = ''
            self.cohort.clear()
            self.generation += 1
            return True

    def confirm_owner_exited(self,value):
        """Release the privacy cover only after this capture's owned process group is gone.

        This recovery proves that the controller's capture owner exited. It does
        not claim that the native recorder completed STOP, saved successfully,
        or passed QC.
        """
        with self.lock:
            start=self.start_proof
            if (not self.committed or self.phase not in ('arming','recording','stopping','fault')
                    or not isinstance(start,dict) or not isinstance(value,dict)
                    or value.get('schema_version')!=1
                    or value.get('policy')!='capture-owner-group-exit-v1'
                    or value.get('session_id')!=self.session or value.get('nonce')!=self.nonce
                    or value.get('privacy_instance')!=self.instance
                    or value.get('privacy_generation')!=start.get('privacy_generation')
                    or value.get('screen_privacy')!=start
                    or value.get('owner_run')!=start.get('owner_run')
                    or not isinstance(start.get('owner_run'),str) or not start['owner_run']
                    or value.get('registry_run')!=start.get('owner_run')
                    or value.get('registry_kind')!='capture'
                    or (value.get('registry_finish_verified') is not True
                        and value.get('registry_liveness_verified') is not True)
                    or type(value.get('registry_cleanup_confirmed')) is not bool
                    or value.get('group_absent') is not True
                    or value.get('adapter_reaped') is not True
                    or value.get('native_shutdown_confirmed') is not False
                    or value.get('native_qc_success') is not False
                    or value.get('boot_id')!=start.get('boot_id')
                    or value.get('boot_id')!=boot_identity()
                    or value.get('controller_pid')!=start.get('controller_pid')
                    or value.get('controller_start_ticks')!=start.get('controller_start_ticks')
                    or value.get('controller_pid')!=os.getpid()
                    or process_start_ticks(os.getpid())!=value.get('controller_start_ticks')
                    or value.get('adapter_pid')!=start.get('adapter_pid')
                    or value.get('adapter_start_ticks')!=start.get('adapter_start_ticks')
                    or value.get('worker_pid')!=start.get('worker_pid')
                    or value.get('worker_start_ticks')!=start.get('worker_start_ticks')
                    or type(value.get('pgid')) is not int or value.get('pgid')!=value.get('adapter_pid')
                    or value.get('sid')!=value.get('pgid')
                    or type(value.get('observed_monotonic_ns')) is not int
                    or value['observed_monotonic_ns']<self.committed_ns
                    or value['observed_monotonic_ns']>time.monotonic_ns()):
                return False
            try:
                from scan_registry import RegistryError,group_exists,proc_identity
                if group_exists(value['pgid']):return False
                for key,start_key in (('adapter_pid','adapter_start_ticks'),('worker_pid','worker_start_ticks')):
                    identity=proc_identity(value[key])
                    if identity and identity.get('start')==value[start_key] and identity.get('state') not in ('Z','X'):
                        return False
            except (OSError,ValueError,TypeError,RegistryError):return False
            self.last_stopped_target={'instance':self.instance,'session_id':self.session,
                'nonce':self.nonce,'generation':self.generation}
            self.phase='idle';self.nonce='';self.committed=False;self.cancelled=False
            self.error='';self.cohort.clear();self.owner={};self.generation+=1
            return True

    def snapshot(self):
        with self.lock:
            now = self.clock()
            active = [k for k, v in self.clients.items() if now - v['seen'] <= self.lease]
            return {'policy': POLICY, 'required': self.phase != 'idle', 'phase': self.phase,
                    'nonce': self.nonce, 'session_id': self.session, 'instance': self.instance,
                    'generation': self.generation,
                    'stop_confirmed': self.phase == 'idle', 'display_count': len(active),
                    'acknowledged_count': sum(k in self.clients and self.clients[k]['ack'] == self.nonce
                                              for k in self.cohort) if self.nonce else 0,
                    'cancel_requested': self.cancelled, 'error': self.error or None}


def validate_screen_start(root, now_ns=None):
    root = Path(root)
    try:
        with (root / 'screen_privacy_gate.json').open('rb') as handle:
            data = handle.read(8193)
        if len(data) > 8192:
            raise ValueError('屏幕确认文件过大')
        value = json.loads(data)
        if not isinstance(value,dict):raise ValueError('屏幕确认必须是对象')
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError('START blocked: screen black confirmation is missing') from exc
    now_ns = time.monotonic_ns() if now_ns is None else now_ns
    stamp = value.get('committed_monotonic_ns')
    black_stamp = value.get('black_confirmed_monotonic_ns')
    displays = value.get('display_ids')
    if (value.get('schema_version') != 1 or value.get('policy') != POLICY
            or value.get('session_id') != str(root.resolve())
            or not boot_identity() or value.get('boot_id') != boot_identity() or value.get('display_acknowledged') is not True
            or not isinstance(value.get('nonce'), str) or not re.fullmatch('[0-9a-f]{48}',value['nonce'])
            or type(stamp) is not int or not 0 <= now_ns - stamp <= 10_000_000_000
            or type(black_stamp) is not int or not 250_000_000 <= stamp-black_stamp <= 60_000_000_000
            or not isinstance(displays, list) or not 1 <= len(displays) <= 16
            or any(not isinstance(d, str) or not CLIENT_ID.fullmatch(d) for d in displays)
            or len(set(displays)) != len(displays)):
        raise ValueError('START blocked: screen black confirmation is invalid or stale')
    for role in ('controller','adapter','worker'):
        pid,ticks=value.get(role+'_pid'),value.get(role+'_start_ticks')
        if type(pid) is not int or pid<=0 or type(ticks) is not int or ticks<=0 or process_start_ticks(pid)!=ticks:
            raise ValueError('START blocked: screen confirmation owner is no longer valid')
    if value['worker_pid']!=os.getpid() or value['adapter_pid']!=os.getppid():
        raise ValueError('START blocked: screen confirmation belongs to another worker')
    return copy.deepcopy(value)
