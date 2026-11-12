"""Fixed SSH read adapter. No I/O occurs until an explicit probe is requested."""
import os
import re
import shlex
import stat
import time
from dataclasses import asdict

from host.common.device_registry import Device
from host.common.governance_contract import (
    Evidence, FIELD_IDS, ProbePolicy, ProbeResult, validate_probe_policy, validate_probe_result,
)
from host.common.strict_json import canonical_json, strict_json_loads


# This program is constant, not a policy-supplied command. It never forks. Stages
# flush before potentially slow source/disk reads. SIGALRM is not a D-state cure.
_REMOTE_SCRIPT = r'''
import os, sys, json, stat, signal, hashlib
from datetime import datetime, timezone
signal.signal(signal.SIGALRM, lambda *args: sys.exit(124))
signal.setitimer(signal.ITIMER_REAL, 4.5)
p = json.loads(sys.stdin.buffer.read(65537))
def stamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
def read_file(path, limit, ordinary=True):
    if ordinary:
        current = '/'
        for part in path.strip('/').split('/'):
            current = os.path.join(current, part)
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise ValueError('symlink')
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('not regular')
        data = bytearray()
        while len(data) <= limit:
            part = os.read(fd, min(16384, limit + 1 - len(data)))
            if not part:
                return bytes(data)
            data.extend(part)
        raise ValueError('too large')
    finally:
        os.close(fd)
names = ('boot_id', 'ram_available_bytes', 'disk_available_bytes',
         'controller_state', 'installed_components', 'runtime_components')
fields = {n: dict(value_json=None, source='probe:not-proven', observed_at=None,
                  quality='unknown', reason='no read evidence') for n in names}
verified = False
def emit(status):
    row = dict(registry_digest=p['registry_digest'], device_id=p['device_id'],
               generation=p['generation'], identity_verified=verified,
               status=status, fields=[[n, fields[n]] for n in names])
    print(json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False), flush=True)
def observed(name, value, source):
    fields[name] = dict(value_json=json.dumps(value, sort_keys=True, separators=(',', ':'),
                       ensure_ascii=False, allow_nan=False), source=source,
                       observed_at=stamp(), quality='observed', reason=None)
try:
    identity = read_file(p['identity_path'], 32).decode('ascii')
    if identity not in (p['device_id'], p['device_id']+'\n'):
        emit('identity_rejected')
        sys.exit(0)
    verified = True
    emit('partial')
except (OSError, ValueError, UnicodeError):
    emit('identity_rejected')
    sys.exit(0)
try:
    observed('boot_id', read_file('/proc/sys/kernel/random/boot_id', 64, False).decode('ascii').strip(), 'proc:boot_id')
except (OSError, ValueError, UnicodeError):
    pass
try:
    mem = read_file('/proc/meminfo', 16384, False).decode('ascii')
    for line in mem.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == 'MemAvailable:' and parts[2] == 'kB':
            observed('ram_available_bytes', int(parts[1])*1024, 'proc:meminfo')
except (OSError, ValueError, UnicodeError):
    pass
emit('partial')
components = {}
for component, path in p['component_paths']:
    row = dict(sha256=None, status='unknown', source='file:'+path)
    try:
        row.update(sha256=hashlib.sha256(read_file(path, 262144)).hexdigest(), status='observed')
    except (OSError, ValueError):
        pass
    components[component] = row
observed('installed_components', components, 'probe:single-file-hashes')
emit('partial')
if p['disk_mount'] is not None:
    # A foreground, alarm-bounded final stage; proc evidence already flushed.
    signal.setitimer(signal.ITIMER_REAL, 0.5)
    try:
        v = os.statvfs(p['disk_mount'])
        observed('disk_available_bytes', v.f_bavail*v.f_frsize, 'statvfs:explicit-mount')
    except (OSError, ValueError):
        pass
    emit('partial')
'''


def _unknown(device, digest, generation, status='unavailable'):
    return ProbeResult(digest, device.device_id, generation, False, status,
                       tuple((n, Evidence(None, 'probe:not-proven', None, 'unknown',
                                          'no verified observation')) for n in FIELD_IDS))


def _validate_request(device, digest, generation, policy):
    if type(device) is not Device:
        raise ValueError('Expected Device')
    device.__post_init__()
    if not device.enrolled or not device.monitor_enabled:
        raise ValueError('Monitor denied')
    validate_probe_policy(policy)
    _unknown(device, digest, generation)  # validates strict digest/generation
    paths = (policy.ssh_config, policy.known_hosts, policy.identity_path,
             *(path for _, path in policy.component_paths))
    if any(len(p) > 4096 for p in paths):
        raise ValueError('Path too long')
    for _, path in policy.component_paths:
        if not path.endswith(('.py', '.sh', '.service', '.rules', '.js', '.ts', '.toml')):
            raise ValueError('Only explicitly approved ordinary source files')
        if path.startswith(('/proc/', '/sys/', '/dev/')):
            raise ValueError('Not an ordinary source path')


