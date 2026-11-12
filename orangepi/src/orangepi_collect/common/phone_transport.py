"""Read-only USB tether discovery and versioned, fail-closed cache contract.

An addressed Linux interface is NOT a phone identity or display registration.
No network configuration, ADB, DHCP, probes or transport fallback lives here.
"""
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import TypedDict
import uuid

CONFIG = '/etc/headring-phone-transport.json'
CACHE = '/run/headring-phone-tether/link.json'
DRIVERS = frozenset(('rndis_host', 'cdc_ncm'))
STATES = frozenset(('absent', 'identifying', 'no_address', 'addressed', 'ambiguous', 'stale', 'error'))
TTL = 5.0


class TransportConfig(TypedDict):
    mode: str
    selector: dict[str, str]


class TetherLink(TypedDict):
    kind: str
    interface: str
    address: str


class Observation(TypedDict):
    state: str
    reason: str
    links: list[TetherLink]


def private_read(path, limit=32768, private=False):
    """Reject symlink components, nonregular files, oversized or writable data."""
    path = Path(path).absolute()
    # Walk using directory descriptors, not a check-then-open through symlinkable
    # parents. Keep each ancestor pinned until the next component is opened.
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            next_directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        if private:
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('cache parent must be private and observer-owned')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_mode & 0o022:
            raise ValueError('unsafe file')
        if info.st_uid not in (0, os.getuid()):
            raise ValueError('unexpected owner')
        if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError('cache must be private and observer-owned')
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError('oversized file')
        return json.loads(data)
    finally:
        os.close(fd)


def validate_config(value: object) -> TransportConfig:
    if not isinstance(value, dict) or set(value) - {'mode', 'selector'}:
        raise ValueError('invalid transport configuration')
    mode = value.get('mode', 'adb')
    if mode not in ('adb', 'usb_tether'):
        raise ValueError('invalid transport mode')
    selector = value.get('selector', {})
    if not isinstance(selector, dict) or set(selector) - {'usb_port', 'vid', 'pid', 'serial'}:
        raise ValueError('invalid selector')
    if mode == 'usb_tether':
        if not isinstance(selector.get('usb_port'), str) or not re.fullmatch(r'[0-9]+-[0-9]+(?:\.[0-9]+)*', selector['usb_port']):
            raise ValueError('reviewed USB port required')
        for key in ('vid', 'pid'):
            if not isinstance(selector.get(key), str) or not re.fullmatch(r'[0-9a-f]{4}', selector[key]):
                raise ValueError('reviewed VID/PID required')
        if 'serial' in selector and (not isinstance(selector['serial'], str) or not selector['serial'] or len(selector['serial']) > 256):
            raise ValueError('invalid serial')
    return {'mode': mode, 'selector': selector}


def load_config(path: str | Path = CONFIG) -> TransportConfig:
    try:
        value = private_read(path)
    except FileNotFoundError:
        value = {}
    return validate_config(value)


def boot_id() -> str:
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def device_id(path: Path = Path('/opt/egorecord/DEVICE_ID')) -> str | None:
    """Existing deployment identity, not the USB phone's identity. Unknown stays null."""
    try:
        if path.is_symlink() or path.stat().st_size > 128:
            return None
        value = path.read_text().strip()
        return value if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) else None
    except OSError:
        return None


def ipv4(value: str) -> str:
    address = ipaddress.IPv4Address(value)
    if not address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
        raise ValueError('not usable local IPv4')
    return str(address)


def observe(config: TransportConfig, rows: list[dict], sysnet: Path = Path('/sys/class/net')) -> Observation:
    selector = config['selector']
    matches = []
    if config['mode'] != 'usb_tether':
        return {'state': 'absent', 'reason': 'mode_adb', 'links': []}
    for row in rows:
        name = row.get('ifname', '')
        if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,64}', name):
            continue
        try:
            device = (sysnet / name / 'device').resolve(strict=True)
            driver = (device / 'driver').resolve(strict=True).name
            if driver not in DRIVERS:
                continue
            for parent in (device, *device.parents):
                if (parent / 'idVendor').is_file() and (parent / 'idProduct').is_file():
                    break
            else:
                continue
            if parent.name != selector['usb_port']:
                continue
            if any((parent / file).read_text().strip().lower() != selector[key] for key, file in (('vid', 'idVendor'), ('pid', 'idProduct'))):
                continue
            if 'serial' in selector and (parent / 'serial').read_text().strip() != selector['serial']:
                continue
            matches.append((name, row))
        except (OSError, ValueError, RuntimeError):
            continue
    if len(matches) != 1:
        return {'state': 'ambiguous' if matches else 'absent', 'reason': 'multiple_matches' if matches else 'no_qualified_interface', 'links': []}
    name, row = matches[0]
    links = []
    if 'LOWER_UP' in row.get('flags', []):
        for item in row.get('addr_info', []):
            if item.get('family') != 'inet' or item.get('tentative') or item.get('deprecated'):
                continue
            try:
                links.append({'kind': 'usb_tether', 'interface': name, 'address': ipv4(item['local'])})
            except (ValueError, KeyError):
                continue
    return {'state': 'addressed' if links else 'no_address', 'reason': 'pi_address_only' if links else 'awaiting_address', 'links': links}


def ip_rows() -> list[dict]:
    return json.loads(subprocess.check_output(['ip', '-j', 'address', 'show'], timeout=2))


def read_cache(path: str | Path = CACHE, config: TransportConfig | None = None,
               now: float | None = None, boot: str | None = None) -> Observation:
    """Never returns unvalidated cache fields or writes on a consumer read."""
    failure = {'state': 'stale', 'reason': 'cache_unavailable_or_invalid', 'links': []}
    try:
        value = private_read(path, private=True)
        now = time.monotonic() if now is None else now
        boot = boot_id() if boot is None else boot
        if type(value['schema']) is not int or value['schema'] != 1 or value['boot_id'] != boot or type(value['monotonic']) not in (int, float) or not 0 <= now - value['monotonic'] <= TTL:
            return failure
        uuid.UUID(value['observer_instance'])
        if value['state'] not in STATES or value['selector'] != config['selector'] or value['mode'] != config['mode']:
            return failure
        links = []
        for link in value['links']:
            if link['kind'] != 'usb_tether' or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,64}', link['interface']):
                return failure
            links.append({'kind': 'usb_tether', 'interface': link['interface'], 'address': ipv4(link['address'])})
        if (value['state'] == 'addressed') != bool(links):
            return failure
        # Revalidate current topology and addresses: unplug/rename invalidates old IP immediately.
        current = observe(config, ip_rows())
        if current['state'] != value['state'] or current['links'] != links:
            return failure
        return {'state': value['state'], 'reason': current['reason'], 'links': links}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        return failure


def write_cache(path: str | Path, config: TransportConfig, result: Observation, instance: str) -> None:
    path = Path(path)
    parent = path.parent
    if parent.is_symlink() or parent.resolve() != parent.absolute():
        raise ValueError('unsafe cache directory')
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('cache directory must be private and owned by observer')
    value = dict(result, schema=1, boot_id=boot_id(), monotonic=time.monotonic(), observer_instance=instance,
                 selector=config['selector'], mode=config['mode'])
    temp = parent / ('.link-' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
