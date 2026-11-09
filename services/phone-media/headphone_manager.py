"""Bounded Pi-owned classic Bluetooth headphone controls for the phone interface.

All D-Bus, filesystem and Pulse operations run on one daemon worker. HTTP
threads only copy a bounded status or enqueue an explicit action. No recording
or controller code is imported, and a Bluetooth connection is never a wearing
confirmation.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

import classic_bluetooth_mode

VERSION = 1
MAC = re.compile(r'[0-9A-F]{2}(?::[0-9A-F]{2}){5}\Z')
AUDIO_SINK = '0000110b-0000-1000-8000-00805f9b34fb'
LEGACY_ADDRESS = '41:42:98:47:58:B5'
PULSE_SERVER = 'unix:/run/user/1000/pulse/pi-collect-headphones'
SPEAKER = 'alsa_output.usb-Timesintelli_USB_Audio_Device_223483-00.mono-fallback'
PLAYER = '/opt/opi-headphone-audio/guarded-paplay'
MAX_DEVICES = 24
SCAN_SECONDS = 12.0


def address(value):
    if not isinstance(value, str) or not MAC.fullmatch(value):
        raise ValueError('invalid_address')
    return value


def sink_name(value):
    return 'bluez_sink.' + address(value).replace(':', '_') + '.a2dp_sink'


def clean_name(value):
    # ensure_ascii JSON encodes an astral character as two six-byte escapes;
    # 24 rows plus every state field must still fit the native 16KiB limit.
    return ''.join(c if c.isprintable() else ' ' for c in str(value))[:32]

def battery_percent(interfaces):
    try:
        value=interfaces.get('org.bluez.Battery1',{}).get('Percentage')
        number=int(value)
        return number if 0<=number<=100 else None
    except (ValueError,TypeError,OverflowError):return None


def unique_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_field')
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda unused: (_ for _ in ()).throw(ValueError('invalid_number')))


class SelectionSaveError(OSError):
    def __init__(self, committed):
        super().__init__('selection_durability_failed' if committed else 'selection_save_failed')
        self.committed = committed


def save_selection(path, value):
    """The same-user collector reads this durable, strict, small JSON file."""
    if value is not None:
        address(value)
    path = Path(path)
    raw = json.dumps({'version': 1, 'address': value}, separators=(',', ':')).encode('ascii') + b'\n'
    fd, temporary = tempfile.mkstemp(prefix='.headphone-selection-', dir=str(path.parent))
    committed = False
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.chmod(temporary, 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
        committed = True
        if os.name == 'posix':
            directory = os.open(str(path.parent), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError:
        raise SelectionSaveError(committed)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class BluetoothError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class BluezTransport:
    """Synchronous, private D-Bus connection owned by the manager thread."""
    def __init__(self, pulse_server=PULSE_SERVER, tone=None):
        self.bus = None
        self.dbus = None
        self.adapter = None
        self.paths = {}
        self.preferred_address = None
        self.pulse_server = pulse_server
        self.tone = str(tone or Path(__file__).with_name('headphone-test.wav'))

    def _interface(self, path, interface):
        try:
            if self.bus is None:
                import dbus
                self.dbus = dbus
                self.bus = dbus.SystemBus(private=True)
            return self.dbus.Interface(self.bus.get_object('org.bluez', path), interface)
        except Exception:
            self.close()
            raise BluetoothError('bluetooth_unavailable')

    @staticmethod
    def _error(exc, fallback):
        name = getattr(exc, 'get_dbus_name', lambda: '')()
        if fallback == 'scan_failed' and name.endswith('InProgress'):
            return BluetoothError('scan_busy')
        if name.endswith(('NotAuthorized', 'AccessDenied', 'AuthenticationRejected')):
            return BluetoothError('permission_denied')
        if name.endswith(('NoReply', 'Timeout', 'TimedOut')):
            return BluetoothError('timeout')
        if name.endswith(('NotReady',)):
            return BluetoothError('bluetooth_off')
        if name.endswith(('ServiceUnknown', 'NameHasNoOwner', 'Disconnected')):
            return BluetoothError('bluetooth_unavailable')
        return BluetoothError(fallback)

    def inspect(self):
        try:
            objects = self._interface('/', 'org.freedesktop.DBus.ObjectManager').GetManagedObjects(timeout=3)
        except BluetoothError:
            raise
        except Exception as exc:
            self.close()
            raise self._error(exc, 'bluetooth_unavailable')
        adapters = sorted((str(p), i['org.bluez.Adapter1']) for p, i in objects.items()
                          if 'org.bluez.Adapter1' in i)
        if not adapters:
            self.adapter = None
            raise BluetoothError('adapter_missing')
        # Prefer the adapter containing the already known headset if present.
        preferred = self.preferred_address or LEGACY_ADDRESS
        known = next((str(p).rsplit('/dev_', 1)[0] for p, i in objects.items()
                      if str(i.get('org.bluez.Device1', {}).get('Address', '')) == preferred), None)
        self.adapter, adapter = next((x for x in adapters if x[0] == known), adapters[0])
        devices = []
        self.paths = {}
        for path, interfaces in objects.items():
            properties = interfaces.get('org.bluez.Device1')
            if properties is None or str(properties.get('Adapter', '')) != self.adapter:
                continue
            mac = str(properties.get('Address', '')).upper()
            if not MAC.fullmatch(mac):
                continue
            uuids = {str(x).lower() for x in properties.get('UUIDs', [])}
            # Classic inquiry may reveal the audio/video Class before SDP has
            # populated UUIDs. Show such candidates; ConnectProfile plus an
            # actual A2DP Pulse sink still gate successful saved selection.
            audio = (AUDIO_SINK in uuids or
                     (int(properties.get('Class', 0)) & 0x1f00) == 0x0400)
            if not audio:
                continue
            self.paths[mac] = str(path)
            devices.append({'address': mac,
                            'name': clean_name(properties.get('Alias') or properties.get('Name') or mac),
                            'paired': bool(properties.get('Paired', False)),
                            'trusted': bool(properties.get('Trusted', False)),
                            'connected': bool(properties.get('Connected', False)),
                            'battery_percent': battery_percent(interfaces),
                            'audio': True})
        return {'available': True, 'powered': bool(adapter.get('Powered', False)), 'devices': devices}

    def power(self):
        try:
            self._interface(self.adapter, 'org.freedesktop.DBus.Properties').Set(
                'org.bluez.Adapter1', 'Powered', self.dbus.Boolean(True), timeout=4)
        except Exception as exc:
            raise self._error(exc, 'bluetooth_off')

    def classic_preflight(self):
        return classic_bluetooth_mode.assess(
            classic_bluetooth_mode.disk_mode(), classic_bluetooth_mode.runtime_mode(self.adapter))

    def start_scan(self):
        try:
            adapter = self._interface(self.adapter, 'org.bluez.Adapter1')
            adapter.SetDiscoveryFilter({'Transport': self.dbus.String('bredr')}, timeout=4)
            adapter.StartDiscovery(timeout=4)
        except Exception as exc:
            raise self._error(exc, 'scan_failed')

    def stop_scan(self):
        if self.adapter:
            try:
                self._interface(self.adapter, 'org.bluez.Adapter1').StopDiscovery(timeout=2)
            except Exception:
                pass

    def pair(self, mac):
        # Application-scoped NoInputNoOutput agent: no Pi display/default GUI
        # agent is needed. Verify BlueZ state rather than trusting CLI text.
        try:
            address(mac)
            subprocess.run(['bluetoothctl','--agent','NoInputNoOutput','--timeout','20','pair',mac],
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=23,check=False)
            if not any(row['address']==mac and row['paired'] for row in self.inspect()['devices']):
                raise BluetoothError('pair_failed')
        except subprocess.TimeoutExpired:raise BluetoothError('timeout')
        except OSError:raise BluetoothError('bluetooth_unavailable')

    def trust(self, mac):
        try:
            self._interface(self.paths[mac], 'org.freedesktop.DBus.Properties').Set(
                'org.bluez.Device1', 'Trusted', self.dbus.Boolean(True), timeout=4)
        except Exception as exc:
            raise self._error(exc, 'trust_failed')

    def connect(self, mac):
        try:
            self._interface(self.paths[mac], 'org.bluez.Device1').ConnectProfile(AUDIO_SINK, timeout=15)
        except Exception as exc:
            if getattr(exc, 'get_dbus_name', lambda: '')().endswith('AlreadyConnected'):
                return
            raise self._error(exc, 'connect_failed')

    def disconnect(self, mac, audio_only=False):
        if mac not in self.paths:
            return
        try:
            device=self._interface(self.paths[mac], 'org.bluez.Device1')
            if audio_only:device.DisconnectProfile(AUDIO_SINK,timeout=5)
            else:device.Disconnect(timeout=5)
        except Exception as exc:
            raise self._error(exc, 'disconnect_failed')

    def sinks(self):
        try:
            process = subprocess.run(['pactl', '--server', self.pulse_server, 'list', 'short', 'sinks'],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, timeout=1, check=False)
            if process.returncode or len(process.stdout) > 65536:
                return set()
            return {line.split()[1].decode('ascii') for line in process.stdout.splitlines()
                    if len(line.split()) >= 2}
        except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
            return set()

    def test(self, mac):
        env = dict(os.environ)
        env['PULSE_SERVER'] = self.pulse_server
        env['PULSE_SINK'] = sink_name(mac)
        try:
            result = subprocess.run([PLAYER, '--server', self.pulse_server, '--sink', sink_name(mac), self.tone],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=env, timeout=3, check=False)
            if result.returncode:
                raise BluetoothError('test_failed')
        except (OSError, subprocess.TimeoutExpired):
            raise BluetoothError('test_failed')

    def close(self):
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:
                pass
        self.bus = None


class HeadphoneManager:
    def __init__(self, base, transport=None, start=True, clock=time.monotonic, capture_gate=None):
        self.selection_path = Path(base) / 'configs/headphone_selection.json'
        self.transport = transport or BluezTransport()
        self.capture_gate = capture_gate or (lambda: None)
        self.clock = clock
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.pending = None
        self.selected = None
        self.saved = False
        self.explicit = False
        self.legacy = False
        self.scan_until = 0.0
        self.scan_generation = None
        self.next_retry = 0.0
        self.retry_delay = 5.0
        self.auto_paused = False
        self.devices = []
        self.sinks = set()
        self.state = {'version': 1, 'available': False, 'powered': False,
                      'scanning': False, 'busy': False, 'status': 'unavailable', 'error': '', 'scan_error': '', 'classic_warning': '',
                      'pairing_progress': 'idle', 'selected_address': '', 'selected_name': '',
                      'selected_saved': False, 'connected_address': '', 'audio_ready': False,
                      'output': 'none', 'devices': [], 'retry_seconds': 0,
                      'test_result': 'never', 'wearing_confirmed': False}
        self.state.update(bluetooth_connected=False,paired=False,trusted=False,battery_percent=None,
                          auto_reconnect=False,auto_reconnect_paused=False)
        self.thread = None
        self.start_requested = start
        if start:
            self.thread = threading.Thread(target=self._work, name='pi-headphones', daemon=True)
            try:
                self.thread.start()
            except RuntimeError:
                self.thread = None
                self.state.update(status='error', error='worker_unavailable')

    def snapshot(self):
        with self.lock:
            result = dict(self.state)
            result['devices'] = [dict(x) for x in self.state['devices']]
            result['retry_seconds'] = max(0, int(self.next_retry - self.clock())) if self.selected else 0
            return result

    def _update(self, **changes):
        with self.lock:
            self.state.update(changes)

    def submit(self, action, mac=None):
        if self.stopped.is_set() or (self.start_requested and
                (self.thread is None or not self.thread.is_alive())):
            return 503, 'worker_unavailable'
        if action not in ('scan', 'connect', 'reconnect', 'disconnect', 'forget', 'test'):
            return 400, 'invalid_action'
        if action == 'connect':
            try:
                address(mac)
            except ValueError:
                return 400, 'invalid_address'
        elif mac is not None:
            return 400, 'unexpected_address'
        try:generation=self._check_generation()
        except BluetoothError:return 423, 'capture_busy'
        with self.lock:
            if self.state['busy'] or self.pending is not None:
                return 409, 'busy'
            if action == 'scan' and self.state['scanning']:
                return 202, ''
            if action == 'connect' and not any(x['address'] == mac and x['audio'] for x in self.state['devices']):
                return 400, 'device_unavailable'
            if action in ('test', 'reconnect') and not self.state['selected_address']:
                return 409, 'no_saved_headphone'
            if action == 'test' and not self.state['audio_ready']:
                return 409, 'audio_unavailable'
            self.pending = (action, mac, generation)
            self.state.update(busy=True, error='', scan_error='', status='scanning' if action == 'scan' else
                              'connecting' if action in ('connect', 'reconnect') else self.state['status'])
        self.wake.set()
        return 202, ''

    def _load(self):
        try:
            with self.selection_path.open('rb') as handle:
                raw = handle.read(4097)
            if len(raw) > 4096:
                raise ValueError()
            value = unique_json(raw)
            if (not isinstance(value, dict) or set(value) != {'version', 'address'} or
                    type(value['version']) is not int or value['version'] != 1):
                raise ValueError()
            self.selected = None if value['address'] is None else address(value['address'])
            self.explicit = True
            self.saved = True
        except FileNotFoundError:
            self.legacy = True
        except (OSError, ValueError, UnicodeError, RecursionError):
            self._update(error='selection_invalid')

    def _save(self, mac):
        error = None
        try:
            save_selection(self.selection_path, mac)
        except SelectionSaveError as exc:
            if not exc.committed:
                raise BluetoothError('selection_save_failed')
            # Rename already committed the visible preference. Reflect exactly
            # what the audio reader will see, while reporting durability doubt.
            error = 'selection_sync_failed'
        except OSError:
            raise BluetoothError('selection_save_failed')
        self.selected = mac
        self.saved = True
        self.explicit = True
        self.legacy = False
        self._update(selected_address=mac or '', selected_saved=True, error=error or '',
                     selected_name=next((x['name'] for x in self.devices if x['address'] == mac), ''))
        if error:
            raise BluetoothError(error)

    def _refresh(self):
        if hasattr(self.transport,'preferred_address'):self.transport.preferred_address=self.selected
        view = self.transport.inspect()
        self.devices = view['devices']
        self.sinks = self.transport.sinks()
        if self.legacy and not self.stopped.is_set():
            old = next((x for x in self.devices if x['address'] == LEGACY_ADDRESS and
                        x['paired'] and x['trusted']), None)
            if old:
                self._save(LEGACY_ADDRESS)
        target = next((x for x in self.devices if x['address'] == self.selected), None)
        ready = bool(target and target['connected'] and sink_name(self.selected) in self.sinks)
        rows = sorted(self.devices, key=lambda x: (x['address'] != self.selected,
                      not x['connected'], not x['paired'], x['name'], x['address']))[:MAX_DEVICES]
        rows = [dict(x, selected=x['address'] == self.selected) for x in rows]
        current = self.snapshot()
        status = current['status'] if current['busy'] or self.scan_until else (
            'connected' if ready else 'disconnected' if view['powered'] else 'unavailable')
        error = current['error']
        if ((ready and error not in ('selection_invalid', 'selection_save_failed', 'selection_sync_failed', 'scan_busy')) or
                (view['powered'] and error in ('bluetooth_unavailable', 'adapter_missing', 'bluetooth_off'))):
            error = ''
        self._update(available=True, powered=view['powered'], devices=rows,
                     selected_address=self.selected or '', selected_name=target['name'] if target else '',
                     selected_saved=self.saved, connected_address=self.selected if ready else '',
                     audio_ready=ready, output='headphones' if ready else 'speaker' if SPEAKER in self.sinks else 'none',
                     status=status, error=error, scanning=bool(self.scan_until))
        self._update(bluetooth_connected=bool(target and target['connected']),
            paired=bool(target and target['paired']),trusted=bool(target and target['trusted']),
            battery_percent=target.get('battery_percent') if target else None,
            auto_reconnect=bool(self.selected and not self.auto_paused),auto_reconnect_paused=self.auto_paused)
        if ready:
            self.retry_delay = 5.0
            self.next_retry = self.clock() + 5.0
        return target, ready

    def _stop_scan(self):
        if self.scan_until:
            self.transport.stop_scan()
            self.scan_until = 0.0
            self.scan_generation = None
            self._update(scanning=False)

    def _connect(self, mac, manual, generation=None):
        generation=self._check_generation(generation)
        self.auto_paused = False
        self._stop_scan()
        self._update(busy=True, status='connecting' if manual else 'reconnecting', error='', pairing_progress='connecting')
        target, unused = self._refresh()
        row = next((x for x in self.devices if x['address'] == mac), None)
        if row is None or not row['audio']:
            raise BluetoothError('device_unavailable')
        self._ensure_running()
        if not self.snapshot()['powered']:
            self._check_generation(generation)
            self.transport.power()
        if not row['paired']:
            if not manual:
                raise BluetoothError('not_paired')
            self._ensure_running()
            self._update(pairing_progress='pairing')
            self._check_generation(generation)
            self.transport.pair(mac)
            self._check_generation(generation)
        self._ensure_running()
        if manual and not row['trusted']:
            self._update(pairing_progress='trusting')
            self._check_generation(generation)
            self.transport.trust(mac)
            self._check_generation(generation)
        self._ensure_running()
        self._update(pairing_progress='connecting')
        self._check_generation(generation)
        self.transport.connect(mac)
        self._check_generation(generation)
        self._update(pairing_progress='waiting_audio')
        deadline = self.clock() + 5.0
        while not self.stopped.is_set():
            self._refresh()
            row = next((x for x in self.devices if x['address'] == mac), None)
            if row and row['connected'] and sink_name(mac) in self.sinks:
                self._ensure_running()
                self._check_generation(generation)
                if manual:
                    self._save(mac)
                self._update(pairing_progress='ready', error='', status='connected')
                self._refresh()
                return
            if self.clock() >= deadline:
                break
            self.stopped.wait(.25)
        raise BluetoothError('audio_unavailable')

    def _check_generation(self, expected=None):
        try:current=self.capture_gate()
        except Exception:current=None
        if current is None or (expected is not None and current!=expected):
            raise BluetoothError('capture_busy')
        return current

    def _ensure_running(self):
        if self.stopped.is_set():
            raise BluetoothError('cancelled')

    def _execute(self, action, mac, generation=None):
        generation=self._check_generation(generation)
        if action == 'scan':
            self._refresh()
            if not self.snapshot()['powered']:
                self._check_generation(generation)
                self.transport.power()
            self._check_generation(generation)
            preflight=getattr(self.transport,'classic_preflight',None)
            error,warning=preflight() if preflight else ('','')
            self._update(classic_warning=warning)
            if error:raise BluetoothError(error)
            self._check_generation(generation)
            self.transport.start_scan()
            self.scan_until = self.clock() + SCAN_SECONDS
            self.scan_generation = generation
            try:self._check_generation(generation)
            except BluetoothError:
                self._stop_scan();raise
            self._update(scanning=True, status='scanning', error='')
        elif action in ('connect', 'reconnect'):
            chosen = mac if action == 'connect' else self.selected
            if not chosen:
                raise BluetoothError('no_saved_headphone')
            self._connect(chosen, manual=action == 'connect', generation=generation)
        elif action in ('disconnect','forget'):
            self._stop_scan()
            chosen = self.selected
            if action=='forget':self._save(None)
            self.auto_paused=action=='disconnect'
            row=next((x for x in self.devices if x['address']==chosen),None)
            if chosen and row and row['connected']:
                self._check_generation(generation)
                self.transport.disconnect(chosen,audio_only=action=='disconnect')
            self._update(pairing_progress='idle', error='')
        elif action == 'test':
            self._refresh()
            if not self.snapshot()['audio_ready']:
                raise BluetoothError('audio_unavailable')
            self._ensure_running()
            self._update(test_result='running')
            self._check_generation(generation)
            self.transport.test(self.selected)
            self._update(test_result='played')

    def _backoff(self):
        self.next_retry = self.clock() + self.retry_delay
        self.retry_delay = min(60.0, self.retry_delay * 2)

    def _step(self):
        with self.lock:
            command = self.pending
            self.pending = None
        try:
            # A scan belongs to this D-Bus client. Stop only that discovery
            # session when capture starts or status cannot be authenticated.
            if self.scan_until:
                try:self._check_generation(self.scan_generation)
                except BluetoothError:self._stop_scan()
            if command:
                try:
                    self._execute(*command)
                finally:
                    self._update(busy=False)
            target, ready = self._refresh()
            if self.scan_until and self.clock() >= self.scan_until:
                self._stop_scan()
                self._refresh()
            elif (not self.scan_until and self.selected and not ready and not self.auto_paused and self.clock() >= self.next_retry):
                generation=self._check_generation()
                self._connect(self.selected, manual=False, generation=generation)
        except BluetoothError as exc:
            if exc.code in ('bluetooth_unavailable', 'adapter_missing'):
                self.sinks = self.transport.sinks()
                self._update(available=False, powered=False, audio_ready=False, connected_address='',
                             output='speaker' if SPEAKER in self.sinks else 'none')
                self.scan_until = 0.0
            elif exc.code == 'selection_sync_failed':
                try:
                    self._refresh()
                except Exception:
                    pass
            scan_error=exc.code if command and command[0]=='scan' and (exc.code=='scan_busy' or exc.code.startswith('classic_')) else self.snapshot().get('scan_error','')
            self._update(error=exc.code, scan_error=scan_error, status='error', busy=False, scanning=bool(self.scan_until),
                         test_result='failed' if command and command[0] == 'test' else self.snapshot()['test_result'])
            self._backoff()
        except Exception:
            self._update(error='operation_failed', status='error', busy=False)
            self._backoff()
        finally:
            self._update(busy=False)

    def _work(self):
        try:
            self._load()
            while not self.stopped.is_set():
                self._step()
                self.wake.wait(.7 if self.scan_until else 2.0)
                self.wake.clear()
        finally:
            self._stop_scan()
            self.transport.close()

    def close(self):
        self.stopped.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=.2)
