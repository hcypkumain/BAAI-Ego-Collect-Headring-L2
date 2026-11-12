"""Best-effort bounded event trace; producers never perform filesystem I/O."""
import json
import math
import os
from pathlib import Path
import queue
import re
import stat
import threading
import time

EVENTS = frozenset(('action_submitted', 'action_taken', 'action_complete', 'impedance_gate',
                    'scan_deferred', 'scan_start', 'scan_end', 'lock_wait', 'loop_lag'))
WORDS = {
    'action': frozenset(('start', 'stop', 'impedance_start', 'impedance_end', 'impedance_review', 'rescan', 'other')),
    'phase': frozenset(('idle', 'arming', 'recording', 'stopping', 'fault', 'unknown')),
    'reason': frozenset(('ok', 'failed', 'cancelled', 'busy', 'owner_active', 'incomplete_window',
                         'missing_valid_channels', 'normal_recovery_unconfirmed', 'persist_failed',
                         'owner_changed', 'measurement_changed', 'capture_armed', 'unknown')),
}
NUMBERS = frozenset(('revision', 'generation', 'publish_seq', 'enabled_count', 'valid_count',
                     'duration_ms', 'lag_ms', 'publish_age_ms', 'dropped'))


def safe_event(event, fields):
    if event not in EVENTS:return None
    out = {'event': event}
    for key, value in fields.items():
        if key in WORDS and isinstance(value, str) and value in WORDS[key]:out[key] = value
        elif key in ('request_id', 'measurement_epoch') and isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value):out[key] = value
        elif key in NUMBERS and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**12:out[key] = value
        elif key in ('ok', 'recovered', 'persisted') and type(value) is bool:out[key] = value
    return out


class EventWriter:
    def __init__(self, directory, max_bytes=5*1024*1024, files=4, capacity=512):
        self.directory = Path(directory); self.max_bytes = max_bytes; self.files = files
        self.queue = queue.Queue(maxsize=capacity); self.dropped = 0
        self.thread = threading.Thread(target=self._run, name='diagnostic-events', daemon=True)
        self.thread.start()

    def emit(self, event, **fields):
        try:
            value = safe_event(event, fields)
            if value is None:return
            value['monotonic_ms'] = round(time.monotonic()*1000)
            value['dropped'] = self.dropped
            self.queue.put_nowait(value)
        except queue.Full:self.dropped += 1
        except Exception:pass

    def _write(self, value):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        path = self.directory/'events.jsonl'
        line = (json.dumps(value, separators=(',', ':'), ensure_ascii=True)+'\n').encode()
        try:size = path.stat().st_size
        except FileNotFoundError:size = 0
        if size + len(line) > self.max_bytes:
            for n in range(self.files-1, 0, -1):
                source = path if n == 1 else self.directory/('events.jsonl.'+str(n-1))
                destination = self.directory/('events.jsonl.'+str(n))
                try:os.replace(source, destination)
                except FileNotFoundError:pass
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line)
        finally:os.close(fd)

    def _run(self):
        while True:
            value = self.queue.get()
            try:self._write(value)
            except Exception:self.dropped += 1
            finally:self.queue.task_done()


class NullEvents:
    def emit(self, event, **fields):
        pass


def export_events(directory, limit=2*1024*1024):
    """Revalidate every field, including files modified outside this writer."""
    output = bytearray()
    for suffix in ('.3', '.2', '.1', ''):
        path = Path(directory)/('events.jsonl'+suffix)
        try:
            fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):continue
                raw_file=stream.read(5*1024*1024+1)
                if len(raw_file)>5*1024*1024:continue
                for line in raw_file.splitlines():
                    if len(line) > 4096:continue
                    try:
                        raw = json.loads(line); value = safe_event(raw.get('event'), raw)
                        if value is None:continue
                        stamp = raw.get('monotonic_ms')
                        if type(stamp) is int and 0 <= stamp <= 10**12:value['monotonic_ms'] = stamp
                        encoded = (json.dumps(value, separators=(',', ':'))+'\n').encode()
                        if len(output)+len(encoded)>limit:return bytes(output)
                        output.extend(encoded)
                    except (ValueError, TypeError, AttributeError):continue
        except OSError:continue
    return bytes(output)
