"""Nonblocking, kernel-event-backed EMG insertion order. Never opens serial IO."""
import copy
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

METHOD = 'ordered_insertion'
VERSION = 3
SLOTS = ('emg_left', 'emg_right')
EMPTY_STABLE_NS = 300_000_000
MAX_BUFFER = 128 * 1024
MAX_EVENTS = 64
RESOLVE_TIMEOUT_NS = 3_000_000_000
MESSAGES = {
    'unplug_all': '请只拔下两只肌电接收器，头环、腕部相机和脑电帽保持连接；两只接收器都断开后，再按提示顺序插入。',
    'wait_left': '请先插入左侧设备对应的肌电接收器，等待系统确认已识别第一只后，再插右侧接收器。',
    'wait_right': '已记录第一只为左臂。现在请插入实际右臂的肌电接收器。',
    'verify_signals': '左右插入顺序已记录，正在等待两路当前有效肌电数据。',
    'error': '肌电接入证据已失效。请点击重新确认，只将两只肌电接收器拔下后按提示顺序插入；其他设备保持连接。',
}
SPEECH = {
    'unplug_all': '请只拔下两只肌电接收器，其他设备保持连接。',
    'wait_left': '请先插入左侧肌电接收器，等待系统确认。',
    'wait_right': '左侧接收器已识别。现在请插入右侧肌电接收器。',
    'verify_signals': '接入顺序已记录，正在等待两路有效肌电数据。',
    'error': '肌电接入确认未通过。请重新开始，只重新插入两只肌电接收器。',
}


class PairingError(ValueError):
    pass


def canonical_stamp(stamp):
    if not isinstance(stamp, list):
        raise PairingError('invalid_inventory')
    rows = []
    for row in stamp:
        if (not isinstance(row, list) or len(row) != 5 or
                any(not isinstance(v, str) or not v.strip() for v in row[:4]) or
                type(row[4]) is not int or row[4] <= 0):
            raise PairingError('invalid_inventory')
        rows.append(list(row))
    if len({r[0] for r in rows}) != len(rows) or len({r[3] for r in rows}) != len(rows):
        raise PairingError('ambiguous_receivers')
    return sorted(rows)  # Canonical evidence only; this never assigns a side.


def _usb_id(vendor, product):
    try:
        return '%04x:%04x' % (int(vendor, 16), int(product, 16))
    except (ValueError, TypeError):
        return None


def usb_units(settings=None, sys_root=Path('/sys')):
    allowed = set((settings or {}).get('emg_usb_ids', ['1a86:55d3']))
    units = []
    for node in (Path(sys_root) / 'bus/usb/devices').glob('*'):
        try:
            vendor = (node / 'idVendor').read_text().strip()
            product = (node / 'idProduct').read_text().strip()
            if _usb_id(vendor, product) not in allowed:
                continue
            try:
                serial = (node / 'serial').read_text().strip()
            except OSError:
                serial = ''
            units.append(dict(vendor=vendor, product=product, serial=serial,
                devpath='/' + str(node.resolve().relative_to(Path(sys_root).resolve()))))
        except (OSError, ValueError):
            continue
    return units


