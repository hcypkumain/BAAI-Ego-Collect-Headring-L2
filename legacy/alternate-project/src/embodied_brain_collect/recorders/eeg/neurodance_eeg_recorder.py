"""NeuroDance USB SDK adapter, shared by Windows and Linux.

SDK callbacks deliver channel-major 200 ms packets. Only the recorder thread
mutates recording buffers. SDK time is preserved, expanded within each packet
and explicitly labelled unverified; there is no amplifier TTL clock fit.
"""
from __future__ import annotations

from importlib.metadata import version
import json
from pathlib import Path
import queue
import re
import time

import numpy as np

from .base_eeg_recorder import BaseEegRecorder
from .eeg_recorder_config import NeuroDanceEegRecorderConfig


class ImpedanceCheckError(RuntimeError):
    """Startup must abort when electrode impedance cannot be accepted."""


def impedance_in_kohm(values, unit):
    values = np.asarray(values, dtype=np.float64)
    if values.shape != (8,) or not np.isfinite(values).all() or np.any(values < 0):
        raise ImpedanceCheckError("Impedance must contain eight finite, non-negative channel values")
    if unit == "kohm":
        return values.copy()
    if unit == "ohm":
        return values / 1000.0
    raise ImpedanceCheckError("Confirm SDK read_impedance() unit with vendor, then set impedance_sdk_unit to 'ohm' or 'kohm'")


def require_acceptable_impedance(values, threshold_kohm):
    if not np.isfinite(threshold_kohm) or threshold_kohm <= 0:
        raise ValueError("impedance_threshold_kohm must be finite and positive")
    bad = np.flatnonzero(values > threshold_kohm)
    if bad.size:
        channels = ", ".join(f"CH{i + 1}={values[i]:.3f} kOhm" for i in bad)
        raise ImpedanceCheckError(f"Impedance exceeds {threshold_kohm:g} kOhm: {channels}; recording aborted")


def normalize_mac(value) -> str:
    if isinstance(value, (bytes, bytearray, list, tuple)):
        value = bytes(value).hex()
    value = re.sub(r"[:\-\s]", "", str(value)).upper()
    if not re.fullmatch(r"[0-9A-F]{12}", value):
        raise ValueError(f"Invalid six-byte device MAC: {value!r}")
    return value


def select_usb_port(configured: str, ports) -> str:
    # Explicit Linux /dev/serial/by-id symlinks need not appear in comports().
    if configured:
        return configured
    candidates = [p.device for p in ports if (
        getattr(p, "vid", None) is not None
        or str(getattr(p, "hwid", "")).upper().startswith("USB")
        or p.device.startswith(("/dev/ttyUSB", "/dev/ttyACM")))]
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one USB serial receiver, found {candidates}; set eeg.port explicitly")
    return candidates[0]


