"""File transaction primitives; callers must enforce identity and hold capture locks.

Not a production entry point: service integration and root input validation are
not yet implemented. The explicit root parameter supports isolated tests.
"""
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import json

def digest(data):
    return hashlib.sha256(data).hexdigest()

def classify(path, content, mode, owner=0, group=0):
    try:
        st = path.lstat()
    except FileNotFoundError:
        return 'absent'
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise ValueError('unsafe existing target: ' + str(path))
    if path.read_bytes() == content and stat.S_IMODE(st.st_mode) == mode and st.st_uid == owner and st.st_gid == group:
        return 'same'
    return 'different'

def safe_dirs(root, parent, root_group_only, owner=0):
    root, parent = Path(root), Path(parent)
    for rel in [Path('.'), *[Path(*parent.relative_to(root).parts[:i]) for i in range(1, len(parent.relative_to(root).parts)+1)]]:
        path = root / rel
        try:
            st = path.lstat()
        except FileNotFoundError:
            path.mkdir(mode=0o755)
            os.chmod(path, 0o755)  # umask 077 must not make helper untraversable
            st = path.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != owner or st.st_mode & 0o002:
            raise ValueError('unsafe directory: ' + str(path))
        if st.st_mode & 0o020 and (st.st_gid != 0 or not root_group_only):
            raise ValueError('untrusted group-writable directory: ' + str(path))

def install(root, entries, root_group_only=False, fail_after=None, owner=0, group=0):
    """Install sandbox entries and rollback changed files if an operation fails.

    Entries map relative paths to (bytes, mode). Different existing targets are
    deliberately rejected until review and persistent backups are implemented.
    """
    root = Path(root)
    prepared = []
    for rel, (content, mode) in entries.items():
        path = Path(rel)
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise ValueError('invalid component path')
        target = root / path
        # Classify before creating directories. Symlink ancestors are rejected below.
        prepared.append((target, content, mode))
    installed = []
    try:
        for target, content, mode in prepared:
            safe_dirs(root, target.parent, root_group_only, owner)
            state = classify(target, content, mode, owner, group)
            if state == 'same':
                continue
            if state == 'different':
                raise ValueError('existing component differs; manual review required')
            fd, name = tempfile.mkstemp(prefix='.fleet-', dir=target.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(name, mode)
                # link fails rather than overwriting a concurrent creation.
                os.link(name, target, follow_symlinks=False)
                installed.append((target, digest(content)))
            finally:
                os.unlink(name)
            if fail_after is not None and len(installed) >= fail_after:
                raise RuntimeError('injected failure')
        return {'status': 'installed' if installed else 'already_current', 'installed': len(installed)}
    except Exception:
        for path, expected in reversed(installed):
            try:
                st = path.lstat()
                if stat.S_ISREG(st.st_mode) and digest(path.read_bytes()) == expected:
                    path.unlink()
            except FileNotFoundError:
                pass
        raise

def snapshot(path):
    try:
        st=path.lstat()
    except FileNotFoundError:
        return {'type':'absent'}
    if not stat.S_ISREG(st.st_mode): raise ValueError('unsafe target: '+str(path))
    return {'type':'regular','sha256':digest(path.read_bytes()),'mode':stat.S_IMODE(st.st_mode),
            'uid':st.st_uid,'gid':st.st_gid}

def install_reviewed(root, entries, baseline, backup_dir, *, owner=0, group=0,
                     root_group_only=False, after_install=None):
    """Sandbox primitive for reviewed replacements, not a production adapter.

    Backups are persisted under an exclusive backup directory. Caller must
    enforce capture locking, device identity, safe inputs and service checks.
    """
    root=Path(root); backup_dir=Path(backup_dir); prepared=[]
    for rel,(data,mode) in entries.items():
        rel=Path(rel)
        if rel.is_absolute() or not rel.parts or '..' in rel.parts: raise ValueError('invalid target')
        path=root/rel
        safe_dirs(root,path.parent,root_group_only,owner)
        before=snapshot(path)
        if before!=baseline.get(str(rel)): raise ValueError('target changed since review: '+str(rel))
        prepared.append((rel,path,data,mode,before))
    backup_dir.mkdir(mode=0o700)
    metadata={}; installed=[]
    try:
        for rel,path,data,mode,before in prepared:
            metadata[str(rel)]=before
            if before['type']=='regular':
                dest=backup_dir/rel; dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                with dest.open('xb') as out: out.write(path.read_bytes())
                if digest(dest.read_bytes())!=before['sha256']: raise ValueError('backup changed during read')
                os.chmod(dest,0o600)
        (backup_dir/'metadata.json').write_text(json.dumps(metadata,sort_keys=True,indent=2))
        os.chmod(backup_dir/'metadata.json',0o600)
        for rel,path,data,mode,before in prepared:
            if before['type']=='regular' and before['sha256']==digest(data) and (before['mode'],before['uid'],before['gid'])==(mode,owner,group): continue
            if snapshot(path)!=before: raise ValueError('target changed before replacement: '+str(rel))
            fd,name=tempfile.mkstemp(prefix='.fleet-component-',dir=path.parent)
            try:
                with os.fdopen(fd,'wb') as stream: stream.write(data); stream.flush(); os.fsync(stream.fileno())
                os.chmod(name,mode)
                if os.geteuid()==0: os.chown(name,owner,group)
                os.replace(name,path); installed.append((rel,path,digest(data),before))
            finally:
                if os.path.exists(name): os.unlink(name)
            if after_install: after_install(rel,path)
        return {'status':'installed' if installed else 'already_current','installed':len(installed),'backup':str(backup_dir)}
    except Exception as exc:
        problems=[]
        for rel,path,expected,before in reversed(installed):
            try:
                now=snapshot(path)
                if now.get('sha256')!=expected:
                    problems.append(str(rel)); continue
                if before['type']=='absent': path.unlink()
                else:
                    backup=backup_dir/rel
                    if digest(backup.read_bytes())!=before['sha256']: problems.append(str(rel)); continue
                    fd,name=tempfile.mkstemp(prefix='.fleet-component-rollback-',dir=path.parent)
                    try:
                        with os.fdopen(fd,'wb') as out: out.write(backup.read_bytes()); out.flush(); os.fsync(out.fileno())
                        os.chmod(name,before['mode'])
                        if os.geteuid()==0: os.chown(name,before['uid'],before['gid'])
                        os.replace(name,path)
                    finally:
                        if os.path.exists(name): os.unlink(name)
            except Exception: problems.append(str(rel))
        if problems: raise RuntimeError('rollback incomplete; preserve external changes: '+', '.join(problems)) from exc
        raise
