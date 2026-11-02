"""Controller-main-thread adapter for ordered EMG insertion; no blocking plug loop."""
import copy
import json
import os
import tempfile
import time
from pathlib import Path

from project_paths import BASE, devices
from device_discovery import inventory, fingerprint
import emg_calibration
import emg_insertion
import wrist_camera_settings as capture_settings


def _atomic_runtime_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    name=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=str(path.parent),
                prefix='.emg-insertion-',delete=False) as handle:
            name=handle.name;json.dump(value,handle,ensure_ascii=False,indent=2,allow_nan=False)
            handle.flush();os.fsync(handle.fileno())
        os.replace(name,str(path));name=None
    finally:
        if name is not None:
            try:os.unlink(name)
            except OSError:pass


class EmgInsertionControls:
    def _say_emg_pairing_phase(self, pairing):
        if getattr(self, 'worker', None):
            return
        if pairing.phase == getattr(self, '_emg_pairing_spoken_phase', None):
            return
        self._emg_pairing_spoken_phase = pairing.phase
        # Same synchronous controller voice/prompt/skip/fatal-error policy as
        # existing calibration. Never catch audio errors as pairing failures.
        self.voice.say(emg_insertion.SPEECH[pairing.phase])

    def close_emg_pairing(self, clear=True):
        monitor = getattr(self, '_emg_pairing_monitor', None)
        if monitor is not None:
            try:
                monitor.close()
            finally:
                self._emg_pairing_monitor = None
        if clear:
            self._emg_pairing = None

    def emg_pairing_status(self):
        pairing = getattr(self, '_emg_pairing', None)
        return pairing.snapshot() if pairing is not None else None

    def emg_pairing_preview_allowed(self):
        pairing = getattr(self, '_emg_pairing', None)
        return pairing is not None and pairing.preview_ready and not getattr(self, 'worker', None)

    def start_emg_calibration(self):
        if getattr(self, 'worker', None):
            return  # Recording/prepared owner must be released by its existing API path.
        config = devices()
        if not config.get('emg_calibration', {}).get('enabled', False):
            return
        method = emg_calibration.assignment_method(config)
        self.close_emg_pairing()
        if method == emg_calibration.METHOD:
            self.start_emg_manual_calibration()
            return
        self.stop_preview()
        emg_calibration.invalidate(BASE)
        self.emg_wizard = None
        pairing = emg_insertion.OrderedPairing(emg_calibration.boot_id())
        self._emg_pairing = pairing
        self._emg_pairing_config = copy.deepcopy(config)
        self._emg_pairing_non_emg = tuple(sorted((x['kind'],x['path'],x['serial'],x['generation'])
            for x in inventory(config.get('auto_discovery', {})) if x['kind'] != 'emg'))
        self._emg_pairing_next_poll = 0
        self._emg_pairing_last = None
        try:
            factory = getattr(self, '_emg_monitor_factory', emg_insertion.KernelUSBMonitor)
            self._emg_pairing_monitor = factory(config.get('auto_discovery', {}))
        except (OSError, ValueError) as error:
            pairing.fault(str(error))
        self.state = 'emg_pairing'
        self._emg_pairing_last = pairing.snapshot()
        self._emg_pairing_spoken_phase = None
        self.state_write()
        self._say_emg_pairing_phase(pairing)

    def _read_pairing_signals(self):
        signals = {}
        for slot in emg_calibration.SLOTS:
            path = Path(self.preview_dir)/(slot+'.json')
            try:
                if path.stat().st_size > 128*1024:
                    continue
                value = json.loads(path.read_text())
                if isinstance(value, dict):
                    signals[slot] = value
            except (OSError, ValueError):
                pass
        return signals

    def _finish_ordered_emg(self, proof, evidence):
        """Called with the shared capture lease, after the preview reader closes."""
        emg_calibration.save(BASE, proof)
        config = devices()
        if not config.get('emg_calibration_status', {}).get('valid', False):
            emg_calibration.invalidate(BASE)
            raise emg_insertion.PairingError('connection_changed_before_commit')
        old = list(getattr(self, 'last_scan', []))
        by_path = {row.get('device'):row for row in old if row.get('name') in emg_calibration.SLOTS}
        updated = [row for row in old if row.get('name') not in emg_calibration.SLOTS]
        for slot in emg_calibration.SLOTS:
            item = config['emg'][slot]
            updated.append(dict(by_path.get(item['port'], {}), name=slot,label=item['label'],
                connected=True,present=True,status='ready',device=item['port'],config=item,
                role_confirmed=True,readiness_source='current_preview_frames'))
        self.last_scan = updated
        self.web_config = config
        self.scan_available = [row['name'] for row in updated if row.get('connected')]
        self.scan_ok = bool(self.scan_available)
        selected = tuple(getattr(self, 'selected_devices', None) or self.scan_available)
        self.selected_devices = tuple(dict.fromkeys([n for n in selected if n in self.scan_available] + list(emg_calibration.SLOTS)))
        # Only acknowledge the intended EMG hotplug changes. A concurrent other
        # device change must still trigger the controller's normal full rescan.
        non_emg = tuple(sorted((x['kind'],x['path'],x['serial'],x['generation'])
            for x in inventory(config.get('auto_discovery', {})) if x['kind'] != 'emg'))
        if non_emg == self._emg_pairing_non_emg:
            self.usb_fingerprint = fingerprint(config.get('auto_discovery', {}))
        _atomic_runtime_json(BASE/'runtime/devices.json', updated)
        self.emg_wizard = None
        self.close_emg_pairing()
        self.state = 'idle'
        self.state_write()

    def poll_emg_pairing(self):
        now = time.monotonic()
        if now < getattr(self, '_emg_pairing_next_poll', 0):
            return
        self._emg_pairing_next_poll = now+.1
        pairing = getattr(self, '_emg_pairing', None)
        if pairing is None:
            return
        if pairing.phase == 'error':
            return
        completed = False
        try:
            if getattr(self, 'worker', None):
                raise emg_insertion.PairingError('capture_owner_active')
            monitor = getattr(self, '_emg_pairing_monitor', None)
            if monitor is None:
                raise emg_insertion.PairingError('kernel_monitor_unavailable')
            events = monitor.drain()
            settings = self._emg_pairing_config.get('auto_discovery', {})
            units = emg_insertion.usb_units(settings)
            stamp = emg_calibration.current_stamp(self._emg_pairing_config)
            pairing.observe(emg_calibration.boot_id(), units, stamp, events, monitor.last_sequence)
            if pairing.phase == 'error':
                raise emg_insertion.PairingError(pairing.error)
            preview = getattr(self, 'preview_worker', None)
            if pairing.preview_ready and preview is not None and preview.poll() is None:
                sources = dict(getattr(self, 'emg_preview_sources', {}))
                if set(sources.values()) != {row[0] for row in pairing.stamp}:
                    self.stop_preview()  # Normal ensure_preview starts the correct snapshot next tick.
                else:
                    preview_id = getattr(self, 'preview_snapshot_id', None)
                    evidence = pairing.signal_evidence(sources, self._read_pairing_signals(), preview_id)
                    if evidence:
                        # Validate freshness at acceptance, while the readers
                        # are still live. Closing unrelated preview sources may
                        # take longer than the 3s evidence window.
                        proof = pairing.proof(sources, evidence)
                        self.stop_preview()
                        with capture_settings.capture_guard(BASE):
                            if getattr(self, 'worker', None):
                                raise emg_insertion.PairingError('capture_owner_active')
                            current = emg_insertion.canonical_stamp(emg_calibration.current_stamp(self._emg_pairing_config))
                            expected_stamp=copy.deepcopy(pairing.stamp)
                            # tty removal/creation can lag its real USB event.
                            # Recheck both under the shared capture lease.
                            current_events=monitor.drain()
                            current_units=emg_insertion.usb_units(settings)
                            pairing.observe(emg_calibration.boot_id(),current_units,current,
                                current_events,monitor.last_sequence)
                            if pairing.phase=='error' or current!=expected_stamp or pairing.stamp!=expected_stamp:
                                raise emg_insertion.PairingError('connection_changed_before_commit')
                            self._finish_ordered_emg(proof, evidence)
                        completed = True
        except (OSError, ValueError, KeyError, TypeError) as error:
            pairing.fault(str(error))
            self.close_emg_pairing(clear=False)
            # Only close this controller's idle preview, never a capture worker.
            if not getattr(self, 'worker', None):
                self.stop_preview()
        if completed:
            # Commit/state_write already succeeded. An audio failure must
            # propagate under the original fatal-audio policy, without turning
            # a truthful persisted proof back into a pairing error.
            self.voice.say(emg_calibration.PROMPTS['done'])
            return
        snapshot = pairing.snapshot()
        if snapshot != getattr(self, '_emg_pairing_last', None):
            self._emg_pairing_last = snapshot
            self.state_write()
        self._say_emg_pairing_phase(pairing)
