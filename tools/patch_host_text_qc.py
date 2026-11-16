#!/usr/bin/env python3
"""Apply only text-reference validation to the existing local host QC copy."""
import argparse,hashlib,os,stat,tempfile
from pathlib import Path

PAIRS=(('_frozen_reference_selection','_frozen_session_identity'),('_write_pi_session_metadata','standardize_emg'))

def block(text,name,next_name):
    start=text.index('def '+name+'(');end=text.index('def '+next_name+'(',start);return text[start:end]

def prepare(original,baseline,feature):
    old=original.decode('utf-8');base=baseline.decode('utf-8');new=feature.decode('utf-8')
    for name,next_name in PAIRS:
        if block(old,name,next_name)!=block(base,name,next_name):raise ValueError('host QC validation function differs from reviewed base')
        old=old.replace(block(old,name,next_name),block(new,name,next_name),1)
    return old.encode('utf-8')

def atomic(path,data,mode):
    if path.is_symlink():raise ValueError('target symlink')
    fd,tmp=tempfile.mkstemp(prefix='.text-qc-',dir=str(path.parent))
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path);d=os.open(str(path.parent),os.O_RDONLY|os.O_DIRECTORY);os.fsync(d);os.close(d)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('plan','apply'));p.add_argument('--host-root',type=Path,required=True);p.add_argument('--base-framework',type=Path,required=True);p.add_argument('--feature-framework',type=Path,required=True);p.add_argument('--text-module',type=Path,required=True);p.add_argument('--backup',type=Path,required=True);p.add_argument('--expected-host-sha256',required=True);a=p.parse_args()
    src=a.host_root/'collector/orangepi/src/orangepi_collect';target=src/'framework/framework_qc.py';module=src/'common/text_reference.py'
    if target.is_symlink() or module.is_symlink() or not target.is_file() or module.exists():raise ValueError('unsafe or pre-existing host target')
    original=target.read_bytes()
    if hashlib.sha256(original).hexdigest()!=a.expected_host_sha256:raise ValueError('host QC changed')
    after=prepare(original,a.base_framework.read_bytes(),a.feature_framework.read_bytes());text=a.text_module.read_bytes()
    compile(after,str(target),'exec');compile(text,str(module),'exec')
    print('host_before='+hashlib.sha256(original).hexdigest()+' host_after='+hashlib.sha256(after).hexdigest()+' module='+hashlib.sha256(text).hexdigest(),flush=True)
    if a.action=='plan':return
    a.backup.mkdir(parents=True,exist_ok=False);atomic(a.backup/'framework_qc.before.py',original,0o600)
    # Add the module first; no existing workload refers to it before validation changes.
    fd=os.open(str(module),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
    try:
        with os.fdopen(fd,'wb') as f:f.write(text);f.flush();os.fsync(f.fileno())
        if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest()!=a.expected_host_sha256:raise ValueError('host QC changed during apply')
        mode=stat.S_IMODE(target.stat().st_mode);atomic(target,after,mode)
    except BaseException:
        # os.replace may have committed even when a later directory fsync fails.
        # Never remove the new module while the new validator may still use it.
        try:
            current=target.read_bytes() if target.is_file() and not target.is_symlink() else None
            if current==after:atomic(target,original,mode)
            if target.is_file() and not target.is_symlink() and target.read_bytes()==original:
                if module.exists() and not module.is_symlink() and hashlib.sha256(module.read_bytes()).digest()==hashlib.sha256(text).digest():module.unlink()
        except Exception:
            pass  # Keep the module if restoration is uncertain; inspect backup.
        raise
    print('host text QC compatibility installed; service restart not required')
if __name__=='__main__':main()
