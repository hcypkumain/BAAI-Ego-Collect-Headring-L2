"""Python 3.8 controller bridge to a single Python 3.11 EEG owner."""
import json
import copy
import math
import os
from pathlib import Path
import subprocess
import threading
import time

BASE = Path(__file__).resolve().parents[3]


class EegProcess:
    name = 'eeg'

    def __init__(self, config, output=None, owner=None):
        self.owner = owner
        self.ready = False
        self.seen = 0
        self.last = time.monotonic()
        self.error = None
        self.summary = {}
        self.config = config
        self.state_lock = threading.RLock()
        self.pending_impedance = None
        self.write_lock = threading.Lock()
        self.impedance = dict(required=os.environ.get('OMNIBCI_ENABLE_IMPEDANCE_PREFLIGHT') == '1',
            supported=False, phase='unavailable', can_record=False, active=False, pending=False,
            channels=[], loff_off_confirmed=False, frontend_restored=False)
        self.process = subprocess.Popen(
            [str(BASE/'.venvs/eeg/bin/python'), '-u', str(Path(__file__).with_name('eeg_worker.py'))] +
            (['--output', str(output)] if output else ['--preview']),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True, bufsize=1)
        self.process.stdin.write(json.dumps(config)+'\n'); self.process.stdin.flush()
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _watch(self):
        try:
            for line in self.process.stdout:
                message = json.loads(line)
                if message.get('event') == 'impedance':
                    state = message.get('state')
                    if not isinstance(state,dict): raise ValueError('invalid impedance worker state')
                    with self.state_lock:
                        pending=self.pending_impedance
                        if pending:
                            matches=(state.get('ack_request_id')==pending['request_id']
                                and (pending['command']=='IMPEDANCE_START' or
                                     state.get('ack_measurement_epoch')==pending.get('measurement_epoch')==state.get('measurement_epoch')))
                            if matches:
                                self.pending_impedance=None
                                if state.get('ack_ok') is not True:state=dict(state,can_record=False)
                            else:state=dict(state,pending=True,can_record=False)
                        self.impedance = copy.deepcopy(state)
                        if state.get('active') or state.get('pending'): self.ready = False
                        # A state snapshot retains the preceding check's sample
                        # time. Repeating/reviewing it is not a new data arrival.
                        # Never rewind the ordinary stream's health clock.
                        if state.get('phase') == 'measuring' and state.get('active'):
                            stamp = state.get('last_sample_monotonic_ns')
                            if isinstance(stamp,int) and not isinstance(stamp,bool) and stamp > 0:
                                self.last = max(self.last, stamp/1e9)
                        if state.get('samples_seen') is not None: self.seen = state['samples_seen']
                if message.get('event') in ('ready', 'heartbeat'):
                    self.ready = True
                    self.seen = message['samples_seen']
                    self.last = max(self.last, message['last_sample_monotonic_ns']/1e9)
                if message.get('event') == 'error':
                    self.error = message['error']
                    if self.owner: self.owner.fail('eeg: '+self.error)
                if message.get('event') == 'closed': self.summary = message['summary']
        except Exception as exc:
            self.error = str(exc)
            if self.owner: self.owner.fail('eeg bridge: '+str(exc))

    def health(self):
        if self.process.poll() is not None:
            raise RuntimeError('eeg: worker exited: '+str(self.error or self.process.returncode))

    def impedance_snapshot(self):
        with self.state_lock: return copy.deepcopy(self.impedance)

    def can_record(self):
        if self.process.poll() is not None:return False
        state = self.impedance_snapshot()
        if state.get('required',os.environ.get('OMNIBCI_ENABLE_IMPEDANCE_PREFLIGHT') == '1') is not True:
            return True
        from impedance_quality import review_eligibility
        if not review_eligibility(state)['can_review']:return False
        return (state.get('supported') is True and state.get('can_record') is True
            and not state.get('active') and not state.get('pending') and not state.get('error')
            and state.get('loff_off_confirmed') is True and state.get('frontend_restored') is True
            and self.ready)

    def intentional_pause(self, now=None):
        state = self.impedance_snapshot()
        deadline = state.get('deadline_monotonic')
        now = time.monotonic() if now is None else now
        return (state.get('pending') is True and isinstance(deadline,(int,float))
            and not isinstance(deadline,bool) and math.isfinite(deadline) and 0 < deadline-now <= 12)

    def send_impedance(self, command):
        if (not isinstance(command,dict) or set(command)-{'command','request_id','measurement_epoch'}
                or command.get('command') not in ('IMPEDANCE_START','IMPEDANCE_END','IMPEDANCE_REVIEW')
                or not isinstance(command.get('request_id'),str) or not 0<len(command['request_id'])<=64):
            raise ValueError('invalid impedance command')
        if command['command']!='IMPEDANCE_START' and (not isinstance(command.get('measurement_epoch'),str) or not 0<len(command['measurement_epoch'])<=64):
            raise ValueError('invalid impedance measurement epoch')
        if self.process.poll() is not None: raise RuntimeError('EEG owner has exited')
        with self.state_lock:
            state=self.impedance
            if self.pending_impedance:raise ValueError('上一条阻抗操作尚未确认')
            if state.get('supported') is not True: raise ValueError('当前 EEG 后端不支持阻抗检查')
            if command['command']!='IMPEDANCE_START' and command.get('measurement_epoch')!=state.get('measurement_epoch'):
                raise ValueError('阻抗测量已变化，请刷新后重试')
            self.pending_impedance=dict(command)
            self.impedance=dict(state,can_record=False,pending=True,
                request_id=command['request_id'], deadline_monotonic=time.monotonic()+12)
            self.ready=False
        with self.write_lock:
            self.process.stdin.write(json.dumps(command)+'\n');self.process.stdin.flush()

    def close(self):
        if self.process.poll() is None:
            try:
                with self.write_lock:
                    self.process.stdin.write('STOP\n'); self.process.stdin.flush()
            except BrokenPipeError: pass
            try: self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try: self.process.wait(timeout=8)
                except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(timeout=3)
                self.error = self.error or 'EEG worker did not stop within its deadline'
        self.thread.join(timeout=2)
        self.process.stdin.close(); self.process.stdout.close()
        if self.process.returncode or self.error:
            raise RuntimeError(self.error or 'EEG worker failed: '+str(self.process.returncode))
        if not self.summary: raise RuntimeError('EEG worker exited without a final summary')
        return self.summary


def probe(config):
    result = subprocess.run(
        [str(BASE/'.venvs/eeg/bin/python'), str(Path(__file__).with_name('eeg_worker.py')), '--probe'],
        input=json.dumps(config)+'\n', capture_output=True, text=True, timeout=12)
    lines = result.stdout.splitlines()
    if result.returncode or not lines:
        error = json.loads(lines[-1]).get('error') if lines else 'EEG environment unavailable'
        raise RuntimeError(error)
    return json.loads(lines[-1])
