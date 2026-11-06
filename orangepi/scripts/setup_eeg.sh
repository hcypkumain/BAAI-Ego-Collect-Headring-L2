#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$(uname -m)" == aarch64 ]] || { echo 'EEG profile requires Linux aarch64'; exit 1; }
UV="${UV_BIN:-$HOME/.local/bin/uv}"
[[ -x "$UV" ]] || UV="$(command -v uv)"
[[ -x "$BASE/.venvs/eeg/bin/python" ]] || "$UV" venv --python "$BASE/.venvs/framework/bin/python" "$BASE/.venvs/eeg"
# Both processes use CPython 3.11. Share the already verified framework NumPy
# and SciPy packages, while keeping OmniBCI isolated in the EEG environment.
"$BASE/.venvs/framework/bin/python" - "$BASE/.venvs/eeg/bin/python" <<'PY'
import os,pathlib,subprocess,sys,sysconfig
import numpy,scipy
assert numpy.__version__=='2.2.6' and scipy.__version__=='1.15.3','Verify framework scientific dependencies before EEG setup'
shared=sysconfig.get_paths()['purelib']
target=pathlib.Path(subprocess.check_output([sys.argv[1],'-c','import sysconfig; print(sysconfig.get_paths()["purelib"])'],text=True).strip())
with (target/'shared-framework-libraries.pth').open('w') as handle:
    handle.write(shared+'\n');handle.flush();os.fsync(handle.fileno())
PY
if [[ -n "${1:-}" ]]; then
    EEG_WHEEL="$1/omnibci-0.1.2-cp311-abi3-manylinux_2_28_aarch64.whl"
    [[ "$(sha256sum "$EEG_WHEEL" | cut -d' ' -f1)" == b8346b25cf51e1abf4d153faa334b4d4a7b5f420cc981deaa1c6febcf2a14b12 ]]
    "$UV" pip install --offline --no-deps --python "$BASE/.venvs/eeg/bin/python" "$EEG_WHEEL"
else
    "$UV" pip install --no-deps --python "$BASE/.venvs/eeg/bin/python" 'https://github.com/Omni-Intel/omnibci-python/releases/download/v0.1.2/omnibci-0.1.2-cp311-abi3-manylinux_2_28_aarch64.whl#sha256=b8346b25cf51e1abf4d153faa334b4d4a7b5f420cc981deaa1c6febcf2a14b12'
fi
"$BASE/.venvs/eeg/bin/python" -c 'import omnibci,numpy,scipy; print("EEG runtime OK",numpy.__version__,scipy.__version__)'
sync
