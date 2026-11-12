#!/usr/bin/env python3
"""Pinned fleet code and USB component deployment."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PIN = 'f5d03b7b5fb197eb76e78610a0bce672e5cc4853'
MANIFEST_SHA256 = '95c02f40ad634a02218df4bc84c4093c3571cae3eace0dc6c95fa0e87f462109'
RESOURCES = HERE / 'resources'
COMPONENTS = {
    'orangepi/scripts/usb_tasks_mount_readonly.py': '/usr/local/lib/collector-usb-task-import/mount-readonly',
    'deployment/sudoers/collector-usb-task-import': '/etc/sudoers.d/collector-usb-task-import',
    'deployment/systemd/collector-usb-task-mount.service': '/etc/systemd/system/collector-usb-task-mount.service',
    'deployment/systemd/collector-usb-task-mount.timer': '/etc/systemd/system/collector-usb-task-mount.timer',
    'deployment/udev/72-collector-usb-task-import.rules': '/etc/udev/rules.d/72-collector-usb-task-import.rules',
}

def sha(data):
    return hashlib.sha256(data).hexdigest()

def git(*args):
    return subprocess.run(['git', '-C', str(REPO), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

def target(value):
    match = re.fullmatch(r'cp_?(\d{4})', value)
    if not match:
        raise argparse.ArgumentTypeError('target must be cp followed by four digits')
    return 'cp_' + match.group(1)

def address(value):
    import ipaddress
    try:
        name, ip = value.split('=', 1)
        return target(name), str(ipaddress.ip_address(ip))
    except (ValueError, argparse.ArgumentTypeError) as exc:
        raise argparse.ArgumentTypeError('address must be cpNNNN=IP') from exc

def pinned_file(path):
    if path.startswith('/') or '..' in Path(path).parts:
        raise ValueError('invalid repository path')
    return git('show', PIN + ':' + path)

def build(state_dir):
    if git('rev-parse', 'release/headring-v8.5-integrity-stop^{commit}').decode().strip() != PIN:
        raise ValueError('release ref does not match reviewed pin')
    manifest_bytes = (RESOURCES / 'manifest.json').read_bytes()
    if sha(manifest_bytes) != MANIFEST_SHA256:
        raise ValueError('reviewed manifest hash mismatch')
    manifest = json.loads(manifest_bytes)
    if manifest['commit'] != PIN or len(manifest['files']) != 236:
        raise ValueError('reviewed manifest pin/count mismatch')
    # All bytes are read from the Git object database, never the dirty worktree.
    payload = []
    for entry in manifest['files']:
        rel = entry['path']
        if rel == 'orangepi/src/orangepi_collect/control/reference_credentials.py':
            raise ValueError('credential file forbidden')
        data = pinned_file(rel)
        if sha(data) != entry['sha256'] or len(data) != entry['size']:
            raise ValueError('pinned file differs from reviewed manifest: ' + rel)
        payload.append((rel, data))
    components = {}; component_data = {}
    for source, dest in COMPONENTS.items():
        data = pinned_file(source)
        components[source] = {'destination': dest, 'sha256': sha(data), 'size': len(data),
                              'mode': 0o440 if dest.startswith('/etc/sudoers.d/') else 0o755 if dest.startswith('/usr/local/') else 0o644}
        component_data[source] = data
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='candidate-code-', suffix='.tar.gz', dir=state_dir)
    os.close(fd)
    bundle = Path(name)
    # Only exact validated entries are written, with neutral archive metadata.
    with tarfile.open(bundle, 'w:gz') as tar:
        for name, data in [('manifest.json', manifest_bytes)] + [('payload/' + name, data) for name, data in payload]:
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    os.chmod(bundle, 0o600)
    fd, name = tempfile.mkstemp(prefix='usb-components-', suffix='.tar', dir=state_dir)
    os.close(fd); component_bundle = Path(name)
    component_manifest = (json.dumps({'commit':PIN,'files':components},sort_keys=True,separators=(',',':'))+'\n').encode()
    with tarfile.open(component_bundle, 'w') as tar:
        for name,data in [('manifest.json',component_manifest)]+[('payload/'+name,data) for name,data in component_data.items()]:
            info=tarfile.TarInfo(name); info.mode=0o600; info.size=len(data); tar.addfile(info,io.BytesIO(data))
    os.chmod(component_bundle,0o600)
    return {'commit': PIN, 'manifest_sha256': sha(manifest_bytes), 'files': len(payload),
            'bundle': str(bundle), 'bundle_sha256': sha(bundle.read_bytes()), 'components': components,
            'component_bundle':str(component_bundle),'component_bundle_sha256':sha(component_bundle.read_bytes()),
            'component_manifest_sha256':sha(component_manifest)}

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release', default='stable', help='registered release: stable (default) or eeg-impedance-advisory')
    p.add_argument('--offline-plan', action='store_true', help='build and validate pinned bundle without contacting devices')
    p.add_argument('--targets', nargs='+', type=target, default=['cp_0014', 'cp_0018'])
    p.add_argument('--address', action='append', type=address, default=[])
    p.add_argument('--state-dir', type=Path, default=Path.home()/'.cache/headring-fleet-maintenance')
    p.add_argument('--ssh-config', type=Path, default=Path.home()/'headring-qc/ssh/config')
    p.add_argument('--bind-interface')
    p.add_argument('--bind-address')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--dry-run', action='store_true', help='read-only plan (the default)')
    p.add_argument('--verify', action='store_true')
    p.add_argument('--accepted-plan', help='SHA-256 of reviewed per-device plan; required for apply/verify')
    p.add_argument('--sudo-command-file', type=Path, help='stage inputs and write separate password-free root dry-run/apply argv instead of executing')
    p.add_argument('--approve-unknown-baseline', action='store_true', help='allow a plan for reviewed source modifications outside pinned Git history')
    p.add_argument('--audit-recordings', action='store_true')
    p.add_argument('--local-root', action='append', type=Path, default=[], help='explicit local raw-backup candidate root for read-only audit')
    args = p.parse_args(argv)
    if args.dry_run and (args.apply or args.verify):
        p.error('--dry-run cannot be combined with --apply or --verify')
    if args.sudo_command_file and not args.apply:
        p.error('--sudo-command-file requires --apply and an accepted plan')
    addresses = dict(args.address)
    if len(addresses) != len(args.address) or set(addresses) - set(args.targets):
        p.error('duplicate or unselected target address')
    if args.release.startswith('cp15-start-impedance-diagnostics-'):
        import cp15_release
        return cp15_release.main(args,p)
    if args.release != 'stable':
        import delta_release
        return delta_release.main(args, p)
    if args.offline_plan and (args.apply or args.verify or args.audit_recordings or args.accepted_plan):
        p.error('offline plan cannot be combined with device operations')
    if args.audit_recordings:
        if args.apply or args.verify or args.accepted_plan or args.approve_unknown_baseline:
            p.error('recording audit cannot be combined with deployment operations')
        import online
        import audit_recordings
        results=[]
        for device in args.targets:
            try:
                ssh=online.SSH(args.ssh_config,device,addresses.get(device),args.bind_interface,args.bind_address)
                results.append(audit_recordings.audit(ssh,device,args.local_root))
            except (online.Failure,OSError,subprocess.SubprocessError,ValueError,KeyError) as exc:
                results.append({'device':device,'status':'not_audited','error':str(exc)})
        print(json.dumps({'results':results},indent=2,sort_keys=True))
        return 2 if any(x.get('status')=='not_audited' for x in results) else 0
    if args.apply and args.verify:
        p.error('--apply already verifies; use --verify separately')
    if (args.apply or args.verify) and (not args.accepted_plan or len(args.targets) != 1):
        p.error('--apply/--verify require one target and --accepted-plan')
    result = build(args.state_dir)
    result.update(targets=args.targets, addresses=addresses, mode='offline_plan' if args.offline_plan else 'dry_run')
    if args.offline_plan:
        result['status'] = 'offline_plan_ready_not_deploy_ready'
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    import online
    manifest = json.loads((RESOURCES/'manifest.json').read_text())
    summaries=[]; exit_code=0
    for device in args.targets:
        try:
            ssh=online.SSH(args.ssh_config, device, addresses.get(device), args.bind_interface, args.bind_address)
            live=online.read_live(ssh, manifest)
            if args.apply or args.verify:
                plan=online.load_plan(args.state_dir,args.accepted_plan)
                if args.apply:
                    online.matches(plan, live, device, manifest, result)
                    if plan['unknown_approved'] and not args.approve_unknown_baseline:
                        raise online.Failure('accepted plan contains unknown baseline; explicit --approve-unknown-baseline required')
                    root=online.stage_and_apply(ssh,live,plan,result,device,args.sudo_command_file) if args.sudo_command_file else online.stage_and_apply(ssh,live,plan,result,device)
                    if isinstance(root,dict):
                        summaries.append({'device':device,**root,'deploy_ready':False}); exit_code=max(exit_code,3)
                        continue
                    live=online.read_live(ssh, manifest)
                    summary=online.verify(live,plan,manifest,device,result,root_components_verified=True); summary['root_input']=root
                else:
                    if plan['device'] != device or plan['commit'] != PIN or plan['manifest_sha256'] != result['manifest_sha256']:
                        raise online.Failure('accepted plan identity or manifest mismatch')
                    online.verify(live,plan,manifest,device,result)
                    root=online.stage_components_verify(ssh,plan,result,device)
                    live=online.read_live(ssh,manifest)
                    summary=online.verify(live,plan,manifest,device,result,root_components_verified=True); summary['root_input']=root
                if not summary['deploy_ready']: exit_code=max(exit_code,3)
            else:
                plan=online.prepare_plan(live,manifest,device,result,args.approve_unknown_baseline)
                digest,path=online.write_plan(args.state_dir,plan)
                summary={'device':device,'status':'plan_ready','plan_sha256':digest,'plan_path':str(path),
                         'diff':plan['diff'],'protected':plan['protected'],'camd_pid':plan['camd_pid'],
                         'deploy_ready':False,'needs_system_components':True,'system_components':live.get('system_components',{}),
                         'component_diff':plan['component_diff']}
            summaries.append(summary)
        except (online.Failure, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            summaries.append({'device':device,'status':'failed','error':str(exc)})
            exit_code=max(exit_code,2)
    print(json.dumps({'results':summaries}, indent=2, sort_keys=True))
    return 2 if any(item['status'] == 'failed' for item in summaries) else exit_code

if __name__ == '__main__':
    raise SystemExit(main())
