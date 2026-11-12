"""Thread-free bounded subprocess ledger, incremental strict JSON, fair rotation."""
import math
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field, replace

from host.common.device_registry import Device, _validate_devices
from host.common.governance_contract import ProbePolicy, ProbeResult
from host.common.strict_json import canonical_json
from .readonly_probe import (_decode_result, _payload, _ssh_argv, _unknown,
                             _validate_request)


# Process-wide, not merely per registry/scheduler. Strong owner references retain
# the unreaped ledger even if a caller mistakenly drops its scheduler variable.
# This module is deliberately single-threaded; all schedulers share main's loop.
_PROCESS_DOMAINS = {}
# Small identity-only tombstones, separate from handles. No production reset:
# local reap does not prove that a remote D-state reader was cleaned up.
_PROCESS_QUARANTINE = {}


def _identity_count():
    return len(_PROCESS_DOMAINS.keys() | _PROCESS_QUARANTINE.keys())


def _admission_blocked(key):
    return key in _PROCESS_DOMAINS or key in _PROCESS_QUARANTINE or _identity_count() >= 64


def _reap_closed_domains():
    # once() may exhaust its entire budget before local reap. Later API activity
    # reaps those closed owners without retrying any quarantined identity.
    for key, (owner, domain) in tuple(_PROCESS_DOMAINS.items()):
        if getattr(owner, '_closed', False) and owner._poll(domain) is not None:
            owner._finish(key, domain)


@dataclass
class _Domain:
    device: object
    generation: int
    process: object
    started: float
    phase: str = 'running'
    boundary: float = 0.0
    output: bytearray = field(default_factory=bytearray)
    sent: int = 0
    received: int = 0
    stderr_bytes: int = 0
    latest: object = None
    invalid: bool = False
    reason: str = 'timeout'
    nonblocking: set = field(default_factory=set)
    kill_sent: bool = False


def _spawn(device, digest, generation, policy):
    return subprocess.Popen(_ssh_argv(device, policy), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, close_fds=True)


