#!/usr/bin/env python3
"""Authorized physical Android USB peers; no Wi-Fi ADB or recorder controls."""
import argparse
import hashlib
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
VERSION = 'headring-usb-20261109-v8.1'
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
    # Ambient ADB server selection must never redirect recovery to another server.
    for key in ('ADB_SERVER_SOCKET','ANDROID_ADB_SERVER_PORT','ANDROID_SERIAL'):
        env.pop(key,None)
    # Omitting -H lets adb start its dedicated local server if it is absent.
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
            transport = next((part[13:] for part in parts[2:] if part.startswith('transport_id:')), '')
            result.append(dict(serial=parts[0], state=parts[1], usb=usb, transport_id=transport))
    return result

def choose(rows):
    # Physical USB attachment plus Android authorization determines the peer.
    # Historical phone-binding.json is audit history, never a routing policy.
    authorized = [row for row in rows if row['state'] == 'device']
    if len(authorized) == 1: return authorized[0], None
    if len(rows) == 1: return rows[0], None
    if not rows: return None, 'phone_missing'
    return None, 'multiple_phones'

def data_role_state():
    try:
        value = json.loads(Path('/run/headring-usb-data-host.json').read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError): return {}

def once(tunnel=None):
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
    if tunnel is not None:
        tunnel.ensure(row)
        result.update(reverse_refresh_count=tunnel.refresh_count, reverse_reason=tunnel.reason)
    else:
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
    importing = value.get('state') == 'usb_importing' and not value.get('session')
    state = value.get('state') or ''
    preparing = state in ('preparing','ready','emg_manual','emg_pairing') or state.startswith('wrist_')
    worker = value.get('worker') or {}
    if ((state not in ('device_confirm', 'idle', 'failed_wait', 'exhausted', 'starting', 'scanning', 'usb_importing') and not preparing)
            or (value.get('busy') and not (importing or preparing)) or worker.get('recording_active')
            or worker.get('state') in ('recording','saving','checking','finalizing','postprocessing')
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
    # USB configuration changes may assign a new devnum to the same phone.
    # They must not send another Android VIEW intent to the already-open page.
    return hashlib.sha256((row['serial'] + '\0' + row['usb']).encode()).hexdigest()


def transport_key(row):
    try: number = (Path('/sys/bus/usb/devices') / row['usb'] / 'devnum').read_text().strip()
    except OSError: number = ''
    return row['serial'], row['usb'], number, row.get('transport_id', '')


class ReverseTunnel:
    def __init__(self, clock=time.monotonic):
        self.clock=clock;self.current=None;self.missing_since=None
        self.refresh_count=0;self.reason=''

    def offline(self):
        if self.missing_since is None:self.missing_since=self.clock()
        if self.clock()-self.missing_since>=3:self.current=None

    def ensure(self,row):
        if self.missing_since is not None and self.clock()-self.missing_since>=3:self.current=None
        self.missing_since=None
        key=transport_key(row)
        maps=adb(['-s',row['serial'],'reverse','--list'])
        present=any(line.split()[-2:]==['tcp:8765','tcp:8765'] for line in maps.splitlines())
        if key!=self.current or not present:
            # Default ADB reverse permits rebinding. Refresh only our port,
            # without clearing authorization, other forwards, or the ADB server.
            adb(['-s',row['serial'],'reverse','tcp:8765','tcp:8765'])
            verified=adb(['-s',row['serial'],'reverse','--list'])
            if not any(line.split()[-2:]==['tcp:8765','tcp:8765'] for line in verified.splitlines()):
                raise RuntimeError('USB collection reverse mapping was not restored')
            self.reason='new_usb_connection' if key!=self.current else 'mapping_missing'
            self.current=key;self.refresh_count+=1


class BrowserLaunchGate:
    def __init__(self, previous=None, boot_id='', clock=time.monotonic):
        self.clock = clock
        self.previous = previous or {}
        self.boot_id = boot_id
        self.opened = None
        self.pending = None
        self.stable_since = None
        self.missing_since = None
        self.last_attempt = -1e9
        self.open_count = 0

    def observe(self, row):
        now = self.clock()
        if row is None:
            if self.missing_since is None: self.missing_since = now
            if now - self.missing_since >= 15:
                self.opened = None; self.pending = None; self.previous = {}
            return False
        if self.missing_since is not None and now - self.missing_since >= 15:
            self.opened = None; self.pending = None; self.previous = {}
        self.missing_since = None
        key = connection_key(row)
        old = self.previous
        if old:
            same_boot = not old.get('boot_id') or old['boot_id'] == self.boot_id
            same_phone = (old.get('browser_connection_id') == key or
                          (not old.get('browser_connection_id') and old.get('usb_path') == row['usb'] and
                           old.get('phone_serial_suffix') == row['serial'][-4:]))
            if same_boot and same_phone and old.get('browser_opened'):
                self.opened = key; self.open_count = old.get('browser_open_count', 0)
            self.previous = {}
        if self.pending != key:
            self.pending = key; self.stable_since = now
        return (self.opened != key and now - self.stable_since >= 3 and
                now - self.last_attempt >= 10)

    def attempted(self): self.last_attempt = self.clock()

    def launched(self, row):
        self.opened = connection_key(row); self.open_count += 1

def physical_adb_devices():
    found = []
    for path in Path('/sys/bus/usb/devices').iterdir():
        if not (path / 'idVendor').is_file(): continue
        try:
            serial = (path / 'serial').read_text().strip()
            if not SERIAL.fullmatch(serial): continue
            interfaces = path.parent.glob(path.name + ':*')
            for interface in interfaces:
                triple = [(interface / name).read_text().strip().lower() for name in
                          ('bInterfaceClass', 'bInterfaceSubClass', 'bInterfaceProtocol')]
                if triple == ['ff', '42', '01']:
                    found.append(dict(serial=serial, usb=path.name)); break
        except OSError: continue
    return found


def service_idle():
    try:
        token=(BASE/'runtime/dashboard_token').read_text().strip()
        req=urllib.request.Request('http://127.0.0.1:8765/api/display-state',headers={'X-Access-Token':token})
        with urllib.request.urlopen(req,timeout=2) as reply:value=json.load(reply)
        privacy=value.get('privacy') or {};worker=value.get('worker') or {}
        return (value.get('state') in ('device_confirm','idle','failed_wait','exhausted')
                and not value.get('session') and not value.get('busy')
                and worker.get('recording_active') is not True
                and worker.get('state') not in ('starting','recording','stopping','saving','checking','finalizing','postprocessing')
                and privacy.get('required') is False and privacy.get('stop_confirmed') is True
                and privacy.get('phase')=='idle')
    except Exception:return False



class TransportRecovery:
    def __init__(self, clock=time.monotonic):
        self.clock=clock; self.offline_since={}; self.next_retry={}; self.attempts={}
        self.missing_since=None

    def recover(self, rows, physical):
        now=self.clock(); events=[]
        offline={row['serial'] for row in rows if row['state']=='offline'}
        reported={row['serial'] for row in rows}
        unavailable=offline|({row['serial'] for row in physical}-reported)
        for serial in set(self.offline_since)-unavailable:
            self.offline_since.pop(serial,None);self.next_retry.pop(serial,None);self.attempts.pop(serial,None)
        for serial in unavailable:self.offline_since.setdefault(serial,now)
        for serial in sorted(offline):
            since=self.offline_since[serial]
            if now-since<8 or now<self.next_retry.get(serial,0):continue
            count=self.attempts.get(serial,0);self.attempts[serial]=count+1
            self.next_retry[serial]=now+(10,30,60)[min(count,2)]
            try:
                adb(['-s',serial,'reconnect'],timeout=5)
                events.append('offline_peer_reconnect')
            except Exception:events.append('offline_peer_retry_failed')
        # Server-wide restart cannot be guarded against a simultaneous capture
        # start with the current lifecycle locks. Keep retrying individual offline
        # peers, but never kill/start the server here.
        return events



class PhoneMonitor:
    def __init__(self, saved=None, boot_id='', clock=time.monotonic):
        self.clock=clock;self.boot_id=boot_id;self.saved=saved or {};self.peers={}
        self.recovery=TransportRecovery(clock)

    def unknown(self, reason):
        # An enumeration failure is neither an empty list nor a confirmed
        # disconnect. Preserve per-peer gates until enumeration succeeds.
        return dict(version=VERSION,pi_id=pi_id(),state='adb_enumeration_unknown',
                    connected=False,connection_known=False,binding_required=False,
                    reason=reason,message='USB调试状态暂不可确认，正在检查连接')

    def refresh(self, rows, physical=None):
        # Every authorized physical USB peer gets its own port mapping. A
        # second phone does not prevent the first one from connecting.
        state=dict(version=VERSION,pi_id=pi_id(),url=URL,transport='USB_ADB',binding_required=False)
        seen=set();reports=[]
        for row in rows:
            serial=row['serial'];seen.add(serial)
            if serial not in self.peers:
                old=self.saved if self.saved.get('phone_serial_suffix')==serial[-4:] else {}
                old=next((v for v in self.saved.get('devices',[]) if v.get('browser_connection_id')==connection_key(row)),old)
                self.peers[serial]=(ReverseTunnel(self.clock),BrowserLaunchGate(old,self.boot_id,self.clock))
            tunnel,gate=self.peers[serial]
            report=dict(phone_serial_suffix=serial[-4:],usb_path=row['usb'],connected=False)
            if row['state']=='unauthorized':
                tunnel.offline();gate.observe(None)
                report.update(state='authorization_required',message='请在此设备上允许USB调试')
            elif row['state']!='device':
                tunnel.offline();gate.observe(None)
                report.update(state='transport_offline',message='USB调试断线，正在恢复连接')
            else:
                try:
                    tunnel.ensure(row)
                    should_open=gate.observe(row)
                    if should_open:
                        gate.attempted()
                        try:
                            if launch_browser(row):gate.launched(row)
                        except Exception:report['browser_message']='可手动打开 '+URL
                    report.update(state='connected',connected=True,reverse_refresh_count=tunnel.refresh_count,
                                  reverse_reason=tunnel.reason,browser_opened=gate.opened==connection_key(row),
                                  browser_connection_id=connection_key(row),browser_open_count=gate.open_count,
                                  boot_id=self.boot_id,message='USB网页通道正常，无需固定绑定')
                except Exception:
                    tunnel.offline();gate.observe(None)
                    report.update(state='transport_error',message='端口映射未确认，正在重试')
            reports.append(report)
        for serial in set(self.peers)-seen:
            tunnel,gate=self.peers[serial];tunnel.offline();gate.observe(None)
            if tunnel.missing_since is not None and self.clock()-tunnel.missing_since>300:
                del self.peers[serial]
        state.update(devices=reports,connected=any(v['connected'] for v in reports),
                     connected_count=sum(v['connected'] for v in reports))
        if len(reports)==1:state.update(reports[0])
        elif reports:
            states={report['state'] for report in reports}
            if state['connected']:
                state.update(state='connected',message='已连接 %d 台USB设备'%state['connected_count'])
            elif states=={'authorization_required'}:
                state.update(state='waiting_authorization',message='USB设备等待授权调试')
            elif states=={'transport_offline'}:
                state.update(state='transport_offline',message='USB调试设备离线，正在恢复')
            elif states=={'transport_error'}:
                state.update(state='transport_error',message='USB端口映射未确认，正在重试')
            else:
                state.update(state='mixed',message='USB设备状态不同，请查看各设备详情')
        else:
            role=data_role_state()
            state.update(state='phone_missing',message='等待USB调试设备接入；请在设备上允许调试')
            if physical:state.update(state='usb_detected_adb_missing',message='USB设备已识别，调试通道正在恢复')
            elif role.get('state') not in (None,'host'):
                state.update(state='usb_data_role_pending',message=role.get('message','等待USB主机端口恢复'))
        state['recovery_events']=self.recovery.recover(rows,physical or [])
        return state


def monitor():
    previous=None;last_write=-1e9;enumeration_failures=0
    try:saved=json.loads(STATE.read_text())
    except (OSError,ValueError):saved={}
    try:boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:boot_id=''
    manager=PhoneMonitor(saved,boot_id)
    while True:
        try:
            physical=physical_adb_devices()
            try: rows=devices()
            except subprocess.TimeoutExpired: rows=None;reason='adb_enumeration_timeout'
            except Exception: rows=None;reason='adb_enumeration_failed'
            if rows is None:
                enumeration_failures+=1
                state=manager.unknown(reason)
            else:
                enumeration_failures=0
                state=manager.refresh(rows,physical)
        except Exception:
            for tunnel,gate in manager.peers.values():tunnel.offline();gate.observe(None)
            state=dict(version=VERSION,pi_id=pi_id(),state='transport_error',connected=False,
                       binding_required=False,message='USB调试通道暂不可用，正在重试')
        stamp=json.dumps(state,ensure_ascii=False,sort_keys=True);now=time.monotonic()
        if stamp!=previous or now-last_write>=30:
            atomic(STATE,dict(state,updated_at_unix=time.time()));last_write=now
            if stamp!=previous:print(stamp,flush=True)
            previous=stamp
        time.sleep((1,2,4,8,15)[min(enumeration_failures,4)] if enumeration_failures else 1)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['list','connect','status','monitor','bind'])
    parser.add_argument('--serial');args=parser.parse_args()
    if args.action=='monitor':monitor();return
    rows=devices()
    if args.serial:rows=[row for row in rows if row['serial']==args.serial]
    result=rows if args.action=='list' else PhoneMonitor().refresh(rows,physical_adb_devices())
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
