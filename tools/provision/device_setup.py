"""Runs on a fresh Orange Pi as root, using only the Python 3.8 stdlib."""
import argparse
import hashlib
import json
import os
from pathlib import Path
try:
    import pwd
except ImportError:  # Only pure validation helpers are imported by Windows build/tests.
    pwd = None
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import time

PROJECT = Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2')
BASE = PROJECT / 'orangepi'
CALIBRATION = Path('/opt/egorecord/calibrations')
PRESERVE = [CALIBRATION, Path('/opt/egorecord/DEVICE_ID'),
            Path('/opt/egorecord/HARDWARE_MODEL'), Path('/opt/egorecord/camd/camd.conf')]
REQUIRED_CALIBRATION = ['camera-camchain.yaml', 'camera_imu-camchain-imucam.yaml', 'camera_imu-imu.yaml']
NEW_SERVICES = ['embodied-pi-collect', 'pi-phone-media', 'easytier', 'usb-phone-link',
                'headring-usb-data-host', 'headring-lan-time', 'headring-lan-time-refresh.timer']


def run(args, timeout=90, check=True, **kwargs):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, timeout=timeout, **kwargs)
    if check and p.returncode:
        raise RuntimeError('%s failed: %s' % (args[0], (p.stderr or p.stdout)[-1200:]))
    return p.stdout.strip()


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def preserved_hashes():
    files = []
    for p in PRESERVE:
        files.extend(sorted(x for x in p.rglob('*') if x.is_file()) if p.is_dir() else [p])
    return {str(p): sha(p) for p in files}


def device_number(value):
    match = re.fullmatch(r'cp_(\d{4})', value.strip())
    if not match:
        raise ValueError('Unexpected DEVICE_ID; expected cp_0001 style identity')
    number = int(match.group(1))
    if not 1 <= number <= 154:
        raise ValueError('Device number must be 1..154 for the current IP allocation')
    return number


def factory_camera_state():
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(4)
    try:
        connection.connect('/run/camd.sock')
        connection.sendall(b'{"cmd":"STATUS"}\n')
        data = bytearray()
        while b'\n' not in data and len(data) < 65536:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return json.loads(bytes(data).split(b'\n', 1)[0]).get('state', 'unknown')
    except (OSError, ValueError):
        return 'unavailable'
    finally:
        connection.close()


