#!/usr/bin/env python3
"""Install reviewed Pi files and mandatory guarded USB-host policy; never starts capture."""
import argparse,datetime,importlib.util,os,platform,re,shutil,stat,subprocess,tempfile
from pathlib import Path

USB_ROOT=Path('/home/orangepi/maintenance/usb-phone-adb')

def render_systemd_unit(raw,root):
    """Render only known legacy project-root prefixes without overlapping replacements."""
    text=raw.decode('utf-8')
    marker='__HEADRING_PROJECT_ROOT__'
    if marker in text:raise ValueError('unit source contains reserved renderer marker')
    roots=('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2',
           '/home/orangepi/Desktop/Embodied-Brain-Collect-main')
    for old in sorted(roots,key=len,reverse=True):
        text=re.sub(re.escape(old)+r'(?![A-Za-z0-9._-])',marker,text)
    return text.replace(marker,str(root)).encode('utf-8')

def plan(root,include_vendor,include_lan_time=False):
    result=[]
    result.append((root/'deployment/tmpfiles/headring-acquisition-time.conf',Path('/etc/tmpfiles.d/headring-acquisition-time.conf'),False))
    for source in sorted((root/'services/phone-media').rglob('*')):
        if source.is_file() and '__pycache__' not in source.parts and source.suffix in ('.py','.wav'):
            result.append((source,Path('/opt/pi-phone-media')/source.relative_to(root/'services/phone-media'),False))
    result.append((root/'services/usb-phone-link/usb_phone_link.py',USB_ROOT/'usb_phone_link.py',False))
    result.append((root/'services/usb-phone-link/usb_data_host.py',Path('/usr/local/sbin/headring-usb-data-host'),False))
    result.append((root/'orangepi/scripts/usb_tasks_mount_readonly.py',Path('/usr/local/lib/collector-usb-task-import/mount-readonly'),False))
    result.append((root/'deployment/sudoers/collector-usb-task-import',Path('/etc/sudoers.d/collector-usb-task-import'),False))
    result.append((root/'orangepi/scripts/headring_power_shutdown.sh',Path('/usr/local/lib/headring-power/shutdown'),False))
    result.append((root/'deployment/sudoers/headring-power-shutdown',Path('/etc/sudoers.d/headring-power-shutdown'),False))
    units=['collector-usb-task-mount.service','collector-usb-task-mount.timer','camd.service','embodied-pi-collect.service','embodied-pi-diagnostics.service','pi-phone-media.service','usb-phone-link.service','headring-usb-data-host.service']
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
    for name in ('91-pi-wrist-camera-power.rules','99-icm20602.rules','71-pi-auto-android.rules','72-collector-usb-task-import.rules','61-usbdevice.rules','83-typec.rules'):
        result.append((root/'deployment/udev'/name,Path('/etc/udev/rules.d')/name,False))
    if include_vendor:
        vendor=root/'vendor/pi-runtime/egorecord'
        for name in ('camd/camd.conf','camd/gen_camd_conf.sh','camd/build/camd','camd/lib/librkaiq_d',
                     'config/sensor_iq/sc233hgs_default_default.json'):
            result.append((vendor/name,Path('/opt/egorecord')/name,False))
        result.append((root/'vendor/pi-runtime/opi-headphone-audio/guarded-paplay',Path('/opt/opi-headphone-audio/guarded-paplay'),False))
    return result

