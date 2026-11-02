"""Bounded, same-UID latest-JPEG IPC. Recording callbacks perform no file IO.

Only lease writers and the Publisher background thread touch the filesystem.
Published samples are immutable inodes, atomically replaced after close.
"""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import threading
import time
import uuid

DEFAULT_DIRECTORY = '/dev/shm/pi-collect-wrist-preview'
CAMERAS = {'cam_left_wrist': 1, 'cam_right_wrist': 2}
MAX_INPUT = 2 * 1024 * 1024
LEASE_SECONDS = 2.0
LEASE_REFRESH_SECONDS = .5
LEASE_POLL_SECONDS = .2
MAX_LEASE_FUTURE = 2.5
MAX_SOURCE_AGE = 2.0
HEADER = struct.Struct('!4s16sBIQI')
MAGIC = b'RVW1'
_EPOCH = re.compile(r'^[0-9a-f]{32}$')
_LEASE_LOCKS = {}
_LEASE_LOCKS_GUARD = threading.Lock()


def _owner_mode(st, regular):
    if not (stat.S_ISREG(st.st_mode) if regular else stat.S_ISDIR(st.st_mode)):
        raise OSError('IPC path has an invalid file type')
    if hasattr(os, 'getuid'):
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise OSError('IPC path must be private to the current UID')


def _directory(path):
    try:
        os.mkdir(str(path), 0o700)
    except FileExistsError:
        pass
    _owner_mode(os.lstat(str(path)), False)  # lstat rejects a symlink directory.


def _entry(path):
    try:
        st = os.lstat(str(path))
    except FileNotFoundError:
        return None
    _owner_mode(st, True)
    return st


def _open_read(path, maximum):
    before = _entry(path)
    if before is None:
        return None
    fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_BINARY', 0))
    try:
        after = os.fstat(fd)
        _owner_mode(after, True)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise OSError('IPC entry changed during open')
        if after.st_size > maximum:
            raise OSError('IPC entry exceeds its size limit')
        return os.fdopen(fd, 'rb', buffering=0)
    except BaseException:
        os.close(fd)
        raise


def _lease(path, now, require_live=True):
    handle = _open_read(path, 1024)
    if handle is None:
        return None
    with handle:
        raw = handle.read(1025)
    value = json.loads(raw.decode('ascii'))
    if (not isinstance(value, dict) or type(value.get('v')) is not int or value['v'] != 1 or
            not isinstance(value.get('epoch'), str) or not _EPOCH.fullmatch(value['epoch']) or
            not isinstance(value.get('camera'), str) or value['camera'] not in CAMERAS or
            type(value.get('expires_mono')) not in (int, float)):
        return None
    expiry = value['expires_mono']
    if not math.isfinite(expiry):
        return None
    if require_live and (expiry <= now or expiry > now + MAX_LEASE_FUTURE):
        return None
    return value


class _AtomicFiles:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.temporary = set()
        self.lock = threading.Lock()

    def replace(self, name, pieces):
        _directory(self.directory)
        destination = self.directory / name
        _entry(destination)
        temporary = self.directory / ('.%s.%d.%s.tmp' % (name, os.getpid(), uuid.uuid4().hex))
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), 0o600)
        with self.lock:
            self.temporary.add(temporary)
        try:
            with os.fdopen(fd, 'wb') as handle:
                for piece in pieces:
                    handle.write(piece)
            _entry(destination)
            os.replace(str(temporary), str(destination))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            with self.lock:
                self.temporary.discard(temporary)

    def close(self):
        with self.lock:
            names = tuple(self.temporary)
        for path in names:
            try:
                path.unlink()
            except OSError:
                pass


@contextmanager
def _lease_guard(directory):
    _directory(directory)
    key = str(directory.absolute())
    with _LEASE_LOCKS_GUARD:
        guard = _LEASE_LOCKS.setdefault(key, threading.RLock())
    with guard:
        path = directory / '.lease.lock'
        _entry(path)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            _owner_mode(os.fstat(fd), True)
            if os.name == 'posix':
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class LeaseWriter:
    def __init__(self, directory=DEFAULT_DIRECTORY, clock=time.monotonic):
        self.directory = Path(directory)
        self.clock = clock
        self.epoch = None
        self.camera = None
        self.last_refresh = float('-inf')
        self.files = _AtomicFiles(directory)

    def _write(self, now):
        value = {'v': 1, 'epoch': self.epoch, 'camera': self.camera,
                 'expires_mono': now + LEASE_SECONDS}
        self.files.replace('lease.json', (json.dumps(value, separators=(',', ':')).encode('ascii'),))
        self.last_refresh = now

    def select(self, camera):
        if not isinstance(camera, str) or camera not in CAMERAS:
            raise ValueError('Unknown wrist preview camera')
        with _lease_guard(self.directory):
            self.camera, self.epoch = camera, uuid.uuid4().hex
            self._write(self.clock())
            return self.epoch

    def refresh(self):
        if self.epoch is None:
            return False
        now = self.clock()
        if now - self.last_refresh < LEASE_REFRESH_SECONDS:
            return False
        with _lease_guard(self.directory):
            current = _lease(self.directory / 'lease.json', now, require_live=False)
            if not current or current['epoch'] != self.epoch or current['camera'] != self.camera:
                return False
            self._write(now)
            return True

    def close(self):
        try:
            if self.epoch is not None:
                with _lease_guard(self.directory):
                    current = _lease(self.directory / 'lease.json', self.clock(), require_live=False)
                    if current and current['epoch'] == self.epoch:
                        _entry(self.directory / 'lease.json')
                        (self.directory / 'lease.json').unlink()
        except (OSError, ValueError, UnicodeError):
            pass
        self.epoch = self.camera = None
        self.files.close()


