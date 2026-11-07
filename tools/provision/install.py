#!/usr/bin/env python3
"""Windows/Linux controller for the headring fresh-install bundle."""
import argparse
import base64
import datetime
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sys
import time

ROOT = Path(__file__).resolve().parent
if (ROOT / 'lib').is_dir():
    sys.path.insert(0, str(ROOT / 'lib'))
import paramiko

VERSION = 'headring-unified-20261107-v3'
REMOTE_STAGE = '/home/orangepi/maintenance/headring-setup-' + VERSION
BASE = '/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi'


def state_dir():
    override = os.environ.get('HEADRING_SETUP_STATE')
    if override:
        return Path(override)
    if os.name == 'nt':
        return ROOT / 'state'
    return Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'headring-setup'


STATE = state_dir()


class PinNewHost(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        # A new LAN address is trusted on first use and then pinned.
        fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
        print('首次连接 %s，记录 SSH 指纹 SHA256:%s' % (hostname, fingerprint), flush=True)
        client.get_host_keys().add(hostname, key.get_name(), key)
        client.save_host_keys(str(STATE / 'known_hosts'))


def connect(host, password, expected_key=None, timeout=12):
    c = paramiko.SSHClient()
    for p in [ROOT / 'seed_known_hosts', STATE / 'known_hosts']:
        if p.exists():
            c.load_host_keys(str(p))
    if expected_key is not None:
        c.get_host_keys().add(host, expected_key.get_name(), expected_key)
        c.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        c.set_missing_host_key_policy(PinNewHost())
    try:
        c.connect(host, username='orangepi', password=password, look_for_keys=False,
                  allow_agent=False, timeout=timeout, auth_timeout=timeout, banner_timeout=timeout)
        return c
    except BaseException:
        c.close()
        raise


def run(client, command, timeout=60, check=True):
    _, out, err = client.exec_command(command, timeout=timeout)
    stdout, stderr = out.read().decode('utf-8', 'replace'), err.read().decode('utf-8', 'replace')
    status = out.channel.recv_exit_status()
    if check and status:
        raise RuntimeError((stderr or stdout)[-1800:])
    return stdout.strip()


def embedded(client, expression, hardware=False):
    module = base64.b64encode((ROOT / 'device_setup.py').read_bytes()).decode('ascii')
    code = "import base64,json;ns={'__name__':'headring_installer'};exec(base64.b64decode(%r),ns);" % module
    code += expression
    python = BASE + '/.venvs/hardware/bin/python' if hardware else '/usr/bin/python3'
    # The unified manifest is too large for Linux's per-argument limit. Stream
    # trusted installer code and its request over SSH stdin, not a shell argv.
    stdin, out, err = client.exec_command(shlex.quote(python) + ' -', timeout=75)
    stdin.write(code + '\n'); stdin.flush(); stdin.channel.shutdown_write()
    stdout, stderr = out.read().decode('utf-8', 'replace'), err.read().decode('utf-8', 'replace')
    if out.channel.recv_exit_status():
        raise RuntimeError((stderr or stdout)[-1800:])
    return stdout.strip()


def check_ip_conflict(seed, vpn_ip, target_key):
    if seed is None:
        # Detect any reachable address from the setup computer even without a seed.
        try:
            sock = socket.create_connection((vpn_ip, 22), timeout=1.5)
        except OSError:
            return
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=4)
            if transport.get_remote_server_key() != target_key:
                raise RuntimeError('EasyTier 地址 %s 已被其他设备占用，停止安装。' % vpn_ip)
        finally:
            transport.close()
        return
    table = run(seed, '/usr/local/bin/easytier-cli peer')
    if not re.search(r'(?<![\d.])' + re.escape(vpn_ip) + r'/\d+', table):
        return
    channel = seed.get_transport().open_channel('direct-tcpip', (vpn_ip, 22), ('127.0.0.1', 0), timeout=6)
    transport = paramiko.Transport(channel)
    try:
        transport.start_client(timeout=6)
        if transport.get_remote_server_key() != target_key:
            raise RuntimeError('EasyTier 地址 %s 已被其他设备占用，停止安装。' % vpn_ip)
    finally:
        transport.close()


