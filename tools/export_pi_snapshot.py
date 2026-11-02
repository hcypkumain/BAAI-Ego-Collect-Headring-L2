"""Export deployed collection sources without recordings, caches or credentials."""
import collections,datetime,hashlib,io,json,os,re,stat,subprocess,tarfile
from pathlib import Path

PROJECT=Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2')
ALTERNATE=Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main')
OUT=Path('/home/orangepi/maintenance/github-publication-20261102')
OUT.mkdir(parents=True,exist_ok=True)
SKIP_DIRS={'.git','.venvs','.uv-cache','__pycache__','.pytest_cache','node_modules','.mypy_cache'}
SKIP_MEDIA={'.png','.jpg','.jpeg','.bmp','.mp4','.mkv','.h264','.h265','.hevc','.mjpeg','.npz','.npy','.pkl','.h5','.hdf5','.db','.sqlite','.sqlite3','.pyc','.pyo','.log','.zip','.whl','.deb','.so'}
BACKUP=re.compile(r'(?:\.before(?:[-.]|$)|\.bak(?:[-_.]|$)|_bak(?:[_\.]|$)|\.orig$|~$|\.tar(?:\.gz|\.xz)?$)')
records=[];items={};omitted=collections.Counter();redactions=[]
known=[]
private=PROJECT/'orangepi/private/secrets.env'
if private.is_file():
    for line in private.read_text().splitlines():
        if '=' not in line or line.lstrip().startswith('#'):continue
        key,value=line.split('=',1);value=value.strip().strip('"\'')
        if len(value)>=8:known.append((key.strip(),value.encode()))

def eligible(relative,is_dir=False):
    parts=relative.parts
    if any(part in SKIP_DIRS for part in parts):return False
    if parts and parts[0] in ('data','delivery_backups'):return False
    for prefix in (('orangepi','runtime'),('orangepi','private'),('orangepi','tests','results'),('configs','environments')):
        if parts[:len(prefix)]==prefix:return False
    if is_dir:return not BACKUP.search(relative.name)
    if BACKUP.search(relative.name):return False
    if relative.name in ('secrets.env','dashboard_token','vault.key','credentials.enc') or relative.suffix in ('.pem','.key'):return False
    if relative.name.endswith('.env') and not relative.name.endswith('.env.example'):return False
    if relative.suffix.lower() in SKIP_MEDIA:return False
    if relative.suffix.lower()=='.wav' and parts[:3]!=('orangepi','assets','cloud_voice'):return False
    return True

def project_files(root):
    for current,dirs,names in os.walk(str(root),followlinks=False):
        current=Path(current)
        dirs[:]=[name for name in sorted(dirs) if not (current/name).is_symlink() and eligible((current/name).relative_to(root),True)]
        for name in sorted(names):
            path=current/name;relative=path.relative_to(root)
            if path.is_symlink() or not eligible(relative):omitted['project_non_source_or_private']+=1;continue
            if not path.is_file():continue
            yield path,relative.as_posix()

def add(path,destination,component,binary=False):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise RuntimeError('Source is not a regular file: '+str(path))
    before=path.stat();raw=path.read_bytes();after=path.stat()
    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):raise RuntimeError('Source changed during export: '+str(path))
    changed=[]
    for key,value in known:
        if value in raw:
            if binary:raise RuntimeError('Credential fingerprint in binary: '+destination)
            raw=raw.replace(value,('REDACTED_'+key).encode());changed.append(key)
    if destination in items:raise RuntimeError('Duplicate export path: '+destination)
    mode=stat.S_IMODE(before.st_mode)&0o777
    items[destination]=(raw,mode,int(before.st_mtime))
    row=dict(path=destination,component=component,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),mode=oct(mode),source=str(path))
    if changed:row['sanitized_keys']=changed;redactions.append(dict(path=destination,keys=changed))
    if binary:row['source_available']=False
    records.append(row)

for path,relative in project_files(PROJECT):add(path,relative,'collector-and-framework')
alternate=[]
for path,relative in project_files(ALTERNATE):
    if not relative.startswith(('src/','scripts/','tests/')):continue
    main=PROJECT/relative
    if main.is_file() and path.read_bytes()==main.read_bytes():continue
    add(path,'legacy/alternate-project/'+relative,'alternate-project-differences');alternate.append(relative)

sidecar=Path('/opt/pi-phone-media')
if not sidecar.is_dir():raise RuntimeError('Export the migrated phone media installation; old device snapshots remain in Git history')
for path in sorted(sidecar.rglob('*')):
    relative=path.relative_to(sidecar)
    if path.is_symlink() or not path.is_file() or 'backups' in relative.parts or BACKUP.search(path.name):continue
    if path.suffix not in ('.py','.md','.sh','.json','.toml','.txt','.c','.h'):continue
    add(path,'services/phone-media/'+relative.as_posix(),'preview-and-replay')
