#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export UV_CACHE_DIR="$BASE/.uv-cache"
command -v uv >/dev/null || { echo 'Install uv first; see README.' >&2; exit 1; }
mkdir -p "$BASE/.venvs"
# GI/GStreamer use distro ABI-bound packages; keep the system Python runtime.
if [[ ! -x "$BASE/.venvs/hardware/bin/python" ]]; then
    uv venv --system-site-packages --python /usr/bin/python3 "$BASE/.venvs/hardware"
fi
grep -q 'include-system-site-packages = true' "$BASE/.venvs/hardware/pyvenv.cfg"
(cd "$BASE/configs/environments/hardware"; UV_PROJECT_ENVIRONMENT="$BASE/.venvs/hardware" uv sync --frozen)
(cd "$BASE/configs/environments/framework"; UV_PROJECT_ENVIRONMENT="$BASE/.venvs/framework" uv sync --frozen --python 3.11)
set +u
source "$BASE/scripts/env.sh"
set -u
"$BASE/.venvs/hardware/bin/python" -c 'import gi,numpy,yaml; gi.require_version("Gst","1.0"); from gi.repository import Gst; print("Hardware runtime OK")'
"$BASE/.venvs/framework/bin/python" -c 'import numpy,cv2,yaml,loguru,dashscope; from dashscope.audio.qwen_tts_realtime import QwenTtsRealtime; import embodied_brain_collect.checkers; print("Framework runtime OK")'

# EEG edition retains the isolated CPython 3.11 OmniBCI owner.
bash "$BASE/scripts/setup_eeg.sh"
