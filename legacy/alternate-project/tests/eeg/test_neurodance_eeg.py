"""No hardware required: packet contracts, commit, QC, and nested SDK process."""
import multiprocessing as mp
import pickle
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from embodied_brain_collect.recorders.eeg.neurodance_eeg_recorder import (
    NeuroDanceEegRecorder, normalize_mac, select_usb_port)
from embodied_brain_collect.recorders.eeg.eeg_recorder_config import NeuroDanceEegRecorderConfig
from embodied_brain_collect.checkers.eeg import NeuroDanceStatus, BlockContinuity
from embodied_brain_collect.checkers.base import CheckContext
from embodied_brain_collect.session.launcher import launch, run_qc
from embodied_brain_collect.session.recorder_presets import build_recorder


def recorder(**kwargs):
    return NeuroDanceEegRecorder(NeuroDanceEegRecorderConfig(**kwargs))


def packet(first=None, n=400, value=1):
    first = time.time() if first is None else first
    return (first + 0.2, time.monotonic(), first * 1000,
            np.full((8, n), value, dtype=np.float32))


def test_factory_and_spawn_pickle_without_sdk():
    rec = build_recorder("eeg", {"kind": "neurodance_eeg", "sample_rate": 2000}, "")
    restored = pickle.loads(pickle.dumps(rec))
    assert restored.config.sample_rate == 2000
    assert restored.requires_child_processes
    assert restored._device is None
    assert restored._packets is None


def test_port_selection_excludes_bluetooth_and_rejects_ambiguity():
    bt = SimpleNamespace(device="COM3", hwid="BTHENUM", vid=None)
    usb = SimpleNamespace(device="COM7", hwid="USB VID:PID=0000:0000", vid=0)
    assert select_usb_port("", [bt, usb]) == "COM7"
    for ports in ([bt], [usb, SimpleNamespace(device="/dev/ttyACM0", hwid="", vid=None)]):
        with pytest.raises(RuntimeError):
            select_usb_port("", ports)
    assert select_usb_port("/dev/serial/by-id/receiver", []) == "/dev/serial/by-id/receiver"


def test_mac_normalization_and_invalid_input():
    assert normalize_mac("3e:ee:72:19:5b:cf") == "3EEE72195BCF"
    with pytest.raises(ValueError):
        normalize_mac("anything")


def test_packet_conversion_metadata_and_no_fake_trigger_or_amp_index():
    rec = recorder()
    data = packet()
    data[3][1, :] = 2
    rec._accept_packet(data)
    out = rec._build_output()
    assert out["eeg_data"].shape == (400, 8)
    assert np.all(out["eeg_data"][:, 1] == 2)
    np.testing.assert_allclose(np.diff(out["eeg_timestamps_pc"]), 0.0005, atol=3e-7)
    assert out["eeg_n_eeg_channels"] == 8
    assert "eeg_block_start" not in out
    assert "eeg_start_amp_sample" not in out
    assert "eeg_fit_fitted" not in out
    assert not out["eeg_sync_verified"]
    assert out["eeg_packet_sdk_timestamp_ms"][0] == data[2]
    assert out["eeg_packet_arrival_unix_s"][0] == data[0]


def test_wrong_rate_and_invalid_time_are_rejected():
    rec = recorder()
    with pytest.raises(ValueError, match="no silent"):
        rec._accept_packet(packet(n=50))  # 250 Hz packet when 2000 Hz was requested
    with pytest.raises(ValueError, match="timestamp"):
        rec._accept_packet(packet(first=1))
    assert rec._total_samples == 0


def test_packet_gaps_and_clock_regression_not_rewritten():
    rec = recorder()
    start = time.time()
    for t in (start, start + 0.4, start + 0.1):
        rec._accept_packet(packet(first=t))
    times = rec._build_output()["eeg_timestamps_pc"]
    assert times[400] - times[399] > 0.2
    assert times[800] < times[799]


def test_commit_clears_preroll_and_trims_straddling_packet():
    rec = recorder()
    rec._packets = queue.Queue()
    rec._accept_packet(packet())
    rec._packets.put(packet())
    rec._discard_preface()
    assert rec._total_samples == 0
    assert rec._packets.empty()
    boundary = rec._not_before_pc
    rec._accept_packet(packet(first=boundary - 0.3))
    assert rec._total_samples == 0
    rec._accept_packet(packet(first=boundary - 0.1))
    out = rec._build_output()
    assert 199 <= out["eeg_n_samples"] <= 201
    assert out["eeg_timestamps_pc"][0] >= boundary
    assert out["eeg_packet_trimmed_prefix"][0] >= 199
    assert len(out["eeg_packet_sdk_timestamp_ms"]) == 1


def test_stall_and_overflow_stop_with_diagnostic():
    rec = recorder(stream_timeout=0.01)
    rec._packets = queue.Queue()
    rec._last_packet_at = time.monotonic() - 1
    with pytest.raises(RuntimeError, match="stalled"):
        rec._poll(time.time())
    rec._fatal_error = "callback queue overflow"
    with pytest.raises(RuntimeError, match="overflow"):
        rec._poll(time.time())


