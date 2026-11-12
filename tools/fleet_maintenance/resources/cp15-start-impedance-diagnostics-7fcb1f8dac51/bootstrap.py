import os,sys,hashlib,io,zipfile,pathlib,stat,json
source=pathlib.Path(sys.argv[1]);expected=sys.argv[2];manifest=sys.argv[3]
if not source.is_absolute() or len(expected)!=64 or len(manifest)!=64:raise SystemExit('invalid pins')
fd=os.open(str(source),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
with os.fdopen(fd,'rb') as f:
 st=os.fstat(f.fileno())
 if not stat.S_ISREG(st.st_mode) or st.st_size>4000000:raise SystemExit('invalid archive')
 data=f.read()
if hashlib.sha256(data).hexdigest()!=expected:raise SystemExit('archive sha mismatch')
names=['manifest.json', 'transaction.py', 'payload/orangepi/src/orangepi_collect/acquisition/eeg_bridge.py', 'payload/orangepi/src/orangepi_collect/acquisition/eeg_worker.py', 'payload/orangepi/src/orangepi_collect/common/eeg_impedance.py', 'payload/orangepi/src/orangepi_collect/common/impedance_quality.py', 'payload/orangepi/src/orangepi_collect/common/impedance_record.py', 'payload/orangepi/src/orangepi_collect/common/scan_registry.py', 'payload/orangepi/src/orangepi_collect/control/dashboard.html', 'payload/orangepi/src/orangepi_collect/control/dashboard.py', 'payload/orangepi/src/orangepi_collect/control/device_diagnostics.py', 'payload/orangepi/src/orangepi_collect/control/diagnostic_events.py', 'payload/orangepi/src/orangepi_collect/control/launcher.py', 'payload/orangepi/src/orangepi_collect/control/phone_recording.js', 'payload/orangepi/src/orangepi_collect/control/scan_process.py', 'payload/orangepi/src/orangepi_collect/control/web_controls.py', 'payload/orangepi/src/orangepi_collect/framework/framework_recorder.py']
z=zipfile.ZipFile(io.BytesIO(data));infos=z.infolist()
if len(infos)!=len(names) or set(x.filename for x in infos)!=set(names) or any(x.file_size>500000 for x in infos):raise SystemExit('archive members mismatch')
if hashlib.sha256(z.read('manifest.json')).hexdigest()!=manifest:raise SystemExit('manifest sha mismatch')
base=pathlib.Path('/var/lib/headring-cp15-canary');base.mkdir(mode=0o700,exist_ok=True)
def check_dir(path):
 s=path.lstat()
 if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or stat.S_IMODE(s.st_mode)!=0o700:raise SystemExit('unsafe root staging directory')
check_dir(base);dest=base/manifest
try:dest.mkdir(mode=0o700)
except FileExistsError:pass
check_dir(dest)
allowed_files={dest/name for name in names};allowed_dirs={dest}
for path in allowed_files:
 parent=path.parent
 while parent!=dest:
  allowed_dirs.add(parent);parent=parent.parent
for parent in sorted(allowed_dirs,key=lambda p:len(p.parts)):
 if parent==dest:continue
 try:parent.mkdir(mode=0o700)
 except FileExistsError:pass
 check_dir(parent)
for name in names:
 target=dest/name;data=z.read(name)
 try:
  fd=os.open(str(target),os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
 except FileExistsError:
  fd=os.open(str(target),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
  with os.fdopen(fd,'rb') as f:
   s=os.fstat(f.fileno())
   if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or stat.S_IMODE(s.st_mode)!=0o600 or f.read()!=data:raise SystemExit('existing root bundle mismatch')
 else:
  with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
for root,dirs,files in os.walk(dest,followlinks=False):
 for name in dirs:
  path=pathlib.Path(root)/name
  if path not in allowed_dirs:raise SystemExit('unexpected root bundle directory')
  check_dir(path)
 for name in files:
  if pathlib.Path(root)/name not in allowed_files:raise SystemExit('unexpected root bundle file')
print(json.dumps({'root_bundle':str(dest),'manifest_sha256':manifest,'archive_sha256':expected},sort_keys=True))
