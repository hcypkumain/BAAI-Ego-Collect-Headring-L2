#!/usr/bin/env bash
set -euo pipefail
backup="${1:-}"
if [[ ! "$backup" =~ ^/var/backups/headring-usb-export/[0-9TZ-]+$ ]] || ! sudo test -d "$backup"; then
  echo '请提供安装时输出的备份目录。' >&2; exit 1
fi
for target in /usr/local/lib/collector-usb-task-import/mount-readonly /usr/local/lib/collector-usb-task-import/mount-export /etc/sudoers.d/collector-usb-export; do
  name="$(basename "$target")"
  if sudo test -e "$backup/$name"; then sudo cp -a "$backup/$name" "$target"
  elif sudo test -e "$backup/$name.absent"; then sudo rm -f "$target"
  else echo "备份不完整：$name" >&2; exit 1
  fi
done
sync
printf '已恢复U盘助手；采集服务代码需单独回滚。\n'
