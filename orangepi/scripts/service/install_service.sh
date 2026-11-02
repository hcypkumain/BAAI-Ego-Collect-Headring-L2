#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ $(id -u) == 0 ]]; then echo 'Run as the collection user, without sudo; installer will request sudo.' >&2; exit 1; fi
export ORANGEPI_INSTALL_ROOT="$BASE" ORANGEPI_INSTALL_USER="$(id -un)" ORANGEPI_INSTALL_GROUP="$(id -gn)"
python3 - <<'PY'
import os
from pathlib import Path
b=Path(os.environ['ORANGEPI_INSTALL_ROOT'])
s=(b/'configs/systemd/embodied-pi-collect.service').read_text()
for key,value in [('ROOT',str(b)),('PROJECT',str(b.parent)),('USER',os.environ['ORANGEPI_INSTALL_USER']),('GROUP',os.environ['ORANGEPI_INSTALL_GROUP'])]:
    if any(c in value for c in '\n\r"%'):raise ValueError('Unsupported character in systemd installation path/user')
    s=s.replace('@'+key+'@',value)
(b/'runtime').mkdir(exist_ok=True)
(b/'runtime/embodied-pi-collect.service').write_text(s)
PY
sudo install -m 644 "$BASE/runtime/embodied-pi-collect.service" /etc/systemd/system/embodied-pi-collect.service
sudo systemctl daemon-reload
sudo systemctl enable --now embodied-pi-collect.service