def unsafe_typec_rules(directories=None, planned=None):
    """Inspect the effective same-name udev rules in priority order.

    `planned` represents the exact files this transaction will install in
    /etc. Their content is inspected too; an override is never assumed safe.
    """
    if directories is None:
        directories=[Path('/etc/udev/rules.d'),Path('/run/udev/rules.d'),Path('/usr/local/lib/udev/rules.d'),Path('/usr/lib/udev/rules.d'),Path('/lib/udev/rules.d')]
    effective={}
    for directory in directories:
        try:
            info=directory.stat()
        except FileNotFoundError as exc:
            if directory.is_symlink():raise RuntimeError('Cannot inspect udev directory: '+str(directory)) from exc
            continue
        except OSError as exc:
            raise RuntimeError('Cannot inspect udev directory: '+str(directory)) from exc
        try:
            if not stat.S_ISDIR(info.st_mode):raise OSError('not a directory')
            with os.scandir(directory) as entries:
                paths=sorted(Path(entry.path) for entry in entries if entry.name.endswith('.rules'))
        except OSError as exc:
            raise RuntimeError('Cannot inspect udev directory: '+str(directory)) from exc
        for path in paths:
            effective.setdefault(path.name,path)
    for name,path in (planned or {}).items():
        if Path(name).name!=name or not name.endswith('.rules'):raise RuntimeError('Invalid planned udev rule name')
        effective[name]=Path(path)
    bad=[]
    for path in effective.values():
        try:
            resolved=path.resolve(strict=True)
            if path.is_symlink() and resolved==Path('/dev/null'):continue
            if not resolved.is_file():raise OSError('not a regular rule file')
            lines=[line for line in path.read_text().splitlines() if not line.lstrip().startswith('#')]
            content='\n'.join(lines)
            if (re.search(r'(typec_usb2\.sh|usbdevice\b|fc000000\.usb)',content)
                    or (re.search(r'SUBSYSTEMS?\s*==\s*["\'](?:typec|android_usb)["\']',content)
                        and re.search(r'\b(?:RUN(?:\{[^}]+\})?|PROGRAM|IMPORT(?:\{[^}]+\})?)\s*[+:]?=',content))):
                bad.append(str(path))
        except (OSError,UnicodeError,RuntimeError) as exc:
            raise RuntimeError('Cannot inspect udev rule: '+str(path)) from exc
    return sorted(set(bad))

def preflight_targets(operations):
    """Reject existing or broken target symlinks before any backup or write."""
    for _,target,_ in operations:
        if target.is_symlink():raise RuntimeError('Refusing to overwrite a symlink: '+str(target))

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

