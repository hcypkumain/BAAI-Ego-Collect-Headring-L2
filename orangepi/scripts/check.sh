#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
set +u
source "$BASE/scripts/env.sh"
set -u
mkdir -p "$BASE/runtime"
for test in test_daily_collection.py test_control.py test_v4.py test_delivery.py test_cloud_only.py test_storage.py test_wrist.py test_discovery.py test_hotplug.py test_boot_calibration.py test_emg_calibration.py test_emg_manual_control.py test_headband_calibration.py test_headband_integration.py test_dashboard.py test_prompt_flow.py test_recording_review.py test_preview_nonblocking.py test_preview_lease.py test_board_imu_writer.py test_receive_evidence.py test_wrist_camera_settings.py test_wrist_camera_dashboard.py; do
    "$BASE/.venvs/hardware/bin/python" -m unittest discover -s "$BASE/tests" -p "$test"
done
"$BASE/.venvs/framework/bin/python" -m unittest discover -s "$BASE/tests" -p test_framework.py
"$BASE/.venvs/framework/bin/python" -m unittest discover -s "$BASE/tests" -p test_wrist_format.py
"$BASE/.venvs/framework/bin/python" -m unittest discover -s "$BASE/tests" -p test_qc_sampling.py
