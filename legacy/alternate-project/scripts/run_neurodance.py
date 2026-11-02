"""Record only NeuroDance EEG through collect's normal launcher and QC.

Examples: python scripts/run_neurodance.py --port COM7 --duration 10
          python scripts/run_neurodance.py --port /dev/serial/by-id/... --duration 60
"""
import argparse
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from embodied_brain_collect.config.load import load_recorders
from embodied_brain_collect.session.recorder_presets import build_recorder
from embodied_brain_collect.session.launcher import launch, run_qc, _write_session_meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Override eeg.port; Linux stable by-id paths supported")
    parser.add_argument("--device-mac", help="Override expected acquisition-device MAC")
    parser.add_argument("--pair", action="store_true", help="Pair specified MAC if no device currently responds")
    parser.add_argument("--impedance-threshold-kohm", type=float, help="Override startup threshold in kOhm (strictly greater fails)")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--session-dir", type=Path, default=Path("data/neurodance"))
    args = parser.parse_args()
    import math
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error("duration must be finite and positive")
    cfg = dict(load_recorders()["eeg"])
    if cfg.get("kind") != "neurodance_eeg":
        parser.error("configs/recorders.yaml eeg.kind must be neurodance_eeg")
    cfg["sample_rate"] = 2000
    if args.port is not None:
        cfg["port"] = args.port
    if args.device_mac is not None:
        cfg["device_mac"] = args.device_mac
    if args.pair:
        cfg["pair_on_open"] = True
    if args.impedance_threshold_kohm is not None:
        cfg["impedance_threshold_kohm"] = args.impedance_threshold_kohm
    run_dir = args.session_dir.resolve() / datetime.now().strftime("%Y-%m-%d-%H-%M-%S-%f")
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_session_meta(run_dir, task_id=None)
    print(f"Session: {run_dir}; NeuroDance EEG 2000 Hz")
    rec = build_recorder("eeg", cfg, str(run_dir), duration=args.duration)
    result = launch({"eeg": rec}, duration=args.duration)
    run_qc(run_dir)
    return int(result)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
