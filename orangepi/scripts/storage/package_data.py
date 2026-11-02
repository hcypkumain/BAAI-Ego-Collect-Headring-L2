#!/usr/bin/env python3
"""Archive one finished SD session without renaming or converting its contents."""
import argparse
from datetime import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src/orangepi_collect/common'))
from project_paths import BASE
import storage_paths


def digest(path):
    sha=hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda:file.read(1024*1024),b''):sha.update(block)
    return sha.hexdigest()


def package(session):
    volume=storage_paths.volume_root(check_write=True).resolve()
    session=Path(session).resolve(strict=True)
    relative=session.relative_to(volume)
    if relative.parts[0] not in ('data','diagnostics') or len(relative.parts)<2:
        raise ValueError('Choose a single session under the configured SD data/ or diagnostics/')
    state=json.loads((session/'status.json').read_text()).get('state')
    if state not in ('success','qc_error','error','cancelled'):
        raise ValueError('Session is still recording, saving or checking; wait until it finishes')
    with (BASE/'runtime/capture.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        paths=sorted(session.rglob('*'))
        if any(p.is_symlink() for p in paths):raise ValueError('Session contains symlinks; copy actual data before packaging')
        files=[p for p in paths if p.is_file()]
        snapshot={str(p.relative_to(volume)):(p.stat().st_size,p.stat().st_mtime_ns) for p in files}
        output_dir=storage_paths.export_root()/'data'
        output_dir.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(output_dir).free<sum(p.stat().st_size for p in files)+1024**3:
            raise ValueError('SD free space is insufficient for archive plus 1 GiB reserve')
        name='orangepi-data-%s-%s-%s.tar.gz'%(storage_paths.config()['device_id'],session.name,datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        output=output_dir/name
        partial=output.with_suffix(output.suffix+'.partial')
        with tarfile.open(partial,'x:gz',compresslevel=1) as archive:
            archive.add(session,arcname=str(relative),recursive=True)
        after={str(p.relative_to(volume)):(p.stat().st_size,p.stat().st_mtime_ns) for p in session.rglob('*') if p.is_file()}
        if snapshot!=after:raise RuntimeError('Source session changed during packaging; .partial is not deliverable')
        partial.rename(output)
        output.with_suffix(output.suffix+'.sha256').write_text(digest(output)+'  '+output.name+'\n')
        print(output)
        return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session',type=Path)
    args=parser.parse_args()
    package(args.session)
