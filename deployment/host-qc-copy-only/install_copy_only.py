#!/usr/bin/env python3
"""Reviewed same-UID switch under STOP. Never restores a queue database."""
import ast,hashlib,json,os,signal,sqlite3,stat,subprocess,sys,tempfile,time,urllib.request,uuid
from pathlib import Path

FILES=('service.py','handoff_receiver.py','receiver_state.py','receiver.html')
ROOT=Path('/home/l2-headring-qc/headring-qc');UNIT='headring-qc.service';API='http://127.0.0.1:8788'
def sha(data):return hashlib.sha256(data).hexdigest()
def require(value,message):
 if not value:raise RuntimeError(message)
def read_regular(path,limit=10_000_000):
 path=Path(path);fd=os.open(str(path),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 try:
  st=os.fstat(fd);require(stat.S_ISREG(st.st_mode) and st.st_size<=limit,'unsafe file '+str(path));parts=[];total=0
  while True:
   block=os.read(fd,1048576)
   if not block:break
   total+=len(block);require(total<=limit,'oversized file');parts.append(block)
  after=os.fstat(fd);require((st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns),'file changed while reading')
  return b''.join(parts),st
 finally:os.close(fd)
def atomic(path,data,mode):
 path=Path(path);fd,tmp=tempfile.mkstemp(prefix='.copy-only-',dir=str(path.parent))
 try:
  with os.fdopen(fd,'wb') as f:os.fchmod(f.fileno(),mode);f.write(data);f.flush();os.fsync(f.fileno())
  os.replace(tmp,path);sync_dir(path.parent)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
def sync_dir(path):
 fd=os.open(str(path),os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
def status():
 with urllib.request.urlopen(API+'/api/receive/status',timeout=5) as f:return json.load(f)
def unit(allow_inactive=False):
 raw=subprocess.check_output(['systemctl','show',UNIT,'-p','MainPID','-p','ControlGroup','-p','Restart','-p','ActiveState','-p','NeedDaemonReload','-p','WorkingDirectory','-p','ExecStart'],text=True)
 v=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
 require(v.get('Restart')=='always' and bool(v.get('ControlGroup')),'service is not under automatic restart')
 expected='argv[]='+str(ROOT/'.venv/bin/python')+' -u '+str(ROOT/'service.py')+' ;'
 require(v.get('NeedDaemonReload')=='no' and v.get('WorkingDirectory')==str(ROOT) and expected in v.get('ExecStart',''),'service unit differs or needs reload')
 pid=int(v.get('MainPID','0'))
 if allow_inactive and (pid<=1 or v.get('ActiveState')!='active'):return 0,v['ControlGroup']
 require(v.get('ActiveState')=='active' and pid>1,'service is not active')
 p=Path('/proc')/str(pid);owner=p.stat().st_uid
 require(owner==os.geteuid(),'service user differs from installer')
 groups=(p/'cgroup').read_text().splitlines();require(any(x.split(':',2)[-1]==v['ControlGroup'] for x in groups),'PID is not in expected service cgroup')
 return pid,v['ControlGroup']
def guarded_signal(pid):
 fd=os.pidfd_open(pid,0)
 try:
  now,_=unit();require(now==pid,'service PID changed before signal');signal.pidfd_send_signal(fd,signal.SIGTERM)
 finally:os.close(fd)
def db_state(connection):
 row=connection.execute('SELECT receiving,revision FROM control WHERE id=1').fetchone();count=connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
 require(row==(0,2) and count==0,'receiver is not STOP/rev2 with zero jobs')
def read_status_stopped(expected_version=None):
 x=status();control=x.get('control') or {}
 require(control.get('receiving') is False and control.get('revision')==2 and control.get('stopping') is False and not x.get('jobs'),'receiver API is not stopped/empty')
 if expected_version=='legacy':require(x.get('copy_only') is not True,'legacy service did not recover')
 elif expected_version is not None:require(x.get('copy_only') is True and x.get('copy_only_version')==expected_version,'copy-only service fingerprint missing')
 return x
def install(stage,manifest_hash):
 stage=Path(stage);manifest_bytes,_=read_regular(stage/'manifest.json');require(sha(manifest_bytes)==manifest_hash,'stage manifest hash mismatch')
 m=json.loads(manifest_bytes);require(m.get('schema')==1 and m.get('version')=='20261119-copy-only-v1' and set(m.get('files',{}))==set(FILES),'unexpected stage manifest')
 payload={};pre={};modes={}
 for name in FILES:
  data,_=read_regular(stage/name);require(sha(data)==m['files'][name]['after'],'stage file mismatch: '+name)
  if name.endswith('.py'):ast.parse(data,filename=name)
  payload[name]=data
  old,st=read_regular(ROOT/name);require(st.st_uid==os.geteuid() and sha(old)==m['files'][name]['before'],'preimage mismatch: '+name);pre[name]=old;modes[name]=stat.S_IMODE(st.st_mode)
 config_bytes,_=read_regular(ROOT/'config.json');cfg=json.loads(config_bytes);require(cfg.get('receive_strategy')=='manual_handoff_v1','unexpected receiver strategy')
 db=Path(cfg.get('receiver_state_root',str(ROOT/'state/receiver')))/'receiver.sqlite3';require(db.is_absolute() and not db.is_symlink() and db.is_file(),'receiver DB unavailable')
 read_status_stopped('legacy');old_pid,cgroup=unit()
 backup_parent=ROOT/'backups'
 if not backup_parent.exists():backup_parent.mkdir(mode=0o700);sync_dir(ROOT)
 require(not backup_parent.is_symlink() and backup_parent.is_dir(),'unsafe backup parent')
 backup=backup_parent/('copy-only-'+str(time.time_ns()));backup.mkdir(mode=0o700);sync_dir(backup_parent)
 for name in FILES:atomic(backup/name,pre[name],modes[name])
 atomic(backup/'manifest.json',manifest_bytes,0o600)
 # Consistent evidence snapshot only. Never use it to roll a live DB backward.
 src=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True,timeout=10);dst=sqlite3.connect(backup/'receiver-evidence.sqlite3')
 try:src.backup(dst);dst.commit()
 finally:dst.close();src.close()
 with (backup/'receiver-evidence.sqlite3').open('rb') as f:os.fsync(f.fileno())
 sync_dir(backup)
 conn=sqlite3.connect(db,timeout=10);changed=[];signaled=False
 try:
  conn.execute('BEGIN IMMEDIATE');db_state(conn)
  require(sha(read_regular(ROOT/'config.json')[0])==sha(config_bytes),'configuration changed')
  pid,cg=unit();require((pid,cg)==(old_pid,cgroup),'service changed during preflight')
  for name in FILES:
   require(sha(read_regular(ROOT/name)[0])==m['files'][name]['before'],'source changed before switch')
   changed.append(name)  # intent before replace; fsync itself may fail
   atomic(ROOT/name,payload[name],modes[name])
  guarded_signal(old_pid);signaled=True
  conn.commit()
 except BaseException:
  # With the database write lock held, no new receive can have been authorized.
  for name in reversed(changed):
   current,_=read_regular(ROOT/name);current_sha=sha(current)
   if current_sha==m['files'][name]['before']:continue
   require(current_sha==m['files'][name]['after'],'cannot safely restore changed source')
   atomic(ROOT/name,pre[name],modes[name])
  conn.rollback()
  if signaled:
   # If a restart began while restoration was in progress, terminate only the
   # verified replacement unit and wait for the original program to recover.
   for _ in range(60):
    try:
     candidate,cg=unit(allow_inactive=True);require(cg==cgroup,'unexpected service cgroup')
     if candidate and candidate!=old_pid:
      x=status()
      if x.get('copy_only') is True:guarded_signal(candidate)
      else:read_status_stopped('legacy');break
    except Exception:pass
    time.sleep(1)
   else:raise RuntimeError('legacy service did not recover after interrupted switch')
  raise
 finally:conn.close()
 try:
  for _ in range(60):
   try:
    pid,cg=unit()
    if pid!=old_pid and cg==cgroup:
     read_status_stopped(m['version']);break
   except Exception:pass
   time.sleep(1)
  else:raise RuntimeError('copy-only service did not recover safely')
  c=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True,timeout=5)
  try:db_state(c)
  finally:c.close()
  for name in FILES:require(sha(read_regular(ROOT/name)[0])==m['files'][name]['after'],'installed source changed')
  atomic(backup/'installed.json',json.dumps({'version':m['version'],'old_pid':old_pid,'new_pid':pid,'db_restored':False}).encode(),0o600)
  print(json.dumps({'result':'installed','version':m['version'],'backup':str(backup),'old_pid':old_pid,'new_pid':pid}))
 except BaseException:
  # Roll back program files only, and only if STOP/zero jobs still holds. A
  # changed database is never restored from the evidence snapshot.
  recovery=sqlite3.connect(db,timeout=10)
  try:
   recovery.execute('BEGIN IMMEDIATE');db_state(recovery);pid,cg=unit(allow_inactive=True);require(cg==cgroup,'unexpected service cgroup')
   for name in FILES:
    current,_=read_regular(ROOT/name);require(sha(current)==m['files'][name]['after'],'cannot roll back changed source')
   for name in FILES:atomic(ROOT/name,pre[name],modes[name])
   if pid:guarded_signal(pid)
   recovery.commit()
  except BaseException:
   recovery.rollback();raise
  finally:recovery.close()
  for _ in range(60):
   try:
    newpid,_=unit()
    if newpid!=pid:read_status_stopped('legacy');break
   except Exception:pass
   time.sleep(1)
  else:raise RuntimeError('legacy service did not recover after program rollback')
  raise
if __name__=='__main__':
 require(len(sys.argv)==3,'usage: install_copy_only.py STAGE MANIFEST_SHA256')
 install(sys.argv[1],sys.argv[2])
