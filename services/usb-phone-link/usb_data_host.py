#!/usr/bin/env python3
"""Guarded Pi USB data-host policy. Never grants Android trust or sets power roles."""
import argparse
from contextlib import contextmanager
try:
    import fcntl
except ImportError:  # Offline policy tests are also runnable on Windows.
    fcntl = None
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.request

BASE = Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi')
STATE = Path('/run/headring-usb-data-host.json')
SAFE = {'device_confirm', 'idle', 'failed_wait', 'exhausted'}
OWNER_SUFFIXES = ('/control/launcher.py', '/control/service_entry.py',
                  '/framework/framework_recorder.py', '/acquisition/capture.py',
                  '/bin/egorecord')
VERSION = 'usb-data-host-20261109-v2'


def read(path):
    try: return Path(path).read_text().strip()
    except OSError: return ''


def active_role(value):
    match = re.search(r'\[([^\]]+)\]', value)
    return match.group(1) if match else value.strip()


def write(path, value):
    Path(path).write_text(value + '\n')


def safe_snapshot(value):
    if not isinstance(value, dict) or value.get('state') not in SAFE:
        return False, 'workflow_busy_or_starting'
    privacy = value.get('privacy') or {}
    worker = value.get('worker') or {}
    if not isinstance(privacy, dict) or not isinstance(worker, dict):
        return False, 'malformed_capture_status'
    if (value.get('session') or value.get('busy') or worker.get('recording_active')
            or worker.get('state') in {'recording', 'saving', 'checking', 'finalizing', 'postprocessing'}
            or privacy.get('required') is not False
            or privacy.get('phase') != 'idle' or privacy.get('stop_confirmed') is not True):
        return False, 'capture_or_privacy_not_idle'
    return True, 'safe_idle'


class CaptureGuard:
    def __init__(self, base=BASE):
        self.base = Path(base)

    def owner_active(self, include_controller):
        suffixes = OWNER_SUFFIXES if include_controller else OWNER_SUFFIXES[2:]
        for process in Path('/proc').iterdir():
            if not process.name.isdigit(): continue
            try: arguments = (process / 'cmdline').read_bytes().split(b'\0')
            except OSError: continue
            if any(argument.decode('utf-8', 'replace').endswith(suffixes)
                   for argument in arguments if argument): return True
        return False

    def camera_idle(self, missing_ok=False):
        if not Path('/run/camd.sock').exists():
            return (True, 'before_camera_start') if missing_ok else (False, 'camera_status_unavailable')
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(1)
        try:
            connection.connect('/run/camd.sock'); connection.sendall(b'{"cmd":"STATUS"}\n')
            result = json.loads(connection.recv(65536).split(b'\n', 1)[0])
            return (True, 'camera_idle') if result.get('state') == 'IDLE' else (False, 'camera_not_idle')
        except Exception: return False, 'camera_status_unavailable'
        finally: connection.close()

    def allowed(self):
        try:
            token = (self.base / 'runtime/dashboard_token').read_text().strip()
            request = urllib.request.Request('http://127.0.0.1:8765/api/display-state',
                                             headers={'X-Access-Token': token})
            with urllib.request.urlopen(request, timeout=1) as reply:
                allowed, reason = safe_snapshot(json.load(reply))
            if not allowed: return allowed, reason
            if self.owner_active(False): return False, 'capture_owner_still_active'
            return self.camera_idle()
        except Exception:
            # Only an actual pre-application boot/install window can use this
            # fallback. A broken live controller never authorizes a role swap.
            if self.owner_active(True): return False, 'application_active_without_authoritative_status'
            allowed, reason = self.camera_idle(missing_ok=True)
            return (True, 'before_application_start') if allowed else (False, reason)

    @contextmanager
    def lease(self):
        if fcntl is None: raise OSError('Linux capture leases are required for role changes')
        project = self.base.parent
        roots = [self.base, project / 'profiles/emg/orangepi', project / 'profiles/eeg/orangepi']
        descriptors = []
        seen = set()
        try:
            for base in roots:
                runtime = base / 'runtime'
                if not runtime.is_dir(): continue
                path = runtime / 'capture.lock'
                identity = str(path.resolve())
                if identity in seen: continue
                seen.add(identity)
                flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
                try:
                    fd = os.open(str(path), flags | os.O_CREAT | os.O_EXCL, 0o644)
                    stat = runtime.stat()
                    os.fchown(fd, stat.st_uid, stat.st_gid)
                except FileExistsError:
                    fd = os.open(str(path), flags)
                descriptors.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            for fd in reversed(descriptors): os.close(fd)


