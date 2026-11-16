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

# Install export helper and preserve previous helpers for rollback.
"$BASE/scripts/service/install_usb_export.sh"

# Narrow root helpers and sudoers rules. Without these the collector starts but
# USB task-import mounting and the console shutdown action silently fail.
sudo install -o root -g root -m 755 "$BASE/scripts/usb_tasks_mount_readonly.py" /usr/local/lib/collector-usb-task-import/mount-readonly
sudo install -o root -g root -m 755 "$BASE/scripts/headring_power_shutdown.sh" /usr/local/lib/headring-power/shutdown
for rule in collector-usb-task-import headring-power-shutdown; do
  sudo visudo -cf "$BASE/../deployment/sudoers/$rule" >/dev/null
  sudo install -o root -g root -m 440 "$BASE/../deployment/sudoers/$rule" "/etc/sudoers.d/$rule"
done
for unit in collector-usb-task-mount.service collector-usb-task-mount.timer; do
  sudo install -o root -g root -m 644 "$BASE/../deployment/systemd/$unit" "/etc/systemd/system/$unit"
done
sudo systemctl daemon-reload
sudo systemctl enable --now embodied-pi-collect.service collector-usb-task-mount.timer
# The root filesystem commits lazily; flush now so a power cut right after
# installation cannot erase these system files.
sync