class ProbeScheduler:
    def __init__(self, devices: tuple[Device, ...], *, registry_digest: str,
                 policy: ProbePolicy, concurrency: int = 2):
        _validate_devices(devices)
        if type(concurrency) is not int or not 1 <= concurrency <= 64:
            raise ValueError('Invalid concurrency')
        # Validate even empty schedules without touching SSH admission files.
        from host.common.governance_contract import validate_probe_policy
        validate_probe_policy(policy)
        import re
        if type(registry_digest) is not str or not re.fullmatch('[0-9a-f]{64}', registry_digest):
            raise ValueError('Invalid digest')
        for device in devices:
            _validate_request(device, registry_digest, 0, policy)
        self.devices, self.digest, self.policy = devices, registry_digest, policy
        self.concurrency = concurrency
        self._generations = {d.device_id: 0 for d in devices}
        self._domains, self._results, self._quarantine = {}, {}, set()
        self._cursor, self._closed, self._last_now = 0, False, -math.inf
        self._factory = _spawn
        self._spawned = self._reaped = self._rejected = 0

    def _signal(self, domain, sig):
        try:
            os.killpg(domain.process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            self._quarantine_device(domain.device.device_id, 'signal-error')
            return
        if sig == signal.SIGKILL:
            domain.kill_sent = True

    def _quarantine_device(self, key, reason):
        self._quarantine.add(key)
        if key in _PROCESS_DOMAINS or key in _PROCESS_QUARANTINE or _identity_count() < 64:
            _PROCESS_QUARANTINE.setdefault(key, reason)

    @staticmethod
    def _close_pipes(domain):
        domain.nonblocking.clear()
        domain.payload = b''
        for stream in (domain.process.stdin, domain.process.stdout, domain.process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass

    def _fail_io(self, domain):
        domain.invalid = True
        self._stop(domain, self._last_now, 'pipe-error')
        self._close_pipes(domain)

    def _poll(self, domain):
        try:
            return domain.process.poll()
        except OSError:
            self._fail_io(domain)
            return None

    def _stop(self, domain, now, reason):
        if domain.phase == 'running':
            domain.phase, domain.boundary, domain.reason = 'term', now + 0.5, reason
            # Never retry timed-out devices: a killed local SSH cannot prove a
            # remote D-state reader exited. Healthy devices continue rotating.
            self._quarantine_device(domain.device.device_id, reason)
            self._signal(domain, signal.SIGTERM)

    def _read(self, domain):
        for stream, stdout in ((domain.process.stdout, True), (domain.process.stderr, False)):
            if stream is None or stream.closed or stream.fileno() not in domain.nonblocking:
                continue
            # Fixed read budget per tick; a noisy peer cannot monopolize main.
            for _ in range(8):
                try:
                    data = os.read(stream.fileno(), 16384)
                except BlockingIOError:
                    break
                except OSError:
                    self._fail_io(domain)
                    return
                if not data:
                    try:
                        stream.close()
                    except OSError:
                        self._fail_io(domain)
                        return
                    break
                if not stdout:
                    domain.stderr_bytes = min(8192, domain.stderr_bytes + len(data))
                    continue  # never retain/echo stderr secrets
                domain.received += len(data)
                if domain.received > self.policy.maximum_bytes:
                    domain.invalid = True
                    domain.reason = 'overflow'
                    domain.output.clear()
                    return
                domain.output.extend(data)
                while b'\n' in domain.output:
                    line, _, rest = domain.output.partition(b'\n')
                    domain.output = bytearray(rest)
                    try:
                        result = _decode_result(line, domain.device, self.digest, domain.generation)
                        if domain.phase == 'running':
                            domain.latest = result
                    except (ValueError, TypeError, UnicodeError, RecursionError):
                        self._rejected += 1
                        domain.invalid = True
                        domain.reason = 'invalid'
                        domain.output.clear()
                        return

    def _publish(self, domain, status=None):
        result = domain.latest
        if result is None or domain.invalid:
            result = _unknown(domain.device, self.digest, domain.generation,
                              status or 'unavailable')
        elif status:
            result = replace(result, status=status)
        self._results[domain.device.device_id] = result

    def _finish(self, key, domain):
        if (domain.process.returncode != 0 or domain.invalid or domain.latest is None
                or not domain.latest.identity_verified):
            self._quarantine_device(key, 'failed-observation')
        if domain.phase != 'quarantined':
            self._publish(domain, 'timeout' if domain.phase != 'running' else None)
        self._close_pipes(domain)
        del self._domains[key]
        del _PROCESS_DOMAINS[key]
        self._reaped += 1

    def tick(self, now_monotonic: float) -> None:
        if (type(now_monotonic) not in (int, float) or not math.isfinite(now_monotonic)
                or now_monotonic < self._last_now):
            raise ValueError('Expected increasing finite monotonic time')
        self._last_now = now = now_monotonic
        _reap_closed_domains()
        for key, domain in tuple(self._domains.items()):
            if domain.phase == 'quarantined':
                if self._poll(domain) is not None:
                    self._finish(key, domain)
                continue
            if (domain.phase == 'running' and
                    now - domain.started >= self.policy.timeout_ms / 1000):
                self._stop(domain, now, 'timeout')
            self._read(domain)
            if self._poll(domain) is not None:
                # Drain residual pipe bytes with a bounded second pass.
                self._read(domain)
                if domain.output and domain.phase == 'running':
                    domain.invalid = True  # truncated JSON is not evidence
                self._finish(key, domain)
                continue
            if domain.invalid or (domain.phase == 'running' and
                                  now - domain.started >= self.policy.timeout_ms / 1000):
                self._stop(domain, now, domain.reason)
            if domain.phase == 'term' and now >= domain.boundary:
                self._signal(domain, signal.SIGKILL)
                domain.phase, domain.boundary = 'kill', now + 0.5
            elif domain.phase == 'kill' and now >= domain.boundary:
                domain.phase = 'quarantined'
                self._publish(domain, 'quarantined')
                # Keep child/process handle and its ledger slot until reaped.
                self._close_pipes(domain)
        if self._closed or not self.devices:
            return
        active = sum(d.phase != 'quarantined' for d in self._domains.values())
        for _ in range(len(self.devices)):
            if active >= self.concurrency:
                break
            device = self.devices[self._cursor]
            self._cursor = (self._cursor + 1) % len(self.devices)
            key = device.device_id
            if (key in _PROCESS_QUARANTINE or _identity_count() >= 64) and key not in _PROCESS_DOMAINS:
                if key not in self._quarantine:
                    self._quarantine_device(key, 'admission-quarantined')
                    self._generations[key] += 1
                    self._results[key] = _unknown(device, self.digest, self._generations[key], 'quarantined')
                continue
            if key in _PROCESS_DOMAINS or key in self._quarantine or key in self._results:
                continue
            generation = self._generations[key] + 1
            self._generations[key] = generation
            try:
                payload = _payload(device, self.digest, generation, self.policy)
                process = self._factory(device, self.digest, generation, self.policy)
            except (OSError, ValueError, UnicodeError):
                self._results[key] = _unknown(device, self.digest, generation)
                self._quarantine_device(key, 'admission-error')
                continue
            domain = _Domain(device, generation, process, now)
            self._domains[key] = domain  # ledger before any fallible pipe action
            _PROCESS_DOMAINS[key] = (self, domain)
            self._spawned += 1
            active += 1
            # Input can exceed PIPE_BUF. Nonblocking partial writes continue in tick.
            domain.payload = payload
            try:
                for stream in (process.stdin, process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                    domain.nonblocking.add(stream.fileno())
            except OSError:
                self._fail_io(domain)
        for domain in self._domains.values():
            stream = domain.process.stdin
            if (domain.phase != 'running' or stream is None or stream.closed
                    or stream.fileno() not in domain.nonblocking):
                continue
            try:
                domain.sent += os.write(stream.fileno(), domain.payload[domain.sent:])
                if domain.sent == len(domain.payload):
                    stream.close()
                    domain.payload = b''
            except BlockingIOError:
                pass
            except OSError:
                self._fail_io(domain)

    def drain(self) -> tuple[ProbeResult, ...]:
        result = tuple(self._results.values())
        self._results.clear()
        return result

    def close(self, timeout_s: float = 2.0) -> bool:
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError('Invalid close timeout')
        self._closed = True
        start = time.monotonic()
        base = max(start, self._last_now)
        for domain in self._domains.values():
            self._stop(domain, base, 'close')
        while self._domains:
            now = time.monotonic()
            # A short/expired close budget must not spend 0.5s waiting for TERM
            # before requesting KILL. Never read unconfirmed blocking pipes.
            if now - start >= max(0, timeout_s - 0.5):
                for domain in tuple(self._domains.values()):
                    if not domain.kill_sent:
                        self._signal(domain, signal.SIGKILL)
            self.tick(max(base + now - start, self._last_now))
            if time.monotonic() - start >= timeout_s and self._domains:
                for domain in self._domains.values():
                    if domain.phase != 'quarantined':
                        self._publish(domain, 'quarantined')
                    domain.phase = 'quarantined'
                    self._close_pipes(domain)
                return False
            if self._domains:
                time.sleep(min(0.005, max(0, timeout_s - (time.monotonic() - start))))
        return True

    def stats(self) -> dict[str, int]:
        return dict(active=sum(d.phase != 'quarantined' for d in self._domains.values()),
                    quarantined=len(self._quarantine), workers=len(self._domains),
                    children=len(self._domains), queued=len(self._results),
                    bytes=sum(len(d.output) + len(getattr(d, 'payload', b'')) +
                              (len(canonical_json(asdict(d.latest)).encode('utf-8'))
                               if d.latest is not None else 0)
                              for d in self._domains.values()) +
                    sum(len(canonical_json(asdict(r)).encode('utf-8')) for r in self._results.values()),
                    spawned=self._spawned, reaped=self._reaped, rejected=self._rejected,
                    global_workers=len(_PROCESS_DOMAINS), global_children=len(_PROCESS_DOMAINS),
                    global_quarantined=len(_PROCESS_QUARANTINE), global_identities=_identity_count())


def _test_scheduler(devices, *, registry_digest, policy, factory, concurrency=2):
    """Private local synthetic factory seam, not a production CLI/plugin option."""
    scheduler = ProbeScheduler(devices, registry_digest=registry_digest,
                               policy=policy, concurrency=concurrency)
    scheduler._factory = factory
    return scheduler
