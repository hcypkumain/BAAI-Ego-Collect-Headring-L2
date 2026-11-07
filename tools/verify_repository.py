#!/usr/bin/env python3
"""Verify source coverage, syntax, excluded data and possible credential literals."""
import hashlib,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
RELOCATED={'README.md':'docs/framework/README.md','.gitignore':'docs/framework/gitignore.original'}
LOCAL_SELECTIONS={'orangepi/configs/reference_control.json','orangepi/configs/headphone_selection.json','orangepi/configs/audio_output.json'}

def main():
    manifest=json.loads((ROOT/'deployment/manifests/pi-source-snapshot.json').read_text(encoding='utf-8'))
    migration_path=ROOT/'deployment/manifests/repository-changes.json'
    changes=json.loads(migration_path.read_text(encoding='utf-8'))['files'] if migration_path.is_file() else []
    changes={row['original_path']:row for row in changes}
    verified=retired=modified=0
    for row in manifest['files']:
        if row['path'] in LOCAL_SELECTIONS:
            assert not (ROOT/row['path']).exists(),'Do not publish station preferences: '+row['path']
            continue
        change=changes.get(row['path'])
        if change:
            assert change['baseline_sha256']==row['sha256'],'Baseline drift: '+row['path']
            if change['action']=='removed':
                assert not (ROOT/row['path']).exists(),'Retired file returned: '+row['path']
                retired+=1;continue
        path=ROOT/(change['current_path'] if change else RELOCATED.get(row['path'],row['path']))
        assert path.is_file(),'Missing deployed source: '+row['path']
        expected=change['published_sha256'] if change else row['sha256']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==expected,'Changed source: '+row['path']
        if change:modified+=1
        verified+=1
    pointer=ROOT/'deployment/manifests/current-release.json'
    release_name=json.loads(pointer.read_text(encoding='utf-8'))['manifest'] if pointer.is_file() else 'headring-common-20261106-v3.json'
    assert Path(release_name).name==release_name,'Invalid release manifest name'
    release=json.loads((ROOT/'deployment/manifests'/release_name).read_text(encoding='utf-8'))
    for relative,expected in release['files'].items():
        assert hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()==expected,'Fleet release drift: '+relative
    for relative,expected in release.get('installer_sources',{}).items():
        assert hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()==expected,'Installer source drift: '+relative
    syntax=0
    for folder in ('services','orangepi/src','deployment/systemd'):
        for path in (ROOT/folder).rglob('*'):
            if not path.is_file() or path.suffix not in ('.py','.js','.css','.html','.service','.conf'):continue
            data=path.read_bytes().lower()
            assert b'rv06' not in data and b'192.168.250.' not in data,'Retired terminal dependency: '+str(path.relative_to(ROOT))
    for path in ROOT.rglob('*'):
        if '.git' in path.relative_to(ROOT).parts or not path.is_file():continue
        relative=path.relative_to(ROOT)
        assert not path.is_symlink(),'Unexpected symbolic link: '+str(relative)
        assert relative.parts[:2] not in (('orangepi','runtime'),('orangepi','private')),'Private runtime file: '+str(relative)
        assert relative.parts[0] not in ('profiles','data','delivery_backups','deleted-recordings','archives'),'Recording/archive content: '+str(relative)
        assert path.suffix.lower() not in ('.mp4','.mkv','.npz','.npy','.h264','.h265','.hevc','.sqlite','.sqlite3'),str(relative)
        if path.suffix=='.py':compile(path.read_bytes(),str(relative),'exec');syntax+=1
    process=subprocess.run([sys.executable,str(ROOT/'tools/audit_secrets.py'),str(ROOT)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf-8',env=dict(os.environ,PYTHONUTF8='1'))
    if process.returncode:
        print(process.stdout);print(process.stderr,file=sys.stderr);raise SystemExit(process.returncode)
    audit=json.loads(process.stdout)
    print(json.dumps(dict(snapshot_or_derived_files_verified=verified,retired_files_verified=retired,
        documented_repository_changes=modified,local_selection_files_excluded=len(LOCAL_SELECTIONS),
        fleet_release=release['version'],fleet_release_files_verified=len(release['files']),
        runtime_terminal_dependencies_absent=True,
        python_files_compiled=syntax,secret_audit=audit),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
