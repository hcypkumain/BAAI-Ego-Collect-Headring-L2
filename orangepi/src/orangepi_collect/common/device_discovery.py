"""Linux USB discovery without opening streams. Python 3.8; no extra packages.

Unique receiver serials identify EMG units, never their body side in manual
mode. Duplicate camera serials only identify a model: remembered ports preserve
A/B slots, never establish physical left/right.
Resolved paths are snapshotted per recording; a live stream is never retargeted.
"""
import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile
from collections import Counter


def _usb_identity(node, sys_root):
    branch = 'video4linux' if node.name.startswith('video') else 'tty'
    path = (sys_root / 'class' / branch / node.name / 'device').resolve()
    for parent in (path,) + tuple(path.parents):
        if (parent / 'idVendor').is_file() and (parent / 'idProduct').is_file():
            def read(name):
                try:
                    return (parent / name).read_text().strip()
                except OSError:
                    return ''
            return dict(vendor=read('idVendor'), product=read('idProduct'),
                        serial=read('serial'), topology=parent.name.split('-', 1)[-1])
    return None


def inventory(settings=None, dev_root=Path('/dev'), sys_root=Path('/sys')):
    settings = settings or {}
    allowed = {
        'wrist': set(settings.get('wrist_usb_ids', ['0bda:5858'])),
        'emg': set(settings.get('emg_usb_ids', ['1a86:55d3'])),
        'eeg': set(settings.get('eeg_usb_ids', ['303a:1001'])),
    }
    found = []
    seen = set()
    paths = [('wrist', p) for p in (dev_root / 'v4l/by-path').glob('*usb*video-index0')]
    paths += [('serial', p) for p in (dev_root / 'serial/by-id').glob('*')]
    for kind, path in sorted(paths, key=lambda pair: str(pair[1])):
        try:
            node = path.resolve(strict=True)
            identity = _usb_identity(node, sys_root)
            if identity and kind == 'serial':
                usb_id = identity['vendor'] + ':' + identity['product']
                kind = next((k for k in ('emg', 'eeg') if usb_id in allowed[k]), 'unknown')
            if not identity or identity['vendor'] + ':' + identity['product'] not in allowed.get(kind, set()):
                continue
            if str(node) in seen:
                continue
            seen.add(str(node))
            found.append(dict(identity, kind=kind, path=str(path), node=str(node),
                              generation=node.stat().st_ctime_ns))
        except OSError:
            # A USB disconnect during enumeration is normal; next poll retries.
            continue
    return found


def fingerprint(settings=None):
    return tuple(sorted((x['kind'], x['path'], x['serial'], x.get('generation'))
                        for x in inventory(settings)))


def _same_unit(a, b):
    return all(a.get(key, '') == b.get(key, '') for key in ('vendor', 'product', 'serial'))


def _suffix(a, b):
    count = 0
    for left, right in zip(reversed(a.split('.')), reversed(b.split('.'))):
        if left != right:
            break
        count += 1
    return count


def _resolve_manual_emg(slots, available, bindings, settings):
    """Give supported current units temporary A/B names, never inherited sides."""
    names = ('emg_left', 'emg_right')
    if set(slots) != set(names):
        raise ValueError('Manual EMG assignment requires two left/right stream slots')
    allowed = set(value.lower() for value in settings.get('emg_usb_ids', ['1a86:55d3']))
    available = [item for item in available
                 if (str(item.get('vendor', '')).lower()+':'+str(item.get('product', '')).lower()) in allowed]
    serials = [item.get('serial', '') for item in available]
    nodes = [item.get('node') or item['path'] for item in available]
    identities_unique = (all(isinstance(value, str) and value.strip() for value in serials)
                         and len(set(serials)) == len(serials)
                         and len(set(nodes)) == len(nodes)
                         and len({item['path'] for item in available}) == len(available))
    ambiguous = len(available) > 2 or not identities_unique
    # A sole known unit may be previewed as A; it can never produce a pair proof.
    assignable = [] if ambiguous else available
    for index, name in enumerate(names):
        item = slots[name]
        item['label'] = '肌电设备 '+chr(65+index)+'（左右未确认）'
        if index < len(assignable):
            candidate = assignable[index]
            item['port'] = candidate['path']
            item['discovery'] = dict(present=True, role_confirmed=False,
                serial=candidate['serial'], topology=candidate.get('topology', ''),
                path=candidate['path'], generation=candidate.get('generation'),
                binding='temporary', candidate_count=len(available))
            bindings[name] = dict(candidate, role_confirmed=False)
        else:
            # No stale configured port may open a former left/right receiver.
            item['port'] = '/dev/emg-unassigned-'+chr(65+index)
            item['discovery'] = dict(present=False, role_confirmed=False,
                reason='ambiguous' if ambiguous else 'missing',
                binding='temporary', candidate_count=len(available))
            bindings.pop(name, None)