def inspect():
    if pwd.getpwnam('orangepi').pw_uid != 1000:
        raise RuntimeError('This image expects the orangepi account to have UID 1000')
    identity = Path('/opt/egorecord/DEVICE_ID').read_text().strip()
    number = device_number(identity)
    for name in REQUIRED_CALIBRATION:
        p = CALIBRATION / name
        if not p.is_file() or p.stat().st_size == 0:
            raise RuntimeError('Missing original calibration: ' + str(p))
    for p in PRESERVE[1:]:
        if not p.is_file():
            raise RuntimeError('Missing original hardware configuration: ' + str(p))
    if run(['uname', '-m']) != 'aarch64' or sys.version_info[:2] != (3, 8):
        raise RuntimeError('This release supports aarch64 Ubuntu with Python 3.8')
    mount = '/media/orangepi/sdcard'
    uuid = run(['findmnt', '-n', '-o', 'UUID', '--target', mount])
    target = run(['findmnt', '-n', '-o', 'TARGET', '--target', mount])
    if target != mount or not re.fullmatch(r'[A-Za-z0-9-]+', uuid):
        raise RuntimeError('The device SD card must be mounted at ' + mount)
    return dict(device_id=identity, number=number, vpn_ip='10.231.47.' + str(100 + number),
                collector_id='L2-' + str(number).zfill(3), sd_uuid=uuid,
                hardware_model=Path('/opt/egorecord/HARDWARE_MODEL').read_text().strip(),
                preserved=preserved_hashes(), existing_project=PROJECT.exists(),
                camd_state=factory_camera_state(),
                free_bytes=shutil.disk_usage('/').free,
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def archive_allowed(name):
    p = Path(name)
    if p.is_absolute() or '..' in p.parts:
        return False
    prefixes = ['home/orangepi/Desktop/Embodied-Brain-Collect-main.2/',
                'home/orangepi/.local/share/uv/', 'home/orangepi/maintenance/usb-phone-adb/',
                'opt/pi-phone-media/', 'opt/opi-headphone-audio/',
                'etc/systemd/system/embodied-pi-collect.service',
                'etc/systemd/system/pi-phone-media.service',
                 'etc/systemd/system/usb-phone-link.service',
                 'etc/systemd/system/headring-usb-data-host.service',
                 'etc/systemd/system/headring-lan-time',
                 'etc/systemd/system/camd.service.d/95-usb-data-host.conf',
                 'etc/systemd/system/camd.service.d/90-lan-time.conf',
                 'usr/local/sbin/headring-usb-data-host',
                 'usr/local/sbin/headring-lan-time',
                 'etc/headring-lan-time.json',
                 'etc/udev/rules.d/71-pi-auto-android.rules',
                'etc/udev/rules.d/91-pi-wrist-camera-power.rules',
                'usr/local/bin/easytier-']
    normalized = name.rstrip('/') + ('/' if name.endswith('/') else '')
    if name.rstrip('/') in [x.rstrip('/') for x in prefixes if x.endswith('/')]:
        return True
    if not any(normalized.startswith(x) for x in prefixes):
        return False
    forbidden = [PROJECT.as_posix().lstrip('/') + '/configs/environments',
                 PROJECT.as_posix().lstrip('/') + '/profiles',
                 BASE.as_posix().lstrip('/') + '/runtime', BASE.as_posix().lstrip('/') + '/configs/reference_control.json',
                 BASE.as_posix().lstrip('/') + '/configs/task_block_policy.json',
                 BASE.as_posix().lstrip('/') + '/configs/headphone_selection.json',
                 BASE.as_posix().lstrip('/') + '/configs/audio_output.json',
                 BASE.as_posix().lstrip('/') + '/configs/storage.yaml',
                 'home/orangepi/maintenance/usb-phone-adb/phone-binding.json',
                 'home/orangepi/maintenance/usb-phone-adb/keys']
    return not any(normalized == x or normalized.startswith(x + '/') for x in forbidden)


def backup(stage, info):
    dest = stage / 'backup'
    dest.mkdir(mode=0o700, exist_ok=True)
    path = dest / 'preserved.json'
    if path.exists():
        original = json.loads(path.read_text())
        if original['preserved'] != info['preserved']:
            raise RuntimeError('Original calibration or hardware identity changed since the first attempt')
        return original
    paths = [str(p).lstrip('/') for p in PRESERVE]
    paths += ['opt/egorecord/config', 'etc/systemd/system/egorecord.service',
              'home/orangepi/.asoundrc', 'etc/pulse/default.pa', 'etc/chrony/chrony.conf']
    paths += [str(p).lstrip('/') for p in Path('/etc/systemd/system').glob('seeker*.service')]
    with tarfile.open(str(dest / 'original-device.tar.gz'), 'w:gz') as tf:
        for p in paths:
            if Path('/' + p).exists():
                tf.add('/' + p, arcname=p, recursive=True)
    shutil.copy2('/etc/pulse/default.pa', str(dest / 'pulse-default.pa'))
    states = {}
    for service in ['egorecord', 'seeker_cam_bridge', 'seeker_imu', 'chrony'] + NEW_SERVICES:
        states[service] = dict(active=run(['systemctl', 'is-active', service], check=False) == 'active',
                               enabled=run(['systemctl', 'is-enabled', service], check=False) == 'enabled')
    info = dict(info, service_states=states)
    path.write_text(json.dumps(info, indent=2))
    user = pwd.getpwnam('orangepi')
    os.chown(str(dest), user.pw_uid, user.pw_gid)
    for p in dest.iterdir():
        os.chown(str(p), user.pw_uid, user.pw_gid)
        os.chmod(str(p), 0o600)
    return info


def assert_preserved(expected):
    if preserved_hashes() != expected:
        raise RuntimeError('Original calibration/hardware files changed; inspect the device backup')


def own_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.install-tmp')
    temporary.write_text(data, encoding='utf-8')
    user = pwd.getpwnam('orangepi')
    os.chmod(str(temporary), mode)
    os.chown(str(temporary), user.pw_uid, user.pw_gid)
    os.replace(str(temporary), str(path))


def get_status():
    import urllib.request
    token = (BASE / 'runtime/dashboard_token').read_text().strip()
    req = urllib.request.Request('http://127.0.0.1:8765/api/status?display=installer&signals=&cameras=0',
                                 headers={'X-Access-Token': token})
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.load(response)


def verify(manifest, expected=None, expected_ip=None):
    import urllib.request
    if expected:
        assert_preserved(expected)
    number = device_number(Path('/opt/egorecord/DEVICE_ID').read_text())
    vpn_ip = expected_ip or '10.231.47.' + str(100 + number)
    for name in ['camd', 'easytier', 'embodied-pi-collect', 'pi-phone-media', 'headring-usb-data-host']:
        if run(['systemctl', 'is-active', name], check=False) != 'active':
            raise RuntimeError('Service not active: ' + name)
    if run(['systemctl', 'is-enabled', 'egorecord'], check=False) != 'masked':
        raise RuntimeError('Original EgoRecord is not masked')
    files = manifest['files']
    for name, entry in files.items():
        if sha(entry['target']) != entry['sha256']:
            raise RuntimeError('Code hash mismatch: ' + name)
    status = get_status()
    if status.get('state') in ('starting', 'fatal'):
        raise RuntimeError('Collector not ready: ' + str(status.get('state')))
    host_state = json.loads(Path('/run/headring-usb-data-host.json').read_text())
    if host_state.get('state') != 'host':
        raise RuntimeError('Mandatory USB data-host initialization is incomplete: ' + str(host_state.get('state')))
    with urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=8) as response:
        media = json.load(response)
    prefs = json.loads((BASE / 'configs/reference_control.json').read_text())
    if prefs['collector_id'] != 'L2-' + str(number).zfill(3):
        raise RuntimeError('Collector identity does not match this headring')
    if vpn_ip + '/' not in run(['ip', '-o', '-4', 'addr', 'show']):
        raise RuntimeError('EasyTier address missing: ' + vpn_ip)
    peers = run(['/usr/local/bin/easytier-cli', 'peer'])
    if len(set(re.findall(r'10\.231\.47\.\d+/\d+', peers))) < 2:
        raise RuntimeError('EasyTier has not connected to another fleet node yet')
    sys.path.insert(0, str(BASE / 'src'))
    from orangepi_collect.recorders.camera import camd_source
    frames = {}
    for name in ['left', 'right', 'bleft', 'bright']:
        w, h, stamp, raw = camd_source.snapshot(name)
        if (w, h) != (1920, 1200) or len(raw) != w * h * 3 // 2:
            raise RuntimeError('Unexpected camera frame: ' + name)
        frames[name] = dict(width=w, height=h, age_ms=round((time.time() - stamp / 1e6) * 1000, 1))
    return dict(device_id=Path('/opt/egorecord/DEVICE_ID').read_text().strip(), number=number,
                usb_data_host=host_state,
                vpn_ip=vpn_ip, collector_id=prefs['collector_id'], state=status['state'],
                common_files_match=len(files), frames=frames, media_version=media.get('version'),
                sensors=[{k: x.get(k) for k in ['name', 'connected', 'sample_rate_hz']} for x in status.get('devices', [])],
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                preserved=preserved_hashes())


