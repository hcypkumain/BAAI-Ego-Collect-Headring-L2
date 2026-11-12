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


PINNED_FILES = [{'path': 'orangepi/src/orangepi_collect/common/impedance_quality.py', 'before_sha256': 'b46fd30276c758c74ccdbe90468f6551d28a36be4878f12a922f9ea57fcccef4', 'sha256': 'da24ab2e98343042a764199465907935bd2eb3d07ae5b9f905913f0857d42fab', 'size': 9317, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/common/eeg_settings.py', 'before_sha256': '330acc2822e2966a35e1e770ac6a005f453c7ed69adf21982b5ade2da0f506c5', 'sha256': 'a90774add55180ad2e1477996b63e95a25b2bb10a0ee7ea8f9dfa07e9d2dbbe0', 'size': 1991, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/common/impedance_record.py', 'before_sha256': '8eb1524cddcd56e02b73305914076bd9e1e95d9305c3706d9e17b8457b924a94', 'sha256': 'dc437d13d2e488a361671949fc6dc9199a7148447191720bf2287c36a65a09d3', 'size': 19182, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/control/dashboard.html', 'before_sha256': '2b473bc8e8d24af727255c4e9632935de489b3303ff0f434956e8067c270164a', 'sha256': 'b787ed400e657e77770ce9a627b07cc28636ab0c2faf6b524944d559ff992ff3', 'size': 95065, 'git_mode': '100644'}, {'path': 'orangepi/src/orangepi_collect/framework/eeg_qc.py', 'before_sha256': '9f9749ea369378a8f1e73987095616bd8683c5ee52598dc5aee49a9b9fc7c1b0', 'sha256': 'b362e196b9f2d568e9e64037667a0cfbf31e2b7c12751344cbf7fdb730a9b78d', 'size': 18349, 'git_mode': '100644'}]
PINNED_MANIFEST_SHA = '36d8059918ce370f7928cc7cd3207e2ac1d26471b3e8dcdef74b98b7888cd62a'

def delta_bundle(directory):
    require_root_private_tree(directory)
    manifest_path=directory/'manifest.json'
    if digest(manifest_path)!=PINNED_MANIFEST_SHA: raise Abort('manifest pin mismatch')
    manifest=json.loads(manifest_path.read_text())
    if manifest.get('schema')!=1 or manifest.get('project')!=str(PROJECT) or manifest.get('commit')!='f5d03b7b5fb197eb76e78610a0bce672e5cc4853' or manifest.get('files')!=PINNED_FILES:
        raise Abort('delta manifest mismatch')
    for item in PINNED_FILES:
        path=directory/'payload'/item['path']
        if not path.is_file() or path.is_symlink() or path.stat().st_size!=item['size'] or digest(path)!=item['sha256']:
            raise Abort('delta payload mismatch: '+item['path'])
    return manifest

def delta_inventory():
    paths=[x['path'] for x in PINNED_FILES]
    result=snapshot(paths)
    for item in PINNED_FILES:
        state=result[item['path']]
        if state.get('type')!='regular' or state.get('sha256')!=item['before_sha256']:
            raise Abort('baseline mismatch: '+item['path'])
    shadow=PROJECT/'profiles/eeg/orangepi/src'
    if shadow.resolve()!=(PROJECT/'orangepi/src').resolve():
        raise Abort('EEG profile source is not the reviewed primary tree')
    for item in PINNED_FILES:
        suffix=item['path'][len('orangepi/src/'):]
        if (shadow/suffix).resolve()!=(PROJECT/item['path']).resolve(): raise Abort('EEG source shadow mismatch')
    import yaml
    config=PROJECT/'profiles/eeg/orangepi/configs/devices.yaml'
    if config.is_symlink() or not config.is_file(): raise Abort('EEG config missing or symlinked')
    eeg=(yaml.safe_load(config.read_text()) or {}).get('eeg') or {}
    if eeg.get('impedance_acceptance_policy','advisory')!='advisory':
        raise Abort('EEG explicit acceptance policy requires separate review')
    return result

def delta_main():
    global TARGET_DEVICE
    parser=argparse.ArgumentParser()
    parser.add_argument('--device',required=True)
    parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args(); TARGET_DEVICE=args.device
    if os.geteuid()!=0: raise Abort('requires root')
    os.umask(0o077); require_device(); require_root_private_tree(Path(__file__).absolute())
    directory=Path(__file__).absolute().parent
    manifest=delta_bundle(directory)
    if args.manifest_sha256!=PINNED_MANIFEST_SHA: raise Abort('approval manifest hash mismatch')
    files=delta_inventory(); assert_idle(api_state()); locks=check_locks()
    if not service_active('embodied-pi-collect') or not service_active('camd'): raise Abort('required service inactive')
    unit=run(['systemctl','show','embodied-pi-collect','-p','ExecStart','--value']).stdout.decode()
    if str(PROJECT/'orangepi/scripts/run.sh') not in unit: raise Abort('controller unit uses unexpected source')
    mainpid=run(['systemctl','show','embodied-pi-collect','-p','MainPID','--value']).stdout.decode().strip()
    if not mainpid.isdigit() or mainpid=='0': raise Abort('controller PID missing')
    cmdline=(Path('/proc')/mainpid/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
    if str(PROJECT/'orangepi/src/orangepi_collect/control/launcher.py') not in cmdline: raise Abort('controller process uses unexpected source')
    pid=camd_pid(); protected=protected_snapshot()
    if not args.apply:
        print(json.dumps({'status':'dry_run_passed','device':TARGET_DEVICE,'manifest':PINNED_MANIFEST_SHA,'camd_pid':pid,'capture_locks':locks,'protected_entries':len(protected)},sort_keys=True));return
    maintenance=Path('/var/backups/headring-code');maintenance.mkdir(mode=0o700,exist_ok=True);require_root_private_tree(maintenance)
    tx=Path(tempfile.mkdtemp(prefix=TARGET_DEVICE+'-eeg-delta-',dir=str(maintenance)));backup_dir=tx/'backup';diag=tx/'diag';diag.mkdir(mode=0o700)
    record={'device_id':TARGET_DEVICE,'manifest_sha256':PINNED_MANIFEST_SHA,'camd_pid_before':pid,'status':'preparing'}
    installed=[];meta=None
    try:
        write_json(diag/'protected-before.json',protected)
        stage_and_compile(directory,tx)
        if delta_inventory()!=files or protected_snapshot()!=protected or camd_pid()!=pid: raise Abort('preflight drift')
        assert_idle(api_state());check_locks()
        with stop_controller_and_verify(files) as held:
            meta=backup_files(PINNED_FILES,backup_dir,files)
            record.update(status='backed_up',capture_locks_held=held,backup_metadata=str(backup_dir/'metadata.json'))
            write_json(diag/'transaction.json',record)
            atomic_install(PINNED_FILES,tx/'stage',meta,files,installed)
        run(['systemctl','start','embodied-pi-collect'],timeout=60)
        state=health_check(manifest,pid)
        after=protected_snapshot();write_json(diag/'protected-after.json',after)
        if after!=protected: raise Abort('protected state changed')
        record.update(status='committed',api_after=state,camd_pid_after=camd_pid(),replaced_paths=installed)
        write_json(diag/'transaction.json',record)
        print(json.dumps({'status':'committed','backup':str(backup_dir),'receipt':str(diag/'transaction.json')},sort_keys=True))
    except Exception as exc:
        record.update(error_type=type(exc).__name__,error=str(exc)[:500])
        recover_after_failure(record,diag/'transaction.json',installed,PINNED_FILES,meta,backup_dir,pid)
        raise

if __name__=='__main__':
    try: delta_main()
    except Abort as exc: raise SystemExit('aborted safely: '+str(exc))
