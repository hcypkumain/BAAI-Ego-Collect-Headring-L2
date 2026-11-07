#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$BASE/.."
mkdir -p "$BASE/runtime" "$BASE/tests/results"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export PYTHONUNBUFFERED=1
unset ROS_MASTER_URI ROS_PACKAGE_PATH ROS_DISTRO ROS_VERSION HARDWARE_PYTHON_VERSION ROSLISP_PACKAGE_DIRECTORIES
export PYTHONPATH="$BASE/src/orangepi_collect/common:$BASE/../src"
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENCV_FFMPEG_CAPTURE_OPTIONS='threads;1'
HARDWARE_PYTHON="$BASE/.venvs/hardware/bin/python"
FRAMEWORK_PYTHON="$BASE/.venvs/framework/bin/python"
ensure_runtime() {
    if [[ ! -x "$HARDWARE_PYTHON" || ! -x "$FRAMEWORK_PYTHON" ]]; then
        echo 'Run bash orangepi/scripts/setup_envs.sh first.' >&2
        return 1
    fi
}
pid_file="$BASE/runtime/launcher.pid"
is_running() {
    [[ -f "$pid_file" ]] || return 1
    task_pid="$(cat "$pid_file")"
    [[ "$task_pid" =~ ^[0-9]+$ ]] || return 1
    [[ -r "/proc/$task_pid/cmdline" ]] || return 1
    tr '\0' ' ' < "/proc/$task_pid/cmdline" | grep -Fq "$BASE/src/orangepi_collect/control/launcher.py"
}
startup_error() {
    result=$?
    trap - ERR
    echo 'Startup failed; see journal or runtime/launcher.log' >&2
    # Only a pre-rendered Aliyun clip; no local synthesis and no recursive API call.
    [[ ! -f "$BASE/assets/cloud_voice/fatal.wav" ]] || python3 "$BASE/src/orangepi_collect/audio/startup_fatal.py" --base "$BASE" --timeout 12 2>/dev/null || true
    exit "$result"
}
trap startup_error ERR
ensure_hardware() {
    # camd owns the headband cameras; never restart the retired ROS bridge.
    if systemctl is-active --quiet egorecord.service; then
        echo 'EgoRecord is running; release it before starting the collector.' >&2
        return 1
    fi
    if ! systemctl is-active --quiet camd.service || [[ ! -S /run/camd.sock ]]; then
        echo 'camd is not ready; check camd.service and /run/camd.sock.' >&2
        return 1
    fi
}
action="${1:-start}"
if [[ $# -gt 0 ]]; then shift; fi
case "$action" in
start)
    if is_running; then
        if [[ $# -gt 0 ]]; then echo 'Already running; stop it before changing the image.' >&2; exit 1; fi
        echo "Already running: $task_pid"; exit 0
    fi
    ensure_runtime
    ensure_hardware
    nohup "$HARDWARE_PYTHON" -u "$BASE/src/orangepi_collect/control/launcher.py" "$@" >> "$BASE/runtime/launcher.log" 2>&1 < /dev/null &
    task_pid=$!
    echo "$task_pid" > "$pid_file"
    ready=0
    for attempt in $(seq 1 300); do
        if ! kill -0 "$task_pid" 2>/dev/null; then tail -20 "$BASE/runtime/launcher.log"; exit 1; fi
        if /usr/bin/python3 -c 'import json,sys; s=json.load(open(sys.argv[1]));sys.exit(not(s.get("pid")==int(sys.argv[2]) and s.get("state") in ("idle","waiting_keyboard","device_confirm","scanning")))' "$BASE/runtime/controller.json" "$task_pid" 2>/dev/null; then ready=1; break; fi
        sleep .2
    done
    if [[ "$ready" != 1 ]]; then
        kill -TERM "$task_pid" 2>/dev/null || true
        echo 'Startup did not finish successfully; see runtime/launcher.log' >&2
        exit 1
    fi
    echo "Started: $task_pid"
    echo "A: reference / start / ask to stop / confirm and next. B: repeat / retry. Continuous collection."
    ;;
service)
    ensure_runtime
    export ORANGEPI_EARLY_WEB=1
    exec "$HARDWARE_PYTHON" -u "$BASE/src/orangepi_collect/control/launcher.py"
    ;;
foreground)
    ensure_runtime
    ensure_hardware
    exec "$HARDWARE_PYTHON" -u "$BASE/src/orangepi_collect/control/launcher.py" "$@"
    ;;
smoke)
    ensure_runtime
    ensure_hardware
    duration="${1:-15}"
    if [[ $# -gt 0 ]]; then shift; fi
    exec "$HARDWARE_PYTHON" -u "$BASE/src/orangepi_collect/control/launcher.py" --smoke "$duration" "$@"
    ;;
list-images)
    ensure_runtime
    exec "$HARDWARE_PYTHON" "$BASE/src/orangepi_collect/control/launcher.py" --list-images
    ;;
stop)
    if is_running; then
        kill -TERM "$task_pid"
        echo "Stop requested; current recording will be finalized."
    else
        echo "Not running."
    fi
    ;;
status)
    if is_running; then echo "Running: $task_pid"; else echo "Not running."; fi
    [[ ! -f "$BASE/runtime/controller.json" ]] || cat "$BASE/runtime/controller.json"
    ;;
*)
    echo "Usage: $0 {start|foreground|service|stop|status|list-images|smoke [seconds]} [--image filename]" >&2
    exit 2
    ;;
esac
