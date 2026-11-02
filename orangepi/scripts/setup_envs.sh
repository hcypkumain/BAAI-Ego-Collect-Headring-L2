#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export UV_CACHE_DIR="$BASE/.uv-cache"
command -v uv >/dev/null || { echo 'Install uv first; see README.' >&2; exit 1; }
mkdir -p "$BASE/.venvs"
# ROS and GI are distro ABI-bound. Do not replace this with CPython 3.11.
if [[ ! -x "$BASE/.venvs/ros/bin/python" ]]; then
    uv venv --system-site-packages --python /usr/bin/python3 "$BASE/.venvs/ros"
fi
grep -q 'include-system-site-packages = true' "$BASE/.venvs/ros/pyvenv.cfg"
(cd "$BASE/configs/environments/ros"; UV_PROJECT_ENVIRONMENT="$BASE/.venvs/ros" uv sync --frozen)
(cd "$BASE/configs/environments/framework"; UV_PROJECT_ENVIRONMENT="$BASE/.venvs/framework" uv sync --frozen --python 3.11)
set +u
source "$BASE/scripts/env.sh"
set -u
"$BASE/.venvs/ros/bin/python" -c 'import gi,numpy,yaml,rospy; gi.require_version("Gst","1.0"); from gi.repository import Gst; print("ROS runtime OK")'
"$BASE/.venvs/framework/bin/python" -c 'import numpy,cv2,yaml,loguru,dashscope; from dashscope.audio.qwen_tts_realtime import QwenTtsRealtime; import embodied_brain_collect.checkers; print("Framework runtime OK")'
