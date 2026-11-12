"""Bounded, on-demand diagnostics. Never opens a sensor or changes settings."""
import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import zipfile

USB_NAME = re.compile(r'\d+-\d+(?:\.\d+)*\Z')
SECRET = re.compile(r'(?i)(?:token|password|passwd|secret|authorization|api[-_ ]?key|'
                    r'access[-_ ]?code|reference[-_ ]?key|signature|credential|passphrase|\bpsk\b|\bnonce\b)')
KERNEL_EVENT = re.compile(r'(?i)(not enough bandwidth|device descriptor read|'
                          r'usb disconnect|reset .{0,80}usb device|usb.{0,100}(?:error -|failed))')
MAX_JSON = 384 * 1024
MAX_BUNDLE = 2 * 1024 * 1024


def scrub_text(value):
    lines = str(value).splitlines()
    return '\n'.join('[含凭据字段的日志行已省略]' if SECRET.search(line) else line[:2048] for line in lines)


def clean(value, depth=0):
    if depth > 10:return '[内容层级已截断]'
    if isinstance(value, dict):
        return {str(k)[:128]: '[已隐藏]' if SECRET.search(str(k)) else clean(v, depth+1)
                for k, v in list(value.items())[:200]}
    if isinstance(value, (list, tuple)):return [clean(v, depth+1) for v in value[:200]]
    if isinstance(value, str):return scrub_text(value[:8192])
    if isinstance(value, float) and not math.isfinite(value):return None
    if value is None or isinstance(value, (int, float, bool)):return value
    return str(value)[:256]


def read_small(path, limit=512):
    descriptor = None
    try:
        if Path(path).is_symlink():return None
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):return None
        raw = os.read(descriptor, limit+1)
        if len(raw) > limit:return None
        return raw.decode('utf-8', 'replace').strip()
    except OSError:return None
    finally:
        if descriptor is not None:os.close(descriptor)


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):return None


def usb_topology(devices, sys_root=Path('/sys')):
    sys_root = Path(sys_root); directory = sys_root/'bus/usb/devices'
    rows = []; roots = {}; error = None
    try:entries = sorted(directory.iterdir(), key=lambda p:p.name)[:256]
    except OSError:entries = []; error = 'USB 系统信息暂不可读取'
    for path in entries:
        if re.fullmatch(r'usb\d+', path.name):
            roots[path.name[3:]] = number(read_small(path/'speed'))
        if not USB_NAME.fullmatch(path.name):continue
        vendor, product = read_small(path/'idVendor'), read_small(path/'idProduct')
        if not vendor or not product:continue
        bus = read_small(path/'busnum') or path.name.split('-')[0]
        bus = str(int(bus)) if bus.isdecimal() else bus
        drivers = set()
        try:
            for interface in list(path.glob(path.name+':*'))[:32]:
                driver = interface/'driver'
                if driver.is_symlink():drivers.add(driver.resolve().name)
        except OSError:pass
        rows.append(dict(port=path.name, bus=bus, device=read_small(path/'devnum'),
            speed_mbps=number(read_small(path/'speed')), vendor_id=vendor, product_id=product,
            name=(read_small(path/'product') or 'USB 设备')[:160],
            device_class=read_small(path/'bDeviceClass'), drivers=sorted(drivers)))
    by_port = {row['port']:row for row in rows}; roles = []
    for device in devices[:32]:
        name = str(device.get('name', '')); config = device.get('config') or {}
        # Headband cameras arrive over the camd socket, not USB or ROS.
        transport = ('camd' if config.get('camd') else
                     'character_device' if name=='board_imu' and config.get('device')=='/dev/icm20602' else
                     'ros' if config.get('topic') else 'usb')
        role = dict(name=name, label=device.get('label', name), connected=device.get('connected') is True,
                    transport=transport, port=None, bus=None, speed_mbps=None)
        if config.get('camd'):
            role['camd'] = str(config['camd'])[:64]
            role['socket'] = '/run/camd.sock'
        elif transport=='character_device':
            role['device'] = '/dev/icm20602'
        elif config.get('topic'):
            role['topic'] = str(config['topic'])[:256]
        elif role['connected']:
            target = Path(str(device.get('device') or config.get('port') or config.get('device') or '/missing')).resolve()
            subsystem = 'video4linux' if re.fullmatch(r'video\d+', target.name) else 'tty' if target.name.startswith(('ttyACM','ttyUSB')) else None
            if subsystem:
                path = (sys_root/'class'/subsystem/target.name/'device').resolve()
                for parent in (path, *path.parents):
                    if parent.name in by_port:
                        usb = by_port[parent.name]
                        role.update(port=usb['port'], bus=usb['bus'], speed_mbps=usb['speed_mbps'])
                        break
        roles.append(role)
    warnings = []; groups = {}
    for role in roles:
        if role['connected'] and role['name'].startswith('cam_') and role['transport']=='usb' and role['bus']:
            groups.setdefault(role['bus'], []).append(role)
    for bus, cameras in groups.items():
        speed = roots.get(bus)
        if len(cameras)>1 and speed is not None and speed<=480:
            warnings.append(dict(code='shared_usb2_camera_bus', level='notice', roles=[c['name'] for c in cameras],
                message='多路 USB 视频共用总线 '+bus+'（协商总速率 '+str(int(speed))+' Mb/s），可能发生带宽竞争。请核对 USB 3.0 线材与独立接口。'))
    return dict(devices=rows, roles=roles, root_bus_speed_mbps=roots, warnings=warnings, error=error,
        rate_note='速率是 USB 协商速率，不是实测吞吐量；同一总线的设备共享带宽。头环 camd 与板载 IMU 不占用 USB 视频总线。')


