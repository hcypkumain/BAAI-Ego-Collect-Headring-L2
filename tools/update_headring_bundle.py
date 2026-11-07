#!/usr/bin/env python3
"""Build a new internal setup bundle without altering its existing source bundle.

Python/ARM runtime dependencies are retained; published collector files and the
mandatory host-role initializer come from the reviewed current Git manifest.
The original bundle may contain private network/voice settings: never push the
generated payload or templates to a public repository.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile

REPO=Path(__file__).resolve().parents[1]
BASE='home/orangepi/Desktop/Embodied-Brain-Collect-main.2/'

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def build(source,output):
    if output.exists():raise RuntimeError('Choose a new output directory; existing bundles are not overwritten')
    pointer=json.loads((REPO/'deployment/manifests/current-release.json').read_text())
    release=json.loads((REPO/'deployment/manifests'/pointer['manifest']).read_text())
    original=json.loads((source/'release.json').read_text())
    replacements={target.lstrip('/'):REPO/name for name,target in release['runtime_targets'].items()}
    for name in ['embodied-pi-collect.service','embodied-pi-collect.service.d/60-impedance-preflight.conf','embodied-pi-collect.service.d/70-camd-migration.conf']:
        replacements['etc/systemd/system/'+name]=REPO/'deployment/systemd'/name
    replacements[BASE+'orangepi/configs/collection_mode.json']=REPO/'orangepi/configs/collection_mode.json'
    replacements['etc/headring-lan-time.json']=REPO/'deployment/lan-time/headring-lan-time.example.json'
    # Keep clock alignment before the camera, without delaying the early web UI.
    generated={
        'etc/systemd/system/camd.service.d/90-lan-time.conf':b'[Unit]\nWants=headring-lan-time.service\nAfter=headring-lan-time.service\n',
        'etc/systemd/system/embodied-pi-collect.service.d/90-lan-time.conf':b'[Unit]\nWants=headring-lan-time.service\n',
    }
    for name,path in replacements.items():
        if not path.is_file():raise RuntimeError('Missing published bundle source: '+str(path))
    output.mkdir(parents=True)
    program=dict(original['program_files'])
    pending=dict(replacements);pending_generated=dict(generated)
    def write_member(dst,name,data,mode=0o644):
        member=tarfile.TarInfo(name);member.size=len(data);member.mode=mode;member.mtime=0
        import io
        dst.addfile(member,io.BytesIO(data));program[name]=hashlib.sha256(data).hexdigest()
    with tarfile.open(source/'payload.tgz','r:gz') as src,tarfile.open(output/'payload.tgz','w:gz',compresslevel=3) as dst:
        for member in src:
            name=member.name.rstrip('/')
            if name in pending:
                data=pending.pop(name).read_bytes()
                write_member(dst,name,data,0o755 if data.startswith(b'#!') else 0o644)
            elif name in pending_generated:write_member(dst,name,pending_generated.pop(name))
            else:dst.addfile(member,src.extractfile(member) if member.isfile() else None)
        for name,path in pending.items():
            data=path.read_bytes();write_member(dst,name,data,0o755 if data.startswith(b'#!') else 0o644)
        for name,data in pending_generated.items():write_member(dst,name,data)
    features={name:{'target':target,'sha256':release['files'][name]} for name,target in release['runtime_targets'].items()}
    manifest=dict(original,version=release['version'],payload_sha256=digest(output/'payload.tgz'),files=features,program_files=program,
                  usb_data_host_required=True,reference_images_included=False)
    (output/'release.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8',newline='\n')
    for name in ['install.py','device_setup.py']:shutil.copy2(REPO/'tools/provision'/name,output/name)
    for name in ['node.template.toml','easytier.template.service','seed_known_hosts']:
        shutil.copy2(source/name,output/name)
    for name in ['runtime','lib']:
        if (source/name).is_dir():shutil.copytree(source/name,output/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ['安装头环.cmd','headring-setup.cmd']:
        if (source/name).is_file():shutil.copy2(source/name,output/name)
    # Validate every extraction destination with the current fresh installer.
    # The backend imports Linux-only modules. Use syntax validation everywhere;
    # its extraction allow-list is also exercised by Linux regression tests.
    compile((output/'device_setup.py').read_bytes(),'device_setup.py','exec')
    (output/'README.txt').write_text('头环统一版 '+release['version']+'\n安装时必须初始化 Pi USB 数据主机，并启用开机/重插守卫。\n只安装全新、同型号 aarch64 Ubuntu 20.04 头环；已有设备使用安全升级流程。\n保留本机标定与 SD UUID，不复制任务进度或手机授权。\n首次手机调试授权必须由用户确认。\n此包含原安装包的内部网络/语音配置，仅限当前团队使用，不可公开推送。\n',encoding='utf-8',newline='\n')
    return {'version':release['version'],'output':str(output),'payload_bytes':(output/'payload.tgz').stat().st_size,'published_runtime_files':len(features),'usb_data_host_required':True}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(build(args.bundle.resolve(),args.output.resolve()),ensure_ascii=False))

if __name__=='__main__':main()
