"""Startup impedance gate with simulated SDK; no hardware claims."""
import json
import queue
from types import SimpleNamespace

import numpy as np
import pytest

from embodied_brain_collect.recorders.eeg.eeg_recorder_config import NeuroDanceEegRecorderConfig
from embodied_brain_collect.recorders.eeg.neurodance_eeg_recorder import (
    NeuroDanceEegRecorder, ImpedanceCheckError, impedance_in_kohm,
    require_acceptable_impedance,
)


def test_units_and_strict_threshold():
    values = np.full(8, 50000.0)
    converted = impedance_in_kohm(values, "ohm")
    require_acceptable_impedance(converted, 50)
    np.testing.assert_equal(impedance_in_kohm(converted, "kohm"), converted)
    converted[2] = 50.001
    with pytest.raises(ImpedanceCheckError, match="CH3"):
        require_acceptable_impedance(converted, 50)
    require_acceptable_impedance(converted, 60)


@pytest.mark.parametrize("values", [[1]*7, [1]*9, [float("nan")]*8, [float("inf")]*8, [-1]*8])
def test_invalid_measurements(values):
    with pytest.raises(ImpedanceCheckError):
        impedance_in_kohm(values, "kohm")


def test_unknown_unit_prevents_device_creation(monkeypatch, tmp_path):
    rec = NeuroDanceEegRecorder(NeuroDanceEegRecorderConfig(session_dir=str(tmp_path)))
    monkeypatch.setattr(rec, "_create_device", lambda: pytest.fail("must not connect"))
    assert not rec._open()
    assert "impedance_sdk_unit" in rec._open_error


def test_real_sdk_callback_cache_is_separate_from_recording(monkeypatch):
    sdk = pytest.importorskip("neuro_dance")
    from neuro_dance.linkedlist import NdLinkedlist

    def offline_init(device, **kwargs):
        device.eeg_datas = NdLinkedlist(maxlen=20)

    monkeypatch.setattr(sdk.NdDevice, "__init__", offline_init)
    rec = NeuroDanceEegRecorder(NeuroDanceEegRecorderConfig())
    rec._packets = queue.Queue()
    device = rec._create_device()
    data = {"timestamp": 1700000000000, "data": np.ones((8, 400))}
    rec._delivery_mode = "impedance"
    for _ in range(5):
        device.eeg_received(data)
    assert rec._impedance_packets == 5
    assert len(device.eeg_datas.snapshot()) == 5
    assert rec._packets.empty()
    rec._delivery_mode = "discard"
    device.eeg_received(data)
    assert rec._packets.empty()
    rec._delivery_mode = "record"
    device.eeg_received(data)
    assert rec._packets.qsize() == 1
    assert len(device.eeg_datas.snapshot()) == 5


@pytest.mark.parametrize("outcome", ["pass", "high", "timeout", "invalid_current", "invalid_values"])
def test_measurement_cleanup_and_report(outcome, tmp_path):
    rec = NeuroDanceEegRecorder(NeuroDanceEegRecorderConfig(
        session_dir=str(tmp_path), impedance_sdk_unit="kohm", impedance_timeout_s=0.06))
    rec._packets = queue.Queue()
    calls = []
    values = np.full(8, 50.0)
    if outcome == "high":
        values[4] = 51
    if outcome == "invalid_values":
        values[0] = np.nan

    def enable():
        assert rec._delivery_mode == "impedance"
        rec._impedance_packets = 5

    rec._device = SimpleNamespace(
        eeg_datas=SimpleNamespace(removeAll=lambda: calls.append("clear")),
        eeg_channel_config=lambda rate, imp: calls.append((rate, imp)),
        eeg_channel_enable=enable, eeg_disable=lambda: calls.append("disable"),
        get_ac_current=lambda: None if outcome == "timeout" else (0 if outcome == "invalid_current" else 1),
        read_impedance=lambda: values,
    )
    if outcome == "pass":
        rec._run_impedance_check()
        rec._reset_stream_state()
        np.testing.assert_equal(rec._build_output()["eeg_impedance_kohm"], values)
    else:
        with pytest.raises(ImpedanceCheckError):
            rec._run_impedance_check()
    report = json.loads((tmp_path / rec.output_dir / "impedance_check.json").read_text())
    assert report["status"] == ("passed" if outcome == "pass" else "failed")
    if outcome == "high":
        assert "CH5" in report["error"]
    assert calls == ["clear", (2000, True), "disable", (2000, False), "clear"]
    assert rec._delivery_mode == "discard"
    assert not rec._stream_enabled
    assert rec._packets.empty()
    assert rec._total_samples == 0