def _admit_identity_file(config, alias):
    """Read ONLY the explicitly passed config; never expand Include/Match/%/~.

    Tiny accepted grammar: exact Host sections and one absolute IdentityFile per
    section. The file is data here, never passed to ssh. Main must approve it.
    """
    fd = os.open(config, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('SSH admission requires a regular file')
        data = os.read(fd, 65537)
    finally:
        os.close(fd)
    if len(data) > 65536:
        raise ValueError('SSH config too large')
    sections, current = {}, None
    for line in data.decode('utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError('SSH config outside admission grammar')
        key, value = parts
        if key.lower() == 'host' and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
            if value in sections:
                raise ValueError('Duplicate Host')
            sections[value] = None
            current = value
        elif key.lower() == 'identityfile' and current is not None:
            if (sections[current] is not None or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value)
                    or any(p in ('', '.', '..') for p in value.split('/')[1:])):
                raise ValueError('Invalid IdentityFile')
            sections[current] = value
        else:
            raise ValueError('SSH directive not admitted')
    if alias not in sections or sections[alias] is None:
        raise ValueError('Explicit identity selection required')
    return sections[alias]


def _ssh_argv(device, policy):
    identity = _admit_identity_file(policy.ssh_config, device.ssh_alias)
    options = {
        'HostName': device.address, 'HostKeyAlias': device.host_key_alias,
        'User': device.ssh_user, 'StrictHostKeyChecking': 'yes', 'BatchMode': 'yes',
        'UserKnownHostsFile': '"' + policy.known_hosts + '"', 'GlobalKnownHostsFile': '/dev/null',
        'KnownHostsCommand': 'none', 'UpdateHostKeys': 'no', 'VerifyHostKeyDNS': 'no',
        'CheckHostIP': 'no', 'IdentityAgent': 'none', 'IdentitiesOnly': 'yes',
        'PasswordAuthentication': 'no', 'KbdInteractiveAuthentication': 'no',
        'PreferredAuthentications': 'publickey', 'NumberOfPasswordPrompts': '0',
        'PKCS11Provider': 'none', 'SecurityKeyProvider': 'internal',
        'ControlMaster': 'no', 'ControlPersist': 'no', 'ControlPath': 'none',
        'ForwardAgent': 'no', 'ForwardX11': 'no', 'ClearAllForwardings': 'yes',
        'PermitLocalCommand': 'no', 'ProxyCommand': 'none', 'ProxyJump': 'none',
        'RequestTTY': 'no', 'ConnectionAttempts': '1', 'ConnectTimeout': '5',
        'ServerAliveInterval': '1', 'ServerAliveCountMax': '1', 'LogLevel': 'ERROR',
    }
    # OpenSSH option tokenization is separate from subprocess argv tokenization.
    if any(c in policy.known_hosts for c in ('"', '%', '$', '~')):
        raise ValueError('Trust path contains SSH expansion syntax')
    argv = ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-i', identity]
    for key, value in options.items():
        argv.extend(('-o', key + '=' + value))
    argv.extend(('--', device.ssh_alias, '/usr/bin/python3 -I -S -c ' + shlex.quote(_REMOTE_SCRIPT)))
    return argv


def _payload(device, digest, generation, policy):
    value = dict(device_id=device.device_id, registry_digest=digest, generation=generation,
                 identity_path=policy.identity_path, component_paths=policy.component_paths,
                 disk_mount=policy.disk_mount)
    data = canonical_json(value).encode('utf-8')
    if len(data) > 65536:
        raise ValueError('Input too large')
    return data


def _decode_result(line, device, digest, generation):
    result = validate_probe_result(strict_json_loads(line.decode('utf-8')))
    if (result.device_id, result.registry_digest, result.generation) != (
            device.device_id, digest, generation):
        raise ValueError('Late or foreign result')
    return result


def probe_once(device: Device, *, registry_digest: str, generation: int,
               policy: ProbePolicy) -> ProbeResult:
    """One bounded direct SSH domain; no registry/config discovery or retries."""
    from .isolated_transport import ProbeScheduler
    _validate_request(device, registry_digest, generation, policy)
    scheduler = ProbeScheduler((device,), registry_digest=registry_digest, policy=policy)
    scheduler._generations[device.device_id] = generation - 1
    try:
        while True:
            scheduler.tick(time.monotonic())
            result = scheduler.drain()
            if result:
                return result[-1]
            time.sleep(0.005)
    finally:
        scheduler.close()
