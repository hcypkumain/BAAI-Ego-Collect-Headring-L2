#!/usr/bin/env python3
"""Bounded on-SD system evidence that survives loss of the volatile /var/log."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time


def relevant(event):
    message = str(event.get('MESSAGE', ''))
    if '0 kbit/s downlink 0 kbit/s uplink' in message:
        return False
    if event.get('_SYSTEMD_UNIT') == 'camd.service':
        # Keep connection accounting and faults across reboot. Frame chatter is
        # excluded; the existing rotated SD log still bounds total storage.
        return bool(re.search(r'客户端|\b(?:ERROR|WARN|FATAL)\b|失败|超时|拒绝|上限|退出|启动', message, re.I))
    if event.get('_TRANSPORT') == 'kernel':
        return bool(re.search(r'USB disconnect|new .*USB device|hub nested|panic|Oops|Call trace|watchdog|reboot|reset|shutdown|power.?off|rk806|pmic|oom|Out of memory|allocation fail|thermal|voltage|SError|mali|gpu|encoder|decoder|timeout', message, re.I))
    if event.get('_SYSTEMD_UNIT') == 'systemd-logind.service':
        return bool(re.search(r'power key|reboot|power.?off|shutdown', message, re.I))
    if event.get('_PID') == '1' or event.get('SYSLOG_IDENTIFIER') in ('systemd', 'systemd-shutdown'):
        return bool(re.search(r'Shutting down|Rebooting|Powering off|Reached target.*(?:Shutdown|Reboot|Power-Off)|Watchdog', message, re.I))
    return event.get('_SYSTEMD_UNIT') == 'embodied-pi-collect.service' and bool(re.search(r'error|fail|Traceback', message, re.I))


def snapshot(base):
    value = {'event': 'health', 'time': time.time(), 'uptime': float(Path('/proc/uptime').read_text().split()[0]), 'load': os.getloadavg()}
    value['memory'] = {line.split(':')[0]: line.split(':')[1].strip() for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemAvailable:', 'CmaFree:', 'SwapFree:'))}
    value['temperature_mC'] = {}
    for p in Path('/sys/class/thermal').glob('thermal_zone*/temp'):
        try: value['temperature_mC'][p.parent.name] = int(p.read_text())
        except (OSError, ValueError): pass
    try:
        c = json.loads((base / 'runtime/controller.json').read_text())
        value['controller'] = {k: c.get(k) for k in ('pid', 'state', 'time', 'session')}
        session = c.get('session')
        if session:
            w = json.loads((Path(session) / 'status.json').read_text())
            value['worker'] = {k: w.get(k) for k in ('pid', 'state', 'time', 'recording_active', 'samples_seen', 'warning_count', 'last_warning', 'errors')}
    except (OSError, ValueError): pass
    return value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mount', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--base', required=True)
    args = ap.parse_args()
    mount, root, base = Path(args.mount), Path(args.output), Path(args.base)
    if not os.path.ismount(str(mount)):
        raise RuntimeError('Diagnostic SD card is not mounted')
    root.resolve().relative_to(mount.resolve())
    root.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(str(root / 'system.jsonl'), maxBytes=3 * 1024 * 1024, backupCount=2, encoding='utf-8')
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    def write(value):
        value.setdefault('boot_id', boot_id)
        handler.emit(logging.makeLogRecord({'msg': json.dumps(value, ensure_ascii=False)}))
    write({'event': 'observer_start', 'time': time.time(), 'boot_id': boot_id})
    handler.flush(); os.fsync(handler.stream.fileno())
    fd = os.open(str(root), os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
    # Read-only journal access; never opens recording devices or controls services.
    # Include this boot's backlog: PMIC reset-source messages precede the observer.
    journal = subprocess.Popen(['journalctl', '-b', '-f', '--since=-5min', '-g', '(?i)USB disconnect|new .*USB device|hub nested|panic|Oops|Call trace|watchdog|reboot|reset|shutdown|power.?off|rk806|pmic|oom|Out of memory|allocation fail|thermal|voltage|SError|mali|gpu|encoder|decoder|timeout|客户端|ERROR|WARN|FATAL|失败|超时|拒绝|上限|退出|启动|power key|Shutting down|Rebooting|Powering off|Reached target|Traceback', '-o', 'json', '--no-pager'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    os.set_blocking(journal.stdout.fileno(), False)
    sel = selectors.DefaultSelector(); sel.register(journal.stdout, selectors.EVENT_READ)
    stopping = []
    signal.signal(signal.SIGTERM, lambda *a: stopping.append(True))
    signal.signal(signal.SIGINT, lambda *a: stopping.append(True))
    pending = b''; next_health = 0
    try:
        while not stopping:
            if journal.poll() is not None: raise RuntimeError('Journal reader exited')
            for key, _ in sel.select(.5):
                pending += os.read(key.fd, 65536)
                lines = pending.split(b'\n'); pending = lines.pop()
                for line in lines:
                    try: event = json.loads(line)
                    except ValueError: continue
                    if relevant(event):
                        write({'event': 'system_event', 'time': time.time(), 'source_us': event.get('__REALTIME_TIMESTAMP'), 'source_boot_id': event.get('_BOOT_ID'), 'transport': event.get('_TRANSPORT'), 'unit': event.get('_SYSTEMD_UNIT'), 'message': str(event.get('MESSAGE', ''))[:4096]})
                if len(pending) > 1048576: pending = b''
            if time.monotonic() >= next_health:
                if not os.path.ismount(str(mount)): raise RuntimeError('Diagnostic SD card disconnected')
                write(snapshot(base)); handler.flush(); os.fsync(handler.stream.fileno())
                next_health = time.monotonic() + 2
    finally:
        journal.terminate(); journal.wait(timeout=5); sel.close()
        write({'event': 'observer_stop', 'time': time.time()}); handler.flush(); os.fsync(handler.stream.fileno()); handler.close()


if __name__ == '__main__':
    main()
