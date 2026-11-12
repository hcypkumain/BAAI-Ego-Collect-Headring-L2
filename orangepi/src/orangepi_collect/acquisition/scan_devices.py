"""Read-only readiness scan: probe camd, the IMU character device and EMG framing.

Saves no samples. The headband cameras and the board IMU no longer go through
ROS (retired in EgoRecord v0.1.10).
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'common'))
from project_paths import BASE, devices
from orangepi_collect.recorders.camera.uvc_source import probe
from orangepi_collect.recorders.camera import camd_source
import imu_source
import concurrent.futures,json,os,select,termios,time,fcntl,copy,struct,subprocess,tempfile,signal


def camd_camera_status(name):
    """Per-camera STATUS entry, keyed by our cam_ros_* device name."""
    cams=camd_source.status().get('cams') or {}
    camera=camd_source.CAMERA_NAMES.get(name,name)
    entry=cams.get(camera)
    if entry is None:
        raise RuntimeError('camd 未提供相机 %s' % camera)
    return entry


def imu_probe(device=None,seconds=1.0):
    """Read a few ICM20602 frames to prove the device streams and is readable."""
    device=device or imu_source.DEVICE
    if not Path(device).exists():
        return dict(ready=False,present=False,rate_hz=None)
    frame=struct.Struct('<qq7i4x')
    fd=os.open(device,os.O_RDONLY|os.O_NONBLOCK)
    try:
        buf=bytearray();stamps=[];end=time.monotonic()+seconds
        while time.monotonic()<end and len(stamps)<200:
            if not select.select([fd],[],[],0.2)[0]:
                continue
            chunk=os.read(fd,frame.size*16)
            if not chunk:
                break
            buf.extend(chunk)
            while len(buf)>=frame.size:
                row=bytes(buf[:frame.size]);del buf[:frame.size]
                seconds_field,nanoseconds=frame.unpack(row)[:2]
                stamps.append(seconds_field+nanoseconds*1e-9)
        if len(stamps)<2:
            return dict(ready=False,present=True,rate_hz=None)
        span=stamps[-1]-stamps[0]
        return dict(ready=True,present=True,
                    rate_hz=round((len(stamps)-1)/span,1) if span>0 else None)
    finally:
        os.close(fd)

def serial_probe(port):
    fd=os.open(port,os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK)
    original=None;exclusive=False
    try:
        fcntl.ioctl(fd,termios.TIOCEXCL)
        exclusive=True
        original=termios.tcgetattr(fd)
        attrs=copy.deepcopy(original)
        attrs[0]=attrs[1]=attrs[3]=0
        attrs[2]=termios.CS8|termios.CREAD|termios.CLOCAL
        attrs[4]=attrs[5]=termios.B921600
        attrs[6][termios.VMIN]=attrs[6][termios.VTIME]=0
        termios.tcsetattr(fd,termios.TCSANOW,attrs)
        buf=b'';count=0;end=time.monotonic()+3
        while time.monotonic()<end:
            if select.select([fd],[],[],.1)[0]:
                data=os.read(fd,8192)
                if not data:raise OSError('串口已断开')
                count+=len(data)
                buf=(buf+data)[-16384:]
                index=buf.find(b'\xd2\xd2\xd2\xaa')
                if index>=0 and len(buf)-index>=29:return dict(ready=True,bytes_read=count)
        return dict(ready=False,bytes_read=count)
    finally:
        try:
            if original is not None:termios.tcsetattr(fd,termios.TCSANOW,original)
        finally:
            try:
                if exclusive:fcntl.ioctl(fd,termios.TIOCNXCL)
            finally:os.close(fd)

def serial_ready(port):
    return serial_probe(port)['ready']

def check(kind,name,item):
    started=time.monotonic();started_unix_ms=round(time.time()*1000)
    def observed(value):
        return dict(value,probe_started_unix_ms=started_unix_ms,probe_elapsed_ms=max(0,round((time.monotonic()-started)*1000)))
    path=item.get('port',item.get('device'))
    discovery=item.get('discovery',{})
    result=dict(name=name,label=item['label'],connected=False,present=False,
                device=path,role_confirmed=discovery.get('role_confirmed',True),config=item)
    try:
        if path and (discovery.get('present') is False or not Path(path).exists()):
            return observed(dict(result,status='missing',error='未发现匹配设备，请检查连接'))
        result['present']=bool(path)
        if kind=='eeg':
            from eeg_bridge import probe as eeg_probe
            details=eeg_probe(item); ok=details['ready']
            result['sample_rate_hz']=details['sample_rate_hz']
            result['startup_diagnostics']=details.get('startup_diagnostics',{})
        elif kind=='emg':
            details=serial_probe(item['port']);ok=details['ready']
            result['bytes_read']=details['bytes_read']
        elif kind=='wrist':
            result['probe']={};ok=probe(item,report=result['probe'])
        elif kind=='camera':
            # Headband cameras come from camd now; STATUS reports per-camera
            # readiness and live frame rate without opening a subscription.
            entry=camd_camera_status(name)
            ok=bool(entry.get('snapshot_ready'))
            result['present']=True
            result['fps']=entry.get('fps')
            result['service_ready']=bool(entry.get('snapshot_ready'))
            result['fresh_frame']=False
            if entry.get('h264_stream'):
                result['error']='该路相机已被其他程序占用'
                ok=False
            elif ok:
                _,_,first,_=camd_source.snapshot(name,timeout=1.5)
                time.sleep(.08)
                width,height,stamp,_=camd_source.snapshot(name,timeout=1.5)
                if not first or stamp<=first:raise RuntimeError('相机未返回新的快照帧')
                result.update(fresh_frame=True,probe_scope='camd_snapshot',frame_width=width,frame_height=height)
        else:
            details=imu_probe()
            ok=details['ready']
            result['present']=details['present']
            result['sample_rate_hz']=details.get('rate_hz')
        result.update(connected=ok,present=result['present'] or ok,status='ready' if ok else 'no_data')
        if not ok:
            result['error']=('接收器已识别，但未输出数据，请检查肌电本体是否开机并连接接收器' if kind=='emg' and not result['bytes_read']
                             else '已收到串口数据，但未识别到当前肌电协议' if kind=='emg'
                             else '相机已被系统识别，但 %.1f 秒探测仅收到 %d 帧有效 JPEG（需要至少 10 帧），可重新扫描' % (result['probe']['window_seconds'],result['probe']['received_frames']) if kind=='wrist'
                             else '设备已识别，但没有收到有效图像')
        return observed(result)
    except Exception as exc:return observed(dict(result,status='error',error=type(exc).__name__+': '+str(exc)))

def record_wrist_probe_diagnostics(results):
    """Keep bounded failed-start evidence across later successful rescans."""
    rows=[row for row in results if row['name'] in ('cam_left_wrist','cam_right_wrist')]
    if not any(row.get('present') and not row.get('connected') for row in rows):return
    directory=BASE/'runtime/diagnostics/wrist-startup'
    try:
        directory.mkdir(parents=True,exist_ok=True)
        directory.resolve().relative_to((BASE/'runtime/diagnostics').resolve())
        if directory.is_symlink():return
        path=directory/('scan-%020d.json'%time.time_ns());temporary=path.with_suffix('.tmp')
        try:boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:boot_id=None
        with temporary.open('x',encoding='utf-8') as stream:
            json.dump(dict(schema_version=1,at_unix=time.time(),boot_id=boot_id,cameras=rows),stream,ensure_ascii=False)
            stream.flush();os.fsync(stream.fileno())
        temporary.replace(path)
        for old in sorted(directory.glob('scan-*.json'))[:-30]:old.unlink()
        fd=os.open(str(directory),os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    except (OSError,ValueError) as error:
        print('Wrist probe diagnostics unavailable: '+str(error),file=sys.stderr,flush=True)


def record_eeg_probe_diagnostics(results):
    rows=[row for row in results if row['name']=='eeg' and row.get('present')]
    if not rows:return
    directory=BASE/'runtime/diagnostics/eeg-startup'
    try:
        directory.mkdir(parents=True,exist_ok=True)
        if directory.is_symlink():return
        directory.resolve().relative_to((BASE/'runtime/diagnostics').resolve())
        path=directory/('scan-%020d.json'%time.time_ns())
        # Readiness proof only; no EEG signal or credential values.
        brief=[{key:row.get(key) for key in ('name','device','present','connected','status','error','startup_diagnostics')} for row in rows]
        with path.open('x',encoding='utf-8') as stream:
            json.dump(dict(at_unix=time.time(),eeg=brief),stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        for old in sorted(directory.glob('scan-*.json'))[:-30]:old.unlink()
    except (OSError,ValueError) as error:print('EEG probe diagnostics unavailable: '+str(error),file=sys.stderr,flush=True)


def probe_rows(jobs, timeout=10.0, per_device=8.0):
    """Bound each probe and preserve completed rows if a peer hangs.

    Subprocesses inherit the single scan process group; its controller performs
    final group cleanup, including any descendants that outlive their probe.
    """
    deadline=time.monotonic()+timeout
    children=[]
    for kind,name,item in jobs:
        output=tempfile.TemporaryFile(mode='w+t',encoding='utf-8')
        with tempfile.TemporaryFile(mode='w+t',encoding='utf-8') as source:
            json.dump([kind,name,item],source);source.seek(0)
            process=subprocess.Popen([sys.executable,__file__,'--one'],stdin=source,
                stdout=output,stderr=subprocess.DEVNULL,text=True)
        started=time.monotonic()
        children.append((process,kind,name,item,output,started,min(deadline,started+per_device)))
    rows={}
    while len(rows)<len(children):
        now=time.monotonic()
        for index,(process,kind,name,item,output,started,expires) in enumerate(children):
            if index in rows:continue
            if process.poll() is None:
                if now<expires:continue
                try:process.terminate()
                except ProcessLookupError:pass
                rows[index]=dict(name=name,label=item['label'],connected=False,present=None,status='timeout',probe_elapsed_ms=max(0,round((now-started)*1000)),error='本设备探测超时，状态待确认')
                continue
            try:
                output.seek(0);row=json.loads(output.read())
                if not isinstance(row,dict) or row.get('name')!=name or type(row.get('connected')) is not bool:raise ValueError('invalid probe response')
                rows[index]=row
            except (OSError,ValueError):
                rows[index]=dict(name=name,label=item['label'],connected=False,present=None,status='error',probe_elapsed_ms=max(0,round((time.monotonic()-started)*1000)),error='探测进程异常')
        if len(rows)<len(children):time.sleep(.025)
    for process,kind,name,item,output,started,expires in children:
        if process.poll() is None:
            try:process.wait(timeout=.05)
            except subprocess.TimeoutExpired:pass
        output.close()
    return [rows[i] for i in range(len(children))]


if __name__=='__main__':
    from acquisition_time_lock import acquire
    acquire()
    from scan_registry import require_member
    owner=require_member()
    if sys.argv[1:]==['--one']:
        kind,name,item=json.load(sys.stdin)
        print(json.dumps(check(kind,name,item),ensure_ascii=False),flush=True)
        sys.exit(0)
    # Keep the registered leader alive while the controller escalates a
    # timeout; children still receive TERM, then KILL targets this same group.
    signal.signal(signal.SIGTERM,lambda *_:None)
    config=devices()
    jobs=[('emg',name,item) for name,item in config['emg'].items()]
    jobs += [('camera','cam_ros_'+name,item) for name,item in config['cameras'].items()]
    jobs += [('imu','board_imu',config['board_imu'])]
    jobs += [('wrist',name,item) for name,item in config.get('wrist_cameras',{}).items()]
    if config.get('eeg',{}).get('enabled',False):jobs += [('eeg','eeg',config['eeg'])]
    results=probe_rows(jobs)
    request=json.loads(os.environ.get('ORANGEPI_SCAN_REQUEST','null'))
    if not isinstance(request,dict) or set(request)!={'id','fingerprint'}:raise ValueError('缺少扫描请求身份')
    from scan_result import encode
    envelope=encode(owner['run'],owner['nonce'],request['id'],request['fingerprint'],results)
    record_wrist_probe_diagnostics(results)
    record_eeg_probe_diagnostics(results)
    print(envelope,flush=True)
    if any(row.get('status')=='timeout' for row in results):
        # Preserve the process-group leader until its controller performs
        # bounded cleanup of all slow descendants.
        while True:time.sleep(1)
