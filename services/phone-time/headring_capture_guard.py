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
            profile = json.loads((self.base / 'runtime/collection_profile.json').read_text())
            active = self.base if profile['legacy_mode'] == profile['active_mode'] else self.base.parent / 'profiles' / profile['active_mode'] / 'orangepi'
            token = (active / 'runtime/dashboard_token').read_text().strip()
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