if (sidecar/'headphone-test.wav').is_file():add(sidecar/'headphone-test.wav','services/phone-media/headphone-test.wav','preview-and-replay')

for source,destination in (
    ('/etc/pulse/client.conf','deployment/os-reference/pulse/client.conf'),
    ('/etc/pulse/default.pa','deployment/os-reference/pulse/default.pa'),
    ('/etc/avahi/avahi-daemon.conf','deployment/os-reference/avahi/avahi-daemon.conf'),
    ('/etc/avahi/services/pi-collector.service','deployment/os-reference/avahi/pi-collector.service'),
    ('/usr/local/bin/typec_usb2.sh','deployment/os-reference/platform-scripts/typec_usb2.sh'),
    ('/usr/bin/usbdevice','deployment/os-reference/platform-scripts/usbdevice'),
    ('/usr/bin/orangepi_audio.sh','deployment/os-reference/platform-scripts/orangepi_audio.sh')):
    path=Path(source)
    if path.is_file() and not path.is_symlink():
        raw=path.read_bytes()
        if b'\x00' not in raw:add(path,destination,'platform-configuration-reference')

for name in ('embodied-pi-collect.service','embodied-pi-diagnostics.service','pi-phone-media.service','camd.service','egorecord.service'):
    unit=Path('/etc/systemd/system')/name
    if unit.is_file() and not unit.is_symlink():add(unit,'deployment/systemd/'+name,'systemd')
    directory=unit.with_name(name+'.d')
    if directory.is_dir():
        for path in sorted(directory.glob('*.conf')):add(path,'deployment/systemd/'+directory.name+'/'+path.name,'systemd')

for name in ('91-pi-wrist-camera-power.rules','99-icm20602.rules','10-wifi-disable-powermanagement.rules','50-usb-realtek-net.rules','83-typec.rules','84-audio-orangepi.rules','88-rockchip-camera.rules','90-naming-audios.rules','90-pulseaudio-rockchip.rules','99-rockchip-permissions.rules'):
    path=Path('/etc/udev/rules.d')/name
    if path.is_file():add(path,'deployment/udev/'+name,'udev')

ego=Path('/opt/egorecord')
for path in sorted((ego/'config').rglob('*')):
    if not path.is_file() or path.is_symlink() or BACKUP.search(path.name):continue
    if '_legacy_' in path.name:continue
    if path.suffix not in ('.json','.yaml','.yml','.md','.sh','.dtb'):continue
    add(path,'vendor/pi-runtime/egorecord/'+path.relative_to(ego).as_posix(),'egorecord-configuration',binary=path.suffix=='.dtb')
for relative in ('camd/camd.conf','camd/gen_camd_conf.sh','camd/build/camd','camd/lib/librkaiq_d'):
    add(ego/relative,'vendor/pi-runtime/egorecord/'+relative,'camd-runtime',binary=relative in ('camd/build/camd','camd/lib/librkaiq_d'))
add('/opt/opi-headphone-audio/guarded-paplay','vendor/pi-runtime/opi-headphone-audio/guarded-paplay','audio-runtime',binary=True)
add('/home/orangepi/maintenance/uploader/robot_uploader-1.0.0-py3-none-any.whl','vendor/wheels/robot_uploader-1.0.0-py3-none-any.whl','uploader-wheel',binary=True)
audit=Path('/home/orangepi/maintenance/upgrade-20261029-headband30/clock_audit_v2.c')
if audit.is_file():add(audit,'tools/hardware/clock_audit_v2.c','hardware-diagnostics')

manifest=dict(schema_version=1,exported_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    project_source=str(PROJECT),upstream_commit=subprocess.check_output(['git','-C',str(PROJECT),'rev-parse','HEAD'],text=True).strip(),
    source_files=len(records),source_bytes=sum(row['bytes'] for row in records),
    component_counts=dict(collections.Counter(row['component'] for row in records)),
    alternate_project_files=alternate,redactions=redactions,omitted=dict(omitted),files=records)
raw=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
items['deployment/manifests/pi-source-snapshot.json']=(raw,0o644,int(datetime.datetime.now().timestamp()))
archive=OUT/'pi-source-snapshot.tar.gz'
with tarfile.open(str(archive),'w:gz') as stream:
    for destination,(content,mode,mtime) in sorted(items.items()):
        info=tarfile.TarInfo(destination);info.size=len(content);info.mode=mode;info.mtime=mtime
        stream.addfile(info,io.BytesIO(content))
(OUT/'export-summary.json').write_text(json.dumps({key:value for key,value in manifest.items() if key!='files'},ensure_ascii=False,indent=2))
print(json.dumps(dict(source_files=len(records),source_bytes=manifest['source_bytes'],archive_bytes=archive.stat().st_size,
    archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),components=manifest['component_counts'],
    alternate_files=len(alternate),redacted_files=len(redactions)),ensure_ascii=False))
