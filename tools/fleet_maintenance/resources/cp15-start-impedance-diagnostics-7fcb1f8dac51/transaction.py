#!/usr/bin/env python3
"""Root-side, rollback-capable cp_0021 source transaction installer."""
from __future__ import annotations
import argparse
import compileall
from contextlib import contextmanager
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

PROJECT = Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2')
OPT_ROOT = Path('/opt/egorecord')
DEVICE_ID = Path('/opt/egorecord/DEVICE_ID')
TARGET_DEVICE = None
ALLOWED = ('orangepi/src/', 'orangepi/scripts/', 'src/embodied_brain_collect/')
EXTS = {'.py', '.js', '.html', '.css', '.wav', '.sh'}
BLOCKED = {'__pycache__','tests','test','runtime','configs','private','data','calibration','used','images'}
SAFE_STATES = {'device_confirm','idle','failed_wait','exhausted'}
STOPPED_STATES = SAFE_STATES | {'stopped'}

class Abort(RuntimeError): pass

def run(argv, *, check=True, timeout=30, input_bytes=None):
    p = subprocess.run(argv, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and p.returncode:
        raise Abort(f'command failed: {argv[0]} (exit {p.returncode})')
    return p

def digest(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def valid_rel(rel: str) -> bool:
    p=PurePosixPath(rel)
    return (rel != 'orangepi/src/orangepi_collect/control/reference_credentials.py'
        and p.is_absolute() is False and bool(p.parts) and '..' not in p.parts
        and any(rel.startswith(root) for root in ALLOWED)
        and not any(part in BLOCKED or part.startswith('.') for part in p.parts)
        and p.suffix.lower() in EXTS
        and (not rel.startswith('src/embodied_brain_collect/') or p.suffix.lower()=='.py'))

def safe_target(rel: str) -> Path:
    if not valid_rel(rel): raise Abort(f'path outside source allowlist: {rel}')
    target=PROJECT.joinpath(*PurePosixPath(rel).parts)
    cur=PROJECT
    if cur.is_symlink() or not cur.is_dir(): raise Abort('project root is not a real directory')
    for part in PurePosixPath(rel).parts[:-1]:
        cur=cur/part
        try: s=cur.lstat()
        except FileNotFoundError: raise Abort(f'parent directory missing: {cur}')
        if stat.S_ISLNK(s.st_mode) or not stat.S_ISDIR(s.st_mode): raise Abort(f'unsafe parent directory: {cur}')
    return target

def file_state(rel: str) -> dict:
    target=safe_target(rel)
    parents=[]; cur=PROJECT
    for part in PurePosixPath(rel).parts[:-1]:
        cur=cur/part; s=cur.lstat()
        parents.append({'path':str(cur.relative_to(PROJECT)),'type':'dir','uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode)})
    try: s=target.lstat()
    except FileNotFoundError: return {'type':'absent','parents':parents}
    if stat.S_ISREG(s.st_mode): kind='regular'
    elif stat.S_ISLNK(s.st_mode): kind='symlink'
    elif stat.S_ISDIR(s.st_mode): kind='directory'
    else: kind='other'
    result={'type':kind,'uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode),'parents':parents}
    if kind=='regular': result['sha256']=digest(target)
    return result

def snapshot(paths):
    return {rel:file_state(rel) for rel in sorted(set(paths))}

def protected_snapshot():
    """Hash protected calibration/config/identity trees without following links or data."""
    import yaml
    roots=[OPT_ROOT/'calibrations', DEVICE_ID, OPT_ROOT/'HARDWARE_MODEL',
           OPT_ROOT/'camd/camd.conf', PROJECT/'orangepi/configs',
           PROJECT/'orangepi/src/orangepi_collect/control/reference_credentials.py']
    for name in ('wrist_calibration.json','emg_calibration.json','device_bindings.json','collection_profile.json'):
        roots.append(PROJECT/'orangepi/runtime'/name)
    for profile in (PROJECT/'profiles').glob('*/orangepi'):
        roots.append(profile/'configs')
        for name in ('wrist_calibration.json','emg_calibration.json','device_bindings.json','collection_profile.json'):
            roots.append(profile/'runtime'/name)
    configs=[PROJECT/'orangepi/configs/devices.yaml']+list((PROJECT/'profiles').glob('*/orangepi/configs/devices.yaml'))
    for config in configs:
        if config.is_symlink() or not config.is_file(): raise Abort('unsafe or missing devices config')
        value=yaml.safe_load(config.read_text()) or {}
        source=(value.get('headband_calibration') or {}).get('source_dir')
        if source and source!=str(OPT_ROOT/'calibrations'): raise Abort('unreviewed calibration source_dir')
    result={}
    def visit(path):
        key=str(path)
        if key in result: return
        try: st=path.lstat()
        except FileNotFoundError: result[key]={'type':'absent'};return
        item={'uid':st.st_uid,'gid':st.st_gid,'mode':stat.S_IMODE(st.st_mode)}
        if stat.S_ISLNK(st.st_mode):
            # Do not follow a symlink into data or unreviewed external paths.
            raise Abort('unreviewed protected symlink: '+key)
        elif stat.S_ISDIR(st.st_mode):
            item['type']='directory';result[key]=item
            for child in sorted(path.iterdir()): visit(child)
            return
        elif stat.S_ISREG(st.st_mode): item.update(type='regular',sha256=digest(path))
        else: raise Abort('unreviewed protected object: '+key)
        result[key]=item
    for root in roots: visit(root)
    return result

def require_root_private_tree(path):
    """Reject user-writable root inputs, including symlinks and unsafe ancestors."""
    path=Path(path)
    if not path.is_absolute(): raise Abort('input path must be absolute')
    for parent in [path]+list(path.parents):
        st=parent.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid!=0 or st.st_mode & 0o022:
            raise Abort('input path or ancestor not root-owned and non-writable: '+str(parent))
    if path.is_dir():
        for base,dirs,files in os.walk(str(path),followlinks=False):
            for name in dirs+files:
                child=Path(base)/name;st=child.lstat()
                if stat.S_ISLNK(st.st_mode) or st.st_uid!=0 or st.st_mode & 0o022:
                    raise Abort('unsafe input tree object: '+str(child))

def api_state() -> dict:
    token_path=PROJECT/'orangepi/runtime/dashboard_token'
    token=token_path.read_text().strip()
    req=urllib.request.Request('http://127.0.0.1:8765/api/display-state',headers={'X-Access-Token':token})
    with urllib.request.urlopen(req,timeout=4) as response: data=json.load(response)
    privacy=data.get('privacy') or {}
    return {'state':data.get('state'),'busy':data.get('busy'),'session':data.get('session'),
            'privacy':{k:privacy.get(k) for k in ('required','phase','stop_confirmed','session_id')}}

def assert_idle(api):
    p=api.get('privacy') or {}
    if api.get('state') not in SAFE_STATES or api.get('busy') is not False or api.get('session') is not None:
        raise Abort('controller is not idle')
    if p.get('required') is not False or p.get('phase')!='idle' or p.get('stop_confirmed') is not True or p.get('session_id'):
        raise Abort('privacy/stop state is not idle and confirmed')

def service_active(name):
    return run(['systemctl','is-active',name],check=False).stdout.decode().strip()=='active'

def camd_pid():
    return run(['systemctl','show','-p','MainPID','--value','camd']).stdout.decode().strip()

def capture_workers(proc_root=Path('/proc')):
    found=[]
    for p in Path(proc_root).glob('[0-9]*/cmdline'):
        try:
            if p.parts[-2]==str(os.getpid()): continue
            raw=p.read_bytes().replace(b'\0',b' ').decode(errors='replace')
            if any(name in raw for name in ('framework_recorder.py','capture_worker.py','acquisition/capture.py')):
                found.append(p.parts[-2])
        except OSError: pass
    return sorted(found)

def capture_lock_paths():
    candidates=[PROJECT/'orangepi/runtime/capture.lock']
    candidates += list((PROJECT/'profiles').glob('*/orangepi/runtime/capture.lock'))
    if not candidates[0].is_file() or candidates[0].is_symlink(): raise Abort('primary capture lock missing or unsafe')
    return [path for path in candidates if path.exists() or path.is_symlink()]

def check_locks():
    results=[]
    for path in capture_lock_paths():
        st=path.lstat()
        if stat.S_ISLNK(st.st_mode): raise Abort('capture.lock is a symlink')
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        try:
            try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise Abort(f'capture lock is held: {path.parent.name}')
            finally:
                try: fcntl.flock(fd,fcntl.LOCK_UN)
                except OSError: pass
        finally: os.close(fd)
        results.append(str(path.relative_to(PROJECT)))
    if capture_workers(): raise Abort('capture worker process is present')
    return results

@contextmanager
def hold_capture_locks():
    """Hold existing profile lock files across a code mutation; never create locks."""
    if capture_workers(): raise Abort('capture worker process is present before lock acquisition')
    paths=capture_lock_paths(); opened=[]
    try:
        for path in paths:
            st=path.lstat()
            if not stat.S_ISREG(st.st_mode): raise Abort(f'capture lock is not a regular file: {path}')
            fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            actual=os.fstat(fd)
            if (actual.st_dev,actual.st_ino)!=(st.st_dev,st.st_ino):
                os.close(fd); raise Abort(f'capture lock changed during open: {path}')
            try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd); raise Abort(f'capture lock is held: {path}')
            opened.append((fd,path,actual.st_dev,actual.st_ino))
        if set(capture_lock_paths())!=set(paths) or capture_workers():
            raise Abort('capture gates changed during lock acquisition')
        yield [str(path.relative_to(PROJECT)) for _,path,_,_ in opened]
    finally:
        for fd,_,_,_ in reversed(opened):
            try: fcntl.flock(fd,fcntl.LOCK_UN)
            finally: os.close(fd)

def assert_stopped_service_state(locks_held=False):
    """After stop only: tolerate launcher.state='stopped' once all capture gates are clear."""
    if service_active('embodied-pi-collect'): raise Abort('controller service is still active after stop')
    if capture_workers(): raise Abort('capture worker remains after controller stop')
    if not locks_held: check_locks()
    try: state=json.loads((PROJECT/'orangepi/runtime/controller.json').read_text())
    except FileNotFoundError: raise Abort('controller state disappeared after service stop')
    if state.get('state') not in STOPPED_STATES or state.get('session') is not None or state.get('busy') is True:
        raise Abort('controller state is not idle/stopped after service stop')

@contextmanager
def stop_controller_and_verify(preflight_files):
    """Hold capture locks across stop verification and source replacement."""
    assert_idle(api_state()); check_locks()
    with hold_capture_locks() as held:
        assert_idle(api_state())
        run(['systemctl','stop','embodied-pi-collect'],timeout=60)
        time.sleep(0.5)
        if snapshot(preflight_files.keys())!=preflight_files:
            raise Abort('source tree changed while stopping controller')
        assert_stopped_service_state(locks_held=True)
        yield held

@contextmanager
def rollback_mutation_guard():
    """Check before rollback stop, then lock out capture through restoration."""
    was_active=service_active('embodied-pi-collect')
    if was_active:
        assert_idle(api_state()); check_locks()
    else:
        assert_stopped_service_state()
    with hold_capture_locks():
        if was_active:
            assert_idle(api_state())
            try: run(['systemctl','stop','embodied-pi-collect'],timeout=60)
            except Exception:
                if service_active('embodied-pi-collect'): raise
            time.sleep(0.5)
        assert_stopped_service_state(locks_held=True)
        yield

def require_device():
    if not re.fullmatch(r'cp_[0-9]{4}', TARGET_DEVICE or '') or DEVICE_ID.read_text().strip()!=TARGET_DEVICE: raise Abort('device identity mismatch')
    if PROJECT.resolve()!=Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2'): raise Abort('project path mismatch')

def load_bundle(directory: Path):
    if directory.is_symlink() or not directory.is_dir(): raise Abort('bundle directory is invalid')
    manifest_path=directory/'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file(): raise Abort('bundle manifest is missing')
    payload=directory/'payload'
    if payload.is_symlink() or not payload.is_dir(): raise Abort('bundle payload directory is invalid')
    manifest=json.loads(manifest_path.read_text())
    if manifest.get('schema')!=1 or manifest.get('device_id')!='per-target-not-yet-bound' or manifest.get('project')!=str(PROJECT): raise Abort('bundle identity/schema mismatch')
    if manifest.get('commit')!='f5d03b7b5fb197eb76e78610a0bce672e5cc4853': raise Abort('bundle commit is not the reviewed release')
    files=manifest.get('files')
    if not isinstance(files,list) or len(files)!=236: raise Abort('bundle must contain exactly 236 reviewed files')
    seen=set()
    for item in files:
        rel=item.get('path','')
        if rel in seen or not valid_rel(rel): raise Abort(f'invalid or duplicate path in manifest: {rel}')
        seen.add(rel)
        src=payload
        for part in PurePosixPath(rel).parts:
            src=src/part
            try: st=src.lstat()
            except FileNotFoundError: raise Abort(f'bundle payload missing: {rel}')
            if stat.S_ISLNK(st.st_mode): raise Abort(f'bundle payload path contains symlink: {rel}')
        if not stat.S_ISREG(st.st_mode): raise Abort(f'bundle payload is not a regular file: {rel}')
        if src.stat().st_size!=item.get('size') or digest(src)!=item.get('sha256'): raise Abort(f'bundle payload hash/size mismatch: {rel}')
    return manifest

def compare_preflight(report_path: Path, manifest, manifest_sha: str, baseline_path: Path):
    if report_path.is_symlink() or not report_path.is_file(): raise Abort('preflight report missing or symlinked')
    report=json.loads(report_path.read_text())
    if report.get('device_id')!=TARGET_DEVICE or report.get('project')!=str(PROJECT): raise Abort('preflight identity mismatch')
    if report.get('manifest',{}).get('commit')!=manifest['commit']: raise Abort('preflight was generated for another commit')
    if report.get('manifest',{}).get('sha256')!=manifest_sha: raise Abort('preflight was generated for another manifest')
    try: observed=datetime.datetime.strptime(report['observed_utc'],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
    except Exception: raise Abort('preflight timestamp is invalid')
    age=(datetime.datetime.now(datetime.timezone.utc)-observed).total_seconds()
    if age>600 or age < -30: raise Abort('preflight timestamp is stale or in the future')
    paths=report.get('files')
    if not isinstance(paths,dict) or not paths: raise Abort('preflight inventory is empty')
    baseline=json.loads(baseline_path.read_text())
    required={f['path'] for f in baseline}|{f['path'] for f in manifest['files']}
    if not required.issubset(paths): raise Abort('preflight inventory is incomplete')
    current=snapshot(paths.keys())
    if current!=paths: raise Abort('source tree changed since read-only preflight')
    actual_delta=[]
    for old in baseline:
        rel=old['path']; cur=paths[rel]
        expected='absent' if not old.get('exists') else ('symlink' if old.get('symlink') else 'regular')
        if cur.get('type')!=expected or (expected=='regular' and cur.get('sha256')!=old.get('sha256')):
            actual_delta.append(rel)
    if actual_delta: raise Abort(f'{len(actual_delta)} baseline source path(s) drifted; review and refresh the baseline before deployment')
    if report.get('baseline_delta',{}).get('changed_or_missing_count')!=0: raise Abort('read-only preflight reports source drift against the saved baseline')
    shadows=report.get('python_module_shadow_candidates') or {}
    for module,hits in shadows.items():
        if module!='reference_credentials' or any(x.get('path')!='orangepi/src/orangepi_collect/control/reference_credentials.py' for x in hits):
            raise Abort('unexpected Python module shadow: '+module)
    if report.get('capture_workers') or any(x.get('result')!='free' for x in report.get('capture_locks',[])):
        raise Abort('preflight observed active capture workers/locks')
    if report.get('services',{}).get('embodied-pi-collect')!='active' or report.get('services',{}).get('camd')!='active':
        raise Abort('required services were not active at preflight')
    assert_idle(report.get('api') or {})
    return report

def require_regular_parent_tree(rel):
    target=safe_target(rel)
    try: s=target.lstat()
    except FileNotFoundError: return target, None
    if not stat.S_ISREG(s.st_mode): raise Abort(f'target is not a regular file: {rel}')
    return target,s

def backup_files(files, backup_dir, preflight_files):
    backup_dir.mkdir(parents=True,mode=0o700)
    meta={}
    for item in files:
        rel=item['path']; target,st=require_regular_parent_tree(rel)
        if st is None:
            meta[rel]={'exists':False}; continue
        backup=backup_dir/'files'/rel
        backup.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(target,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        opened=os.fstat(fd)
        if (opened.st_dev,opened.st_ino)!=(st.st_dev,st.st_ino):
            os.close(fd); raise Abort(f'target changed while opening backup: {rel}')
        with os.fdopen(fd,'rb') as src, backup.open('xb') as dst: shutil.copyfileobj(src,dst)
        os.chmod(backup,0o600)
        expected=preflight_files.get(rel,{})
        if expected.get('type')!='regular' or digest(backup)!=expected.get('sha256'):
            raise Abort(f'target changed while creating backup: {rel}')
        meta[rel]={'exists':True,'sha256':digest(backup),'uid':st.st_uid,'gid':st.st_gid,'mode':stat.S_IMODE(st.st_mode)}
    (backup_dir/'metadata.json').write_text(json.dumps(meta,sort_keys=True,indent=2)+'\n')
    return meta

def stage_and_compile(bundle_dir, stage_dir):
    payload=bundle_dir/'payload'; stage=stage_dir/'stage'/'payload'
    stage.mkdir(parents=True,exist_ok=False)
    for item in []: pass
    # Copy the validated payload to a private staging tree, retaining only listed files.
    manifest=json.loads((bundle_dir/'manifest.json').read_text())
    for item in manifest['files']:
        rel=item['path']; dst=stage/rel; dst.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(payload/rel,dst); os.chmod(dst,0o700 if item['git_mode']=='100755' else 0o600)
    hw=PROJECT/'orangepi/.venvs/hardware/bin/python'
    fw=PROJECT/'orangepi/.venvs/framework/bin/python'
    for interpreter, expected in ((hw,(3,8)),(fw,(3,11))):
        version=run([str(interpreter),'-c','import sys;print("%d.%d"%sys.version_info[:2])']).stdout.decode().strip()
        if version!='.'.join(map(str,expected)): raise Abort(f'{interpreter} is not Python {expected[0]}.{expected[1]}')
    hw_dirs=[stage/'orangepi/src/orangepi_collect/acquisition',stage/'orangepi/src/orangepi_collect/audio',
             stage/'orangepi/src/orangepi_collect/common',stage/'orangepi/src/orangepi_collect/control',
             stage/'orangepi/src/orangepi_collect/storage']
    hw_dirs += [p for p in (stage/'orangepi/scripts').rglob('*.py')]
    for path in hw_dirs:
        if path.exists():
            target=path.parent if path.is_file() else path
            run([str(hw),'-m','compileall','-q',str(target)],timeout=120)
    fw_dirs=[stage/'orangepi/src/orangepi_collect/framework',stage/'orangepi/src/orangepi_collect/recorders',
             stage/'src/embodied_brain_collect']
    for path in fw_dirs:
        if path.exists(): run([str(fw),'-m','compileall','-q',str(path)],timeout=120)

def atomic_install(files, bundle_dir, backup_meta, preflight_files, installed_paths):
    for item in files:
        rel=item['path']; target,_=require_regular_parent_tree(rel)
        if file_state(rel)!=preflight_files[rel]: raise Abort(f'target changed immediately before replacement: {rel}')
        content=(bundle_dir/'payload'/rel).read_bytes()
        if hashlib.sha256(content).hexdigest()!=item['sha256']: raise Abort(f'payload changed during install: {rel}')
        parent=target.parent
        old=backup_meta[rel]
        if old['exists']:
            uid,gid,mode=old['uid'],old['gid'],old['mode']
        else:
            ps=parent.stat(); uid,gid=ps.st_uid,ps.st_gid
            mode=0o755 if item['git_mode']=='100755' else 0o644
        fd,tmp_name=tempfile.mkstemp(prefix='.cp21-deploy-',dir=parent)
        tmp=Path(tmp_name)
        try:
            with os.fdopen(fd,'wb') as f:
                f.write(content); f.flush(); os.fsync(f.fileno())
            os.chown(tmp,uid,gid); os.chmod(tmp,mode)
            os.replace(tmp,target)
            # Record immediately: directory fsync can fail after os.replace has committed.
            installed_paths.append(rel)
            dfd=os.open(parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try: os.fsync(dfd)
            finally: os.close(dfd)
        finally:
            if tmp.exists(): tmp.unlink()

def rollback(installed_paths, backup_dir, backup_meta, installed_hashes):
    problems=[]
    for rel in reversed(installed_paths):
        try:
            target,_=require_regular_parent_tree(rel)
            current=target.lstat()
            if not stat.S_ISREG(current.st_mode) or digest(target)!=installed_hashes[rel]:
                problems.append({'path':rel,'error':'target changed after install; preserved external content'})
                continue
            old=backup_meta[rel]
            if not old['exists']:
                target.unlink()
            else:
                src=backup_dir/'files'/rel
                if digest(src)!=old.get('sha256'):
                    problems.append({'path':rel,'error':'backup hash mismatch; target preserved'})
                    continue
                fd,tmp_name=tempfile.mkstemp(prefix='.cp21-rollback-',dir=target.parent); tmp=Path(tmp_name)
                try:
                    with os.fdopen(fd,'wb') as dst, src.open('rb') as source:
                        shutil.copyfileobj(source,dst); dst.flush(); os.fsync(dst.fileno())
                    os.chown(tmp,old['uid'],old['gid']); os.chmod(tmp,old['mode']); os.replace(tmp,target)
                finally:
                    if tmp.exists(): tmp.unlink()
        except Exception as e: problems.append({'path':rel,'error':type(e).__name__})
    return problems

def verify_restored(installed_paths, backup_dir, backup_meta):
    problems=[]
    for rel in installed_paths:
        try:
            target=safe_target(rel); old=backup_meta[rel]
            try: st=target.lstat()
            except FileNotFoundError:
                if old['exists']: problems.append({'path':rel,'error':'restored file is missing'})
                continue
            if not old['exists']:
                problems.append({'path':rel,'error':'new file remains after rollback'})
                continue
            if not stat.S_ISREG(st.st_mode) or digest(target)!=old['sha256']:
                problems.append({'path':rel,'error':'restored file hash/type verification failed'})
                continue
            if (st.st_uid,st.st_gid,stat.S_IMODE(st.st_mode))!=(old['uid'],old['gid'],old['mode']):
                problems.append({'path':rel,'error':'restored file metadata verification failed'})
        except Exception as exc: problems.append({'path':rel,'error':'verify:'+type(exc).__name__})
    return problems

def health_check(manifest, camd_before):
    for item in manifest['files']:
        target=PROJECT/item['path']
        if target.is_symlink() or not target.is_file() or digest(target)!=item['sha256']:
            raise Abort(f'post-install hash mismatch: {item["path"]}')
    if not service_active('embodied-pi-collect'): raise Abort('controller service is not active')
    if not service_active('camd') or camd_pid()!=camd_before: raise Abort('camd service/PID changed during transaction')
    deadline=time.monotonic()+35
    last=None
    while time.monotonic()<deadline:
        try:
            last=api_state(); assert_idle(last); return last
        except Exception: time.sleep(1)
    raise Abort(f'controller API did not return healthy idle state ({type(last).__name__ if last else "unreachable"})')

def write_json(path, value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    os.chmod(tmp,0o600); os.replace(tmp,path)

def write_json_best_effort(path, value):
    try:
        write_json(path,value)
        return None
    except Exception as exc:
        return {'path':str(path),'error':type(exc).__name__,'message':str(exc)[:240]}

def recover_after_failure(record, diag_path, installed_paths, files, backup_meta,
                          backup_dir, camd_before):
    """Best-effort diagnostics around a safety-gated, selective rollback."""
    diag_errors=[]; rollback_errors=[]; restore_error=None
    record.update(status='rolling_back',replaced_paths=list(installed_paths))
    first_diag_error=write_json_best_effort(diag_path,record)
    if first_diag_error: diag_errors.append(first_diag_error)
    if installed_paths and backup_meta is not None:
        try:
            with rollback_mutation_guard():
                rollback_errors=rollback(installed_paths,backup_dir,backup_meta,
                                         {item['path']:item['sha256'] for item in files})
                if not rollback_errors:
                    rollback_errors=verify_restored(installed_paths,backup_dir,backup_meta)
        except Exception as exc:
            rollback_errors.append({'path':'rollback_safety_gate','error':type(exc).__name__,'message':str(exc)[:240]})
    # Never restart mixed or unverified code. Only restore the prior active
    # service state after every changed file has been restored and verified.
    if not rollback_errors:
        try:
            if not service_active('embodied-pi-collect'):
                assert_stopped_service_state()
                run(['systemctl','start','embodied-pi-collect'],timeout=60)
                assert_idle(api_state())
                if camd_pid()!=camd_before: raise Abort('camd PID changed during rollback')
        except Exception as exc:
            restore_error={'error':type(exc).__name__,'message':str(exc)[:240]}
    record.update(status='rolled_back' if not rollback_errors and restore_error is None else 'rollback_incomplete',
                  replaced_paths=list(installed_paths),rollback_errors=rollback_errors,
                  service_restore_error=restore_error,diag_write_errors=diag_errors,
                  finished_local=datetime.datetime.now().isoformat(timespec='seconds'))
    final_diag_error=write_json_best_effort(diag_path,record)
    if final_diag_error: diag_errors.append(final_diag_error)
    record['diag_write_errors']=diag_errors
    return record

def main():
    global TARGET_DEVICE
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--bundle-dir',type=Path,required=True,help='directory containing manifest.json and payload/')
    ap.add_argument('--preflight',type=Path,required=True,help='fresh report produced by preflight_cp21.py')
    ap.add_argument('--baseline',type=Path,required=True,help='saved 233-file cp_0021 source baseline')
    ap.add_argument('--apply',action='store_true',help='required explicit execution guard')
    ap.add_argument('--device',required=True)
    ap.add_argument('--manifest-sha256',required=True)
    ap.add_argument('--baseline-sha256',required=True)
    ap.add_argument('--preflight-sha256',required=True)
    args=ap.parse_args()
    TARGET_DEVICE=args.device
    if os.geteuid()!=0: raise SystemExit('must run as root')
    os.umask(0o077)
    require_device()
    require_root_private_tree(Path(__file__).absolute())
    for path in (args.bundle_dir,args.preflight,args.baseline): require_root_private_tree(path)
    if args.bundle_dir.is_symlink(): raise Abort('bundle directory must not be a symlink')
    if args.baseline.is_symlink(): raise Abort('baseline file must not be a symlink')
    bundle_dir=args.bundle_dir.resolve(); manifest=load_bundle(bundle_dir)
    manifest_sha=hashlib.sha256((bundle_dir/'manifest.json').read_bytes()).hexdigest()
    if manifest_sha!=args.manifest_sha256 or digest(args.baseline)!=args.baseline_sha256 or digest(args.preflight)!=args.preflight_sha256:
        raise Abort('pinned manifest, baseline or preflight SHA mismatch')
    report=compare_preflight(args.preflight,manifest,manifest_sha,args.baseline)
    api=api_state(); assert_idle(api)
    if not service_active('embodied-pi-collect') or not service_active('camd'): raise Abort('required service became inactive')
    locks=check_locks()
    camd_before=camd_pid()
    protected_before=protected_snapshot()
    if report.get('protected') != protected_before: raise Abort('protected state changed since preflight')
    if report.get('camd_pid') != camd_before: raise Abort('camd PID changed since preflight')
    if not args.apply:
        print(json.dumps({'status':'dry_run_passed','device_id':TARGET_DEVICE,'protected_entries':len(protected_before),'capture_locks':locks,'camd_pid':camd_before},sort_keys=True))
        return
    if all(report['files'][item['path']].get('type') == 'regular' and report['files'][item['path']].get('sha256') == item['sha256'] for item in manifest['files']):
        print(json.dumps({'status':'already_current','device_id':TARGET_DEVICE,'camd_pid':camd_before},sort_keys=True))
        return
    commit=manifest['commit']
    maintenance=Path('/var/backups/headring-code')
    maintenance.mkdir(mode=0o700,exist_ok=True)
    require_root_private_tree(maintenance)
    stamp=datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    tx=maintenance/f'{TARGET_DEVICE}-code-transaction-{commit[:12]}-{stamp}'
    tx.mkdir(mode=0o700)
    backup_dir=tx/'backup'; diag_dir=tx/'diag'; diag_dir.mkdir(mode=0o700)
    record={'device_id':TARGET_DEVICE,'project':str(PROJECT),'commit':commit,'started_local':stamp,'preflight_observed_utc':report['observed_utc'],
            'service_was_active':True,'camd_pid_before':camd_before,'capture_locks_before':locks,'status':'preparing'}
    write_json(diag_dir/'transaction.json',record)
    files=manifest['files']; installed_paths=[]; backup_meta=None
    try:
        write_json(diag_dir/'protected-before.json',protected_before)
        stage_and_compile(bundle_dir,tx)
        # Recheck source tree and all capture gates directly before service stop.
        if snapshot(report['files'].keys())!=report['files']: raise Abort('runtime source tree drifted after preflight')
        if protected_snapshot()!=protected_before: raise Abort('protected state changed after preflight')
        assert_idle(api_state()); check_locks()
        # Launcher normally publishes state='stopped'; only tolerate it after the
        # service is inactive and every recorder process/profile lock is clear.
        with stop_controller_and_verify(report['files']) as held_locks:
            backup_meta=backup_files(files,backup_dir,report['files'])
            record['status']='backed_up'; record['backup_metadata']=str(backup_dir/'metadata.json')
            record['capture_locks_held']=held_locks; write_json(diag_dir/'transaction.json',record)
            atomic_install(files,tx/'stage',backup_meta,report['files'],installed_paths)
        run(['systemctl','start','embodied-pi-collect'],timeout=60)
        api_after=health_check(manifest,camd_before)
        protected_after=protected_snapshot()
        write_json(diag_dir/'protected-after.json',protected_after)
        if protected_after!=protected_before: raise Abort('protected state changed during transaction')
        record.update(status='committed',finished_local=datetime.datetime.now().isoformat(timespec='seconds'),api_after=api_after,
                      camd_pid_after=camd_pid(),file_count=len(files),replaced_paths=list(installed_paths))
        write_json(diag_dir/'transaction.json',record)
        print(json.dumps({'status':'committed','commit':commit,'backup':str(backup_dir),'receipt':str(diag_dir/'transaction.json')},sort_keys=True))
    except Exception as exc:
        record.update(error_type=type(exc).__name__,error=str(exc)[:500])
        recover_after_failure(record,diag_dir/'transaction.json',installed_paths,files,backup_meta,backup_dir,camd_before)
        raise


# Generated, immutable canary-specific section; common transaction guards above
# are copied from the reviewed EEG delta without modifications.
PINNED_FILES = [{'path': 'orangepi/src/orangepi_collect/acquisition/eeg_bridge.py', 'before_sha256': 'b5e3d1bb6f88a66bc33c903d62ac54e385124518238c58ab8400e9c04341c455', 'sha256': '77d1cf50d3a7223beebb599fcb672267dbfc180771cdb99f1cab3ba1b68b6bb0', 'size': 8264, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/acquisition/eeg_worker.py', 'before_sha256': '7bcbf2b7bc92b8c3ac1ca25227ccf5dadc8576b49cca514281610e9783ebba73', 'sha256': '8706ac7d21cfeaa57ac8caf4e1404207d8196a960738754f4829fc777efc2457', 'size': 18553, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/common/eeg_impedance.py', 'before_sha256': 'c5e6fc15426a33cfa8c03dbcc3f296a79f475d6895dd84bdd023c7c500273104', 'sha256': 'bb5b2c438a9f8b5793b63dd85bf6e836f2ab5012cdaa7c9e402e87bb210b0ad5', 'size': 21016, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/common/impedance_quality.py', 'before_sha256': 'da24ab2e98343042a764199465907935bd2eb3d07ae5b9f905913f0857d42fab', 'sha256': 'dc88a6c31f9b7ae6cd30b62f0d129947b6167993ab117134c47def9c3df3e27f', 'size': 11206, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/common/impedance_record.py', 'before_sha256': 'dc437d13d2e488a361671949fc6dc9199a7148447191720bf2287c36a65a09d3', 'sha256': '49aeecb88d88dcb7673582a7a28157a66fb1a348e494415398964205f1fba09b', 'size': 20167, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/common/scan_registry.py', 'before_sha256': '50084038bc841655c6c89d15e0bced778195ae3c23a5dbea6d9de34fa0c34048', 'sha256': 'e995db7a6111f93c53e6f2667a5f629dddd433734b12351e7c1ba1ee8a4ec9b0', 'size': 41130, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/control/dashboard.html', 'before_sha256': 'b787ed400e657e77770ce9a627b07cc28636ab0c2faf6b524944d559ff992ff3', 'sha256': '0cbc6d6d25515028c62c2eac56817981a456a33035373911a91571dcefb6e972', 'size': 103431, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/control/dashboard.py', 'before_sha256': '01dfa5392edce87cbef68fbd96b868f29b0e69c287e8bc5f186dfe9719ea5c72', 'sha256': '2a8763af97e4a84ecc2789f862369c8e198dec388ef4e38f83723c34c78971a9', 'size': 49705, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/control/device_diagnostics.py', 'before_sha256': '1df9579b0eef10cd26288f9ed9f6d57305c78acf553461e19520f10e17b72573', 'sha256': '41e9f6790bf2a16f188f8358fd67f11b62f4f1de2a96ba3cd5d9184343ed2761', 'size': 13373, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/control/diagnostic_events.py', 'before_sha256': None, 'sha256': '1efbb6698f9bc7be9d62f02ee16a2e25ac21c0b1b75d8359678f67b76e018388', 'size': 5333, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/control/launcher.py', 'before_sha256': '265166136849a8d7b3404b706f151a28693c80fb0798d2cfc4fc6db5ac3ae543', 'sha256': '70270270ea18a730b13270fddbbe46db75c462bfc3d68d36a2b067748d0ea62a', 'size': 104022, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/control/phone_recording.js', 'before_sha256': '0af7d0e381c5a090d963cbe4afb89c39a87f73c24b829e90c843fbf14cc45335', 'sha256': 'f7f426ae3b637d7e6a90cdd0912142ee2c8e5cdeb37903c1869ae27e939ead3e', 'size': 9619, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/control/scan_process.py', 'before_sha256': '36bfe9d47ec9ba72f38e0bd249b64780cd727afd5978a080c9e00f07b2ea3188', 'sha256': '7bdf3bf62e3c6e0ae6a5bd62fb769aa8f34f2f16848605f4cc1557ea0b828ba8', 'size': 5162, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/control/web_controls.py', 'before_sha256': '68731528bb53f328768bbeb70d9cc75fb99cce532a35a9cb6b92453db0a817b3', 'sha256': 'ffa586d502de90097c28022e08e7a2b81071b99336af7bd9c18c255dcfa7bb0c', 'size': 38637, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/framework/framework_recorder.py', 'before_sha256': 'aa9d9f7d315c98fe8a80f8712c99e33503117bcb23d09ea3ab36083bfb9114ac', 'sha256': '88bb478eea4c42673135e0ad6310abfa9a3e642d28876c8020860f7651133701', 'size': 16855, 'git_mode': '100644'}]
EXPECTED_BEFORE = {'orangepi/scripts/audio/build_safety_prompts.py': 'd8cb3d3709b58bccd375b84eb4b20991d6a26953c35740f53c76e1492f8deb9f', 'orangepi/scripts/audio/speech_tools.py': '834802b5d94229b0497150d2dc56494fb26d6c802ff75d4568a87673a8f14d2e', 'orangepi/scripts/check.sh': 'e487ca552b30c92e0ec976592dd41360db017b7dead002ed044fc04045954e29', 'orangepi/scripts/diagnostics/benchmark_qc.py': '1188251623e3d00e23a0426810614b9cbbd82635a0f9d6446ddc3bca153b66db', 'orangepi/scripts/diagnostics/check_busy_device.py': 'a3fe7171db1d441c1803699bc7e9a0341d877fffd33df67a393d44f42fca3788', 'orangepi/scripts/diagnostics/collect_receive_evidence.py': '2e9aa9b96d4b584a2bb55f292ee2fccdb76f791e805effcc922d03d1ebbba23c', 'orangepi/scripts/diagnostics/eeg_smoke.py': 'fee89c22a5d5909fc706977dea47cf3a43a92f2c45caf969911254433745e7f5', 'orangepi/scripts/diagnostics/list_cameras.py': '22b1db792cfa0843ddf9bf0848b7b45b233cc54c0d14239d82db1cb995ecbc04', 'orangepi/scripts/diagnostics/monitor_health.py': 'b28d820c977f9e95763a738687f9d656285fd375241d39cb91f240e539a586fd', 'orangepi/scripts/diagnostics/smoke_capture.py': '1b135cf6c25b57613069b629953d220d15a24317576fd03e5b84d202b3fb773d', 'orangepi/scripts/diagnostics/system_observer.py': '92da5d6beb026f3d40d577e1c9513254677820db264ff154f21c0f06208e2104', 'orangepi/scripts/enable_early_web.py': '327273be5716c8dc87d9a3ca4b38082b9a5c837c57bbd120ade2b39b49e23a06', 'orangepi/scripts/env.sh': '4bb3d2472cd2bea5460184a18fc7c9ec77e27c6ec40da8c5cbadadb14f1e99cb', 'orangepi/scripts/headring_power_shutdown.sh': '190f766f5684d4043eeda2ba371af02c3c4698a92ca81e03ba37db802b4e0b8a', 'orangepi/scripts/package_delivery.sh': '14850c7ac37dad3442f93ca3201684ac0ffaec8a9294175e74636c436c49009a', 'orangepi/scripts/run.sh': 'a80a05bf4f63ff0a86f84d9307e694db0317dd8f5b98c4e9d17bef1f81684974', 'orangepi/scripts/service/install_service.sh': '98be7b01b116d7f8073e7dccc67ede28f767e8abedcb919148e56146a3b71d4a', 'orangepi/scripts/setup_eeg.sh': '4720bef9d5f7e82f37b007d9bae0c187627484165170c59fb9d4e1953baf0c81', 'orangepi/scripts/setup_envs.sh': '099c2d17650de3bda7e5369e66f452f23ec0fe8a22e885165f154bcebc82f137', 'orangepi/scripts/storage/ledger_tools.py': 'f2caf87c0f5c893813f60b8acee85b2b30a1096de2490ee5d08c132a74597551', 'orangepi/scripts/storage/package_data.py': 'c4225746c3abf7a88ea28b5ab91256d60a3f562ae0cb2f3c12894c1c14e6c595', 'orangepi/scripts/usb_tasks_mount_readonly.py': '0e672274737b4cd09539e68f2ca376c8806769945aa63ad202f64d316c3f034d', 'orangepi/src/orangepi_collect/__init__.py': 'a2955148cff6dd02e1cf74026ad7d6a49c7df7bcdca7593cc26d7da6fee370e7', 'orangepi/src/orangepi_collect/acquisition/calibration_probe.py': 'e71155c1c25689524ba8591f0733de266b62b67b8d98f90ea9fe3348349c99fe', 'orangepi/src/orangepi_collect/acquisition/capture.py': '901c64c1cc77d1f1759673664ba8a0892a4ec0098ff83094c0119e148c10a1c6', 'orangepi/src/orangepi_collect/acquisition/eeg_bridge.py': 'b5e3d1bb6f88a66bc33c903d62ac54e385124518238c58ab8400e9c04341c455', 'orangepi/src/orangepi_collect/acquisition/eeg_filter.py': '5384666a8fbd12bf82d7c895150b4f21623da28b918b340e3c048acd56a2755e', 'orangepi/src/orangepi_collect/acquisition/eeg_frontend.py': 'feed368783d7db8d792763d1c66bb89d187ebfb80267072dc15dc68cf5cef239', 'orangepi/src/orangepi_collect/acquisition/eeg_worker.py': '7bcbf2b7bc92b8c3ac1ca25227ccf5dadc8576b49cca514281610e9783ebba73', 'orangepi/src/orangepi_collect/acquisition/emg_calibration_probe.py': 'fcaf6496f4e8cc14189710aa80e0b8627a0a1c545f75c404cbec1020e9e37f9a', 'orangepi/src/orangepi_collect/acquisition/imu_source.py': '1b41ded5054f2b02f95b2d26e1abd447a3d934901dc7fcc481d27e278e867d6a', 'orangepi/src/orangepi_collect/acquisition/preview_worker.py': '48dba842410ff9273f06a300f0db4ccff8a4b46c5f52fe6c0c7af6c36e0d688d', 'orangepi/src/orangepi_collect/acquisition/scan_devices.py': '08cf258eb92c53b6e7b95fc908ffc045f6960c4d99f88556ce6476fbe36778a6', 'orangepi/src/orangepi_collect/audio/advisory_output.py': 'ec1f3d3fe383e01ff491761f2becc5e9b01642892a68afe9cd68f32c05aa82a2', 'orangepi/src/orangepi_collect/audio/cloud_speaker.py': '791569b9e0d06237884f4ee67465bc6f8d055cb53340a80f1644dbe4c97f2b52', 'orangepi/src/orangepi_collect/audio/cloud_tts.py': '740ac93f8f5bb23fe52443107d51a933649a7e497cc548c8ab89ac60d95071b2', 'orangepi/src/orangepi_collect/audio/headphone_route.py': 'a0ba217bec8ef7367e1c8482453bb78ccedc45a4ea778ea0e3226203c62b759f', 'orangepi/src/orangepi_collect/audio/speech_cloud.py': 'fbea006d2a4519a238911c53aa7cc317951f24aabafc7738bbc507d441100bd2', 'orangepi/src/orangepi_collect/audio/speech_prompts.py': '49c3cddbcfb81aea102d31221228f9f20a9036cbfbe0d961f9486d1fa2884362', 'orangepi/src/orangepi_collect/audio/startup_fatal.py': '78e5fd69c82e8228b2516d4d3168c48b1926ab36e53b912927c77670b59b8980', 'orangepi/src/orangepi_collect/common/camera_owner_wrapper.py': 'f50961f61105bbc6d6e2a66699d97afc936b6e8b35bf2c0551d67126e63c6924', 'orangepi/src/orangepi_collect/common/camera_resolution.py': '0278a747897058576dadacaf4cd6b0f8f2e01a710201dc2b0757e6904270a7a9', 'orangepi/src/orangepi_collect/common/capture_qc_receipt.py': '7e1cefd691372357e106e2f986a09444263af2240142690ea1a90c3f93f54e5d', 'orangepi/src/orangepi_collect/common/capture_session_state.py': '74a83bef76cc8323236fb7645aa59740b2c93f97aedd60246e527488f391c5a6', 'orangepi/src/orangepi_collect/common/collection_profiles.py': 'ad0d398676661b9f1160e61a912f2c7c3af8d0725e7a58f17e45598e12225cf4', 'orangepi/src/orangepi_collect/common/device_discovery.py': '7fc11e08cb906de3de8d37137ace498b2019daf9b7433266f30af7bd9ca3cb48', 'orangepi/src/orangepi_collect/common/display_demand.py': '04cc7dc932e2178b938e334b495d7aa99407057fbd394cf4f19087ef66c4550f', 'orangepi/src/orangepi_collect/common/eeg_impedance.py': 'c5e6fc15426a33cfa8c03dbcc3f296a79f475d6895dd84bdd023c7c500273104', 'orangepi/src/orangepi_collect/common/eeg_settings.py': 'a90774add55180ad2e1477996b63e95a25b2bb10a0ee7ea8f9dfa07e9d2dbbe0', 'orangepi/src/orangepi_collect/common/emg_calibration.py': '2efab257cd117e5e51b8ba34d35984e413098be14afcea42b290dfb50fe6cb9e', 'orangepi/src/orangepi_collect/common/emg_insertion.py': 'f17bc597c4956a4416b8d65ad1a3a811ed6dcfcc69b0bae453b5c728096bbfaa', 'orangepi/src/orangepi_collect/common/frame_stream.py': '8a31ce3f9ed6733dc997f2d0429c3718e354fd5048935360c1a5c42e37731ac9', 'orangepi/src/orangepi_collect/common/headband_calibration.py': 'ecb35bdda01de72619f7591ebeb055373c5c47b2e99ce630f5a70c074800eb08', 'orangepi/src/orangepi_collect/common/impedance_estimator.py': 'c0ab3d10cbad1bd820d193b36f6440c7e6c223f9eb6aae2b9e9d6b45a8b279be', 'orangepi/src/orangepi_collect/common/impedance_quality.py': 'da24ab2e98343042a764199465907935bd2eb3d07ae5b9f905913f0857d42fab', 'orangepi/src/orangepi_collect/common/impedance_record.py': 'dc437d13d2e488a361671949fc6dc9199a7148447191720bf2287c36a65a09d3', 'orangepi/src/orangepi_collect/common/live_preview.py': 'c42887f4c486cf58a91d907b408a3a4d1720473e5d444e7e259b2c3fe19891dc', 'orangepi/src/orangepi_collect/common/local_qc_receipt.py': 'd02e6da219e38542517fed165494964f8d40a2259fe53cbc7762dd6714e25a88', 'orangepi/src/orangepi_collect/common/project_paths.py': '94585ec667d188d155fd88eafda9ed4196c90a3c0c49f6ffa9b949b43126322b', 'orangepi/src/orangepi_collect/common/qc_deferred.py': 'ff99a827421592f5806e333bd155903d1b632c7385827d9c9f04cbfadd933a65', 'orangepi/src/orangepi_collect/common/qc_explanations.py': '570061c9ed5991e3f608ec114adc3ab6e36380d8f0d3425b4045fb3331a7ed26', 'orangepi/src/orangepi_collect/common/raw_handoff.py': '3d30f91134d94baa90684c5517d2e5205ed008a67f72a1ac42fc1230640dc491', 'orangepi/src/orangepi_collect/common/required_device_contract.py': 'cb02641d7439d92d805a05838ddb6ab3560931dbf478ef2cacd71694e169bacf', 'orangepi/src/orangepi_collect/common/scan_registry.py': '50084038bc841655c6c89d15e0bced778195ae3c23a5dbea6d9de34fa0c34048', 'orangepi/src/orangepi_collect/common/scan_result.py': '6ca1be45eca78b44a46cb213804cff6aa2a9e96c8409670d31451bd45af31214', 'orangepi/src/orangepi_collect/common/screen_privacy.py': 'c1b014df95da33a4d22df126d736b9f9ac1b57bfed32cb6d644fc8c11744f91e', 'orangepi/src/orangepi_collect/common/serial_v1_impedance.py': 'abc1802530d2547dd5e81421ec52f3718eb13ba0a6bfd9d2908fe9626afeab29', 'orangepi/src/orangepi_collect/common/session_naming.py': 'deb584106ff32ab704a3eea95907d3df462ba3fee27b533e2fedc3c7fbbbd553', 'orangepi/src/orangepi_collect/common/wrist_calibration.py': '7bbf98d035d2d76f9bcda82964128ce8f74cce103c3a4dd531913c03e3b4fb70', 'orangepi/src/orangepi_collect/common/wrist_camera_settings.py': '5cb98074d722a3f81d2693e222f0739a08661b41fc95a51f0c0e20bcd0bc31f5', 'orangepi/src/orangepi_collect/common/wrist_preview_transport.py': '5a2b0c7dae826e4feb2af8e8ff2ea9306b8cecdc6a9f14cfb3cf9f21ada3f95c', 'orangepi/src/orangepi_collect/control/camera_check.py': '1c56f6831e7b113150b825dcca2ece664db9f7a7d37351eb795dd5991be2495c', 'orangepi/src/orangepi_collect/control/cloud_common.py': '64b06513871847645fbcfcfae5d6c29d76e62fc4bab377889336d82212854be7', 'orangepi/src/orangepi_collect/control/cloud_http.py': '330d172ee4f9ebf87215932db14e89685a77d46586d9e7d0edf81642cb45c444', 'orangepi/src/orangepi_collect/control/cloud_uploads.py': 'eb0b4647cd7f5b49dfb5ed2bea776baf5fd5bf4f87c1da661ae578ac7fcaca96', 'orangepi/src/orangepi_collect/control/cloud_worker.py': 'e2d97d58fe96f2f8a5b464086e7a4858da42b0784ed70bf641cb02dd68a2e6a7', 'orangepi/src/orangepi_collect/control/daily_collection.py': 'cce7c4830273ba5a95835c73fa77993fda17d17b5cd71521d467a39833541d7f', 'orangepi/src/orangepi_collect/control/dashboard.html': 'b787ed400e657e77770ce9a627b07cc28636ab0c2faf6b524944d559ff992ff3', 'orangepi/src/orangepi_collect/control/dashboard.py': '01dfa5392edce87cbef68fbd96b868f29b0e69c287e8bc5f186dfe9719ea5c72', 'orangepi/src/orangepi_collect/control/dashboard_settings.js': '85e5e03058cf9c80171c26434f2543743a4871d43da3037b1fa310ad9b9b160f', 'orangepi/src/orangepi_collect/control/device_diagnostics.py': '1df9579b0eef10cd26288f9ed9f6d57305c78acf553461e19520f10e17b72573', 'orangepi/src/orangepi_collect/control/emg_insertion_controls.py': 'bf1f07fc2d1b7e2d39b68817145308d07c6a87bec97d758310877fbd4240941c', 'orangepi/src/orangepi_collect/control/keyboard_input.py': '7823ac328f51f96fe5cb51e95edfa9362fcf2d4efb4041165a3d1cbe3b5750c3', 'orangepi/src/orangepi_collect/control/launcher.py': '265166136849a8d7b3404b706f151a28693c80fb0798d2cfc4fc6db5ac3ae543', 'orangepi/src/orangepi_collect/control/manual_reference.py': 'b943b8b785e32d67ffa93cc5b81d8353c3781ff719a8f963365b97d7ea831a8e', 'orangepi/src/orangepi_collect/control/phone_app.css': '6e07271ba07fdb56687f2da54ef257d6a799a6a446fd4a44c1e1b5a4c8f865c2', 'orangepi/src/orangepi_collect/control/phone_app.js': '114d2ccf87a83a2843249bfd87351c5b12f0a14c846f341d69572acc0905e84d', 'orangepi/src/orangepi_collect/control/phone_capture.js': '8a09973756513083654192df46c0561831d5d63836d0738c554859b42c0834c6', 'orangepi/src/orangepi_collect/control/phone_connections.js': 'dd4f9570a2bf65eb1916af4428ff499df7f67b693094f06612ff35c290b57b47', 'orangepi/src/orangepi_collect/control/phone_diagnostics.js': 'ddf278f74e9dce935355631dc23e7f8353d80f6bfb74cbbc3d2b2922758a255f', 'orangepi/src/orangepi_collect/control/phone_headphones.js': '1a5cc994f9dc8a84a706fcde251109a799ed35164351e17c05f5fff543a79273', 'orangepi/src/orangepi_collect/control/phone_http.py': 'de0e2697da8202beeb7616703a9d390e88907fbeacec84cf8f68c910a95dc574', 'orangepi/src/orangepi_collect/control/phone_links.py': '014186e953e1e9348a5160ee2be018b47df24ca743e7d43d8619ea7c81271454', 'orangepi/src/orangepi_collect/control/phone_recording.js': '0af7d0e381c5a090d963cbe4afb89c39a87f73c24b829e90c843fbf14cc45335', 'orangepi/src/orangepi_collect/control/phone_recordings.js': 'bed865feb1e8ebaa2808317dc39e0cbfd198b903478f32b848a4eb7a4c4ddc4e', 'orangepi/src/orangepi_collect/control/phone_references.js': 'c0dd64e31b36416d695f3b2d373423776a51be384153271d32a70a238dc121f1', 'orangepi/src/orangepi_collect/control/phone_shell.js': '7914cb5538f30bfa3852887ca7012909df7eaaec26f903c0d609cd3d667884b0', 'orangepi/src/orangepi_collect/control/phone_transport.js': '6c122593647d003961b7c4ce9e6fdf3549ca63008304a25eee48c4f8279ca45e', 'orangepi/src/orangepi_collect/control/phone_uploads.js': 'f80c9bc321178382ff46a187f091abc5a709e6a91441e3fa2db847667e96e42f', 'orangepi/src/orangepi_collect/control/qc_details.py': '676364ac8b075116dee375b1f576ed3d937b4dc5a30513911a44620e4248b781', 'orangepi/src/orangepi_collect/control/recording_review.py': 'dcb39f5e1f0949328ac2d4f24a506c684e8d5089be91a96cf25606784cff84cd', 'orangepi/src/orangepi_collect/control/recording_store.py': '4792d0ae41b4ec236c47a9e4a8b8df2e3cc0f3410352e000a37743356c40c6bd', 'orangepi/src/orangepi_collect/control/reference_access.py': 'afc260fed615fdb56650a4a86fad2ca5f0d1cbce13ca56e4817c17ec982cf976', 'orangepi/src/orangepi_collect/control/reference_bundles.js': 'da21ca6156cdcea0dc55851324f9538d70b101ee0dad4d3bd18d918515e9a6eb', 'orangepi/src/orangepi_collect/control/reference_catalog.py': '0d4b43c60606e8a15b1bf6544567686e7ee40be238aeed7f0e584941190d1fa5', 'orangepi/src/orangepi_collect/control/reference_controls.py': 'd060a194826b035fe1322ea6bbdbf8c5d3352be91d64eba2e3c9f21c333d5ae6', 'orangepi/src/orangepi_collect/control/reference_library_http.py': '069c8d67971fd47c17a0fb2818bc576628a5821fe30d187d11377422ddfd5fb2', 'orangepi/src/orangepi_collect/control/references.html': 'a97aaa7361e795f9efa2c483b0d72af81dc21dd57321e5c3f13c898af126565d', 'orangepi/src/orangepi_collect/control/scan_process.py': '36bfe9d47ec9ba72f38e0bd249b64780cd727afd5978a080c9e00f07b2ea3188', 'orangepi/src/orangepi_collect/control/service_entry.py': 'c45ba78341e4b2ac1fe2b2934d69eabbd2332086a08145f6ee9edf5bb0f2cf91', 'orangepi/src/orangepi_collect/control/signal_quality.js': '67f38beadc727a633b9b799a5823a593312f7b208e735af4de34c88adc188cd6', 'orangepi/src/orangepi_collect/control/standard_bridge.py': 'c27b26582c65511ae02b3a21e997b66de3a7af11ddd64a0e0a4b02ae28c48743', 'orangepi/src/orangepi_collect/control/startup_status.py': 'a86aa96f9f0bb8ded18891dfe4196e24eaf9174e85fd219400ef7d6f5b5983af', 'orangepi/src/orangepi_collect/control/task_block_sampling.py': 'caafe63e538d8fc9dfb417ab2cf24bfeb479098f7c65961de2b10b8754cf0b86', 'orangepi/src/orangepi_collect/control/usb_task_import.py': 'f41e197abe6ffc1c66b640ae2264f78e3c179d7faff05efb6fb12b7e86d981ef', 'orangepi/src/orangepi_collect/control/waveform_plot.js': '080979b4233b494105e325c0310f43dbc0dbe907ae5e9eea8669c5a4187bf88d', 'orangepi/src/orangepi_collect/control/web_controls.py': '68731528bb53f328768bbeb70d9cc75fb99cce532a35a9cb6b92453db0a817b3', 'orangepi/src/orangepi_collect/control/wrist_settings_worker.py': 'c5399fbd3c134aebae6bc75ecee3a2bac813b7b00bfc7962f81236d3438f6cbb', 'orangepi/src/orangepi_collect/framework/capture_integrity.py': '65172cdb5d17ec448d2ee45389af428a40aebd0f922c74fd6854f61a6072aee7', 'orangepi/src/orangepi_collect/framework/eeg_qc.py': 'b362e196b9f2d568e9e64037667a0cfbf31e2b7c12751344cbf7fdb730a9b78d', 'orangepi/src/orangepi_collect/framework/emg_recoverability.py': '89bdc0ff0c3e336e412350e6e3df48a6f12594e050002d3e9d2ff042e34d4b40', 'orangepi/src/orangepi_collect/framework/framework_qc.py': '39a7ce7c0318122353a74812324a60000b1a04bf3b11997f8537f7cf8add4756', 'orangepi/src/orangepi_collect/framework/framework_recorder.py': 'aa9d9f7d315c98fe8a80f8712c99e33503117bcb23d09ea3ab36083bfb9114ac', 'orangepi/src/orangepi_collect/framework/gap_thresholds.py': 'aaf1ab0a02a54e916b80a0f887f4f8cab53f04c379c8b9f8757b3af085c6d823', 'orangepi/src/orangepi_collect/framework/mpp_qc.py': '0e9e08afd5fad8989cfe40839669fba194af72e37cbd6a4d58938d0dbfb7e2e1', 'orangepi/src/orangepi_collect/framework/mpp_video_worker.py': '9f1a3031e77e9ce1e3465cc0b5d01a32717105eb2f4b8ef9218fb8afda16c4e2', 'orangepi/src/orangepi_collect/framework/practical_evidence.py': '198636f1a64c2bd465ee07a9e660038b41bfb5f2e123a0df569126377c915ab9', 'orangepi/src/orangepi_collect/framework/qc_host.py': '77a2f0fd110ed408307b8555496fe33e141a787d4541f9869b9a3c504d8dd346', 'orangepi/src/orangepi_collect/framework/qc_policy.py': 'f8681125fc4069664a9202a8e7a7df1c3cbf3189d9dd070843607be203303d92', 'orangepi/src/orangepi_collect/recorders/__init__.py': 'a2955148cff6dd02e1cf74026ad7d6a49c7df7bcdca7593cc26d7da6fee370e7', 'orangepi/src/orangepi_collect/recorders/camera/__init__.py': 'a2955148cff6dd02e1cf74026ad7d6a49c7df7bcdca7593cc26d7da6fee370e7', 'orangepi/src/orangepi_collect/recorders/camera/camd_source.py': 'f56720a4e7c97ba596addf58ede46daca116be081f8299d91fc6bf5d55117f50', 'orangepi/src/orangepi_collect/recorders/camera/frame_queue.py': 'c9e61ed15a76f90b24bb57d8009e751b3f5d21cb1be6bd0a8a8081b190648f49', 'orangepi/src/orangepi_collect/recorders/camera/h264_preview.py': 'd29cf67de21cf2d3847c17347509e38724c801d39a2c0008f7dd8c87201a8502', 'orangepi/src/orangepi_collect/recorders/camera/isolated_preview.py': '15ecd98530b25d0ed6b88c2bbe0b690312c432358ba6dad5cd78bf6f069adfd7', 'orangepi/src/orangepi_collect/recorders/camera/mpp_camera_recorder.py': 'b3c1d8b00d6639a72c6c06edb184542a4815e78add42d5059abfb9237185c508', 'orangepi/src/orangepi_collect/recorders/camera/snapshot_preview.py': 'a738b30dcfda33387bcf47c6601046a0c2ccb04798d280f3a3193e37196d3659', 'orangepi/src/orangepi_collect/recorders/camera/turbojpeg_preview.py': '310e0db5c3fe4fff99558b7789df04fc8f383bb8a1d918c850ab1543bc06df72', 'orangepi/src/orangepi_collect/recorders/camera/uvc_source.py': '3bea08f554ea5e835901f7c4d4a830322a992cfa7cf94adc3cc0264f7ffbc000', 'orangepi/src/orangepi_collect/storage/durability.py': '256254688e16535534a0ea2adef09c2eb93e0aa1f9403c699eafd850dfc06ef1', 'orangepi/src/orangepi_collect/storage/ledger.py': '9337e6c90178c20a755c027f06f5b77e072a5dea8f56107d0daccd73b8e1ee9e', 'orangepi/src/orangepi_collect/storage/storage_paths.py': '7f2f2a3b346e08cb945aa5ac0253cb84968fb7010a66fc6585f4e3dded32b2ab', 'src/embodied_brain_collect/__init__.py': '36c7214d568d09fa45ec05b70f01700cd1bccef4dd3b2b00f7974957bcb420e9', 'src/embodied_brain_collect/checkers/__init__.py': 'acaa2fdb7ec91c08c8ea2eff175df7b80fe301140d2c214e785522f6c552297f', 'src/embodied_brain_collect/checkers/base.py': 'bd47e37a426bc9f15108c905c88e6fb413350a1256931bf31467d45d0207f20f', 'src/embodied_brain_collect/checkers/camera.py': 'd849260ec47149dd0f866f6f76144a1bd01170b2fd8bfe303bd04367a670e657', 'src/embodied_brain_collect/checkers/checks.py': '24810c3e407c9c0d72cddb52c11d0a44f4db7bb5548d18a4aa8225d602814c3a', 'src/embodied_brain_collect/checkers/eeg.py': '7aff3878bd3d544e54296bd4f0180901b27feb3ce507d5d8c6ddf871ec73f14d', 'src/embodied_brain_collect/checkers/ego_headband.py': 'abebd07c4585a842606bcccda2e9d58fcf896e309f7be7d74630865c916b1ff3', 'src/embodied_brain_collect/checkers/emg.py': '5e4474585ee78fbe35649e1cc4baf2d689d68e2f4c55e3c17eec9bc6d1714464', 'src/embodied_brain_collect/checkers/eye.py': '7f8b1e783c0328e286e049bc9f8894d00e3e7e23a72e8134b3d3a18090a9d61d', 'src/embodied_brain_collect/checkers/hand_pose.py': 'e02317818a012f7b4e81551dee6f3ee7d27b68a766a541cbf578043afd108b54', 'src/embodied_brain_collect/checkers/marker.py': '64f37dccc9c9c405f2a6d37352c2e56561430ad26bbf9578262ab9ac52ee1597', 'src/embodied_brain_collect/checkers/position.py': 'b6437424b83d412c79a9ec468040ad629539bf9fa782511d49bad6af77ff75ef', 'src/embodied_brain_collect/checkers/wristband.py': 'b83ee50722928c7ee1d1a0bca6d6293a489fb3a3dfac0e54e0b404b56c8dd3da', 'src/embodied_brain_collect/recorders/__init__.py': '505b4f7b8686de798a182461adb271d0c37d8bde2b9a672704b37e556869e90d', 'src/embodied_brain_collect/recorders/base.py': '83cf846bb10ad172459185d332fcf8120a2a28f1527ac38a4ed8f22cf5de4e1d', 'src/embodied_brain_collect/recorders/camera/__init__.py': '3d7ef403645713d7cd55d3c7588cacbf2bb8a9f3edbb811035d0a30d60b8c8ce', 'src/embodied_brain_collect/recorders/camera/base_camera_recorder.py': 'e3d2f98b9d950e15ba331292a25ff0b464a896d61baa702cc50167e1e03a840b', 'src/embodied_brain_collect/recorders/camera/camera_recorder_config.py': '6b38ce9d6af13a6e88b9a5f40d87eefaa207fb270f5115032d22e2736b554312', 'src/embodied_brain_collect/recorders/camera/depthai_camera_recorder.py': '08613e506b5ce0d8ab47b74a94dd67719e1ecf57d25e8607ce5c78c400e1258a', 'src/embodied_brain_collect/recorders/camera/dummy_camera_recorder.py': '01da46774eda7df2726d9ca47b7990ff7123d7d8e8cf9d7cefcc2c428c720710', 'src/embodied_brain_collect/recorders/camera/opencv_camera_recorder.py': 'dc5f8fed8aaef14568475655088e212b5499478a5402c3a5828d68d810a23296', 'src/embodied_brain_collect/recorders/camera/realsense_camera_recorder.py': 'ea3714b3af325de080922fe9a77c40f18c6c203afc7a8a29a9356e3c69ecf575', 'src/embodied_brain_collect/recorders/eeg/__init__.py': '15a6b8f81d524d4d9dda2e39a4a97add0c88584bad89bd22722863a7d2fbe424', 'src/embodied_brain_collect/recorders/eeg/base_eeg_recorder.py': '813fcd37360929939110e98a7a93ea2e018552ed9ccd2cf59643b54ed047342e', 'src/embodied_brain_collect/recorders/eeg/blackrock_eeg_recorder.py': '00d67aada4ec028d674a3fcd609cb3809efba337b8ad689e0eff51a5f2d1a831', 'src/embodied_brain_collect/recorders/eeg/curry_eeg_recorder.py': '88bdb0105ee11ab980a479d10f3ee904ce8b56cf4f0ace2fa2e00dfebee247e3', 'src/embodied_brain_collect/recorders/eeg/dummy_eeg_recorder.py': '826088bb22586f08809c4fcb8c9d3547b34affa8ea97ea86e81f8aedc54607be', 'src/embodied_brain_collect/recorders/eeg/eeg_recorder_config.py': 'df11a3355a68a71aab39a0f59a1d094bb4b9442f987f444571173cb1d7da9b88', 'src/embodied_brain_collect/recorders/eeg/intan_eeg_recorder.py': '532adde543bbebaa2880aa509353e00e11626b523f22b9e32e02767226520547', 'src/embodied_brain_collect/recorders/ego_headband/__init__.py': 'e6318227afc44d6baeb1cee5ab6d8e3b8e458393be92b047c0482580eb57dd67', 'src/embodied_brain_collect/recorders/ego_headband/base_ego_headband_recorder.py': '6ccd6bd1cea64bbf257281962676122a772552dc8633f5d6b4693cbe8e673a91', 'src/embodied_brain_collect/recorders/ego_headband/dummy_ego_headband_recorder.py': '0de9a326aec6a14e01c6115a44f98c0ccb92cc523166ef9429e17d4ff538ea9e', 'src/embodied_brain_collect/recorders/ego_headband/ego_headband_recorder_config.py': 'c17d664f852c5f16909bb489fcc29499f0565b881f1f0a7b9b1c4d4084c33fd0', 'src/embodied_brain_collect/recorders/ego_headband/microphone_writer.py': '86785e17bbc6ea62670dd8b2f68b8f7a578c36ccdf7bb7be79136697b02396ac', 'src/embodied_brain_collect/recorders/ego_headband/net_ego_headband_recorder.py': 'fed30b3ad934d0c6b732904e43d5dd3c676db0ef6f001d3f3ab5d1318235ac1f', 'src/embodied_brain_collect/recorders/emg/__init__.py': '449f7f53c53cd36378b2e03bbd0497fffa2b35ad9be1c77d78e470c0e2cb49ef', 'src/embodied_brain_collect/recorders/emg/base_emg_recorder.py': 'de444360e90ed3ca751d41e6f2e7a0d69b6ce50c37de374a4e52584645f33e92', 'src/embodied_brain_collect/recorders/emg/dummy_emg_recorder.py': '6a5f958dfdc7dec992e83b6fddd98397ef84d2e71601ffcf645fa7e305c7bc2c', 'src/embodied_brain_collect/recorders/emg/emg_recorder_config.py': '9984b4e53a216be611cf16d960f5732bc9c27f32fce0b9cda0aae673a16b1a67', 'src/embodied_brain_collect/recorders/emg/timestamp_rebuild.py': '9b31fb9bc2315011a8679246d2eb70c83e9f6cb5d5dc7fc716a14bdf96053186', 'src/embodied_brain_collect/recorders/emg/weili_emg_recorder.py': 'c9c50d8366a0bd773ee143e6d33819c986964b41169333cb110f5acb9fde3a12', 'src/embodied_brain_collect/recorders/eye/__init__.py': 'a72de36f90d490e77c5c29045fff5aef25e499f1246fccaac141fca2f4602619', 'src/embodied_brain_collect/recorders/eye/base_eye_recorder.py': '126feff6d9833d94f2578795abe270ded389e8c167cc96aeb256ee4b0ddb21c3', 'src/embodied_brain_collect/recorders/eye/dummy_eye_recorder.py': '156ad2c58700881935b72cb18c0dc26ba02ef27d1a9a23bcba650910754000da', 'src/embodied_brain_collect/recorders/eye/eye_recorder_config.py': '821299743aad32adad46e7107376076d9f701641b39197a4dbd269389f970ef0', 'src/embodied_brain_collect/recorders/eye/neon_eye_async_recorder.py': '44df39d4a1429196321254507f88b6d80ec777b21a339db3783a1a83f70cbfe4', 'src/embodied_brain_collect/recorders/eye/neon_eye_recorder.py': '814d67098f43e9dc7535ca97e1e74ecf96732f04189cf55579d6815274c5a9e4', 'src/embodied_brain_collect/recorders/factory.py': 'b7dd06251a8d0b099478db8e44cfd852892d3934bb8badd68ce8638303461385', 'src/embodied_brain_collect/recorders/ffmpeg_writer.py': '4197016d8b8098e9a1588ff853195cf8d6f16416bf69b501a2d0ee5a9a6ee4a9', 'src/embodied_brain_collect/recorders/hand_pose/__init__.py': 'babf0b55c44ca744d19ca83d80115b4dedea91086b33a4d16e9af73e367ccee1', 'src/embodied_brain_collect/recorders/hand_pose/base_hand_pose_recorder.py': 'beb1b274be8848a4120ecc3c425dc81ebbc7150a4db68357a2dce74d48c13cce', 'src/embodied_brain_collect/recorders/hand_pose/dummy_hand_pose_recorder.py': '32a5df8b59941b7d02a9eb3dd5fb38f075e325d2bf7a0e1a419799ec62d41458', 'src/embodied_brain_collect/recorders/hand_pose/hand_pose_recorder_config.py': '5d25ed705dd8a8f764d66893196657411efb2d905950f0a7de5bc557e83bef3a', 'src/embodied_brain_collect/recorders/hand_pose/manus_hand_pose_recorder.py': '3c9ccfaa0b85a0e9d002ec3e14fdd08797c1c9e3ef3444a428b29e19256c418a', 'src/embodied_brain_collect/recorders/marker/__init__.py': 'de7f104dd3f412d1cbef839a2a1dc1008ed8f45f96d830c89c51b9cfd5645dd3', 'src/embodied_brain_collect/recorders/marker/base_marker_recorder.py': '978f244a11494b2a7a6e644da2fd2eead767d6857f9c7c284e1074fe5c0ab60b', 'src/embodied_brain_collect/recorders/marker/marker_recorder_config.py': '43d4b93202af965e14e078f9f053e5390fa0d3dd6de5b9aba793a4165784b899', 'src/embodied_brain_collect/recorders/marker/udp_marker_recorder.py': '3cd46c8194efbf382e1f71b9df25e291579d9d1640a4488b486567caae571add', 'src/embodied_brain_collect/recorders/position/__init__.py': '88ae7acbc82d585a01b9bc27b2e29692e4074fedb35f18c3580ced4b5b4214e8', 'src/embodied_brain_collect/recorders/position/base_position_recorder.py': '656bf416c775697a0895e45aee212a1874f111c3bba7e5a553b0a1526eecac6f', 'src/embodied_brain_collect/recorders/position/dummy_position_recorder.py': 'b97ae9da7e18762d0284d209c17024f4ff67d9b488be18acd934601f0c6da995', 'src/embodied_brain_collect/recorders/position/openvr_position_recorder.py': 'ca37ebf10c5981b16f182085e44efd95c592e609a9112282a34df0439e7f4d05', 'src/embodied_brain_collect/recorders/position/position_recorder_config.py': 'f51a8178d515b4b982e9ddbc046be6d31bb0840aadfb2ef972e78c9e517669d0', 'src/embodied_brain_collect/recorders/tactile/__init__.py': 'be7a165e49ea870c800b71ad51ed365c6af0e6d99f904beb115e1b9c02cea81f', 'src/embodied_brain_collect/recorders/tactile/base_tactile_recorder.py': '706de299519e94586e3f88991ebe92577bfc6b9c822bf47f49f33c0471e1e72c', 'src/embodied_brain_collect/recorders/tactile/dummy_tactile_recorder.py': '71170819f75d4093b9425deceed581e51bcf05dab6edc255ca61a4e1d0b8bdd0', 'src/embodied_brain_collect/recorders/tactile/tactile_recorder_config.py': '55dc203a1f17a4046a9390b94e3d4fdb1928791bf28b75da86d79d081fdb095d', 'src/embodied_brain_collect/recorders/tactile/touchtronix_tactile_recorder.py': '25be6db66e3aaae0f404846186f552543950813d880d02a9addc9d0ca26a2231', 'src/embodied_brain_collect/recorders/wristband/__init__.py': 'a183391cc1ecd81ec2a61c4e70ef308a4b362868a49fba7f71feb2ee71978076', 'src/embodied_brain_collect/recorders/wristband/dummy_wristband_recorder.py': '2cd6eead61cfd181458b13c5bc2be11bf4041981a2294343a37b255bcb341fb6', 'src/embodied_brain_collect/recorders/wristband/protocol.py': '3504c079f669bd7acc2f9b2c98a193896d432dbe8cde3c99e098f252415a544c', 'src/embodied_brain_collect/recorders/wristband/wristband_recorder.py': 'c44bd19eb3e2c4793a3b5f0361692d46ef228905c940b7d3125ca26a785da8bc', 'src/embodied_brain_collect/recorders/wristband/wristband_recorder_config.py': 'fbef11c62467eb5f0e327ca40379d9281d69a1a59bf3629e07ba305d8bead5a8', 'src/embodied_brain_collect/session/__init__.py': '293b84d01e840b7dffa1093f57729062050cba7a55303e293622dabff2105aa1', 'src/embodied_brain_collect/session/config.py': '40d8a35ceedbaca84380310d5f6374609db2004caa30437831a3c07811f9a0fa', 'src/embodied_brain_collect/session/environment.py': 'b121aa695cf1fa13619bbcf3e2f745679b3ff1d0c277bcd16be2704867af2d73', 'src/embodied_brain_collect/session/launcher.py': '1ae761515e7a535425fe9db42366f3ecf4534cbe8ad7098e8176d43398b92b99', 'src/embodied_brain_collect/session/troubleshooting.py': 'f4b31977a4ba7a745f539370e49890a100f91393a894970c93e0e00b8d92d27f', 'src/embodied_brain_collect/stim/__init__.py': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'src/embodied_brain_collect/stim/base_stim.py': 'fef68b4f62a5c05fe3620b2c91e662a12968229f6f9cda593bd3e49372944e77', 'src/embodied_brain_collect/stim/factory.py': 'bb844bcd0f192ec924f700b3942016c603c6d72856195d81765cef38fab2fa41', 'src/embodied_brain_collect/stim/marker_codes.py': 'ae754326e8c27dac5475b1b20c22124a3af15b707b5f25cb8b077e4ea937572b', 'src/embodied_brain_collect/stim/marker_sender.py': 'f424f1881cf6fd1d0427ccd9de74f8e9dbbccb354e3b424192fd08b3ffdae63b', 'src/embodied_brain_collect/stim/paradigm1_pickplace.py': '9433751f4d3176c269ef9479ca2b3490120a8a31bbd5da61a2596fcc532f910d', 'src/embodied_brain_collect/stim/rgb_cue.py': 'c293e116e1bb296dd56fcf8b87bb92ed4fcde7dfaafbfe0d82da581deebe5205', 'src/embodied_brain_collect/stim/simple_stim.py': '7abd3a5d963de8c2fcdaba6da7d5a3bc59d07aa50001a62ce6c0d2e145040b12', 'src/embodied_brain_collect/stim/sync_test.py': '9d7461cae6457803e729c346fb257d8abe4bb669bdd9936c1173586ff8b5ad60', 'src/embodied_brain_collect/utils/__init__.py': '8b48574e9ebacb5d953c3299e5af73018f65c9aec8bf994975723f56660bdaba', 'src/embodied_brain_collect/utils/media.py': '6687d8e5dbf3d345beba2a935ebbe71eab8c05cef82fe3383ec0d85e6e04a1cb', 'src/embodied_brain_collect/visualizers/__init__.py': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'src/embodied_brain_collect/visualizers/qc_page.py': 'cb91e05c7c45c657f8bfe03de22b64fde5072b830b1702a3d57e02fc07eafd1d', 'src/embodied_brain_collect/visualizers/qc_payload.py': '6ceeb443cdafd86d84e5592721029207bf937f3943d93d8c7fcb85622ac62795', 'src/embodied_brain_collect/visualizers/qc_streams.py': '4ddb482cb490ddd43731e3205f3c206096da4182bfe65dc3786377d275d1f421', 'src/embodied_brain_collect/visualizers/signal_filter.py': 'd4bac9f16e5e9be48aff562d44e8d75a1a5dd078715931ac7400ee7ffa892959', 'orangepi/src/orangepi_collect/control/diagnostic_events.py': None}
PINNED_MANIFEST_SHA = '7fcb1f8dac518c173eee34f8da3069bb296a8ad25290e3256f7655ef0c31e731'

def check_diagnostic_paths():
    roots=[PROJECT/'orangepi/runtime']+list((PROJECT/'profiles').glob('*/orangepi/runtime'))
    for root in roots:
        try:root_stat=root.lstat()
        except FileNotFoundError:continue
        if not stat.S_ISDIR(root_stat.st_mode):raise Abort('unsafe diagnostic runtime parent: '+str(root))
        path=root/'diagnostics'
        try:s=path.lstat()
        except FileNotFoundError:continue
        if not stat.S_ISDIR(s.st_mode) or stat.S_IMODE(s.st_mode)!=0o700 or s.st_uid!=root_stat.st_uid:
            raise Abort('unsafe diagnostic directory: '+str(path))
        for child in path.iterdir():
            if child.name not in ('events.jsonl','events.jsonl.1','events.jsonl.2','events.jsonl.3'):
                raise Abort('unexpected diagnostic file')
            s=child.lstat()
            if not stat.S_ISREG(s.st_mode) or stat.S_IMODE(s.st_mode)!=0o600 or s.st_uid!=root_stat.st_uid:
                raise Abort('unsafe diagnostic event file')

def delta_bundle(directory):
    require_root_private_tree(directory)
    manifest_path=directory/'manifest.json'
    if digest(manifest_path)!=PINNED_MANIFEST_SHA:raise Abort('manifest pin mismatch')
    manifest=json.loads(manifest_path.read_text())
    if (manifest.get('schema')!=1 or manifest.get('project')!=str(PROJECT)
            or manifest.get('base_commit')!='f5d03b7b5fb197eb76e78610a0bce672e5cc4853'
            or manifest.get('candidate_commit')!='46b6538'
            or manifest.get('files')!=PINNED_FILES):raise Abort('canary manifest mismatch')
    for item in PINNED_FILES:
        path=directory/'payload'/item['path']
        if not path.is_file() or path.is_symlink() or path.stat().st_size!=item['size'] or digest(path)!=item['sha256']:
            raise Abort('canary payload mismatch: '+item['path'])
    return manifest

def delta_inventory():
    result=snapshot(EXPECTED_BEFORE)
    for rel,expected in EXPECTED_BEFORE.items():
        state=result[rel]
        if expected is None:
            if state.get('type')!='absent':raise Abort('new runtime path is not absent: '+rel)
        elif state.get('type')!='regular' or state.get('sha256')!=expected:
            raise Abort('baseline mismatch: '+rel)
    shadow=PROJECT/'profiles/eeg/orangepi/src'
    if shadow.resolve()!=(PROJECT/'orangepi/src').resolve():raise Abort('EEG profile source shadow')
    for item in PINNED_FILES:
        suffix=item['path'][len('orangepi/src/'):]
        if (shadow/suffix).resolve()!=(PROJECT/item['path']).resolve():raise Abort('EEG source shadow mismatch')
    import yaml
    config=PROJECT/'profiles/eeg/orangepi/configs/devices.yaml'
    if config.is_symlink() or not config.is_file():raise Abort('EEG config missing or symlinked')
    eeg=(yaml.safe_load(config.read_text()) or {}).get('eeg') or {}
    if eeg.get('impedance_acceptance_policy','advisory')!='advisory':raise Abort('unreviewed EEG policy')
    check_diagnostic_paths()
    return result

def delta_main():
    global TARGET_DEVICE
    parser=argparse.ArgumentParser();parser.add_argument('--device',required=True)
    parser.add_argument('--manifest-sha256',required=True);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();TARGET_DEVICE=args.device
    if TARGET_DEVICE!='cp_0015':raise Abort('canary is limited to cp_0015')
    if os.geteuid()!=0:raise Abort('requires root')
    os.umask(0o077);require_device();require_root_private_tree(Path(__file__).absolute())
    directory=Path(__file__).absolute().parent;manifest=delta_bundle(directory)
    if args.manifest_sha256!=PINNED_MANIFEST_SHA:raise Abort('approval manifest hash mismatch')
    files=delta_inventory();assert_idle(api_state());locks=check_locks()
    if not service_active('embodied-pi-collect') or not service_active('camd'):raise Abort('required service inactive')
    unit=run(['systemctl','show','embodied-pi-collect','-p','ExecStart','--value']).stdout.decode()
    if str(PROJECT/'orangepi/scripts/run.sh') not in unit:raise Abort('unexpected controller unit')
    mainpid=run(['systemctl','show','embodied-pi-collect','-p','MainPID','--value']).stdout.decode().strip()
    if not mainpid.isdigit() or mainpid=='0':raise Abort('controller PID missing')
    cmdline=(Path('/proc')/mainpid/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
    if str(PROJECT/'orangepi/src/orangepi_collect/control/launcher.py') not in cmdline:raise Abort('unexpected controller process')
    pid=camd_pid();protected=protected_snapshot()
    if not args.apply:
        print(json.dumps({'status':'dry_run_passed','device':TARGET_DEVICE,'manifest':PINNED_MANIFEST_SHA,'camd_pid':pid,'capture_locks':locks,'protected_entries':len(protected)},sort_keys=True));return
    maintenance=Path('/var/backups/headring-code');maintenance.mkdir(mode=0o700,exist_ok=True);require_root_private_tree(maintenance)
    tx=Path(tempfile.mkdtemp(prefix=TARGET_DEVICE+'-cp15-canary-',dir=str(maintenance)));backup_dir=tx/'backup';diag=tx/'diag';diag.mkdir(mode=0o700)
    record={'device_id':TARGET_DEVICE,'manifest_sha256':PINNED_MANIFEST_SHA,'camd_pid_before':pid,'status':'preparing'}
    installed=[];meta=None
    try:
        write_json(diag/'protected-before.json',protected);stage_and_compile(directory,tx)
        if delta_inventory()!=files or protected_snapshot()!=protected or camd_pid()!=pid:raise Abort('preflight drift')
        assert_idle(api_state());check_locks()
        with stop_controller_and_verify(files) as held:
            meta=backup_files(PINNED_FILES,backup_dir,files)
            record.update(status='backed_up',capture_locks_held=held,backup_metadata=str(backup_dir/'metadata.json'))
            write_json(diag/'transaction.json',record)
            atomic_install(PINNED_FILES,tx/'stage',meta,files,installed)
        run(['systemctl','start','embodied-pi-collect'],timeout=60)
        state=health_check(manifest,pid);check_locks();check_diagnostic_paths()
        expected=dict(EXPECTED_BEFORE);expected.update({x['path']:x['sha256'] for x in PINNED_FILES})
        for rel,actual in snapshot(expected).items():
            if actual.get('type')!='regular' or actual.get('sha256')!=expected[rel]:raise Abort('post-install source mismatch: '+rel)
        after=protected_snapshot();write_json(diag/'protected-after.json',after)
        if after!=protected:raise Abort('protected state changed')
        record.update(status='committed',api_after=state,camd_pid_after=camd_pid(),replaced_paths=installed)
        write_json(diag/'transaction.json',record)
        print(json.dumps({'status':'committed','backup':str(backup_dir),'receipt':str(diag/'transaction.json')},sort_keys=True))
    except Exception as exc:
        record.update(error_type=type(exc).__name__,error=str(exc)[:500])
        recover_after_failure(record,diag/'transaction.json',installed,PINNED_FILES,meta,backup_dir,pid)
        raise

if __name__=='__main__':
    try:delta_main()
    except Abort as exc:raise SystemExit('aborted safely: '+str(exc))