class DeviceDiagnostics:
    def __init__(self, owner, sys_root=Path('/sys'), runner=subprocess.run, clock=time.time):
        self.owner=owner;self.sys_root=Path(sys_root);self.runner=runner;self.clock=clock
        self.slots=threading.BoundedSemaphore(1);self.cached=None;self.cached_at=-1e9

    def command(self, args):
        try:
            result=self.runner(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2,
                               check=False, env=dict(os.environ, LC_ALL='C'))
            raw=result.stdout
            if isinstance(raw,bytes):raw=raw.decode('utf-8','replace')
            if result.returncode:return '', '日志不可读取或当前用户无权限'
            return scrub_text(raw[-64*1024:]), None
        except (OSError, subprocess.SubprocessError):return '', '日志读取超时或不可用'

    def context(self):
        with self.owner.lock:
            source=copy.deepcopy({k:self.owner.snapshot.get(k) for k in
                ('state','selected_devices','devices','task_list','storage','session','worker','errors','success_count')})
            review_root=self.owner.review_root
        privacy=self.owner.privacy.snapshot()
        source['privacy']={k:privacy.get(k) for k in ('phase','required','stop_confirmed','generation')}
        source['worker']={k:(source.get('worker') or {}).get(k) for k in ('state','pid','recording_active','progress','error')}
        source['devices']=[dict(name=d.get('name'),label=d.get('label'),connected=d.get('connected'),
            present=d.get('present'),role_confirmed=d.get('role_confirmed'),status=d.get('status'),error=d.get('error'),
            device=d.get('device'),config={k:d.get('config',{}).get(k) for k in ('topic','port','device','camd') if d.get('config',{}).get(k)})
            for d in (source.get('devices') or [])[:32]]
        return source,review_root

    def snapshot(self):
        source,_=self.context();now=self.clock()
        signature=json.dumps(source['devices'],sort_keys=True,ensure_ascii=False)
        if not self.cached or now-self.cached_at>3 or self.cached[0]!=signature:
            usb=usb_topology(source['devices'],self.sys_root)
            log,error=self.command(['journalctl','-k','-b','--since','15 minutes ago','-n','160','--no-pager','-o','cat'])
            events=[line[:320] for line in log.splitlines() if KERNEL_EVENT.search(line)][-20:]
            self.cached=(signature,usb,events,error);self.cached_at=now
        _,usb,events,kernel_error=self.cached
        source['server_time']=now
        for device in source['devices']:
            if str(device.get('name','')).startswith('cam_'):
                try:age=now-(self.owner.preview/(device['name']+'.jpg')).stat().st_mtime
                except OSError:age=None
                device['preview_age_seconds']=round(age,3) if age is not None else None
                device['preview_fresh']=age is not None and 0<=age<3
        signals={}
        for name in ('eeg','emg_left','emg_right'):
            raw=read_small(self.owner.preview/(name+'.json'),192*1024)
            if raw is None:signals[name]={'available':False};continue
            try:
                value=json.loads(raw);stamp=number(value.get('time'))
                signals[name]={k:value.get(k) for k in ('time','count','units','mode','filter_preset','preview_skipped_updates')}
                signals[name].update(available=True,age_seconds=round(now-stamp,3) if stamp is not None else None)
            except (ValueError,TypeError):signals[name]={'available':False}
        trigger={}
        for index in range(4):
            path=self.sys_root/'firmware/devicetree/base/camera-sync2'/('cam'+str(index)+'-freq')
            try:
                raw=path.read_bytes()
                if len(raw)==4:trigger['cam'+str(index)]=int.from_bytes(raw,'big')
            except OSError:pass
        system=dict(kernel=os.uname().release if hasattr(os,'uname') else os.name,
            load_average=list(os.getloadavg()) if hasattr(os,'getloadavg') else None,
            headband_trigger_hz_from_device_tree=trigger,
            trigger_note='设备树触发配置；实际话题帧率需单独测量。')
        return clean(dict(version=1,created_at_unix=now,status=source,usb=usb,system=system,kernel_events=events,
            kernel_log_note=kernel_error,signals=signals,
            scope_note='手动读取设备与预览状态；不启动采集，不修改设备参数。'))

    def bundle(self, snapshot):
        files={'diagnostics.json':json.dumps(snapshot,ensure_ascii=False,indent=2).encode()};skipped=[]
        log,error=self.command(['journalctl','-u','embodied-pi-collect.service','--since','30 minutes ago','-n','100','--no-pager','-o','cat'])
        files['service-recent.log']=(log or error or '近期无服务日志').encode()
        _,review=self.context()
        if review:
            root=Path(review)
            for name in ('qc_report.json','capture_summary.json','stop_gate.json'):
                path=root/name;raw=read_small(path,MAX_JSON)
                if raw is None:skipped.append(name+'：不存在、不是普通文件或超过大小限制');continue
                try:value=json.loads(raw)
                except (ValueError,TypeError):skipped.append(name+'：JSON 无效');continue
                files['latest-finalized-session/'+name]=json.dumps(clean(value),ensure_ascii=False,indent=2).encode()
        files['README.txt']=('本包仅包含设备、USB、预览计数摘要、近期日志与已有的采集 QC 报告。\n'
            '不包含参考图、相机图像、原始波形、Wi-Fi 配置、访问码文件或环境变量。\n'
            '预览间断不等于正式文件丢样，USB 协商速率不等于可用吞吐量。\n'
            '没有历史 QC 文件时，仍可使用其余诊断信息。\n').encode()
        manifest=dict(version=1,created_at_unix=self.clock(),skipped=skipped,
            files={name:dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()) for name,raw in files.items()})
        files['manifest.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,raw in files.items():archive.writestr(name,raw)
        payload=output.getvalue()
        if len(payload)>MAX_BUNDLE:raise ValueError('诊断包超过大小限制，请联系维护人员')
        return payload


def export_event_trace(base, destination):
    """Export only schema-validated event records, never journal or session files."""
    from diagnostic_events import export_events
    payload=export_events(Path(base)/'runtime/diagnostics')
    fd=os.open(str(destination),os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'wb') as output:output.write(payload)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Export only sanitized diagnostic events')
    parser.add_argument('--base',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    export_event_trace(args.base,args.output)
