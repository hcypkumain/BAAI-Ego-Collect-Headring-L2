import os,sys,hashlib,io,zipfile,pathlib,stat,json
source=pathlib.Path(sys.argv[1]); expected=sys.argv[2]; manifest=sys.argv[3]
if not source.is_absolute() or len(expected)!=64 or len(manifest)!=64:raise SystemExit('invalid pins')
fd=os.open(str(source),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
with os.fdopen(fd,'rb') as f:
 st=os.fstat(f.fileno())
 if not stat.S_ISREG(st.st_mode) or st.st_size>2000000:raise SystemExit('invalid archive')
 data=f.read()
if hashlib.sha256(data).hexdigest()!=expected:raise SystemExit('archive sha mismatch')
rels=['common/impedance_quality.py','common/eeg_settings.py','common/impedance_record.py','control/dashboard.html','framework/eeg_qc.py']
names=['manifest.json','transaction.py']+['payload/orangepi/src/orangepi_collect/'+p for p in rels]
z=zipfile.ZipFile(io.BytesIO(data));infos=z.infolist()
if len(infos)!=len(names) or set(x.filename for x in infos)!=set(names) or any(x.file_size>300000 for x in infos):raise SystemExit('archive members mismatch')
if hashlib.sha256(z.read('manifest.json')).hexdigest()!=manifest:raise SystemExit('manifest sha mismatch')
base=pathlib.Path('/var/lib/headring-eeg-delta');base.mkdir(mode=0o700,exist_ok=True)
st=base.lstat()
if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or st.st_mode&0o077:raise SystemExit('unsafe root staging base')
dest=base/manifest
dest.mkdir(mode=0o700,exist_ok=False)
for name in names:
 target=dest/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 with target.open('xb') as f:f.write(z.read(name))
 os.chmod(target,0o600)
print(json.dumps({'root_bundle':str(dest),'manifest_sha256':manifest,'archive_sha256':expected},sort_keys=True))