class DataHost:
    def __init__(self, base=BASE, sys_root=Path('/sys'), port='port0',
                 controller='fc000000.usb', guard=None, clock=time.monotonic,
                 writer=write, snapshot=None):
        self.sys_root = Path(sys_root)
        self.port = self.sys_root / 'class/typec' / port
        self.controller = controller
        self.guard = guard or CaptureGuard(base)
        self.clock = clock
        self.writer = writer
        self.snapshot_provider = snapshot
        self.key = None
        self.attempts = 0
        self.next_attempt = 0

    def snapshot(self):
        if self.snapshot_provider: return self.snapshot_provider()
        partner = self.port.with_name(self.port.name + '-partner')
        key = None
        if partner.exists():
            stat = partner.stat()
            key = (stat.st_ino, stat.st_ctime_ns)
        physical = (self.sys_root / 'bus/platform/devices' / self.controller).resolve()
        devices = self.sys_root / 'bus/usb/devices'
        children = []
        if devices.exists():
            for path in devices.iterdir():
                if path.name.startswith('usb') or not (path / 'idVendor').is_file(): continue
                if physical in path.resolve().parents: children.append(path.name)
        return dict(port_present=self.port.exists(), partner=partner.exists(), key=key,
                    data_role=active_role(read(self.port / 'data_role')),
                    power_role=active_role(read(self.port / 'power_role')),
                    driver_mode=read(self.sys_root / 'kernel/debug/usb' / self.controller / 'mode'),
                    host_children=children)

    @staticmethod
    def host(snapshot):
        if snapshot['driver_mode'] == 'host':
            return (not snapshot['partner'] or snapshot['data_role'] == 'host'
                    or bool(snapshot['host_children']))
        return snapshot['data_role'] == 'host' and snapshot['driver_mode'] != 'device'

    def result(self, snapshot, state, reason='', **extra):
        messages = {
            'host': 'Pi 已作为 USB 数据主机，等待手机调试授权或连接。',
            'host_requested': '已请求 USB 数据主机角色，正在等待手机与内核完成切换。',
            'deferred': '当前采集或设备初始化未结束，暂不切换 USB 数据角色。',
            'waiting_retry': 'USB-C 尚未完成数据主机切换，将按间隔重试。',
            'partner_refused': 'USB-C 对端未接受数据角色切换；可改用 USB 主机口。',
            'unsupported': '当前接口没有可用的数据角色切换能力；请使用 USB 主机口。',
            'topology_busy': '此控制器有其他 USB 设备，不强制改动它们的连接。',
        }
        return dict(version=VERSION, state=state, reason=reason, message=messages[state],
                    port=self.port.name, controller=self.controller,
                    data_role=snapshot['data_role'], driver_mode=snapshot['driver_mode'],
                    power_role=snapshot['power_role'], power_role_written=False, **extra)

    def ensure(self):
        before = self.snapshot()
        key = before['key'] if before['partner'] else ('unattached',)
        if key != self.key:
            self.key = key; self.attempts = 0; self.next_attempt = 0
        if self.host(before): return self.result(before, 'host', changed=False)
        if before['host_children']:
            return self.result(before, 'topology_busy', changed=False)
        # Keep retrying at the capped interval after transient role failures.
        # Every retry still requires the capture lease and an idle device.
        if self.clock() < self.next_attempt: return self.result(before, 'waiting_retry', changed=False)
        try:
            with self.guard.lease():
                allowed, reason = self.guard.allowed()
                if not allowed: return self.result(before, 'deferred', reason, changed=False)
                current = self.snapshot()
                if current['key'] != before['key'] or current['partner'] != before['partner']:
                    return self.result(current, 'waiting_retry', 'connection_changed', changed=False)
                if self.host(current): return self.result(current, 'host', changed=False)
                if current['host_children']: return self.result(current, 'topology_busy', changed=False)
                if current['partner']:
                    if not current['port_present'] or current['data_role'] != 'device':
                        return self.result(current, 'unsupported', 'no_standard_typec_swap', changed=False)
                    path = self.port / 'data_role'
                    method = 'standard_typec_data_swap'
                else:
                    # No connected partner and no downstream devices: initialize
                    # only this controller's normal driver mode for installation.
                    path = self.sys_root / 'kernel/debug/usb' / self.controller / 'mode'
                    if current['driver_mode'] not in ('device', 'otg'):
                        return self.result(current, 'unsupported', 'no_host_initialization_interface', changed=False)
                    method = 'unattached_driver_initialization'
                # Recheck the authoritative capture/boot gate immediately before
                # the write while holding every existing capture-profile lease.
                allowed, reason = self.guard.allowed()
                if not allowed: return self.result(current, 'deferred', reason, changed=False)
                self.attempts += 1
                self.next_attempt = self.clock() + (10, 30, 60)[min(self.attempts - 1, 2)]
                try: self.writer(path, 'host')
                except OSError as error:
                    return self.result(self.snapshot(), 'partner_refused', 'role_write_rejected',
                                       changed=False, errno=error.errno, method=method)
                after = self.snapshot()
                return self.result(after, 'host' if self.host(after) else 'host_requested',
                                   changed=True, method=method,
                                   power_role_changed_observed=after['power_role'] != current['power_role'])
        except (BlockingIOError, OSError):
            return self.result(before, 'deferred', 'capture_lease_unavailable', changed=False)


def save_state(value, path=STATE):
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(dict(value, updated_at_unix=time.time()), stream, ensure_ascii=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, 0o644)
        os.replace(name, str(path))
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--boot', action='store_true', help='Boot invocation; safety checks are not bypassed')
    parser.add_argument('--base', type=Path, default=BASE)
    args = parser.parse_args()
    if os.geteuid() != 0: raise SystemExit('USB data-role policy requires root')
    manager = DataHost(base=args.base)
    previous = None
    while True:
        try: value = manager.ensure()
        except Exception:
            value = dict(version=VERSION, state='deferred', reason='role_check_failed',
                         message='USB 数据角色检查暂不可用，稍后重试。', power_role_written=False)
        stamp = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if stamp != previous or args.once:
            save_state(value); print(stamp, flush=True); previous = stamp
        if args.once: return 0
        time.sleep(2)


if __name__ == '__main__': raise SystemExit(main())
