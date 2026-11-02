"""Separate, narrowly scoped image-library access code. Never grants collection actions."""
from collections import deque
import hmac
import os
from pathlib import Path
import secrets
import stat
import threading
import time

ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
TRUSTED_PEERS = frozenset(('127.0.0.1', '::1'))


class ReferenceCredentials:
    def __init__(self, base, clock=time.monotonic):
        directory = Path(base)/'runtime/reference_library'
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory/'access_code'
        self.clock = clock
        self.lock = threading.Lock()
        self.failures = {}
        if self.path.is_symlink():
            raise ValueError('library access-code path cannot be a link')
        try:
            descriptor = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            pass
        else:
            code = ''.join(secrets.choice(ALPHABET) for _ in range(12))
            with os.fdopen(descriptor, 'wb') as output:
                output.write((code+'\n').encode('ascii')); output.flush(); os.fsync(output.fileno())
            if os.name != 'nt':
                descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64:
            raise ValueError('invalid library access code file')
        if os.name != 'nt' and stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('library access code must be private')
        self._code = self.path.read_text(encoding='ascii').strip()
        if len(self._code) != 12 or any(c not in ALPHABET for c in self._code):
            raise ValueError('invalid library access code')

    def code(self):
        """Only call from the trusted on-device access-information endpoint."""
        return self._code

    def check(self, supplied, peer):
        """Return (authorized, rate_limited), with bounded per-peer memory."""
        now = self.clock()
        with self.lock:
            queue = self.failures.setdefault(str(peer), deque(maxlen=12))
            while queue and now-queue[0] >= 30:
                queue.popleft()
            if len(queue) >= 10:
                return False, True
            if len(self.failures) > 256:
                for name in list(self.failures)[:-128]:
                    self.failures.pop(name, None)
            valid = False
            if isinstance(supplied, str) and len(supplied) == 12:
                try:valid = hmac.compare_digest(supplied.encode('ascii'), self._code.encode('ascii'))
                except UnicodeEncodeError:pass
            if valid:
                queue.clear()
                return True, False
            queue.append(now)
            return False, False


def authorize_library(handler, owner):
    """RV/loopback use the control policy; phones receive only library scope."""
    peer = handler.client_address[0]
    if peer in TRUSTED_PEERS and handler.authorized():
        return True, False
    return owner.reference_credentials.check(handler.headers.get('X-Reference-Key', ''), peer)
