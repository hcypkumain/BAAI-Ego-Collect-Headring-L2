#!/usr/bin/env python3
"""Authorized physical Android USB peers; no Wi-Fi ADB or recorder controls."""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import shutil
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path('/home/orangepi/maintenance/usb-phone-adb')
CONFIG = ROOT / 'phone-binding.json'
STATE = ROOT / 'link-state.json'
BASE = Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi')
_BUNDLED_ADB = ROOT / 'root/usr/bin/adb'
ADB = _BUNDLED_ADB if _BUNDLED_ADB.is_file() else Path(shutil.which('adb') or '/usr/bin/adb')
PORT = 5038
URL = 'http://127.0.0.1:8765/phone'
VERSION = 'usb-auto-phone-20261107-v2'
SERIAL = re.compile(r'[A-Za-z0-9._:-]{1,128}')

def atomic(path, value):
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(name, str(path))
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass

def pi_id():
    path = Path('/opt/egorecord/DEVICE_ID')
    return path.read_text().strip() if path.exists() else socket.gethostname()

def adb(args, timeout=6):
    env = dict(os.environ, LD_LIBRARY_PATH=str(ROOT / 'root/usr/lib/aarch64-linux-gnu/android') + ':' + str(ROOT / 'root/usr/lib/aarch64-linux-gnu'), ADB_VENDOR_KEYS=str(ROOT / 'keys/adbkey'))
    result = subprocess.run([str(ADB), '-P', str(PORT)] + list(args), capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode:
        # A browser-launch command may contain a capability fragment. Never log it.
        raise RuntimeError('ADB command failed (exit %s)' % result.returncode)
    return result.stdout.strip()

def devices():
    result = []
    for line in adb(['devices', '-l']).splitlines():
        parts = line.split()
        if len(parts) < 2 or not SERIAL.fullmatch(parts[0]): continue
        usb = next((part[4:] for part in parts[2:] if part.startswith('usb:')), None)
        if usb and re.fullmatch(r'[0-9]+-[0-9.]+', usb):
            result.append(dict(serial=parts[0], state=parts[1], usb=usb))
    return result

def choose(rows):
    # A saved binding remains a preference, not a prerequisite for a new phone.
    if CONFIG.exists():
        try:
            saved = json.loads(CONFIG.read_text())
            if saved.get('pi_id') == pi_id():
                bound = [row for row in rows if row['serial'] == saved.get('phone_serial')]
                if len(bound) == 1: return bound[0], None
        except (OSError, ValueError): pass
    if len(rows) == 1: return rows[0], None
    if not rows: return None, 'phone_missing'
    return None, 'multiple_phones'

def data_role_state():
    try:
        value = json.loads(Path('/run/headring-usb-data-host.json').read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError): return {}

def once():
    row, error = choose(devices())
    result = dict(version=VERSION, pi_id=pi_id(), url=URL, transport='USB_ADB', connected=False, binding_required=False)
    if not row:
        role = data_role_state()
        if error == 'phone_missing' and role.get('state') not in (None, 'host'):
            return dict(result, state='usb_data_role_pending', data_role_state=role.get('state'),
                        message=role.get('message', '等待 Pi 完成 USB 数据主机角色切换')), None
        message = '等待手机接入USB数据口；开启USB调试并允许此Pi' if error == 'phone_missing' else '发现多部USB手机，请只保留一部；不会任意选择手机'
        return dict(result, state=error, message=message), None
    result.update(phone_serial_suffix=row['serial'][-4:], usb_path=row['usb'])
    if row['state'] == 'unauthorized':
        return dict(result, state='authorization_required', message='请解锁手机，点允许USB调试，并勾选始终允许此计算机'), None
    if row['state'] != 'device':
        return dict(result, state='transport_offline', message='USB手机尚未就绪，等待恢复'), None
    maps = adb(['-s', row['serial'], 'reverse', '--list'])
    if not any(line.split()[-2:] == ['tcp:8765', 'tcp:8765'] for line in maps.splitlines()):
        adb(['-s', row['serial'], 'reverse', 'tcp:8765', 'tcp:8765'])
    return dict(result, state='connected', connected=True, message='USB采集网页通道正常，无需手动绑定手机'), row

def browser_ready():
    token = (BASE / 'runtime/dashboard_token').read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{24,511}', token): return None
    req = urllib.request.Request('http://127.0.0.1:8765/api/display-state', headers={'X-Access-Token': token})
    with urllib.request.urlopen(req, timeout=2) as reply: value = json.load(reply)
    privacy = value.get('privacy') or {}
    if (value.get('state') not in ('device_confirm', 'idle', 'failed_wait', 'exhausted', 'starting', 'scanning')
            or value.get('busy') or (value.get('worker') or {}).get('recording_active')
            or privacy.get('required') is True): return None
    return token

def launch_browser(row):
    token = browser_ready()
    if not token: return False
    # The URL fragment is consumed and removed by the existing phone page.
    # Quote the URL again because Android's adb shell invokes a remote shell.
    import shlex
    adb(['-s', row['serial'], 'shell', 'am', 'start', '-a', 'android.intent.action.VIEW', '-d', shlex.quote(URL + '#' + token)], timeout=8)
    return True

def connection_key(row):
    try: number = (Path('/sys/bus/usb/devices') / row['usb'] / 'devnum').read_text().strip()
    except OSError: number = ''
    return row['serial'], row['usb'], number

def monitor():
    previous = None
    opened = None
    last_attempt = -1e9
    while True:
        try:
            state, row = once()
            if row is None:
                opened = None
                last_attempt = -1e9
            else:
                key = connection_key(row)
                if key != opened and time.monotonic() - last_attempt >= 10:
                    last_attempt = time.monotonic()
                    try:
                        if launch_browser(row): opened = key
                    except Exception:
                        state['browser_message'] = '浏览器未自动打开，可手动访问 http://127.0.0.1:8765/phone'
                state['browser_opened'] = opened == key
        except Exception:
            state = dict(version=VERSION, pi_id=pi_id(), state='transport_error', connected=False, message='USB调试通道暂不可用，正在重试')
        stamp = json.dumps(state, ensure_ascii=False, sort_keys=True)
        if stamp != previous:
            atomic(STATE, dict(state, updated_at_unix=time.time()))
            print(stamp, flush=True)
            previous = stamp
        time.sleep(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['list', 'connect', 'status', 'monitor', 'bind'])
    parser.add_argument('--serial')
    args = parser.parse_args()
    if args.action == 'monitor': monitor(); return
    if args.action == 'list': result = devices()
    elif args.action == 'bind':
        rows = [row for row in devices() if (not args.serial or row['serial'] == args.serial)]
        if len(rows) != 1 or rows[0]['state'] != 'device': raise RuntimeError('Connect and authorize exactly one USB phone')
        if CONFIG.exists(): atomic(ROOT / ('phone-binding.previous-' + str(time.time_ns()) + '.json'), json.loads(CONFIG.read_text()))
        row = rows[0]
        atomic(CONFIG, dict(version=1, pi_id=pi_id(), phone_serial=row['serial'], phone_model=adb(['-s', row['serial'], 'shell', 'getprop', 'ro.product.model']), local_port=8765, adb_server_port=PORT))
        result, _ = once()
    else: result, _ = once()
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