def test_qc_labels_unverified_clock_and_reports_errors(tmp_path):
    rec = recorder()
    rec._accept_packet(packet())
    rec._protocol_errors = 2
    ctx = CheckContext(stream="eeg", directory=tmp_path)
    ctx._arrays.update(rec._build_output())
    check = NeuroDanceStatus()
    assert check.applies(ctx)
    assert not BlockContinuity().applies(ctx)
    result = check.run(ctx)
    assert len(result.findings) == 2
    assert result.stats["eeg_protocol_errors"] == 2


def test_pair_uses_fresh_scan_bytes_and_ignores_early_empty_ack(monkeypatch):
    rec = recorder(port="FAKE", device_mac="3EEE72195BCF", pair_on_open=True, impedance_check=False)
    paired = []

    def scan():
        rec._control.put(("scan", (time.monotonic(), [])))
        rec._control.put(("scan", (time.monotonic() + 3.1, [{
            "mac": "3EEE72195BCF", "mac_bytes": [62, 238, 114, 25, 91, 207]}])))

    def enable():
        rec._packets.put(packet())
        rec._packets.put(packet())

    sdk = SimpleNamespace(start=lambda: None, close=lambda: None,
                          host_mac_info=lambda: rec._control.put(("host_mac", "AFE1C2414BCE")),
                          devices_scan=scan, device_pair=paired.append,
                          eeg_channel_config=lambda rate, imp: None,
                          eeg_channel_enable=enable, eeg_disable=lambda: None)
    monkeypatch.setattr(rec, "_create_device", lambda: sdk)
    with patch("embodied_brain_collect.recorders.eeg.neurodance_eeg_recorder.version", return_value="9.8"), \
            patch.object(rec, "_verify_device", side_effect=[TimeoutError(), None]):
        assert rec._open(), rec._open_error
    rec._close()
    assert paired == [[1, 62, 238, 114, 25, 91, 207, 1]]


def test_wrong_connected_mac_is_not_repaired():
    rec = recorder()
    rec._control = queue.Queue()
    rec._device = SimpleNamespace(device_mac_info=lambda: rec._control.put(("device_mac", "000000000001")))
    with pytest.raises(RuntimeError, match="not re-pairing"):
        rec._verify_device("3EEE72195BCF", 0.5)


def synthetic_sdk_worker(packets, stop, enabled, rate):
    """Real grandchild process: fails immediately if launcher uses daemon=True."""
    enabled.wait(5)
    start = time.time()
    seq = 0
    while not stop.wait(0.2):
        n = rate.value // 5
        t = start + seq * 0.2
        packets.put((time.time(), time.monotonic(), t * 1000,
                     np.full((8, n), seq, dtype=np.float32)))
        seq += 1


class SyntheticSdk:
    def __init__(self, owner):
        self.owner = owner
        ctx = mp.get_context("spawn")
        self.packets = ctx.Queue()
        self.stop = ctx.Event()
        self.enabled = ctx.Event()
        self.rate = ctx.Value("i", 0)
        self.process = ctx.Process(target=synthetic_sdk_worker,
                                   args=(self.packets, self.stop, self.enabled, self.rate))
        self.process.start()
        self.thread = threading.Thread(target=self.receive, daemon=True)

    def receive(self):
        while not self.stop.is_set():
            try:
                self.owner._packets.put(self.packets.get(timeout=0.05))
            except queue.Empty:
                pass

    def start(self):
        self.thread.start()

    def host_mac_info(self):
        self.owner._control.put(("host_mac", "AFE1C2414BCE"))

    def device_mac_info(self):
        self.owner._control.put(("device_mac", "3EEE72195BCF"))

    def eeg_channel_config(self, sample_rate, impedance):
        self.rate.value = sample_rate
        assert impedance is False

    def eeg_channel_enable(self):
        self.enabled.set()

    def eeg_disable(self):
        pass

    def close(self):
        self.stop.set()
        self.thread.join(2)
        self.process.join(3)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(2)
            raise RuntimeError("Synthetic SDK did not stop")
        self.packets.close()
        self.packets.join_thread()


class SyntheticRecorder(NeuroDanceEegRecorder):
    def _create_device(self):
        return SyntheticSdk(self)

    def _open(self):
        with patch("embodied_brain_collect.recorders.eeg.neurodance_eeg_recorder.version", return_value="9.8"):
            return super()._open()


def test_launcher_with_nested_sdk_process_and_saved_session(tmp_path):
    rec = SyntheticRecorder(NeuroDanceEegRecorderConfig(
        session_dir=str(tmp_path), port="FAKE", device_mac="3EEE72195BCF",
        sample_rate=2000, duration=1.5, impedance_check=False))
    result = launch({"eeg": rec}, duration=1.5)
    assert result == 0, repr(result)
    with np.load(tmp_path / "eeg" / "eeg.npz", allow_pickle=False) as out:
        assert out["eeg_data"].shape[1] == 8
        assert out["eeg_data"].shape[0] > 1000
        assert out["eeg_data"].shape[0] < 3600  # pre-roll excluded
        assert np.all(out["eeg_data"][:, 0] > 0)
        assert out["eeg_sample_rate"] == 2000
        assert out["eeg_n_samples"] == len(out["eeg_timestamps_pc"])
        assert out["eeg_stream_error"] == ""
    assert run_qc(tmp_path) == 0
    assert (tmp_path / "qc_report.json").exists()
    assert (tmp_path / "qc.html").exists()
    assert "SDK raw (unit unverified)" in (tmp_path / "qc.html").read_text(encoding="utf-8")
