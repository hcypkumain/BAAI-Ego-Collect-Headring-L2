#!/usr/bin/env python3
"""Plan/install the reviewed Pi files. Never starts or stops collection."""
import argparse,datetime,os,platform,re,shutil,subprocess,tempfile
from pathlib import Path

USB_ROOT=Path('/home/orangepi/maintenance/usb-phone-adb')

def plan(root,include_vendor,include_lan_time=False):
    result=[]
    for source in sorted((root/'services/phone-media').rglob('*')):
        if source.is_file() and '__pycache__' not in source.parts and source.suffix in ('.py','.wav'):
            result.append((source,Path('/opt/pi-phone-media')/source.relative_to(root/'services/phone-media'),False))
    result.append((root/'services/usb-phone-link/usb_phone_link.py',USB_ROOT/'usb_phone_link.py',False))
    units=['camd.service','embodied-pi-collect.service','embodied-pi-diagnostics.service','pi-phone-media.service','usb-phone-link.service']
    if include_lan_time:
        units+=['headring-lan-time.service','headring-lan-time-refresh.service','headring-lan-time-refresh.timer']
        result.append((root/'deployment/lan-time/headring_lan_time.py',Path('/usr/local/sbin/headring-lan-time'),False))
        if not Path('/etc/headring-lan-time.json').exists():
            result.append((root/'deployment/lan-time/headring-lan-time.example.json',Path('/etc/headring-lan-time.json'),False))
    for name in units:
        unit=root/'deployment/systemd'/name
        result.append((unit,Path('/etc/systemd/system')/name,True))
        for source in sorted(unit.with_name(unit.name+'.d').glob('*.conf')):
            result.append((source,Path('/etc/systemd/system')/(name+'.d')/source.name,True))
    for name in ('91-pi-wrist-camera-power.rules','99-icm20602.rules','71-pi-auto-android.rules'):
        result.append((root/'deployment/udev'/name,Path('/etc/udev/rules.d')/name,False))
    if include_vendor:
        vendor=root/'vendor/pi-runtime/egorecord'
        for name in ('camd/camd.conf','camd/gen_camd_conf.sh','camd/build/camd','camd/lib/librkaiq_d',
                     'config/sensor_iq/sc233hgs_default_default.json'):
            result.append((vendor/name,Path('/opt/egorecord')/name,False))
        result.append((root/'vendor/pi-runtime/opi-headphone-audio/guarded-paplay',Path('/opt/opi-headphone-audio/guarded-paplay'),False))
    return result

def usb_adb():
    bundled=USB_ROOT/'root/usr/bin/adb'
    return bundled if bundled.is_file() else Path(shutil.which('adb') or '/usr/bin/adb')

def initialize_usb(adb):
    import pwd
    user=pwd.getpwnam('orangepi')
    for directory in [USB_ROOT,USB_ROOT/'keys']:
        directory.mkdir(parents=True,exist_ok=True)
        os.chown(str(directory),user.pw_uid,user.pw_gid)
    os.chmod(str(USB_ROOT/'keys'),0o700)
    key=USB_ROOT/'keys/adbkey'
    if not key.exists():
        env=dict(os.environ,LD_LIBRARY_PATH=str(USB_ROOT/'root/usr/lib/aarch64-linux-gnu/android')+':'+str(USB_ROOT/'root/usr/lib/aarch64-linux-gnu'))
        subprocess.run(['runuser','-u','orangepi','--',str(adb),'keygen',str(key)],env=env,check=True,timeout=15)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--with-vendor-runtime',action='store_true')
    parser.add_argument('--with-lan-time',action='store_true',help='Install guarded LAN time helper/units; preserve an existing time-server setting')
    args=parser.parse_args();root=args.project_root.resolve()
    if not (root/'orangepi/scripts/run.sh').is_file():parser.error('project root does not contain the collector')
    operations=plan(root,args.with_vendor_runtime,args.with_lan_time)
    for source,target,_ in operations:
        if not source.is_file() or source.is_symlink():parser.error('missing/non-regular source: '+str(source))
        print(str(source.relative_to(root))+' -> '+str(target))
    if not args.apply:
        print('Plan only. No files or services changed.');return
    if platform.system()!='Linux' or platform.machine() not in ('aarch64','arm64'):
        parser.error('apply requires the compatible Linux aarch64 Pi')
    if os.geteuid()!=0:parser.error('apply requires sudo')
    adb=usb_adb()
    if not adb.is_file():parser.error('Install the matching system adb package or retain the existing private adb runtime first')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(root)):parser.error('use an absolute deployment path without spaces or shell characters')
    backup=Path('/var/backups/baai-ego-collect')/datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    for source,target,unit in operations:
        if target.is_symlink():raise RuntimeError('Refusing to overwrite a symlink: '+str(target))
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            saved=backup/target.relative_to('/');saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,saved)
        raw=source.read_bytes()
        if unit:
            text=raw.decode('utf-8')
            for old in ('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2','/home/orangepi/Desktop/Embodied-Brain-Collect-main'):
                text=text.replace(old,str(root))
            raw=text.encode('utf-8')
        fd,temporary=tempfile.mkstemp(prefix='.'+target.name+'-',dir=str(target.parent))
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            executable=raw.startswith(b'#!') or source.name in ('camd','guarded-paplay','librkaiq_d')
            os.chmod(temporary,0o755 if executable else 0o644);os.replace(temporary,target)
            directory=os.open(str(target.parent),os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
    initialize_usb(adb)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['udevadm','control','--reload-rules'],check=True)
    print('Installed. Existing-file backups: '+str(backup))
    print('No services were started/stopped; no USB events were triggered.')

if __name__=='__main__':main()
