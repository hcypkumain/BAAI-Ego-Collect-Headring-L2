#!/usr/bin/env python3
"""Plan, apply, or roll back an offline release on an initialized collector."""
import argparse, contextlib, fcntl, hashlib, importlib.util, io, json, os, platform, pwd, grp, re, shutil, stat, subprocess, sys, tarfile, tempfile, time
from pathlib import Path

STATE=Path('/var/lib/headring-offline'); UNITS=Path('/etc/systemd/system')
SERVICES=('headring-offline-entry.service','headring-offline-discovery.service','headring-offline-dhcp-guard.service')
PREIMAGES={'7004315fbc567afc5f0d5e9fcd0552f0db46a9c8':'d636ca9867d09c4d1f57436cb7a8044d171e8b201d0bf1ec1eb0b9b4acb7dc21','460c04cb719aca5d73bfe518748bd636c632e2cf':'be3f2734c80c6f1df24de7b4b4c001ff749c0baa2b064f49aa57387b736de3ea','8c72ed375c33b39d15e00dc9ee9d37c82b54e6b3':'bd626687ef27c70d6a6cf6ab83d5567da147af540e6a8e33877a5da06c939aa3'}
LEGACY={'headring-usb-entry.service':'0dceecb1ba9dd5492f9e1889a76c1a4331a578d5ea991c8423b3bede9401fa23','headring-usb-discovery.service':'f02dfce84ed78136e394272f836d903a0a40ef3132e13a3059cd32870ecffdb8','headring-usb-dhcp-guard.service':'e5f44747cbbb144a4e2b94544ac2ea357695fe40fb6c1f78fb7b557a5e46b232'}
def sha(b):return hashlib.sha256(b).hexdigest()
def require(v,msg):
 if not v:raise RuntimeError(msg)