class KernelEventBuffer:
    """Parse bounded udevadm --kernel output, retaining unresolved add order."""
    def __init__(self, settings=None, sys_root=Path('/sys')):
        self.allowed = set((settings or {}).get('emg_usb_ids', ['1a86:55d3']))
        self.sys_root = Path(sys_root)
        self.buffer = bytearray()
        self.pending = []
        self.cache = {}
        self.last_sequence = 0

    def feed(self, data, now_ns=None):
        if now_ns is None:
            now_ns = time.monotonic_ns()
        self.buffer.extend(data)
        if len(self.buffer) > MAX_BUFFER:
            raise PairingError('event_buffer_overflow')
        while b'\n\n' in self.buffer:
            raw, _, rest = self.buffer.partition(b'\n\n')
            self.buffer = bytearray(rest)
            lines = raw.decode('utf-8', 'strict').splitlines()
            header = next((line for line in lines if line.startswith('KERNEL[')), None)
            if not header:
                continue  # Initial udevadm banner is not a device event.
            props = dict(line.split('=', 1) for line in lines if '=' in line and not line.startswith('KERNEL['))
            if props.get('SUBSYSTEM') != 'usb' or props.get('DEVTYPE') != 'usb_device':
                continue
            product = props.get('PRODUCT', '').split('/')
            if len(product) >= 2 and _usb_id(product[0], product[1]) not in self.allowed:
                continue
            if props.get('ACTION') not in ('add', 'remove'):
                continue
            try:
                sequence = int(props['SEQNUM'])
                match = re.match(r'^KERNEL\[\s*([0-9]+(?:\.[0-9]+)?)\]', header)
                if not match:
                    raise ValueError()
                kernel_ns = int(float(match.group(1)) * 1e9)
            except (KeyError, ValueError, OverflowError):
                raise PairingError('invalid_kernel_event')
            devpath = props.get('DEVPATH', '')
            if (sequence <= self.last_sequence or kernel_ns <= 0 or
                    not devpath.startswith('/devices/') or
                    not re.fullmatch(r'/devices/[A-Za-z0-9_./:+-]+', devpath)):
                raise PairingError('invalid_kernel_event')
            self.last_sequence = sequence
            self.pending.append(dict(action=props['ACTION'], sequence=sequence,
                kernel_time_ns=kernel_ns, devpath=devpath, received_ns=now_ns,
                source='kernel_udevadm'))
            if len(self.pending) > MAX_EVENTS:
                raise PairingError('too_many_kernel_events')

    def _identity(self, event):
        if event['action'] == 'remove':
            return self.cache.get(event['devpath'])
        node = (self.sys_root / event['devpath'].lstrip('/')).resolve()
        try:
            node.relative_to((self.sys_root / 'devices').resolve())
            vendor = (node / 'idVendor').read_text().strip()
            product = (node / 'idProduct').read_text().strip()
            if _usb_id(vendor, product) not in self.allowed:
                return False
            serial = (node / 'serial').read_text().strip()
            if not serial:
                return None
            identity = dict(vendor=vendor, product=product, serial=serial)
            if event['devpath'] not in self.cache and len(self.cache) >= MAX_EVENTS:
                raise PairingError('too_many_kernel_identities')
            self.cache[event['devpath']] = identity
            return identity
        except PairingError:
            raise
        except (OSError, ValueError):
            return None

    def drain(self, now_ns=None):
        if now_ns is None:
            now_ns = time.monotonic_ns()
        ready = []
        while self.pending:
            event = self.pending[0]
            identity = self._identity(event)
            if identity is None and event['action'] == 'add':
                if now_ns - event['received_ns'] > RESOLVE_TIMEOUT_NS:
                    raise PairingError('usb_identity_not_ready')
                break  # A later-ready second receiver must not become the left.
            self.pending.pop(0)
            if identity:
                ready.append(dict(event, **identity))
            if event['action'] == 'remove':
                self.cache.pop(event['devpath'], None)
        return ready


class KernelUSBMonitor:
    """One small, read-only monitor process, only alive during the wizard."""
    def __init__(self, settings=None):
        executable = shutil.which('udevadm')
        if not executable:
            raise PairingError('udevadm_missing')
        self.events = KernelEventBuffer(settings)
        self.process = subprocess.Popen([executable, 'monitor', '--kernel',
            '--subsystem-match=usb', '--property'], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
        try:
            os.set_blocking(self.process.stdout.fileno(), False)
        except BaseException:
            self.close()
            raise

    def drain(self):
        if self.process.poll() is not None:
            raise PairingError('kernel_monitor_stopped')
        for _ in range(4):
            try:
                data = os.read(self.process.stdout.fileno(), 32768)
            except BlockingIOError:
                break
            if not data:
                raise PairingError('kernel_monitor_closed')
            self.events.feed(data)
        return self.events.drain()

    @property
    def last_sequence(self):
        return self.events.last_sequence

    def close(self):
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=.3)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=.3)
        finally:
            self.process.stdout.close()
            self.process = None