def rollback(stage, original):
    print('Installation failed; restoring original service state.', flush=True)
    for name in NEW_SERVICES:
        if name == 'easytier' and original['service_states'].get(name, {}).get('active'):
            continue
        run(['systemctl', 'disable', '--now', name], timeout=75, check=False)
    for name in ['egorecord', 'seeker_cam_bridge', 'seeker_imu']:
        saved = stage / 'backup' / (name + '.service')
        if saved.exists():
            run(['systemctl', 'unmask', name], check=False)
            shutil.copy2(str(saved), '/etc/systemd/system/' + name + '.service')
    if (stage / 'backup/pulse-default.pa').exists():
        shutil.copy2(str(stage / 'backup/pulse-default.pa'), '/etc/pulse/default.pa')
    if (stage / 'backup/original-device.tar.gz').exists():
        with tarfile.open(str(stage / 'backup/original-device.tar.gz'), 'r:gz') as archive:
            try: saved = archive.extractfile('etc/chrony/chrony.conf')
            except KeyError: saved = None
            if saved: Path('/etc/chrony/chrony.conf').write_bytes(saved.read())
    run(['systemctl', 'daemon-reload'])
    for name, value in original['service_states'].items():
        if name in NEW_SERVICES and name != 'easytier':
            continue
        if value['enabled']:
            run(['systemctl', 'enable', name], check=False)
        if value['active']:
            run(['systemctl', 'start', name], timeout=75, check=False)
    previous_chrony = original['service_states'].get('chrony', {})
    run(['systemctl', 'enable' if previous_chrony.get('enabled') else 'disable', 'chrony'], check=False)
    run(['systemctl', 'restart' if previous_chrony.get('active') else 'stop', 'chrony'], timeout=20, check=False)
    assert_preserved(original['preserved'])


