#!/usr/bin/env bash
# Source, image library, current ledgers and Aliyun-generated safety audio only.
set -euo pipefail
BASE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE/.."
if [[ $# -gt 1 ]]; then echo 'Usage: package_delivery.sh [output-directory]' >&2; exit 2; fi
OUTPUT_DIR="${1:-$("$BASE/.venvs/ros/bin/python" "$BASE/src/orangepi_collect/storage/storage_paths.py" export)}"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
python3 - "$OUTPUT_DIR" <<'PY'
from pathlib import Path
import shutil,sys
roots=['src','configs','scripts','tests','orangepi']
exclude={'.venvs','private','runtime','__pycache__','.uv-cache','results'}
def size(p):
    if p.is_symlink():return 0
    if p.is_file():return p.stat().st_size
    return sum(size(x) for x in p.iterdir() if x.name not in exclude)
required=sum(size(Path(p)) for p in roots)+1024**3
if shutil.disk_usage(sys.argv[1]).free<required:
    raise SystemExit('Output disk lacks space for archive plus 1 GiB reserve. Choose SD/USB destination.')
PY
OUTPUT="$OUTPUT_DIR/orangepi-source-$(date +%Y%m%d-%H%M%S).tar.gz"
trap 'echo "Packaging did not finish; .partial is not a deliverable." >&2' ERR
tar --exclude='*/__pycache__' --exclude='*.pyc' --exclude='orangepi/private' \
    --exclude='orangepi/runtime' --exclude='orangepi/.venvs' --exclude='orangepi/.uv-cache' \
    --exclude='orangepi/tests/results' -I "gzip -1" -cf "$OUTPUT.partial" \
    src configs scripts tests orangepi/README.md orangepi/src orangepi/configs \
    orangepi/scripts orangepi/tests orangepi/docs orangepi/assets \
    pyproject.toml requirements.txt README.md CHANGELOG.md .gitignore 采集员操作说明.md
mv -- "$OUTPUT.partial" "$OUTPUT"
(cd "$OUTPUT_DIR"; sha256sum "$(basename "$OUTPUT")" > "$(basename "$OUTPUT").sha256")
echo "$OUTPUT"
