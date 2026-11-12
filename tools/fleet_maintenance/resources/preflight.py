#!/usr/bin/env python3
"""Read-only cp_0021 source/state inventory over the configured SSH alias."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import shlex
import subprocess
from pathlib import Path

REMOTE = r'''import fcntl,glob,hashlib,json,os,stat,subprocess,sys,time,urllib.request
request=json.load(sys.stdin); project=request['project']; paths=sorted(set(request['paths'])); modules=set(request.get('modules',[])); source_targets=set(request.get('source_targets',[]))
report={'schema':1,'observed_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'device_id':None,'project':project,'files':{},'errors':[]}
try: report['device_id']=open('/opt/egorecord/DEVICE_ID').read().strip()
except Exception as e: report['errors'].append('device_id:'+type(e).__name__)
def sha(path):
 h=hashlib.sha256()
 with open(path,'rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
 return h.hexdigest()
def state(rel):
 target=os.path.join(project,rel); parents=[]; cur=project
 for part in rel.split('/')[:-1]:
  cur=os.path.join(cur,part)
  try:
   s=os.lstat(cur); parents.append({'path':os.path.relpath(cur,project),'type':'symlink' if stat.S_ISLNK(s.st_mode) else 'dir' if stat.S_ISDIR(s.st_mode) else 'other','uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode)})
   if stat.S_ISLNK(s.st_mode) or not stat.S_ISDIR(s.st_mode): return {'type':'unsafe-parent','parents':parents}
  except FileNotFoundError: parents.append({'path':os.path.relpath(cur,project),'type':'absent'})
 try: s=os.lstat(target)
 except FileNotFoundError: return {'type':'absent','parents':parents}
 t='regular' if stat.S_ISREG(s.st_mode) else 'symlink' if stat.S_ISLNK(s.st_mode) else 'directory' if stat.S_ISDIR(s.st_mode) else 'other'
 result={'type':t,'uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode),'parents':parents}
 if t=='regular': result['sha256']=sha(target)
 return result
for rel in paths:
 try: report['files'][rel]=state(rel)
 except Exception as e: report['files'][rel]={'type':'error','error':type(e).__name__}
try:
 controller=json.load(open(project+'/orangepi/runtime/controller.json'))
 report['controller']={k:controller.get(k) for k in ('state','pid','busy','session')}
except Exception as e: report['controller_error']=type(e).__name__
try:
 token=open(project+'/orangepi/runtime/dashboard_token').read().strip()
 req=urllib.request.Request('http://127.0.0.1:8765/api/display-state',headers={'X-Access-Token':token})
 with urllib.request.urlopen(req,timeout=4) as response: d=json.load(response)
 privacy=d.get('privacy') or {}
 report['api']={'http':'ok','state':d.get('state'),'busy':d.get('busy'),'session':d.get('session'),
  'privacy':{k:privacy.get(k) for k in ('required','phase','stop_confirmed','session_id')}}
except Exception as e: report['api']={'http_error':type(e).__name__}
services={}
for service in ('embodied-pi-collect','camd'):
 p=subprocess.run(['systemctl','is-active',service],capture_output=True,text=True)
 services[service]=p.stdout.strip() or 'inactive'
report['services']=services
p=subprocess.run(['systemctl','show','-p','MainPID','--value','camd'],capture_output=True,text=True); report['camd_pid']=p.stdout.strip()
workers=[]
for cmdfile in glob.glob('/proc/[0-9]*/cmdline'):
 try:
  if int(cmdfile.split('/')[2])==os.getpid(): continue
  raw=open(cmdfile,'rb').read().replace(b'\0',b' ').decode(errors='replace')
  if any(s in raw for s in ('framework_recorder.py','capture_worker.py','acquisition/capture.py')): workers.append(cmdfile.split('/')[2])
 except Exception: pass
report['capture_workers']=workers
locks=[]
lock_paths=[project+'/orangepi/runtime/capture.lock']+glob.glob(project+'/profiles/*/orangepi/runtime/capture.lock')
for path in lock_paths:
 try:
  st=os.lstat(path)
  if stat.S_ISLNK(st.st_mode): locks.append({'path':os.path.relpath(path,project),'result':'symlink'}); continue
  fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
  try:
   fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); fcntl.flock(fd,fcntl.LOCK_UN); locks.append({'path':os.path.relpath(path,project),'result':'free'})
  except BlockingIOError: locks.append({'path':os.path.relpath(path,project),'result':'locked'})
  finally: os.close(fd)
 except Exception as e: locks.append({'path':os.path.relpath(path,project),'result':type(e).__name__})
report['capture_locks']=locks
# Match the flat import roots established by orangepi_collect/common/project_paths.py.
code_base=os.path.join(project,'orangepi'); active_base=code_base
try:
 profile=json.load(open(os.path.join(code_base,'runtime/collection_profile.json')))
 if profile.get('active_mode')!=profile.get('legacy_mode'):
  active_base=os.path.join(project,'profiles',profile['active_mode'],'orangepi')
except Exception: pass
src_root=os.path.join(code_base,'src/orangepi_collect')
roots=[os.path.join(active_base,'src')]
try: roots += [os.path.join(src_root,n) for n in os.listdir(src_root) if os.path.isdir(os.path.join(src_root,n))]
except OSError: pass
roots += [os.path.join(project,'src')]
shadow={}
for module in sorted(modules):
 hits=[]
 expected_real={os.path.realpath(os.path.join(project,rel)) for rel in source_targets if os.path.basename(rel)==module+'.py'}
 for root in roots:
  candidate=os.path.join(root,module+'.py')
  try:
   st=os.lstat(candidate)
   if stat.S_ISREG(st.st_mode):
    rel=os.path.relpath(candidate,project)
    if os.path.realpath(candidate) not in expected_real:
     hits.append({'path':rel,'sha256':sha(candidate),'root':os.path.relpath(root,project)})
  except FileNotFoundError: pass
 if hits: shadow[module]=hits
report['python_module_shadow_candidates']=shadow
helper='/usr/local/lib/collector-usb-task-import/mount-readonly'
try:
 st=os.stat(helper); report['usb_helper']={'exists':True,'uid':st.st_uid,'gid':st.st_gid,'mode':stat.S_IMODE(st.st_mode),'sha256':sha(helper)}
except FileNotFoundError: report['usb_helper']={'exists':False}
print(json.dumps(report,ensure_ascii=False,sort_keys=True))
'''

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--manifest', type=Path, required=True)
    ap.add_argument('--baseline', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--ssh-config', type=Path, default=Path('/home/l2-headring-qc/headring-qc/ssh/config'))
    ap.add_argument('--device', choices=('cp_0015','cp_0020','cp_0022'), required=True)
    args = ap.parse_args()
    manifest = json.loads(args.manifest.read_text())
    baseline = json.loads(args.baseline.read_text())
    paths = {f['path'] for f in manifest['files']} | {f['path'] for f in baseline}
    request = {'project': manifest['project'], 'paths': sorted(paths), 'modules': manifest.get('python_modules', []),
               'source_targets': [f['path'] for f in manifest['files']]}
    code = 'exec(compile(' + repr(REMOTE) + ',"remote_preflight","exec"))'
    cmd = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=yes',
           '-o','BindInterface=wlx40a5ef5ff812','-o','BindAddress=192.168.20.100','-F', str(args.ssh_config),
           *(['-o','HostName=192.168.20.109','-o','HostKeyAlias=192.168.1.119'] if args.device=='cp_0015' else []),
           args.device, 'python3 -c ' + shlex.quote(code)]
    result = subprocess.run(cmd, input=json.dumps(request).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise SystemExit(f'read-only SSH preflight failed (exit {result.returncode}); stderr: {result.stderr.decode(errors="replace")[-1000:]}')
    live = json.loads(result.stdout)
    if live.get('device_id') != args.device: raise SystemExit('device identity mismatch')
    baseline_by_path = {f['path']: f for f in baseline}
    delta=[]
    for path, old in baseline_by_path.items():
        cur=live['files'].get(path,{})
        expected_type='absent' if not old.get('exists') else ('symlink' if old.get('symlink') else 'regular')
        if cur.get('type') != expected_type or (expected_type=='regular' and cur.get('sha256') != old.get('sha256')):
            delta.append({'path':path,'baseline_sha256':old.get('sha256') if old.get('exists') else None,
                          'live_type':cur.get('type'),'live_sha256':cur.get('sha256')})
    live['baseline_delta']={'changed_or_missing_count':len(delta),'entries':delta}
    live['manifest']={'commit':manifest['commit'],'file_count':len(manifest['files']),'sha256':hashlib.sha256(args.manifest.read_bytes()).hexdigest()}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(live,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    summary={'out':str(args.out),'device_id':live.get('device_id'),'baseline_delta_count':len(delta),
             'api':live.get('api'),'services':live.get('services'),'capture_workers':live.get('capture_workers'),
             'capture_locks':live.get('capture_locks')}
    print(json.dumps(summary,ensure_ascii=False,sort_keys=True))

if __name__ == '__main__': main()