class Producer:
    def __init__(self, directory=DEFAULT_DIRECTORY, clock=time.monotonic, max_fps=30.0):
        if not math.isfinite(max_fps) or not 1 <= max_fps <= 60:
            raise ValueError('Invalid wrist preview rate')
        self.directory = Path(directory)
        self.clock = clock
        self.period = 1.0 / max_fps
        self.lock = threading.Lock()
        self.tick_lock = threading.Lock()
        self.files = _AtomicFiles(directory)
        self.lease = None
        self.latest = None
        self.last_source_ns = 0
        self.next_poll = 0.0
        self.next_write = 0.0
        self.closed = False

    def offer(self, camera, payload, source_mono_ns=None):
        # This function must stay callback-safe: no file IO, decoding, waits,
        # JPEG copies, directory lookup, or blocking lock acquisition.
        if not isinstance(camera, str) or camera not in CAMERAS or not isinstance(payload, bytes) or not 2 <= len(payload) <= MAX_INPUT or payload[:2] != b'\xff\xd8':
            return False
        now = self.clock()
        if source_mono_ns is None:
            source_mono_ns = int(now * 1000000000)
        if type(source_mono_ns) is not int or not 0 < source_mono_ns < (1 << 64):
            return False
        if not self.lock.acquire(False):
            return False
        try:
            lease = self.lease
            if (self.closed or not lease or lease['camera'] != camera or
                    lease['expires_mono'] <= now or source_mono_ns <= self.last_source_ns):
                return False
            self.latest = (lease['epoch'], camera, payload, source_mono_ns)
            self.last_source_ns = source_mono_ns
            return True
        finally:
            self.lock.release()

    @property
    def has_demand(self):
        with self.lock:
            return bool(not self.closed and self.lease and self.lease['expires_mono'] > self.clock())

    @property
    def poll_interval(self):
        with self.lock:
            now = self.clock()
            if self.closed or not self.lease or self.lease['expires_mono'] <= now:
                return .2
            if self.latest is not None and self.next_write > now:
                return max(.0005, min(.01, self.next_write - now))
            return .01

    def tick(self, now=None):
        if not self.tick_lock.acquire(False):
            return False
        try:
            return self._tick(self.clock() if now is None else now)
        except (OSError, ValueError, UnicodeError, OverflowError):
            # Preview IPC failure must never escape into the recorder worker.
            with self.lock:
                self.lease = self.latest = None
                self.last_source_ns = 0
            return False
        finally:
            self.tick_lock.release()

    def _tick(self, now):
        with self.lock:
            if self.closed:
                return False
        if now >= self.next_poll:
            self.next_poll = now + LEASE_POLL_SECONDS
            _directory(self.directory)
            current = _lease(self.directory / 'lease.json', now)
            with self.lock:
                old = self.lease
                if not current or not old or (current['epoch'], current['camera']) != (old['epoch'], old['camera']):
                    self.latest = None
                    self.last_source_ns = 0
                    self.next_write = 0.0
                self.lease = current
        with self.lock:
            if not self.lease or self.lease['expires_mono'] <= now:
                self.lease = self.latest = None
                self.last_source_ns = 0
                return False
            if self.latest is None or now < self.next_write:
                return False
            epoch, camera, payload, stamp = self.latest
            self.latest = None
            if epoch != self.lease['epoch'] or camera != self.lease['camera']:
                return False
            if self.next_write <= 0:
                self.next_write = now + self.period
            else:
                self.next_write += self.period
                if self.next_write <= now:
                    self.next_write += (math.floor((now - self.next_write) / self.period) + 1) * self.period
        header = HEADER.pack(MAGIC, bytes.fromhex(epoch), CAMERAS[camera], os.getpid(), stamp, len(payload))
        self.files.replace('latest.bin', (header, payload))
        return True

    def close(self):
        with self.lock:
            self.closed = True
            self.lease = self.latest = None
        self.files.close()  # Never remove another owner's latest.bin.


class Reader:
    def __init__(self, directory=DEFAULT_DIRECTORY, clock=time.monotonic):
        self.directory = Path(directory)
        self.clock = clock
        self.last_key = None
        self.expected = None

    def reset(self):
        self.last_key = self.expected = None

    def read(self, camera, epoch):
        if not isinstance(camera, str) or camera not in CAMERAS or not isinstance(epoch, str) or not _EPOCH.fullmatch(epoch):
            return None
        expected = (camera, epoch)
        if expected != self.expected:
            self.last_key = None
            self.expected = expected
        try:
            _directory(self.directory)
            lease = _lease(self.directory / 'lease.json', self.clock())
            if not lease or (lease['camera'], lease['epoch']) != expected:
                return None
            handle = _open_read(self.directory / 'latest.bin', HEADER.size + MAX_INPUT)
            if handle is None:
                return None
            with handle:
                header = handle.read(HEADER.size)
                if len(header) != HEADER.size:
                    return None
                magic, raw_epoch, number, pid, stamp, size = HEADER.unpack(header)
                if (magic != MAGIC or raw_epoch.hex() != epoch or number != CAMERAS[camera] or
                        pid == 0 or not 2 <= size <= MAX_INPUT or
                        os.fstat(handle.fileno()).st_size != HEADER.size + size):
                    return None
                arrival = stamp / 1000000000.0
                age = self.clock() - arrival
                if not stamp or age > MAX_SOURCE_AGE or age < -.1:
                    return None
                key = (epoch, pid, stamp)
                if self.last_key and stamp <= self.last_key[2]:
                    return None
                payload = handle.read(size + 1)
                if len(payload) != size or payload[:2] != b'\xff\xd8':
                    return None
            self.last_key = key
            return payload, key, arrival
        except (OSError, ValueError, UnicodeError, struct.error):
            return None