class NeuroDanceEegRecorder(BaseEegRecorder):
    config: NeuroDanceEegRecorderConfig
    _has_trigger_channel = False
    requires_child_processes = True

    def __init__(self, config: NeuroDanceEegRecorderConfig):
        super().__init__(config)
        # No SDK, queue or locks before spawn/pickling.
        self._device = None
        self._packets = None
        self._control = None
        self._packet_times: list[np.ndarray] = []
        self._sample_rate = float(config.sample_rate)
        self._channel_labels = [f"CH{i + 1}" for i in range(8)]
        self._sdk_version = ""
        self._port = ""
        self._device_mac = ""
        self._host_mac = ""
        self._stream_enabled = False
        self._fatal_error = ""
        self._protocol_errors = 0
        self._queue_overflows = 0
        self._last_packet_at = 0.0
        self._not_before_pc = 0.0
        self._delivery_mode = "discard"
        self._impedance_packets = 0
        self._impedance_kohm = np.zeros(0, dtype=np.float64)
        self._impedance_report = {"status": "not_run"}
        self._startup_unsynced_packets = 0

    def _create_device(self):
        from neuro_dance import NdDevice, configure_logging
        if self.config.session_dir:
            from pathlib import Path
            configure_logging(level="INFO", log_file=str(
                Path(self.config.session_dir) / self.output_dir / "neurodance_sdk.log"))
        owner = self

        class Device(NdDevice):
            def eeg_received(self, packet):
                # The SDK owns packet memory; take one copy at the callback boundary.
                try:
                    mode = owner._delivery_mode
                    if mode == "discard":
                        return
                    item = (time.time(), time.monotonic(), float(packet["timestamp"]),
                            np.asarray(packet["data"], dtype=np.float32).copy())
                    if mode == "impedance":
                        expected = (8, owner.config.sample_rate // 5)
                        if item[3].shape != expected or not np.isfinite(item[3]).all():
                            raise ValueError(f"Invalid impedance EEG packet; expected finite {expected}")
                        # read_impedance() consumes NdDevice.eeg_datas, not our queue.
                        super().eeg_received({"timestamp": item[2], "data": item[3]})
                        owner._impedance_packets += 1
                        return
                    owner._packets.put_nowait(item)
                except queue.Full:
                    owner._queue_overflows += 1
                    owner._fatal_error = "NeuroDance callback queue overflow; stopping rather than silently dropping data"
                except Exception as exc:
                    owner._fatal_error = f"Invalid NeuroDance packet: {exc}"

            def eog_received(self, packet):
                pass

            def host_mac_received(self, value):
                owner._control.put(("host_mac", value))

            def device_mac_received(self, value):
                owner._control.put(("device_mac", value))

            def devices_received(self, value):
                owner._control.put(("scan", (time.monotonic(), value)))

            def cmd_error(self, cmd, pm, code):
                owner._fatal_error = f"NeuroDance command rejected: cmd={cmd}, pm={pm}, code={code}"

            def crc_error(self):
                owner._protocol_errors += 1

            def decode_error(self):
                owner._protocol_errors += 1

        return Device(mode="serial", com=self._port, tcp_ip="", tcp_port=None,
                      host_mac_bytes=None)

    def _check_error(self):
        if self._fatal_error:
            raise RuntimeError(self._fatal_error)

    def _wait_value(self, key, seconds, predicate=lambda value: True):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check_error()
            try:
                kind, value = self._control.get(timeout=0.05)
            except queue.Empty:
                continue
            if kind == key and predicate(value):
                return value
        raise TimeoutError(f"No {key} response in {seconds:g}s")

    def _verify_device(self, target, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._device.device_mac_info()
            try:
                value = self._wait_value("device_mac", min(1.0, deadline - time.monotonic()))
            except TimeoutError:
                continue
            actual = normalize_mac(value)
            if target and actual != target:
                raise RuntimeError(f"Connected device MAC {actual} differs from configured {target}; not re-pairing")
            self._device_mac = actual
            return
        raise TimeoutError("Acquisition device did not respond; check power and receiver pairing")

    def _validate_impedance_config(self):
        if not self.config.impedance_check:
            self._impedance_report = {"status": "disabled"}
            self._log("[eeg:neurodance] impedance check explicitly disabled", level="WARNING")
            return
        require_acceptable_impedance(np.zeros(8), self.config.impedance_threshold_kohm)
        impedance_in_kohm(np.zeros(8), self.config.impedance_sdk_unit)
        if not np.isfinite(self.config.impedance_timeout_s) or self.config.impedance_timeout_s <= 0:
            raise ValueError("impedance_timeout_s must be finite and positive")

    def _save_impedance_report(self):
        if self.config.session_dir:
            target = Path(self.config.session_dir) / self.output_dir / "impedance_check.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(self._impedance_report, ensure_ascii=False, indent=2), encoding="utf-8")

    def _run_impedance_check(self):
        """Enable lead-off mode, use SDK's 5-packet FFT estimator, fail closed.

        Unit conversion is configured only after vendor confirmation. In
        particular the SDK's 4.4/2 calibration formula does not prove its unit.
        """
        self._impedance_report = {
            "status": "running", "threshold_kohm": self.config.impedance_threshold_kohm,
            "sdk_unit": self.config.impedance_sdk_unit, "sample_rate": self.config.sample_rate,
            "checked_at_unix_s": time.time(),
        }
        self._impedance_packets = 0
        self._impedance_kohm = np.zeros(0, dtype=np.float64)
        self._delivery_mode = "discard"
        self._device.eeg_datas.removeAll()
        try:
            self._device.eeg_channel_config(self.config.sample_rate, True)
            self._delivery_mode = "impedance"
            self._stream_enabled = True
            self._device.eeg_channel_enable()
            deadline = time.monotonic() + self.config.impedance_timeout_s
            while time.monotonic() < deadline:
                self._check_error()
                # SDK automatically queries the calibration current in its worker.
                # Wait for it as well as five fresh packets (one second of EEG).
                if self._impedance_packets < 5:
                    time.sleep(0.05)
                    continue
                current = self._device.get_ac_current()
                if current is None:
                    time.sleep(0.05)
                    continue
                if not np.isfinite(current) or current <= 0:
                    raise ImpedanceCheckError(f"Invalid SDK impedance calibration current: {current}")
                raw = self._device.read_impedance()
                if raw is None:
                    time.sleep(0.05)
                    continue
                self._impedance_kohm = impedance_in_kohm(raw, self.config.impedance_sdk_unit)
                self._impedance_report.update(
                    raw_values=np.asarray(raw, dtype=float).tolist(),
                    values_kohm=self._impedance_kohm.tolist(), ac_current_sdk=float(current))
                self._log("[eeg:neurodance] impedance: " + ", ".join(
                    f"CH{i + 1}={z:.3f} kOhm" for i, z in enumerate(self._impedance_kohm)))
                require_acceptable_impedance(self._impedance_kohm, self.config.impedance_threshold_kohm)
                self._impedance_report["status"] = "passed"
                return
            raise ImpedanceCheckError(f"Impedance measurement timed out after {self.config.impedance_timeout_s:g}s (need five fresh packets and valid calibration current)")
        except Exception as exc:
            self._impedance_report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._delivery_mode = "discard"
            try:
                # Always disable test mode, including high-impedance/error paths.
                try:
                    self._device.eeg_disable()
                    self._stream_enabled = False
                finally:
                    self._device.eeg_channel_config(self.config.sample_rate, False)
                    time.sleep(0.2)
                    self._device.eeg_datas.removeAll()
            except Exception as exc:
                self._impedance_report.update(status="failed", cleanup_error=str(exc))
                raise
            finally:
                self._save_impedance_report()

    def _open(self) -> bool:
        try:
            if self.config.sample_rate not in (250, 500, 1000, 2000):
                raise ValueError("sample_rate must be 250, 500, 1000 or 2000")
            if self.config.queue_max_packets < 1 or self.config.stream_timeout <= 0:
                raise ValueError("queue_max_packets and stream_timeout must be positive")
            self._validate_impedance_config()
            target = normalize_mac(self.config.device_mac) if self.config.device_mac else ""
            if self.config.pair_on_open and not target:
                raise ValueError("pair_on_open requires an explicit device_mac")
            self._sdk_version = version("neuro-dance")
            if self._sdk_version != "9.8":
                raise RuntimeError(f"Install the bundled platform-specific NeuroDance 9.8 wheel (found {self._sdk_version})")
            from serial.tools.list_ports import comports
            self._port = select_usb_port(self.config.port, comports())
            self._packets = queue.Queue(maxsize=self.config.queue_max_packets)
            self._control = queue.Queue()
            self._fatal_error = ""
            self._device = self._create_device()
            self._device.start()
            self._device.host_mac_info()
            self._host_mac = normalize_mac(self._wait_value("host_mac", 5.0))
            try:
                self._verify_device(target, 3.0)
            except TimeoutError:
                if not self.config.pair_on_open:
                    raise
                started = time.monotonic()
                self._device.devices_scan()
                _, devices = self._wait_value("scan", 8.0, lambda v: v[0] >= started + 3.0)
                matches = [d for d in (devices or []) if normalize_mac(d["mac"]) == target]
                if len(matches) != 1:
                    raise RuntimeError(f"Configured MAC {target} not uniquely present in scan; no pairing command sent")
                raw = matches[0].get("mac_bytes")
                if raw is None or len(raw) != 6:
                    raise ValueError("Scan returned no six-byte mac_bytes; refusing to guess byte order")
                self._device.device_pair([1, *bytes(raw), 1])
                self._verify_device(target, 8.0)
            if self.config.impedance_check:
                self._run_impedance_check()
            self._device.eeg_channel_config(self.config.sample_rate, False)
            self._stream_enabled = True
            self._device.eeg_channel_enable()
            if self.config.impedance_check:
                # Discard transition packets after leaving test mode (5 packets/s).
                time.sleep(1.0)
            self._delivery_mode = "record"
            self._last_packet_at = time.monotonic()
            self._wait_for_synced_packets()
            self._log(f"[eeg:neurodance] {self._port}, 8 channels, requested {self.config.sample_rate} Hz; packet size verified")
            return True
        except Exception as exc:
            self._open_error = (f"No EEG packet within {self.config.stream_timeout:g}s after requesting {self.config.sample_rate} Hz"
                                if isinstance(exc, queue.Empty) else f"{type(exc).__name__}: {exc}")
            self._log(f"[eeg:neurodance] open failed: {self._open_error}", level="ERROR")
            if self.config.impedance_check and self._impedance_report.get("status") == "not_run":
                self._impedance_report = {"status": "not_run", "error": self._open_error}
                self._save_impedance_report()
            self._close()
            return False

    def _wait_for_synced_packets(self, timeout=25.0):
        # SDK heart_seconds=10. The first heartbeat can precede host MAC discovery.
        # Allow two heartbeat cycles, without weakening timestamp checks in _poll.
        deadline = time.monotonic() + timeout
        pending = []
        while time.monotonic() < deadline:
            self._check_error()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._packets.get(timeout=min(self.config.stream_timeout, remaining))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    break
                raise
            self._check_error()
            arrival, _, sdk_ms, data = item
            if data.shape != (8, self.config.sample_rate // 5):
                raise ValueError(f"Unexpected startup EEG packet shape: {data.shape}; no silent sample-rate fallback")
            if not np.isfinite(sdk_ms) or sdk_ms <= 0:
                raise ValueError(f"Invalid SDK timestamp: {sdk_ms}")
            if sdk_ms < 946684800000 or abs(sdk_ms / 1000 - arrival) > 60:
                self._startup_unsynced_packets += 1
                pending.clear()
                if self._startup_unsynced_packets == 1:
                    self._log(f"[eeg:neurodance] waiting up to {timeout:g}s for SDK timestamp synchronization; discarding startup packets", level="WARNING")
                continue
            pending.append(item)
            if len(pending) == 2:
                for valid in pending:
                    self._accept_packet(valid)
                self._log(f"[eeg:neurodance] SDK Unix timestamps available; discarded {self._startup_unsynced_packets} unsynchronized packets (cross-device synchronization remains unverified)")
                return
        raise TimeoutError(f"SDK timestamp synchronization not ready after {timeout:g}s; check SDK heartbeat/device clock; no recording started")

    def _reset_stream_state(self):
        self._blocks.clear()
        self._total_samples = 0
        self._packet_times.clear()
        self._timestamps_pc = None
        self._fit = None
        self._not_before_pc = time.time()
        self._last_packet_at = time.monotonic()
        # BaseEegRecorder._setup clears standard fields, but not adapter fields.
        for key in list(self._buf):
            if key.startswith("eeg_"):
                self._buf.pop(key, None)
        if self._packets is not None:
            while True:
                try:
                    self._packets.get_nowait()
                except queue.Empty:
                    break

    def _accept_packet(self, item):
        arrival, arrived_mono, sdk_ms, data = item
        expected = self.config.sample_rate // 5
        if data.shape != (8, expected):
            raise ValueError(f"NeuroDance requested {self.config.sample_rate} Hz: expected (8, {expected}) per 200 ms packet, got {data.shape}; no silent sample-rate fallback")
        if not np.isfinite(sdk_ms) or sdk_ms < 946684800000:
            raise ValueError(f"Invalid SDK Unix-millisecond timestamp: {sdk_ms}")
        if abs(sdk_ms / 1000 - arrival) > 60:
            raise ValueError("SDK packet time differs from PC arrival by over 60s; check system clocks")
        times = sdk_ms / 1000 + np.arange(expected, dtype=np.float64) / self._sample_rate
        self._last_packet_at = max(self._last_packet_at, arrived_mono)
        # Trim the SDK packet that straddles commit; do not retain buffered pre-roll.
        offset = int(np.searchsorted(times, self._not_before_pc))
        if offset >= expected:
            return
        self._on_block(self._total_samples, data[:, offset:].T)
        self._packet_times.append(times[offset:])
        self._acc("eeg_packet_sdk_timestamp_ms", sdk_ms)
        self._acc("eeg_packet_arrival_unix_s", arrival)
        self._acc("eeg_packet_arrival_monotonic_s", arrived_mono)
        self._acc("eeg_packet_sample_count", expected)
        self._acc("eeg_packet_trimmed_prefix", offset)

    def _poll(self, ts):
        self._check_error()
        # Bounded work keeps commit and stop responsive even under backlog.
        for index in range(32):
            try:
                item = self._packets.get(timeout=0.05 if index == 0 else 0)
            except queue.Empty:
                break
            try:
                self._accept_packet(item)
            except Exception as exc:
                self._fatal_error = f"Invalid NeuroDance EEG stream: {exc}"
                raise
        if time.monotonic() - self._last_packet_at > self.config.stream_timeout:
            self._fatal_error = f"NeuroDance EEG stream stalled for {self.config.stream_timeout:g}s"
            self._check_error()

    def _count_samples(self):
        return self._total_samples

    def _close(self):
        self._delivery_mode = "discard"
        device, self._device = self._device, None
        if device is not None:
            try:
                if self._stream_enabled:
                    device.eeg_disable()
                    time.sleep(0.2)
            finally:
                self._stream_enabled = False
                device.close()
        # Deliberately do not call BaseEegRecorder._align(): SDK provides no TTL events.

    def _heartbeat_stats(self, elapsed):
        return f"eeg_samples={self._total_samples} blocks={len(self._blocks)} configured={self.config.sample_rate}Hz sdk_clock=unverified"

    def _build_output(self):
        out = super()._build_output()
        # A local row counter is not an amplifier sequence number: do not advertise
        # zero packet loss through the existing amplifier BlockContinuity checker.
        out["eeg_block_row_start"] = out.pop("eeg_block_start", np.zeros(0, dtype=np.int64))
        out.pop("eeg_start_amp_sample", None)
        out["eeg_timestamps_pc"] = (np.concatenate(self._packet_times)
                                    if self._packet_times else np.zeros(0, dtype=np.float64))
        if not self._blocks:
            out["eeg_data"] = np.zeros((0, 8), dtype=np.float32)
        out.update({k: np.asarray(v) for k, v in {
            "eeg_n_channels": 8, "eeg_n_eeg_channels": 8,
            "eeg_device_kind": "neurodance", "eeg_sdk_version": self._sdk_version,
            "eeg_serial_port": self._port, "eeg_device_mac": self._device_mac,
            "eeg_host_mac": self._host_mac,
            "eeg_timestamp_source": "sdk_unix_ms_unverified",
            "eeg_sync_verified": False, "eeg_data_unit": "SDK raw (unit unverified)",
            "eeg_protocol_errors": self._protocol_errors,
            "eeg_queue_overflows": self._queue_overflows,
            "eeg_stream_error": self._fatal_error,
            "eeg_startup_unsynced_packets": self._startup_unsynced_packets,
            "eeg_impedance_status": self._impedance_report.get("status", "not_run"),
            "eeg_impedance_threshold_kohm": self.config.impedance_threshold_kohm,
            "eeg_impedance_sdk_unit": self.config.impedance_sdk_unit,
        }.items()})
        out["eeg_impedance_kohm"] = self._impedance_kohm.copy()
        return out