def cmd(a,check=True,timeout=20):
 p=subprocess.run(a,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
 if check and p.returncode:raise RuntimeError('command failed: '+a[0]+' '+(a[1] if len(a)>1 else ''))
 return p

def safe_read(p,limit=16_000_000):
 fd=os.open(str(p),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 try:
  st=os.fstat(fd);require(stat.S_ISREG(st.st_mode) and st.st_size<=limit,'not a safe regular file: '+str(p));chunks=[];total=0
  while True:
   part=os.read(fd,65536)
   if not part:break
   total+=len(part);require(total<=limit,'file too large');chunks.append(part)
  return b''.join(chunks)
 finally:os.close(fd)
def check_ancestors(p):
 for d in [p.parent]+list(p.parent.parents):
  if d.exists() or d.is_symlink():require(not d.is_symlink() and d.is_dir(),'unsafe directory: '+str(d))
def write_atomic(p,data,mode,uid=0,gid=0):
 check_ancestors(p);p.parent.mkdir(parents=True,exist_ok=True);require(not p.is_symlink(),'target symlink: '+str(p))
 fd,tmp=tempfile.mkstemp(prefix='.headring-',dir=str(p.parent))
 try:
  os.fchmod(fd,mode);os.fchown(fd,uid,gid)
  with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
  os.replace(tmp,str(p));d=os.open(str(p.parent),os.O_RDONLY|os.O_DIRECTORY);os.fsync(d);os.close(d)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
def load_bundle(path,expected):
 raw=safe_read(path,40_000_000);require(re.fullmatch(r'[0-9a-f]{64}',expected or '') and sha(raw)==expected,'bundle sha256 mismatch')
 with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as t:
  members=t.getmembers();require(len(members)<=200,'too many bundle members');files={};total=0
  for m in members:
   require(m.isfile() and m.name not in files and not m.name.startswith('/') and all(x not in ('','.','..') for x in m.name.split('/')),'unsafe bundle member')
   total+=m.size;require(m.size<=8_000_000 and total<=30_000_000,'bundle budget exceeded');files[m.name]=t.extractfile(m).read()
 require('manifest.json' in files,'manifest missing');m=json.loads(files.pop('manifest.json'))
 require(m.get('schema')==1 and m.get('baseline_commit')=='083155214907c55dc61d78fb34d65b29d12f2b82' and m.get('base_collector_commit')=='f5d03b7b5fb197eb76e78610a0bce672e5cc4853' and m.get('collector_overlay_manifest_sha256')=='36d8059918ce370f7928cc7cd3207e2ac1d26471b3e8dcdef74b98b7888cd62a','wrong manifest baseline')
 require(re.fullmatch(r'cp_[0-9]{4}',m.get('device_id','')) and re.fullmatch(r'offline-[0-9a-f]{16}',m.get('release_id','')),'invalid release identity')
 require(m.get('release_prefix')=='/opt/headring-offline/releases/'+m['release_id'],'invalid release path')
 require(set(files)==set(m['files']),'manifest membership mismatch')
 for name,entry in m['files'].items():
  require(name.startswith(('release/','collector/','support/')) and sha(files[name])==entry['sha256'] and len(files[name])==entry['size'] and entry['mode'] in (0o644,0o755),'manifest mismatch: '+name)
 names={x[len('collector/'):]:files[x] for x in files if x.startswith('collector/orangepi/src/orangepi_collect/')}
 additions=m.get('collector_additions',[])
 require(isinstance(additions,list) and len(additions)==len(set(additions)) and len(additions)<=32 and all(isinstance(x,str) and x in names for x in additions),'invalid collector additions')
 require(len(names)==123+len(additions),'wrong collector source count')
 require(m.get('task_rotation')=={'block_size':5,'count_basis':'saved','progress_version':4,'preserve_existing_n':True},'saved-count deployment policy missing')
 source_index={name[len('collector/'):]:{'sha256':sha(data),'size':len(data)} for name,data in files.items() if name.startswith('collector/orangepi/')}
 require(m.get('collector_source_sha256')==sha(json.dumps(source_index,sort_keys=True,separators=(',',':')).encode()),'collector source index mismatch')
 if 'source_preimages' in m:
  pre=m['source_preimages'];require(m.get('source_preimage_commit') in PREIMAGES and m.get('source_preimage_sha256')==PREIMAGES[m['source_preimage_commit']] and isinstance(pre,dict),'unknown migration source')
  require(set(pre)=={x[len('collector/'):] for x in files if x.startswith('collector/orangepi/')},'migration source membership mismatch')
  require(all(pre[x] is None for x in additions),'addition preimage must be absent')
  original={k:v for k,v in pre.items() if k not in additions}
  require(sha(json.dumps(original,sort_keys=True,separators=(',',':')).encode())==PREIMAGES[m['source_preimage_commit']],'migration source lock mismatch')
 return m,files

def unit_state(name):
 active=cmd(['/usr/bin/systemctl','is-active',name],False).stdout.strip()=='active'
 enabled=cmd(['/usr/bin/systemctl','is-enabled',name],False).stdout.strip()=='enabled'
 return {'active':active,'enabled':enabled}
def unit_templates(prefix):
 shared='Type=simple\nNoNewPrivileges=yes\nCapabilityBoundingSet=\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\nMemoryMax=128M\nTasksMax=16\nCPUQuota=25%\n'
 return {
 SERVICES[0]:('[Unit]\nDescription=HeadRing offline USB publisher\nAfter=local-fs.target\n[Service]\n'+shared+'User=avahi\nGroup=avahi\nEnvironment=LD_LIBRARY_PATH='+prefix+'/lib\nRuntimeDirectory=avahi-daemon\nRuntimeDirectoryMode=0700\nKillMode=control-group\nRestart=no\nBindReadOnlyPaths='+prefix+'/etc-avahi:/etc/avahi\nExecStart=/usr/bin/python3 -I -S '+prefix+'/dynamic_guard.py\n[Install]\nWantedBy=multi-user.target\n').encode(),
 SERVICES[1]:('[Unit]\nDescription=HeadRing USB discovery and installer\n[Service]\n'+shared+'User=avahi\nGroup=avahi\nRestart=on-failure\nRestartSec=5\nExecStart=/usr/bin/python3 -I -S '+prefix+'/probe.py\n[Install]\nWantedBy=multi-user.target\n').encode(),
 SERVICES[2]:('[Unit]\nDescription=HeadRing selected USB phone DHCP recovery\nAfter=NetworkManager.service\nWants=NetworkManager.service\n[Service]\n'+shared+'Restart=on-failure\nRestartSec=5\nExecStart=/usr/bin/python3 -I -S '+prefix+'/usb_dhcp_guard.py\n[Install]\nWantedBy=multi-user.target\n').encode()}

def inspect_selector(m,files):
 """Read-only USB network selection; no interface is guessed when ambiguous."""
 fd,path=tempfile.mkstemp(prefix='headring-usb-selector-',suffix='.py')
 try:
  with os.fdopen(fd,'wb') as f:f.write(files['release/guard.py'])
  spec=importlib.util.spec_from_file_location('offline_usb_selector',path);guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
  meta=guard.metadata(files['release/metadata.json']);require(meta['device_id']==m['device_id'] and meta['selector']==m['selector'],'USB metadata mismatch')
  sysfs=guard.Sysfs();names=list(sysfs.root.iterdir());require(len(names)<=256,'too many network interfaces');matches=[]
  for item in names:
   try:
    if guard.Sysfs().matches(item.name,meta['selector']):matches.append(item.name)
   except (OSError,guard.Refused,UnicodeError):pass
  require(len(matches)<=1,'multiple eligible USB network interfaces; explicit selection required')
  return {'mode':meta['selector'].get('mode','explicit-port'),'match_count':len(matches),'interface':matches[0] if matches else None}
 finally:os.unlink(path)

def prior_owned_release(device_id):
 """Recognize only the intact complete transaction currently owning all units."""
 present=[(UNITS/name).exists() or (UNITS/name).is_symlink() for name in SERVICES]
 if not any(present):return {}
 require(all(present) and STATE.is_dir() and not STATE.is_symlink(),'partial or unknown existing units')
 journals=list(STATE.glob('transaction-offline-*/journal.json'));require(len(journals)<=64,'too many transactions')
 matches=[]
 for journal in journals:
  check_ancestors(journal)
  try:
   j=json.loads(safe_read(journal));entries=j.get('files',[])
   if j.get('status')!='complete':continue
   indexed={e['target']:e for e in entries}
   if not all(str(UNITS/name) in indexed for name in SERVICES):continue
   if not all(sha(safe_read(UNITS/name))==indexed[str(UNITS/name)]['post_sha'] for name in SERVICES):continue
   for e in entries:
    target=Path(e['target']);require(target.exists() and sha(safe_read(target))==e['post_sha'],'previous release modified: '+str(target))
   prefix=Path('/opt/headring-offline/releases')/j['release_id'];metadata=json.loads(safe_read(prefix/'metadata.json'));require(metadata['device_id']==device_id,'previous release device mismatch')
   matches.append({'journal_sha256':sha(safe_read(journal)),'release_id':j['release_id'],'units':{name:unit_state(name) for name in SERVICES}})
  except (KeyError,ValueError,OSError):continue
 require(len(matches)==1,'existing units not owned by one intact transaction')
 return matches[0]

# These helpers grant narrowly scoped privileged operations. Only replace a
# pre-existing helper when its bytes match a reviewed release.
FEATURE_HELPERS={
 '/usr/local/lib/collector-usb-task-import/mount-readonly':('support/usb_tasks_mount_readonly.py',0o755,{'0e672274737b4cd09539e68f2ca376c8806769945aa63ad202f64d316c3f034d','6030c6685a7fe1d9e9fbc5d68190e8c4bcbabaa5f9c5e4d3c4354a59618d8fcc'}),
 '/usr/local/lib/collector-usb-task-import/mount-export':('support/usb_export_mount.py',0o755,set()),
 '/usr/local/sbin/headring-phone-time':('support/headring-phone-time',0o755,{'3e56928dcabe3f05afff8d61533ee9b81847c5cd5a3d9996b7a6d4826d3fe2d3','27fdee0c80e5afc3d8c88f4cf7a0ff533cc4bac2b7ffb76fb6c1fafe84d96f24'}),
 '/usr/local/sbin/headring-lan-time':('support/headring-lan-time',0o755,{'65cafeb798741873d8164b9ae677b53a2b21ce6144066a800bc0e126b65a628d','c7cf968b85bd2d788369d04b636e5359811bb03a92a3885427841f44eb93733c'}),
 '/usr/local/lib/headring-time/headring_capture_guard.py':('support/headring_capture_guard.py',0o644,set()),
}
def feature_files(files,user):
 require(re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',user or '') is not None,'unsafe collector service user')
 result={}
 for target,(source,mode,known) in FEATURE_HELPERS.items():
  require(source in files,'required feature helper missing: '+source)
  result[target]={'data':files[source],'mode':mode,'known':known}
 result['/etc/sudoers.d/collector-usb-export']={'data':(user+' ALL=(root) NOPASSWD: /usr/local/lib/collector-usb-task-import/mount-readonly "", /usr/local/lib/collector-usb-task-import/mount-export ""\n').encode(),'mode':0o440,'known':set()}
 result['/etc/sudoers.d/headring-phone-time']={'data':(user+' ALL=(root) NOPASSWD: /usr/local/sbin/headring-phone-time ""\n').encode(),'mode':0o440,'known':set()}
 return result

def plan_feature_helpers(m,files):
 if m.get('integrated_helpers_version') is None:return {}
 require(m.get('integrated_helpers_version')==1,'unknown helper version')
 user=cmd(['/usr/bin/systemctl','show','embodied-pi-collect.service','--property=User','--value']).stdout.strip()
 features=feature_files(files,user);result={}
 for name,entry in features.items():
  target=Path(name);check_ancestors(target);require(not target.is_symlink(),'feature helper symlink: '+name)
  for parent in target.parents:
   if parent.exists():
    st=parent.stat();mode=stat.S_IMODE(st.st_mode)
    require(st.st_uid==0 and not mode&0o002 and (not mode&0o020 or st.st_gid==0),'unsafe feature directory: '+str(parent))
  before=None
  if target.exists():
   st=target.stat();require(stat.S_ISREG(st.st_mode) and st.st_uid==0 and stat.S_IMODE(st.st_mode)==entry['mode'],'unsafe feature helper: '+name)
   before=sha(safe_read(target));require(before==sha(entry['data']) or before in entry['known'],'unrecognized feature helper: '+name)
  result[name]={'before':before,'after':sha(entry['data']),'mode':entry['mode']}
 return result

@contextlib.contextmanager
def acquire_feature_operation_locks(m):
 if m.get('integrated_helpers_version') is None:
  yield;return
 user=cmd(['/usr/bin/systemctl','show','embodied-pi-collect.service','--property=User','--value']).stdout.strip()
 require(re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',user or '') is not None,'unsafe collector service user')
 home=Path(pwd.getpwnam(user).pw_dir)
 paths=[Path('/run/collector-usb-task-import/.operation.lock'),home/'maintenance/usb-data-export/export.lock']
 descriptors=[]
 try:
  for path in paths:
   if not path.exists() and not path.is_symlink():continue
   check_ancestors(path);fd=os.open(str(path),os.O_RDWR|os.O_NOFOLLOW);descriptors.append(fd)
   require(stat.S_ISREG(os.fstat(fd).st_mode),'invalid USB operation lock')
   fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  yield
 finally:
  for fd in reversed(descriptors):os.close(fd)

def verify_environment(m,files,root):
 require(os.geteuid()==0,'run as root')
 require(platform.system()=='Linux' and platform.machine() in ('aarch64','arm64'),'requires Linux arm64')
 require(Path('/opt/egorecord/DEVICE_ID').read_text().strip()==m['device_id'],'device identity mismatch')
 require(root.is_absolute() and not root.is_symlink() and (root/'.venvs/hardware/bin/python').is_file() and (root/'scripts/run.sh').is_file(),'initialized collector runtime missing')
 require(unit_state('embodied-pi-collect.service')['active'],'collector service is not active')
 feature_plan=plan_feature_helpers(m,files)
 if m.get('integrated_helpers_version')==1:
  for executable in ('/usr/bin/lsblk','/usr/bin/findmnt','/bin/mount','/bin/umount','/usr/sbin/visudo'):
   require(os.path.isfile(executable) and os.access(executable,os.X_OK),'feature dependency unavailable: '+executable)
  user=cmd(['/usr/bin/systemctl','show','embodied-pi-collect.service','--property=User','--value']).stdout.strip()
  require(re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',user or '') is not None,'unsafe collector service user')
  r=subprocess.run(['/usr/sbin/runuser','-u',user,'--',str(root/'.venvs/hardware/bin/python'),'-c','import yaml; from PIL import Image'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
  require(r.returncode==0,'reference import dependencies unavailable')
  chrony=cmd(['/usr/bin/systemctl','show','chrony.service','--property=LoadState','--value'],False)
  require(chrony.returncode==0 and chrony.stdout.strip()=='loaded','chrony service unavailable')
 running=cmd(['/usr/bin/systemctl','show','embodied-pi-collect.service','--property=ExecStart','--value']).stdout
 require(str(root/'scripts/run.sh') in running,'collector root differs from active service')
 require(Path('/run/camd.sock').exists(),'camera status unavailable')
 require(sys.version_info >= (3,8),'requires Python 3.8 or newer')
 binary=files['release/avahi-daemon'];core=files['release/lib/libavahi-core.so.7']
 require(binary[:6]==b'\x7fELF\x02\x01' and int.from_bytes(binary[18:20],'little')==183 and core[:6]==b'\x7fELF\x02\x01' and int.from_bytes(core[18:20],'little')==183,'invalid arm64 dependency')
 with tempfile.TemporaryDirectory(prefix='headring-ldd-') as tmp:
  exe=Path(tmp)/'avahi-daemon';exe.write_bytes(binary);exe.chmod(0o755);(Path(tmp)/'libavahi-core.so.7').write_bytes(core)
  env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LD_LIBRARY_PATH':tmp};r=subprocess.run(['/usr/bin/ldd',str(exe)],capture_output=True,text=True,timeout=10,env=env)
  require(r.returncode==0 and 'not found' not in r.stdout+r.stderr,'Avahi system dependency missing')
 for directory in (Path('/opt'),Path('/opt/headring-offline'),Path('/opt/headring-offline/releases'),UNITS,STATE):
  if directory.exists() or directory.is_symlink():
   st=directory.lstat();require(stat.S_ISDIR(st.st_mode) and st.st_uid==0 and not stat.S_IMODE(st.st_mode)&0o022,'unsafe system directory: '+str(directory))
 require(not STATE.is_symlink(),'unsafe state directory')
 usb=inspect_selector(m,files)
 existing=STATE/('transaction-'+m['release_id'])/'journal.json'
 check_ancestors(existing)
 if existing.exists():
  j=json.loads(safe_read(existing));require(j.get('status')=='complete','existing transaction needs review')
  for name,data in files.items():
   if name.startswith('collector/orangepi/'):
    p=root/name[len('collector/orangepi/'):];require(p.exists() and sha(safe_read(p))==sha(data),'collector changed since deployment: '+str(p))
  for name in SERVICES:
   state=unit_state(name);require(state['active'] and state['enabled'],'installed service not active/enabled: '+name)
  for e in j['files']:
   p=Path(e['target']);require(p.exists() and sha(safe_read(p))==e['post_sha'],'installed file changed: '+str(p))
  return {'already':True,'collector_changes':[],'legacy_units':{},'usb':usb,'release_prefix':m['release_prefix']}
 previous=prior_owned_release(m['device_id'])
 legacy={}
 for name,want in LEGACY.items():
  p=UNITS/name
  if p.exists():require(sha(safe_read(p))==want,'legacy unit differs: '+name);legacy[name]=unit_state(name)
 # Avoid taking over an unrelated Avahi or UDP port. Legacy entry owns these only when active.
 udp=Path('/proc/net/udp').read_text().splitlines()[1:]+Path('/proc/net/udp6').read_text().splitlines()[1:]
 occupied=any(int(line.split()[1].split(':')[1],16)==5353 for line in udp)
 owned_active=previous.get('units',{}).get(SERVICES[0],{}).get('active',False)
 require(not occupied or legacy.get('headring-usb-entry.service',{}).get('active',False) or owned_active,'UDP 5353 already in use')
 if occupied:
  rows=cmd(['/usr/bin/ss','-H','-lunp','sport = :5353']).stdout.splitlines();require(rows,'cannot identify UDP 5353 owner')
  for row in rows:
   owners=re.findall(r'pid=([0-9]+)',row);require(owners,'cannot identify UDP 5353 owner')
   for pid in owners:
    cgroup=safe_read(Path('/proc')/pid/'cgroup',4096).decode();require(('/headring-usb-entry.service' in cgroup and legacy.get('headring-usb-entry.service',{}).get('active',False)) or ('/'+SERVICES[0] in cgroup and owned_active),'unrelated UDP 5353 owner')
 require(not Path('/run/avahi-daemon').exists() or legacy.get('headring-usb-entry.service',{}).get('active',False) or owned_active,'Avahi runtime already in use')
 diffs=[];prehash={}
 migration=m.get('source_preimages')
 if migration is not None:
  source=root/'src/orangepi_collect';actual=set()
  for p in source.rglob('*'):
   if p.is_file() and p.suffix in ('.py','.js','.html','.css') and '__pycache__' not in p.parts:actual.add('orangepi/'+p.relative_to(root).as_posix())
  expected={name for name,h in migration.items() if h is not None};require(actual==expected,'legacy source file set differs')
 for name,data in files.items():
  if not name.startswith('collector/orangepi/'):continue
  relative=name[len('collector/orangepi/'):];target=root/relative;check_ancestors(target)
  require(not target.is_symlink(),'collector source symlink: '+relative)
  exists=target.exists();before=sha(safe_read(target)) if exists else None
  if migration is None:
   is_addition=('orangepi/'+relative) in m.get('collector_additions',[])
   if is_addition:require(not exists or before==sha(data),'unmanaged added collector file differs: '+relative)
   else:require(exists,'collector source missing: '+relative)
  else:require(before==migration['orangepi/'+relative],'legacy source changed: '+relative)
  prehash[relative]=before
  if before!=sha(data):diffs.append(relative)
 require(len([n for n in files if n.startswith('collector/orangepi/')])==123+len(m.get('collector_additions',[])),'collector file count')
 require(not Path(m['release_prefix']).exists(),'release directory already exists')
 return {'collector_changes':diffs,'collector_prehash':prehash,'legacy_units':legacy,'previous_release':previous,'usb':usb,'release_prefix':m['release_prefix'],'feature_plan':feature_plan}

def acquire_capture_guard(files,root):
 data=files['support/services/usb-phone-link/usb_data_host.py'];fd,path=tempfile.mkstemp(prefix='headring-capture-guard-',suffix='.py')
 try:
  with os.fdopen(fd,'wb') as f:f.write(data)
  spec=importlib.util.spec_from_file_location('capture_guard_release',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  guard=module.CaptureGuard(root)
  with guard.lease():
   allowed,reason=guard.allowed();require(allowed,'collector is busy or unconfirmed: '+reason);yield
 finally:os.unlink(path)
acquire_capture_guard=contextlib.contextmanager(acquire_capture_guard)

def save_journal(path,j):write_atomic(path,(json.dumps(j,sort_keys=True,indent=2)+'\n').encode(),0o600)
def backup_and_write(target,data,mode,j,tx):
 check_ancestors(target);entry={'target':str(target),'post_sha':sha(data),'existed':target.exists(),'phase':'intent'}
 require(not target.is_symlink(),'refusing symlink target');idx=len(j['files']);backup=tx/('file-%04d'%idx)
 if entry['existed']:
  st=target.stat();old=safe_read(target);entry.update(old_sha=sha(old),mode=stat.S_IMODE(st.st_mode),uid=st.st_uid,gid=st.st_gid,backup=str(backup));write_atomic(backup,old,0o600)
 else:entry.update(mode=mode,uid=0,gid=0)
 j['files'].append(entry);save_journal(tx/'journal.json',j)
 write_atomic(target,data,entry['mode'] if entry['existed'] else mode,entry['uid'] if entry['existed'] else 0,entry['gid'] if entry['existed'] else 0);entry['phase']='written';save_journal(tx/'journal.json',j)

def task_state_paths(root):
 bases=[root]+[p for p in (root.parent/'profiles').glob('*/orangepi') if p.is_dir()]
 paths=set()
 for base in bases:
  paths.update((base/'configs/task_block_policy.json',base/'runtime/reference_task_blocks.json'))
  paths.update((base/'runtime/text_task_blocks').glob('blocks-temporary-*.json'))
  catalog=base/'runtime/reference_library/catalog.json'
  if catalog.is_file():
   value=json.loads(safe_read(catalog))
   for row in value.get('lists',{}).values():
    namespace=row.get('text_task_namespace')
    if isinstance(namespace,str) and re.fullmatch(r'temporary-[a-z0-9-]{1,80}',namespace):
     paths.add(base/'runtime/text_task_blocks'/('blocks-'+namespace+'.json'))
 require(len(paths)<=512,'too many task state files')
 return sorted(paths)

def backup_task_state(root,tx,j):
 entries=[];total=0
 for number,path in enumerate(task_state_paths(root)):
  check_ancestors(path);require(not path.is_symlink(),'task state symlink')
  entry={'target':str(path),'existed':path.exists()}
  if entry['existed']:
   data=safe_read(path,1_000_000);total+=len(data);require(total<=16_000_000,'task state backup too large')
   info=path.stat();saved=tx/('task-state-%04d'%number);write_atomic(saved,data,0o600)
   entry.update(backup=str(saved),sha256=sha(data),mode=stat.S_IMODE(info.st_mode),uid=info.st_uid,gid=info.st_gid)
  entries.append(entry)
 j['task_state_backup']=entries;save_journal(tx/'journal.json',j)

def restore_task_state(tx,j):
 for entry in j.get('task_state_backup',[]):
  target=Path(entry['target']);check_ancestors(target);require(not target.is_symlink(),'task state target symlink')
  if entry['existed']:
   data=safe_read(Path(entry['backup']));require(sha(data)==entry['sha256'],'task state backup changed')
   write_atomic(target,data,entry['mode'],entry['uid'],entry['gid'])
  elif target.exists():target.unlink()

def rollback(tx,j):
 # After a completed deployment, never silently overwrite subsequent capture
 # progress. Failed-install rollback still restores its pre-start migration data.
 if j.get('status')=='complete' and j.get('task_state_backup'):
  for entry in j['task_state_backup']:
   path=Path(entry['target'])
   require((entry['existed'] and path.is_file() and sha(safe_read(path))==entry['sha256'])
           or (not entry['existed'] and not path.exists()),'task progress changed; coordinate state restore from transaction backup before rollback')
 # Refuse a rollback if any managed file was independently modified.
 for e in j['files']:
  if e['existed']:require(sha(safe_read(Path(e['backup'])))==e['old_sha'],'backup changed: '+e['target'])
  p=Path(e['target'])
  if e.get('phase')=='written':require(p.exists(),'managed file missing: '+str(p))
  if p.exists():
   current=sha(safe_read(p));require(current==e['post_sha'] or (e['existed'] and current==e.get('old_sha')),'modified since deployment: '+str(p))
 written={e['target'] for e in j['files']}
 for name in SERVICES:
  if str(UNITS/name) in written:cmd(['/usr/bin/systemctl','disable','--now',name],False)
 if j.get('collector_stop_intent'):cmd(['/usr/bin/systemctl','stop','embodied-pi-collect.service'])
 for e in reversed(j['files']):
  p=Path(e['target'])
  if e['existed']:
   original=safe_read(Path(e['backup']));require(sha(original)==e['old_sha'],'backup changed: '+str(p));write_atomic(p,original,e['mode'],e['uid'],e['gid'])
  elif p.exists():p.unlink()
 restore_task_state(tx,j)
 cmd(['/usr/bin/systemctl','daemon-reload'])
 for name,state in list(j.get('legacy_units',{}).items())+list(j.get('previous_units',{}).items()):
  if state['enabled']:cmd(['/usr/bin/systemctl','enable',name])
  if state['active']:cmd(['/usr/bin/systemctl','start',name])
 if j.get('collector_stop_intent'):cmd(['/usr/bin/systemctl','start','embodied-pi-collect.service'])
 j['status']='rolled_back';save_journal(tx/'journal.json',j)

def apply(m,files,root,plan):
 check_ancestors(STATE);require(not STATE.is_symlink(),'unsafe state directory');STATE.mkdir(parents=True,exist_ok=True,mode=0o700);require(STATE.stat().st_uid==0,'unsafe state owner');os.chmod(STATE,0o700)
 lock=os.open(str(STATE/'lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
 try:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  current=verify_environment(m,files,root);require(not current.get('already') and current==plan,'preflight state changed; retry plan')
  need_capture=bool(plan['collector_changes']) or any(x['before']!=x['after'] for x in plan.get('feature_plan',{}).values()) or any(x['active'] for x in plan['legacy_units'].values()) or any(x['active'] for x in plan.get('previous_release',{}).get('units',{}).values())
  capture=acquire_capture_guard(files,root) if need_capture else contextlib.nullcontext()
  with capture, acquire_feature_operation_locks(m):
   # Check identity, units and every collector preimage again while the lease is held.
   require(verify_environment(m,files,root)==plan,'state changed after capture lease')
   tx=STATE/('transaction-'+m['release_id']);require(not tx.exists(),'transaction already exists; inspect or roll back');tx.mkdir(mode=0o700)
   j={'schema':1,'status':'preparing','release_id':m['release_id'],'files':[],'legacy_units':plan['legacy_units'],'previous_units':plan.get('previous_release',{}).get('units',{}),'collector_stop_intent':False,'collector_changed':bool(plan['collector_changes']) or any(x['before']!=x['after'] for x in plan.get('feature_plan',{}).values()),'collector_root':str(root)};save_journal(tx/'journal.json',j)
   write_atomic(tx/'capture_guard.py',files['support/services/usb-phone-link/usb_data_host.py'],0o600)
   j['capture_guard_sha']=sha(files['support/services/usb-phone-link/usb_data_host.py']);save_journal(tx/'journal.json',j)
   try:
    if plan['collector_changes']:
     j['collector_stop_intent']=True;save_journal(tx/'journal.json',j);cmd(['/usr/bin/systemctl','stop','embodied-pi-collect.service'])
     backup_task_state(root,tx,j)
    for name,state in list(plan['legacy_units'].items())+list(plan.get('previous_release',{}).get('units',{}).items()):
     if state['active'] or state['enabled']:cmd(['/usr/bin/systemctl','disable','--now',name])
    for name,data in files.items():
     if name.startswith('collector/orangepi/'):
      relative=name[len('collector/orangepi/'):]
      if relative in plan['collector_changes']:
       target=root/relative;expected=plan['collector_prehash'][relative]
       require(not target.is_symlink() and ((expected is None and not target.exists()) or (expected is not None and target.exists() and sha(safe_read(target))==expected)),'collector changed since preflight: '+relative)
       backup_and_write(root/relative,data,0o644,j,tx)
     elif name.startswith('release/'):
      backup_and_write(Path(m['release_prefix'])/name[len('release/'):],data,m['files'][name]['mode'],j,tx)
    if plan.get('feature_plan'):
     require(plan_feature_helpers(m,files)==plan['feature_plan'],'feature helpers changed after preflight')
     user=cmd(['/usr/bin/systemctl','show','embodied-pi-collect.service','--property=User','--value']).stdout.strip()
     for target,entry in feature_files(files,user).items():
      if plan['feature_plan'][target]['before']==plan['feature_plan'][target]['after']:continue
      if target.startswith('/etc/sudoers.d/'):
       fd,temp=tempfile.mkstemp(prefix='headring-sudoers-',dir=str(tx))
       try:
        os.fchmod(fd,0o440)
        with os.fdopen(fd,'wb') as handle:handle.write(entry['data']);handle.flush();os.fsync(handle.fileno())
        cmd(['/usr/sbin/visudo','-cf',temp])
       finally:
        if os.path.exists(temp):os.unlink(temp)
      backup_and_write(Path(target),entry['data'],entry['mode'],j,tx)
    try:account=pwd.getpwnam('avahi')
    except KeyError:account=None
    try:group=grp.getgrnam('avahi')
    except KeyError:group=None
    require((account is None)==(group is None),'partial avahi account exists')
    if account is not None:require(account.pw_uid!=0 and account.pw_gid==group.gr_gid and account.pw_shell in ('/usr/sbin/nologin','/sbin/nologin','/bin/false'),'unsafe avahi account')
    else:
     j['avahi_account_creation']='attempting';save_journal(tx/'journal.json',j)
     cmd(['/usr/sbin/adduser','--system','--group','--no-create-home','--disabled-login','--home','/run/avahi-daemon','--shell','/usr/sbin/nologin','avahi']);j['avahi_account_creation']='created_and_retained_on_rollback';save_journal(tx/'journal.json',j)
    for name,data in unit_templates(m['release_prefix']).items():backup_and_write(UNITS/name,data,0o644,j,tx)
    cmd(['/usr/bin/systemctl','daemon-reload'])
    for name in SERVICES:cmd(['/usr/bin/systemctl','enable','--now',name])
    time.sleep(1)
    for name in SERVICES:
     state=unit_state(name);require(state['active'] and state['enabled'],'service failed startup: '+name)
    if j['collector_stop_intent']:
     cmd(['/usr/bin/systemctl','start','embodied-pi-collect.service']);time.sleep(1)
     require(unit_state('embodied-pi-collect.service')['active'],'collector failed after restart')
    j['status']='complete';save_journal(tx/'journal.json',j)
   except BaseException:
    try:rollback(tx,j)
    except Exception as e:j['status']='rollback_needs_review';j['rollback_error']=type(e).__name__;save_journal(tx/'journal.json',j)
    raise
 finally:os.close(lock)

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=('plan','apply','rollback'));p.add_argument('--bundle',type=Path);p.add_argument('--sha256');p.add_argument('--collector-root',type=Path,default=Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi'));p.add_argument('--transaction',type=Path);a=p.parse_args()
 if a.action=='rollback':
  require(a.transaction is not None and a.transaction.parent==STATE and re.fullmatch(r'transaction-offline-[0-9a-f]{16}',a.transaction.name),'invalid transaction path');require(os.geteuid()==0,'run as root');lock=os.open(str(STATE/'lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  try:
   j=json.loads(safe_read(a.transaction/'journal.json'));require(j['status']!='rolled_back','already rolled back')
   guard_data=safe_read(a.transaction/'capture_guard.py');require(sha(guard_data)==j['capture_guard_sha'],'capture guard changed');context=acquire_capture_guard({'support/services/usb-phone-link/usb_data_host.py':guard_data},Path(j['collector_root'])) if (j.get('collector_changed') or any(x['active'] for x in j.get('legacy_units',{}).values()) or any(x['active'] for x in j.get('previous_units',{}).values())) else contextlib.nullcontext()
   with context:rollback(a.transaction,j)
  finally:os.close(lock)
  print('rollback complete');return
 require(a.bundle is not None,'bundle required');m,files=load_bundle(a.bundle,a.sha256);plan=verify_environment(m,files,a.collector_root);print(json.dumps({'action':a.action,'device_id':m['device_id'],'collector_changes':plan['collector_changes'],'legacy_units':plan['legacy_units'],'previous_release':plan.get('previous_release',{}),'usb':plan.get('usb'),'release_prefix':plan['release_prefix']}),flush=True)
 if a.action=='apply':
  if plan.get('already'):print('already installed; no changes')
  else:apply(m,files,a.collector_root,plan);print('deployment complete')
if __name__=='__main__':
 try:main()
 except (RuntimeError,ValueError,OSError,KeyError,subprocess.SubprocessError) as e:print('ERROR: '+str(e),file=sys.stderr);sys.exit(2)