class OrderedPairing:
    def __init__(self, boot, now_ns=None):
        if not isinstance(boot, str) or not boot:
            raise PairingError('missing_boot')
        self.boot = boot
        self.session_id = uuid.uuid4().hex
        self.phase = 'unplug_all'
        self.error = None
        self.empty_since_ns = None
        self.baseline = None
        self.events = []
        self.left_checkpoint = None
        self.rows = {}
        self.data_ready = dict.fromkeys(SLOTS, False)
        self.pending_unknown_since = None
        self.stamp = []

    def fault(self, reason):
        self.phase = 'error'; self.error = str(reason)

    @property
    def preview_ready(self):
        return self.phase == 'verify_signals' and len(self.rows) == 2

    def observe(self, boot, units, stamp, events, last_sequence=0, now_ns=None, wall_ns=None):
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        wall_ns = time.time_ns() if wall_ns is None else wall_ns
        if self.phase == 'error':
            return
        try:
            if boot != self.boot:
                raise PairingError('boot_changed')
            current = canonical_stamp(stamp)
            serials = [unit.get('serial') for unit in units]
            if (len(units) > 2 or any(not isinstance(v, str) or not v for v in serials) or
                    len(set(serials)) != len(serials)):
                raise PairingError('ambiguous_receivers')
            if self.phase == 'unplug_all':
                if units or current:
                    self.empty_since_ns = None
                    return
                if self.empty_since_ns is None:
                    self.empty_since_ns = now_ns
                if now_ns - self.empty_since_ns >= EMPTY_STABLE_NS:
                    self.baseline = dict(empty_usb_count=0, empty_serial_count=0,
                        monotonic_ns=now_ns, wall_ns=wall_ns, sequence=last_sequence)
                    self.phase = 'wait_left'
                return  # Ignore every add queued before the empty baseline.
            fresh_adds=[event for event in events if event.get('action')=='add' and
                event.get('sequence',0)>self.baseline['sequence'] and
                event.get('kernel_time_ns',0)>self.baseline['monotonic_ns']]
            if not self.events and (len(units)>1 or len(fresh_adds)>1):
                raise PairingError('two_receivers_before_left_confirmation')
            for event in events:
                if (event.get('sequence', 0) <= self.baseline['sequence'] or
                        event.get('kernel_time_ns', 0) <= self.baseline['monotonic_ns']):
                    continue
                if event.get('action') == 'remove':
                    if any(event.get('devpath') == item['devpath'] for item in self.events):
                        raise PairingError('receiver_disconnected')
                    continue
                if event.get('action') != 'add':
                    continue
                if event.get('source') != 'kernel_udevadm':
                    raise PairingError('untrusted_connection_event')
                if len(self.events) >= 2 or any(event.get('serial') == item['serial'] for item in self.events):
                    raise PairingError('receiver_replugged_or_extra')
                if self.events and event['sequence'] <= self.events[-1]['sequence']:
                    raise PairingError('unordered_connection_events')
                if self.events and (self.left_checkpoint is None or
                        event['kernel_time_ns']<=self.left_checkpoint['monotonic_ns']):
                    raise PairingError('right_inserted_before_left_confirmation')
                if not event.get('serial') or event.get('serial') not in serials:
                    raise PairingError('event_receiver_missing')
                self.events.append(copy.deepcopy(event))
                self.phase = 'wait_right' if len(self.events) == 1 else 'verify_signals'
                if len(self.events)==1:
                    if len(units)!=1:
                        raise PairingError('left_not_observed_alone')
                    self.left_checkpoint=dict(usb_count=1,monotonic_ns=now_ns,
                        wall_ns=wall_ns,sequence=event['sequence'])
            expected = {event['serial'] for event in self.events}
            if expected - set(serials):
                raise PairingError('receiver_disconnected')
            if set(serials) - expected:
                # A kernel add may arrive between drain() and the sysfs read.
                # Wait briefly for its real event, never infer the missing side.
                if self.pending_unknown_since is None:
                    self.pending_unknown_since = now_ns
                if now_ns - self.pending_unknown_since > RESOLVE_TIMEOUT_NS:
                    raise PairingError('connection_event_missing')
            else:
                self.pending_unknown_since = None
            for row in current:
                serial = row[3]
                if serial not in expected:
                    continue
                if row[4] <= self.baseline['wall_ns']:
                    raise PairingError('old_receiver_generation')
                if serial in self.rows and self.rows[serial] != row:
                    raise PairingError('receiver_generation_changed')
                self.rows[serial] = copy.deepcopy(row)
            if set(self.rows) - {row[3] for row in current}:
                raise PairingError('serial_device_disconnected')
            self.stamp = current
        except (PairingError, KeyError, TypeError, ValueError) as error:
            self.fault(str(error))

    def signal_evidence(self, sources, signals, preview_id, now=None):
        now = time.time() if now is None else now
        self.data_ready = dict.fromkeys(SLOTS, False)
        if (not self.preview_ready or not isinstance(preview_id, str) or not preview_id or
                not isinstance(sources, dict) or set(sources) != set(SLOTS) or
                len(set(sources.values())) != 2 or set(sources.values()) != {row[0] for row in self.stamp}):
            return None
        evidence = {}
        for index, event in enumerate(self.events):
            row = self.rows[event['serial']]
            slot = next((key for key, path in sources.items() if path == row[0]), None)
            signal = signals.get(slot, {})
            stamp = signal.get('time')
            rows = signal.get('rows')
            valid_rows = isinstance(rows,list) and bool(rows)
            if valid_rows:
                for bucket in rows:
                    if (not isinstance(bucket,list) or len(bucket)!=2 or type(bucket[0]) not in (int,float) or
                            not math.isfinite(bucket[0]) or not isinstance(bucket[1],list) or len(bucket[1])!=8 or
                            any(not isinstance(bounds,list) or len(bounds)!=2 or
                                any(type(v) not in (int,float) or not math.isfinite(v) for v in bounds)
                                for bounds in bucket[1])):
                        valid_rows=False;break
            if valid_rows:
                valid_rows=any(0<=now-bucket[0]<3 for bucket in rows)
            fresh = (slot is not None and type(stamp) in (int, float) and math.isfinite(stamp) and
                0 <= now - stamp < 3 and signal.get('preview_id') == preview_id and
                signal.get('source_path') == row[0] and type(signal.get('count')) is int and
                signal['count'] > 0 and valid_rows)
            self.data_ready[SLOTS[index]] = bool(fresh)
            if fresh:
                evidence[slot] = dict(source_path=row[0], time=stamp, count=signal['count'])
        if not all(self.data_ready.values()):
            return None
        return dict(session_id=self.session_id, preview_id=preview_id, signals=evidence)

    def proof(self, sources, evidence, completed_at=None):
        completed_at = time.time() if completed_at is None else completed_at
        if not self.preview_ready or not evidence or not all(self.data_ready.values()):
            raise PairingError('fresh_signal_proof_required')
        proof = dict(version=VERSION, method=METHOD, ordered_confirmed=True,
            calibration_id=uuid.uuid4().hex, session_id=self.session_id, boot_id=self.boot,
            completed_at=completed_at, stamp=copy.deepcopy(self.stamp),
            sources=copy.deepcopy(sources),
            roles={slot:self.rows[event['serial']][0] for slot,event in zip(SLOTS,self.events)},
            baseline=copy.deepcopy(self.baseline), connection_events=copy.deepcopy(self.events),
            left_checkpoint=copy.deepcopy(self.left_checkpoint),
            preview=copy.deepcopy(evidence))
        if not valid(proof, self.boot, self.stamp):
            raise PairingError('invalid_ordered_proof')
        return proof

    def snapshot(self):
        return dict(method=METHOD, session_id=self.session_id, phase=self.phase,
            message=MESSAGES[self.phase], error=self.error,
            left_serial=self.events[0]['serial'] if self.events else None,
            right_serial=self.events[1]['serial'] if len(self.events)>1 else None,
            left_data_valid=self.data_ready[SLOTS[0]], right_data_valid=self.data_ready[SLOTS[1]])