def upload_resume(client, local, destination):
    sftp = client.open_sftp()
    partial = destination + '.part'
    try:
        try:
            size = sftp.stat(destination).st_size
        except OSError:
            size = -1
        if size == local.stat().st_size:
            print('安装包已存在，正在验证校验和。', flush=True)
            return
        try:
            offset = sftp.stat(partial).st_size
        except OSError:
            offset = 0
        total = local.stat().st_size
        if offset > total:
            sftp.remove(partial)
            offset = 0
        last = 0.0
        with local.open('rb') as src, sftp.open(partial, 'ab' if offset else 'wb') as dst:
            dst.set_pipelined(True)
            src.seek(offset)
            done = offset
            while True:
                chunk = src.read(512 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                done += len(chunk)
                if time.monotonic() - last > 4:
                    print('传输安装包：%.1f%%（%.1f / %.1f MB）' % (100 * done / total, done / 1e6, total / 1e6), flush=True)
                    last = time.monotonic()
        if size >= 0:
            sftp.remove(destination)
        sftp.rename(partial, destination)
    finally:
        sftp.close()


def sudo_stream(client, command, password, timeout=600):
    stdin, out, err = client.exec_command('sudo -S -p "" ' + command, timeout=timeout)
    stdin.write(password + '\n')
    stdin.flush()
    stdin.channel.shutdown_write()
    result = None
    transcript = []
    for line in out:
        line = line.rstrip('\r\n')
        transcript.append(line)
        if line.startswith('RESULT='):
            result = json.loads(line[len('RESULT='):])
        else:
            print(line, flush=True)
    error = err.read().decode('utf-8', 'replace')
    status = out.channel.recv_exit_status()
    if status:
        raise RuntimeError(error[-2500:] or '\n'.join(transcript[-8:]))
    return result


def retrieve_backup(client, destination):
    sftp = client.open_sftp()
    try:
        sftp.get(REMOTE_STAGE + '/backup/original-device.tar.gz', str(destination / 'original-device.tar.gz'))
        sftp.get(REMOTE_STAGE + '/backup/preserved.json', str(destination / 'original-hashes.json'))
    finally:
        sftp.close()


def wait_after_reboot(host, vpn_ip, password, key, previous_boot):
    deadline = time.monotonic() + 360
    last_message = 0.0
    while time.monotonic() < deadline:
        for address in [host, vpn_ip]:
            try:
                c = connect(address, password, key, timeout=3)
                boot = run(c, 'cat /proc/sys/kernel/random/boot_id')
                if boot != previous_boot:
                    return c, address
                c.close()
            except (OSError, paramiko.SSHException):
                pass
        if time.monotonic() - last_message > 15:
            print('等待头环重启、Wi-Fi 和服务恢复……', flush=True)
            last_message = time.monotonic()
        time.sleep(3)
    raise RuntimeError('安装已完成，但重启后暂未恢复连接；再次运行同一命令即可继续复核。')


def verified(client, release, expected=None):
    expression = 'print(json.dumps(ns["verify"](%r,%r),ensure_ascii=False))' % (release, expected)
    return json.loads(embedded(client, expression, hardware=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description='头环快速安装：只连接目标头环，不复制参考图或其他设备标定。')
    parser.add_argument('--host', help='新头环的局域网 IPv4；不填时交互输入')
    parser.add_argument('--id', type=int, help='校验头环编号；默认从原始 DEVICE_ID 自动读取')
    parser.add_argument('--seed-host', default='192.168.1.117', help='现有19号或其他已入网头环的局域网地址')
    parser.add_argument('--ask-password', action='store_true', help='输入不同于默认值1的SSH/sudo密码')
    parser.add_argument('--no-reboot', action='store_true', help='安装后不自动重启')
    parser.add_argument('--plan', action='store_true', help='只检查设备身份和安装条件，不更改设备')
    parser.add_argument('--check', action='store_true', help='只核验已安装程序，不安装、不重启')
    args = parser.parse_args(argv)
    host = args.host or input('请输入新头环的局域网 IP（例如 192.168.1.120）：').strip()
    ipaddress.IPv4Address(host)
    ipaddress.IPv4Address(args.seed_host)
    password = getpass.getpass('SSH / sudo 密码：') if args.ask_password else os.environ.get('HEADRING_SSH_PASSWORD', '1')
    STATE.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        os.chmod(str(STATE), 0o700)
    release = json.loads((ROOT / 'release.json').read_text(encoding='utf-8'))
    if release['version'] != VERSION:
        raise RuntimeError('Installer/release version mismatch')
    client = connect(host, password)
    seed = None
    target_key = client.get_transport().get_remote_server_key()
    try:
        info = json.loads(embedded(client, 'print(json.dumps(ns["inspect"](),ensure_ascii=False))'))
        if args.id is not None and args.id != info['number']:
            raise RuntimeError('设备原始编号为 %s，与指定的 %s 不符；未修改设备。' % (info['number'], args.id))
        print('设备：%s；采集编号：%s；EasyTier：%s；SD卡：%s' %
              (info['device_id'], info['collector_id'], info['vpn_ip'], info['sd_uuid']), flush=True)
        if args.check:
            result = verified(client, release)
            print('核验通过：程序版本一致，四路相机正常，原程序保持停用。', flush=True)
            (STATE / ('check-' + info['device_id'] + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            return 0
        if args.plan:
            print(json.dumps(info, ensure_ascii=False, indent=2))
            print('只读检查完成，未安装或更改设备。')
            return 0
        try:
            seed = connect(args.seed_host, password, timeout=5)
        except (OSError, paramiko.SSHException):
            print('现有入网头环暂不可达；使用本机可达性检查地址冲突。', flush=True)
        check_ip_conflict(seed, info['vpn_ip'], target_key)
        exists = run(client, 'test -f ' + shlex.quote(REMOTE_STAGE + '/complete.json') + ' && printf yes', check=False) == 'yes'
        if info['existing_project'] and not exists:
            partial = run(client, 'test -f ' + shlex.quote(REMOTE_STAGE + '/payload-extracted.json') + ' && printf yes', check=False)
            if partial != 'yes':
                raise RuntimeError('发现已安装的采集程序。此工具只安装全新头环；可用 --check 只读核验。')
        if not exists and info.get('camd_state') == 'RECORDING':
            raise RuntimeError('原厂程序正在录制；请先正常结束这段录制，再重新运行。尚未上传或切换服务。')
        log_dir = STATE / (info['device_id'] + '-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
        log_dir.mkdir(mode=0o700)
        result = None
        if exists:
            before = json.loads(run(client, 'cat ' + shlex.quote(REMOTE_STAGE + '/complete.json')))
            if before['release_sha256'] != release['payload_sha256']:
                raise RuntimeError('该设备属于其他安装版本，停止覆盖。')
            result = verified(client, release, before['preserved'])
            retrieve_backup(client, log_dir)
            print('该头环已经安装完成，直接复核；保留现有任务、绑定和运行数据。', flush=True)
        else:
            if info['free_bytes'] < 1500 * 1024 * 1024:
                raise RuntimeError('系统盘至少需要1.5 GiB空闲空间。')
            payload = ROOT / 'payload.tgz'
            print('正在校验本地安装包……', flush=True)
            digest = hashlib.sha256()
            with payload.open('rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != release['payload_sha256']:
                raise RuntimeError('本地安装包损坏，请重新解压原始安装包。')
            run(client, 'mkdir -p ' + shlex.quote(REMOTE_STAGE) + ' && chmod 700 ' + shlex.quote(REMOTE_STAGE))
            upload_resume(client, payload, REMOTE_STAGE + '/payload.tgz')
            remote_sha = run(client, 'sha256sum ' + shlex.quote(REMOTE_STAGE + '/payload.tgz')).split()[0]
            if remote_sha != release['payload_sha256']:
                # The next attempt must re-upload a bad but correctly sized file.
                run(client, 'rm -f -- ' + shlex.quote(REMOTE_STAGE + '/payload.tgz'))
                raise RuntimeError('远端安装包校验失败，重新运行可重新上传。')
            check_ip_conflict(seed, info['vpn_ip'], target_key)
            config = (ROOT / 'node.template.toml').read_text(encoding='utf-8')
            config = config.replace('@DEVICE_NAME@', 'orangepi-headring-cp%04d' % info['number']).replace('@VPN_IP@', info['vpn_ip'])
            # A live LAN headring provides an additional bootstrap peer.
            if seed and args.seed_host != host and ('tcp://' + args.seed_host + ':11010') not in config:
                config += '\n[[peer]]\nuri = "tcp://' + args.seed_host + ':11010"\n'
            service = (ROOT / 'easytier.template.service').read_text().replace('@DEVICE_NAME@', 'orangepi-headring-cp%04d' % info['number'])
            sftp = client.open_sftp()
            try:
                for name in ['device_setup.py', 'release.json']:
                    sftp.put(str(ROOT / name), REMOTE_STAGE + '/' + name)
                for name, value in [('install.json', json.dumps(info)), ('node.toml', config), ('easytier.service', service)]:
                    with sftp.open(REMOTE_STAGE + '/' + name, 'w') as f:
                        f.write(value.encode('utf-8'))
                    sftp.chmod(REMOTE_STAGE + '/' + name, 0o600)
            finally:
                sftp.close()
            try:
                result = sudo_stream(client, '/usr/bin/python3 ' + shlex.quote(REMOTE_STAGE + '/device_setup.py') + ' install --stage ' + shlex.quote(REMOTE_STAGE), password)
            finally:
                try:
                    retrieve_backup(client, log_dir)
                except OSError:
                    pass
            if result is None:
                raise RuntimeError('安装未返回核验结果，请查看设备端安装目录。')
        # Re-running a completed installation must never interrupt its operator.
        if not args.no_reboot and not exists:
            previous_boot = result['boot_id']
            print('安装与校验完成，正在重启目标头环并复核开机自启……', flush=True)
            try:
                sudo_stream(client, '/bin/systemctl reboot', password, timeout=20)
            except (OSError, paramiko.SSHException):
                pass
            client.close()
            client, host = wait_after_reboot(host, info['vpn_ip'], password, target_key, previous_boot)
            deadline = time.monotonic() + 90
            while True:
                try:
                    result = verified(client, release, info['preserved'])
                    if result['boot_id'] == previous_boot:
                        raise RuntimeError('Boot ID did not change')
                    result['reboot_verified'] = True
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(3)
        lan_ip = host
        try:
            interfaces = json.loads(run(client, 'ip -j -4 addr show dev wlan0'))
            lan_ip = next(a['local'] for row in interfaces for a in row['addr_info'] if a.get('scope') == 'global')
        except (ValueError, KeyError, StopIteration, RuntimeError):
            pass
        result.update(version=VERSION, lan_ip=lan_ip, connect_address=host,
                      completed_at=datetime.datetime.now().astimezone().isoformat())
        (log_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('\n配置完成：%s / %s' % (info['device_id'], VERSION), flush=True)
        print('局域网页面：http://%s:8765/phone' % lan_ip)
        print('EasyTier：http://%s:8765/phone' % info['vpn_ip'])
        print('自己的标定、硬件映射已保留并校验。' +
              ('参考图和已有任务未改动。' if exists else '参考图库为空，等待自己的任务。'))
        print('备份和核验记录：' + str(log_dir))
        missing = [x['name'] for x in result['sensors'] if not x.get('connected')]
        if missing:
            print('尚未接入的外设：' + '、'.join(missing) + '；未做整套正式采集测试。')
        return 0
    finally:
        client.close()
        if seed:
            seed.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\n已中断。再次运行同一命令可续传或继续核验。', file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print('\n未完成：' + str(error), file=sys.stderr)
        raise SystemExit(1)
