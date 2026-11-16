#!/usr/bin/env bash
set -euo pipefail
base="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
user="$(id -un)"
if [[ $(id -u) == 0 || ! "$user" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*\$?$ ]]; then
  echo '请以采集服务用户运行，用户名必须是常规系统账号。' >&2; exit 1
fi
stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
backup="/var/backups/headring-usb-export/$stamp"
sudo install -d -o root -g root -m 700 "$backup"
for target in /usr/local/lib/collector-usb-task-import/mount-readonly /usr/local/lib/collector-usb-task-import/mount-export /etc/sudoers.d/collector-usb-export; do
  name="$(basename "$target")"
  if sudo test -e "$target"; then sudo cp -a "$target" "$backup/$name"; else sudo touch "$backup/$name.absent"; fi
done
mkdir -p "$base/runtime"
rule="$base/runtime/collector-usb-export.sudoers"
printf '%s ALL=(root) NOPASSWD: /usr/local/lib/collector-usb-task-import/mount-readonly "", /usr/local/lib/collector-usb-task-import/mount-export ""\n' "$user" > "$rule"
sudo visudo -cf "$rule" >/dev/null
sudo install -D -o root -g root -m 755 "$base/scripts/usb_tasks_mount_readonly.py" /usr/local/lib/collector-usb-task-import/mount-readonly
sudo install -D -o root -g root -m 755 "$base/scripts/usb_export_mount.py" /usr/local/lib/collector-usb-task-import/mount-export
sudo install -o root -g root -m 440 "$rule" /etc/sudoers.d/collector-usb-export
sync
printf '已安装U盘助手；回滚备份：%s\n' "$backup"