def valid(proof, boot, stamp):
    try:
        if (not isinstance(proof, dict) or proof.get('version') != VERSION or
                proof.get('method') != METHOD or proof.get('ordered_confirmed') is not True or
                proof.get('boot_id') != boot or not isinstance(boot, str) or not boot):
            return False
        current = canonical_stamp(stamp)
        if len(current) != 2 or proof.get('stamp') != current:
            return False
        completed = proof.get('completed_at')
        if type(completed) not in (int,float) or not math.isfinite(completed) or completed <= 0:
            return False
        if any(not isinstance(proof.get(key),str) or not proof[key] for key in ('calibration_id','session_id')):
            return False
        roles, sources = proof.get('roles'), proof.get('sources')
        if any(not isinstance(value,dict) or set(value)!=set(SLOTS) for value in (roles,sources)):
            return False
        paths = {row[0] for row in current}
        if any(len(set(value.values()))!=2 or set(value.values())!=paths for value in (roles,sources)):
            return False
        baseline = proof['baseline']
        if (type(baseline.get('empty_usb_count')) is not int or baseline['empty_usb_count'] != 0 or
                type(baseline.get('empty_serial_count')) is not int or baseline['empty_serial_count'] != 0 or
                any(type(baseline.get(key)) is not int or baseline[key]<0 for key in ('sequence','monotonic_ns','wall_ns'))):
            return False
        events = proof['connection_events']
        if not isinstance(events,list) or len(events)!=2 or events[0]['sequence']>=events[1]['sequence']:
            return False
        if len({event['serial'] for event in events})!=2 or len({event['devpath'] for event in events})!=2:
            return False
        checkpoint=proof['left_checkpoint']
        if (type(checkpoint.get('usb_count')) is not int or checkpoint['usb_count']!=1 or
                type(checkpoint.get('sequence')) is not int or checkpoint['sequence']!=events[0]['sequence'] or
                type(checkpoint.get('monotonic_ns')) is not int or
                not baseline['monotonic_ns']<checkpoint['monotonic_ns']<events[1]['kernel_time_ns'] or
                events[0]['kernel_time_ns']>checkpoint['monotonic_ns']):
            return False
        rows = {row[3]:row for row in current}
        for slot,event in zip(SLOTS,events):
            row = rows[event['serial']]
            if (event.get('action')!='add' or event.get('source')!='kernel_udevadm' or
                    type(event.get('sequence')) is not int or type(event.get('kernel_time_ns')) is not int or
                    event['sequence']<=baseline['sequence'] or
                    event['kernel_time_ns']<=baseline['monotonic_ns'] or row[4]<=baseline['wall_ns'] or
                    _usb_id(event.get('vendor'),event.get('product'))!=_usb_id(row[1],row[2]) or
                    roles[slot]!=row[0]):
                return False
        preview = proof['preview']
        if (preview.get('session_id')!=proof['session_id'] or
                not isinstance(preview.get('preview_id'),str) or not preview['preview_id'] or
                not isinstance(preview.get('signals'),dict) or set(preview['signals'])!=set(SLOTS)):
            return False
        for slot,evidence in preview['signals'].items():
            at=evidence.get('time')
            if (evidence.get('source_path')!=sources[slot] or type(evidence.get('count')) is not int or evidence['count']<=0 or
                    type(at) not in (int,float) or not math.isfinite(at) or not 0<=completed-at<3):
                return False
        return True
    except (KeyError,TypeError,ValueError,IndexError):
        return False