def resolve(config, candidates, registry):
    """Pure reconciliation: one physical node per slot; unknown sides stay unknown."""
    config = copy.deepcopy(config)
    state = copy.deepcopy(registry)
    state.setdefault('version', 1)
    bindings = state.setdefault('slots', {})
    for kind, section, key, label in [('wrist', 'wrist_cameras', 'device', '腕部相机'),
                                      ('emg', 'emg', 'port', '肌电设备')]:
        slots = config.get(section, {})
        available = sorted((x for x in candidates if x['kind'] == kind), key=lambda x: x['path'])
        if kind == 'emg' and config.get('emg_calibration', {}).get('enabled', False):
            # Manual assignment ignores old fixed serial roles, configured side
            # hints and all registry role_confirmed flags. Serial is identity only.
            config.pop('emg_serial_roles', None)
            _resolve_manual_emg(slots, available, bindings, config.get('auto_discovery', {}))
            continue
        # Explicit serial roles take priority over topology and historical bindings.
        # A missing or unfamiliar receiver must never be substituted into a side.
        roles = config.get('emg_serial_roles', {}) if kind == 'emg' else {}
        if roles:
            if set(roles) != set(slots) or len(set(roles.values())) != len(slots) or not all(roles.values()):
                raise ValueError('Invalid unique EMG serial roles')
            for name, item in slots.items():
                serial = roles[name]
                matches = [x for x in available if x.get('serial') == serial]
                present = len(matches) == 1
                item['discovery'] = dict(present=present, role_confirmed=True, serial=serial,
                    reason=None if present else 'ambiguous' if matches else 'missing', binding='serial')
                if present:
                    candidate = matches[0]; item[key] = candidate['path']
                    item['discovery'].update(path=candidate['path'], topology=candidate.get('topology',''))
                    bindings[name] = dict(candidate, role_confirmed=True)
                else:
                    item[key] = '/dev/serial/by-id/usb-1a86_USB_Single_Serial_'+serial+'-if00'
            continue
        assigned = {}
        used = set()
        serial_counts = Counter(x.get('serial') for x in available)
        historical_counts = Counter(bindings[n].get('serial') for n in slots if n in bindings)

        def take(name, item, method, confirmed=False):
            assigned[name] = (item, method, confirmed)
            used.add(item['path'])

        # Reserve strong matches for ALL slots before allocating replacements.
        for name, item in slots.items():
            saved = bindings.get(name)
            if saved:
                serial = saved.get('serial', '')
                unique = (serial and serial != '200901010001' and
                          serial_counts[serial] == 1 and historical_counts[serial] <= 1)
                matches = [c for c in available if c['path'] not in used and _same_unit(c, saved)
                           and (unique or c['path'] == saved.get('path'))]
                if len(matches) == 1:
                    take(name, matches[0], 'serial' if unique else 'port', saved.get('role_confirmed', False))
            else:
                # Existing explicit known ports/receiver IDs remain valid hints.
                matches = [c for c in available if c['path'] not in used and
                           (c['path'] == item.get(key) or
                            (kind == 'emg' and c.get('serial') and c['serial'] in item.get(key, '')))]
                if len(matches) == 1:
                    take(name, matches[0], 'configured', True)

        # Moving a hub changes its parent prefix; uniquely matching child ports
        # preserve the slot, but physical side is no longer a verified fact.
        for name in slots:
            saved = bindings.get(name)
            if name in assigned or not saved:
                continue
            matches = [(c, _suffix(c.get('topology', ''), saved.get('topology', '')))
                       for c in available if c['path'] not in used and _same_unit(c, saved)]
            best = max([score for _, score in matches] or [0])
            winners = [c for c, score in matches if score == best and score >= 2]
            if len(winners) == 1:
                take(name, winners[0], 'hub-port', False)

        remaining = [c for c in available if c['path'] not in used]
        empty = [n for n in slots if n not in assigned]
        # Do not choose an arbitrary subset when there are too many devices.
        if len(remaining) <= len(empty):
            for name, item in zip(empty, remaining):
                take(name, item, 'automatic', False)

        for index, (name, item) in enumerate(slots.items()):
            saved = bindings.get(name, {})
            if name in assigned:
                candidate, method, confirmed = assigned[name]
                item[key] = candidate['path']
                bindings[name] = dict(candidate, role_confirmed=confirmed)
                item['discovery'] = dict(present=True, role_confirmed=confirmed,
                                         serial=candidate.get('serial', ''),
                                         topology=candidate.get('topology', ''),
                                         path=candidate['path'])
            else:
                confirmed = saved.get('role_confirmed', False)
                item[key] = saved.get('path', item.get(key))
                item['discovery'] = dict(present=False, role_confirmed=confirmed,
                                         reason='ambiguous' if remaining else 'missing')
            if not confirmed:
                item['label'] = label + ' ' + chr(65 + index) + '（左右未确认）'
    eeg = config.get('eeg')
    if eeg and eeg.get('enabled', True):
        serial = eeg.get('serial', '')
        matches = [c for c in candidates if c['kind'] == 'eeg' and
                   (not serial or c.get('serial', '').upper() == serial.upper())]
        present = len(matches) == 1
        eeg['discovery'] = dict(present=present, role_confirmed=True, serial=serial,
            reason=None if present else 'ambiguous' if matches else 'missing', binding='serial')
        if present:
            eeg['serial'] = matches[0].get('serial','')
            eeg['port'] = matches[0]['path']
            eeg['discovery'].update(path=eeg['port'], topology=matches[0].get('topology', ''))
            bindings['eeg'] = dict(matches[0], role_confirmed=True)
        else:
            eeg['port'] = '/dev/omnibci-unconnected'
    return config, state


def discover(config, base):
    settings = config.get('auto_discovery', {})
    if not settings.get('enabled', False):
        return config
    candidates = inventory(settings)
    runtime = Path(base) / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / 'device_bindings.json'
    with (runtime / 'device_bindings.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Corrupt bindings must not silently turn a known right device into left.
        state = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(state, dict) or not isinstance(state.get('slots', {}), dict):
            raise ValueError('Invalid device bindings; restore runtime/device_bindings.json')
        resolved, updated = resolve(config, candidates, state)
        if updated != state:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=str(runtime),
                                                 prefix='.device-bindings-', delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(updated, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(path)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
        return resolved