def initialize_qc_results():
    import pwd
    user=pwd.getpwnam('orangepi')
    for directory in [Path('/home/orangepi/maintenance/server-qc'),Path('/home/orangepi/maintenance/server-qc/results')]:
        if directory.is_symlink():raise RuntimeError('QC result destination must not be a symlink: '+str(directory))
        created=not directory.exists()
        directory.mkdir(parents=True,exist_ok=True)
        if created or directory.stat().st_uid!=user.pw_uid:os.chown(str(directory),user.pw_uid,user.pw_gid)
        if created:os.chmod(str(directory),0o755)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--check',action='store_true',help='Read-only classic headphone mode and installation plan check')
    parser.add_argument('--with-vendor-runtime',action='store_true')
    parser.add_argument('--with-lan-time',action='store_true',help='Install guarded LAN time helper/units; preserve an existing time-server setting')
    args=parser.parse_args();root=args.project_root.resolve()
    if not (root/'orangepi/scripts/run.sh').is_file():parser.error('project root does not contain the collector')
    if args.check and args.apply:parser.error('--check cannot be combined with --apply')
    if args.check:
        spec=importlib.util.spec_from_file_location('classic_bluetooth_mode',root/'services/phone-media/classic_bluetooth_mode.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        mode=module.disk_mode();print('Classic headphone disk ControllerMode:',mode)
        if mode=='le':print('Classic headphone persistence: disk is LE-only; a controlled dual-mode maintenance is needed for classic audio after reload.')
        elif mode in ('dual','default_dual'):print('Classic headphone persistence: dual/default; current loaded state must be checked separately.')
        else:print('Classic headphone persistence: unknown; inspect configuration before maintenance.')
        adapters=sorted(Path('/sys/class/bluetooth').glob('hci[0-9]*'))
        if not adapters:print('Classic headphone runtime: unknown (no adapter visible)')
        for adapter in adapters:
            runtime=module.runtime_mode('/org/bluez/'+adapter.name)
            print('Classic headphone',adapter.name,'runtime:',runtime if runtime is not None else 'unknown', 'assessment:',module.assess(mode,runtime))
        print('Classic headphones require BR/EDR; dual retains LE. Configuration changes and service reload require controlled maintenance; this check changes nothing.')
    operations=plan(root,args.with_vendor_runtime,args.with_lan_time)
    for source,target,_ in operations:
        if not source.is_file() or source.is_symlink():parser.error('missing/non-regular source: '+str(source))
        print(str(source.relative_to(root))+' -> '+str(target))
    if not args.apply:
        print('Plan only. No files or services changed.');return
    if args.with_lan_time:
        parser.error('LAN time protocol migration is not automated: old acquisition and old clock processes must first be quiesced together in a controlled maintenance window; do not install over a live station')
    if platform.system()!='Linux' or platform.machine() not in ('aarch64','arm64'):
        parser.error('apply requires the compatible Linux aarch64 Pi')
    if os.geteuid()!=0:parser.error('apply requires sudo')
    adb=usb_adb()
    if not adb.is_file():parser.error('Install the matching system adb package or retain the existing private adb runtime first')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(root)):parser.error('use an absolute deployment path without spaces or shell characters')
    spec=importlib.util.spec_from_file_location('install_usb_data_host',root/'services/usb-phone-link/usb_data_host.py')
    host_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(host_module)
    allowed,reason=host_module.CaptureGuard(root/'orangepi').allowed()
    if not allowed:parser.error('Finish capture/initialization and use the active station root before installing: '+reason)
    for name in ('collector-usb-task-import','headring-power-shutdown'):
        subprocess.run(['/usr/sbin/visudo','-cf',str(root/'deployment/sudoers'/name)],check=True,timeout=10)
    conflicting=unsafe_typec_rules(planned={name:root/'deployment/udev'/name for name in ('61-usbdevice.rules','83-typec.rules')})
    if conflicting:parser.error('Other USB role-changing udev rules require review: '+', '.join(conflicting))
    preflight_targets(operations)
    backup=Path('/var/backups/baai-ego-collect')/datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    legacy=Path('/usr/local/bin/typec_usb2.sh')
    if legacy.is_file():
        saved=backup/legacy.relative_to('/');saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(legacy,saved)
    for source,target,unit in operations:
        if target.is_symlink():raise RuntimeError('Refusing to overwrite a symlink: '+str(target))
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            saved=backup/target.relative_to('/');saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,saved)
        raw=source.read_bytes()
        if unit:
            raw=render_systemd_unit(raw,root)
        fd,temporary=tempfile.mkstemp(prefix='.'+target.name+'-',dir=str(target.parent))
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            executable=raw.startswith(b'#!') or source.name in ('camd','guarded-paplay','librkaiq_d')
            os.chmod(temporary,0o440 if target.parent==Path('/etc/sudoers.d') else (0o755 if executable else 0o644));os.replace(temporary,target)
            directory=os.open(str(target.parent),os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
        # This item is first in the plan: provision the shared inode before
        # publishing any executable which depends on the lock.
        if target==Path('/etc/tmpfiles.d/headring-acquisition-time.conf'):
            subprocess.run(['systemd-tmpfiles','--create',str(target)],check=True)
    initialize_usb(adb)
    initialize_qc_results()
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['udevadm','control','--reload-rules'],check=True)
    # Mandatory for every installation: initialize this Pi as a USB data host
    # and keep the guarded policy active across boot and reconnect. Do not
    # start/restart any recorder, camera, or media service here.
    subprocess.run(['systemctl','enable','--now','collector-usb-task-mount.timer'],check=True)
    subprocess.run(['systemctl','enable','headring-usb-data-host.service'],check=True)
    subprocess.run(['systemctl','start','headring-usb-data-host.service'],check=True)
    host=subprocess.run(['/usr/local/sbin/headring-usb-data-host','--once','--base',str(root/'orangepi')],capture_output=True,text=True,check=True,timeout=20)
    print('USB data-host initialization: '+host.stdout.strip())
    print('Installed. Existing-file backups: '+str(backup))
    print('USB data-host policy is enabled and active; busy/unsupported role changes are reported and deferred safely.')
    print('No capture/camera/media service was started or stopped. Android authorization remains a user action.')

if __name__=='__main__':main()
