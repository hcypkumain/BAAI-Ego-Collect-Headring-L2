#!/usr/bin/env python3
"""Verify source coverage, syntax, excluded data and possible credential literals."""
import hashlib,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
RELOCATED={'README.md':'docs/framework/README.md','.gitignore':'docs/framework/gitignore.original'}
LOCAL_SELECTIONS={'orangepi/configs/reference_control.json','orangepi/configs/headphone_selection.json','orangepi/configs/audio_output.json'}

def main():
    manifest=json.loads((ROOT/'deployment/manifests/pi-source-snapshot.json').read_text(encoding='utf-8'))
    verified=0
    for row in manifest['files']:
        if row['path'] in LOCAL_SELECTIONS:
            assert not (ROOT/row['path']).exists(),'Do not publish station preferences: '+row['path']
            continue
        path=ROOT/RELOCATED.get(row['path'],row['path'])
        assert path.is_file(),'Missing deployed source: '+row['path']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256'],'Changed deployed source: '+row['path']
        verified+=1
    syntax=0
    for path in ROOT.rglob('*'):
        if '.git' in path.relative_to(ROOT).parts or not path.is_file():continue
        relative=path.relative_to(ROOT)
        assert not path.is_symlink(),'Unexpected symbolic link: '+str(relative)
        assert relative.parts[:2] not in (('orangepi','runtime'),('orangepi','private')),'Private runtime file: '+str(relative)
        assert relative.parts[0] not in ('data','delivery_backups','deleted-recordings','archives'),'Recording/archive content: '+str(relative)
        assert path.suffix.lower() not in ('.mp4','.mkv','.npz','.npy','.h264','.h265','.hevc','.sqlite','.sqlite3'),str(relative)
        if path.suffix=='.py':compile(path.read_bytes(),str(relative),'exec');syntax+=1
    process=subprocess.run([sys.executable,str(ROOT/'tools/audit_secrets.py'),str(ROOT)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf-8',env=dict(os.environ,PYTHONUTF8='1'))
    if process.returncode:
        print(process.stdout);print(process.stderr,file=sys.stderr);raise SystemExit(process.returncode)
    audit=json.loads(process.stdout)
    print(json.dumps(dict(deployed_files_verified=verified,local_selection_files_excluded=len(LOCAL_SELECTIONS),
        python_files_compiled=syntax,secret_audit=audit),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
