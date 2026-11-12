"""Configured host storage with explicit, signature-preserving legacy relocation."""
import os
import pathlib
import subprocess
from host.common.strict_json import strict_json_loads

ROOT = pathlib.Path(__file__).parent

def configuration(root=ROOT):
    return strict_json_loads((pathlib.Path(root) / 'config.json').read_text())

def data_root(config=None, *, config_root=ROOT):
    config = configuration(config_root) if config is None else config
    return pathlib.Path(config['data_root'])

def storage_path(value, config=None, *, config_root=ROOT):
    config = configuration(config_root) if config is None else config
    path = pathlib.Path(value)
    # Only fixed, administrator-configured prefixes may be relocated. Receipt
    # payloads and their signatures remain byte-for-byte unchanged.
    mappings = config.get('storage_relocations', {})
    for old, new in sorted(mappings.items(), key=lambda row: len(row[0]), reverse=True):
        try:
            relative = path.relative_to(pathlib.Path(old))
        except ValueError:
            continue
        if '..' in relative.parts:
            raise ValueError('Unsafe relocated storage path')
        return pathlib.Path(new) / relative
    return path

def require_storage(config=None, write_paths=None, *, config_root=ROOT):
    config = configuration(config_root) if config is None else config
    root = pathlib.Path(config['data_root'])
    if not root.is_absolute() or root.resolve() != root:
        raise RuntimeError('STORAGE_UNAVAILABLE: data root must be an ordinary absolute path')
    expected = config.get('storage_uuid')
    if expected:
        mount = pathlib.Path(config['storage_mount'])
        if not mount.is_mount():
            raise RuntimeError('STORAGE_UNAVAILABLE: archive drive is not mounted')
        root.relative_to(mount)
        actual = subprocess.check_output(
            ['findmnt', '-n', '-o', 'UUID', '--mountpoint', str(mount)],
            text=True, timeout=10).strip()
        if actual != expected:
            raise RuntimeError('STORAGE_UNAVAILABLE: wrong archive drive')
        # Services can protect the parent mount while exposing specific child
        # directories for writing. Check the effective mounts of those paths.
        for path in (write_paths if write_paths is not None else (root,)):
            path = pathlib.Path(path)
            path.relative_to(root)
            if path.resolve() != path or not path.is_dir() or path.stat().st_dev != mount.stat().st_dev:
                raise RuntimeError('STORAGE_UNAVAILABLE: writable directory is not on the archive drive')
            if os.statvfs(path).f_flag & os.ST_RDONLY:
                raise RuntimeError('STORAGE_UNAVAILABLE: archive write directory is read-only')
    return root

def enrolled_devices(config=None):
    config = configuration() if config is None else config
    return {d['id'] for d in config['devices']}