def install(stage):
    spec = json.loads((stage / 'install.json').read_text())
    release = json.loads((stage / 'release.json').read_text())
    info = inspect()
    if info['number'] != spec['number'] or info['sd_uuid'] != spec['sd_uuid']:
        raise RuntimeError('Device identity or SD card changed after the preflight')
    receipt = stage / 'complete.json'
    if receipt.exists():
        old = json.loads(receipt.read_text())
        if old['release_sha256'] != release['payload_sha256']:
            raise RuntimeError('Different release already installed; use the upgrade workflow')
        result = verify(release, old['preserved'], spec['vpn_ip'])
        print(json.dumps(dict(result, already_installed=True), ensure_ascii=False), flush=True)
        return
    extracted = stage / 'payload-extracted.json'
    # This fleet already provisioned EasyTier independently of the collector.
    # Its exact configuration is now backed up and covered by preserved hashes.
    if PROJECT.exists() and not extracted.exists():
        raise RuntimeError('An existing collector installation was found. This tool installs fresh headrings only.')
    if info['free_bytes'] < 1200 * 1024 * 1024:
        raise RuntimeError('At least 1.2 GiB free space is required on the system disk')
    original = backup(stage, info)
    network_paths = [Path('/etc/easytier/node.toml'), Path('/etc/systemd/system/easytier.service')]
    network_before = {str(p): sha(p) for p in network_paths if p.is_file()}
    for p in network_paths:
        if p.is_file() and not (stage / 'backup' / ('network-' + p.name)).exists():
            shutil.copy2(str(p), str(stage / 'backup' / ('network-' + p.name)))
            os.chmod(str(stage / 'backup' / ('network-' + p.name)), 0o600)
    print('Original calibration and configuration backed up.', flush=True)
    if sha(stage / 'payload.tgz') != release['payload_sha256']:
        raise RuntimeError('Payload checksum failed; no service changes have been made')
    with tarfile.open(str(stage / 'payload.tgz'), 'r:gz') as tf:
        for member in tf.getmembers():
            if not archive_allowed(member.name):
                raise RuntimeError('Unexpected archive destination: ' + member.name)
    if extracted.exists():
        if json.loads(extracted.read_text())['sha256'] != release['payload_sha256']:
            raise RuntimeError('Partial installation belongs to a different release')
    else:
        extracted.write_text(json.dumps({'sha256': release['payload_sha256']}))
    # Re-extraction is intentional: incomplete transfers/extractions may be retried.
    # User configuration and runtime directories are excluded from the payload.
    print('Installing program and matching Python environments.', flush=True)
    run(['tar', '-xzf', str(stage / 'payload.tgz'), '-C', '/', '--no-same-owner'], timeout=180)
    for path in [PROJECT, Path('/home/orangepi/.local/share/uv'),
                 Path('/home/orangepi/maintenance/usb-phone-adb')]:
        run(['chown', '-R', 'orangepi:orangepi', str(path)])
    for p in [BASE / 'runtime', PROJECT / 'configs/environments', BASE / 'tests/results']:
        p.mkdir(parents=True, exist_ok=True)
        user = pwd.getpwnam('orangepi')
        os.chown(str(p), user.pw_uid, user.pw_gid)
    if not (BASE / 'configs/storage.yaml').exists() or not (stage / 'configured').exists():
        own_write(BASE / 'configs/storage.yaml',
                  'mode: sd\nmount_point: /media/orangepi/sdcard\nexpected_uuid: %s\ndevice_id: orangepi-cp%04d\ndirectory: Embodied-Brain-Collect\nshift: session-night\nminimum_start_free_mb: 1024\n' % (info['sd_uuid'], info['number']))
    defaults = {
        'reference_control.json': dict(version=1, collector_id=info['collector_id'], selected_list_id='pool'),
        'headphone_selection.json': dict(version=1, address=None),
        'audio_output.json': dict(enabled=False, server='unix:/tmp/pulse-socket', sink='', player='/opt/opi-headphone-audio/guarded-paplay')}
    for name, value in defaults.items():
        if not (BASE / 'configs' / name).exists():
            own_write(BASE / 'configs' / name, json.dumps(value) + '\n', 0o600)
    if not (BASE / 'runtime/dashboard_token').exists():
        own_write(BASE / 'runtime/dashboard_token', secrets.token_hex(16) + '\n', 0o600)
        (stage / 'configured').touch()
    for name, digest in release['program_files'].items():
        if sha('/' + name) != digest:
            raise RuntimeError('Extracted program checksum failed: ' + name)
    initialize = "import sys;sys.path.insert(0,%r);from collection_profiles import initialize;initialize(%r,'emg')" % (str(BASE/'src/orangepi_collect/common'), str(BASE))
    run(['runuser', '-u', 'orangepi', '--', str(BASE/'.venvs/hardware/bin/python'), '-c', initialize])
    env = dict(os.environ, PYTHONPATH=str(BASE / 'src/orangepi_collect/common') + ':' + str(PROJECT / 'src'))
    for python, code in [
        (BASE / '.venvs/hardware/bin/python', 'import gi,numpy,yaml,cv2,PIL;gi.require_version("Gst","1.0");from gi.repository import Gst;Gst.init(None);assert Gst.ElementFactory.find("mpph264enc")'),
        (BASE / '.venvs/framework/bin/python', 'import numpy,cv2,yaml,loguru,dashscope;from dashscope.audio.qwen_tts_realtime import QwenTtsRealtime;import embodied_brain_collect.checkers')]:
        run([str(python), '-c', code], env=env, timeout=60)
    # A recording must never be interrupted by the migration.
    sys.path.insert(0, str(BASE / 'src'))
    from orangepi_collect.recorders.camera import camd_source
    if camd_source.status().get('state') != 'IDLE':
        raise RuntimeError('Original camera recorder is busy; finish the recording and retry')
    assert_preserved(original['preserved'])
    try:
        print('Disabling original services and enabling replacement services.', flush=True)
        for name in ['egorecord', 'seeker_cam_bridge', 'seeker_imu']:
            unit = Path('/etc/systemd/system/' + name + '.service')
            if unit.exists():
                run(['systemctl', 'disable', '--now', name], timeout=75, check=False)
                saved = stage / 'backup' / (name + '.service')
                if not saved.exists() and unit.is_file() and not unit.is_symlink():
                    shutil.copy2(str(unit), str(saved))
                if unit.is_file() and not unit.is_symlink():
                    unit.unlink()
                run(['systemctl', 'mask', name])
        d = Path('/etc/systemd/system/embodied-pi-collect.service.d')
        d.mkdir(exist_ok=True)
        # The reviewed early-web entry now owns readiness. Do not reintroduce a
        # systemd pre-start camera wait that hides the USB collection page.
        et = Path('/etc/easytier')
        et.mkdir(mode=0o755, exist_ok=True)
        if not (et / 'node.toml').exists():
            own_write(et / 'node.toml', (stage / 'node.toml').read_text(), 0o600)
            os.chown(str(et / 'node.toml'), 0, 0)
            shutil.copyfile(str(stage / 'easytier.service'), '/etc/systemd/system/easytier.service')
        usb = Path('/home/orangepi/maintenance/usb-phone-adb/keys')
        usb.mkdir(mode=0o700, exist_ok=True)
        user = pwd.getpwnam('orangepi'); os.chown(str(usb), user.pw_uid, user.pw_gid)
        if not (usb/'adbkey').exists():
            runtime = usb.parent/'root'
            env = dict(os.environ, LD_LIBRARY_PATH=str(runtime/'usr/lib/aarch64-linux-gnu/android')+':'+str(runtime/'usr/lib/aarch64-linux-gnu'))
            run(['runuser', '-u', 'orangepi', '--', str(runtime/'usr/bin/adb'), 'keygen', str(usb/'adbkey')], env=env)
        ud = Path('/etc/systemd/system/usb-phone-link.service.d'); ud.mkdir(exist_ok=True)
        (ud / '10-own-phone-binding.conf').write_text('[Unit]\nConditionPathExists=/home/orangepi/maintenance/usb-phone-adb/phone-binding.json\n')
        pulse = Path('/etc/pulse/default.pa')
        if '/run/user/1000/pulse/pi-collect-headphones' not in pulse.read_text():
            with pulse.open('a') as f:
                f.write('\n.nofail\nload-module module-native-protocol-unix auth-anonymous=1 socket=/run/user/1000/pulse/pi-collect-headphones\n.fail\n')
        run(['udevadm', 'control', '--reload-rules'])
        chrony = Path('/etc/chrony/chrony.conf')
        if chrony.exists():
            text = re.sub(r'\n# BEGIN HEADRING_LAN_TIME\n.*?# END HEADRING_LAN_TIME\n?', '\n', chrony.read_text(), flags=re.S)
            lines = []
            for line in text.splitlines():
                if re.match(r'^\s*(server|pool|peer|makestep|initstepslew|maxslewrate|leapsecmode)\s', line):
                    line = '# Previous source/step setting replaced by guarded LAN policy: ' + line
                lines.append(line)
            chrony.write_text('\n'.join(lines)+'\n\n# BEGIN HEADRING_LAN_TIME\nserver 192.168.1.118 iburst prefer minpoll 4 maxpoll 6\nmaxslewrate 500\nleapsecmode slew\n# END HEADRING_LAN_TIME\n')
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable', 'camd', 'chrony'] + NEW_SERVICES)
        run(['systemctl', 'restart', 'chrony'], timeout=20, check=False)
        # Mandatory in the fresh Windows/Linux installer as well as the Git
        # installer: data-host policy starts before the new collector.
        run(['systemctl', 'start', 'headring-usb-data-host', 'headring-lan-time', 'headring-lan-time-refresh.timer'], timeout=35)
        host = json.loads(run(['/usr/local/sbin/headring-usb-data-host', '--once', '--base', str(BASE)]))
        if host.get('state') != 'host':
            raise RuntimeError('USB data-host initialization deferred/refused; use a compatible host connection before activation')
        run(['systemctl', 'start', 'easytier', 'pi-phone-media', 'embodied-pi-collect', 'usb-phone-link'], timeout=65)
        deadline = time.monotonic() + 75
        while True:
            try:
                result = verify(release, original['preserved'], spec['vpn_ip'])
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        own_write(BASE / 'runtime/feature-baseline.json', json.dumps(dict(
            version=release['version'], device=str(info['number']),
            files={k: v['sha256'] for k, v in release['files'].items()}), indent=2))
        result.update(release_sha256=release['payload_sha256'], version=release['version'])
        if any(sha(path) != value for path, value in network_before.items()):
            raise RuntimeError('Existing network configuration changed during installation')
        result['existing_network_unchanged'] = True
        receipt.write_text(json.dumps(result, indent=2))
        os.sync()
        print('RESULT=' + json.dumps(result, ensure_ascii=False), flush=True)
    except BaseException:
        rollback(stage, original)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['inspect', 'install', 'verify'])
    parser.add_argument('--stage')
    args = parser.parse_args()
    if args.action == 'inspect':
        print(json.dumps(inspect(), ensure_ascii=False))
    elif args.action == 'install':
        if os.geteuid() != 0:
            raise RuntimeError('Root privilege is required')
        install(Path(args.stage))
    else:
        stage = Path(args.stage)
        release = json.loads((stage / 'release.json').read_text())
        before = json.loads((stage / 'backup/preserved.json').read_text())
        print('RESULT=' + json.dumps(verify(release, before['preserved']), ensure_ascii=False))


if __name__ == '__main__':
    main()
