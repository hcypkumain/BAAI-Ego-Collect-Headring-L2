"""Registered five-file EEG overlay; never accepts arbitrary root payloads."""
import hashlib
import base64
import io
import json
import os
from pathlib import Path
import re
import shlex
import time
import zipfile

import fleet
import online

RELEASE = 'eeg-impedance-advisory-36d8059918ce'
ALIASES = (RELEASE, 'eeg-impedance-advisory')
MANIFEST_SHA = '36d8059918ce370f7928cc7cd3207e2ac1d26471b3e8dcdef74b98b7888cd62a'
BUNDLE_SHA = 'ee2eaf919287df1670fc15a8a706b648f13e24a8fc3a9119e748fef4677a759c'
TOOL_SHA = 'fee73265c537bad5d777b21c5043657427b5503c6cd244e5df8d059a816ab057'
BOOTSTRAP_SHA = '455435313b5795f6bb476444a955a4b6a704947d9aa3a0b76e5a5865e9550ab5'
RESOURCE = fleet.RESOURCES / RELEASE
PREFIX = 'orangepi/src/orangepi_collect/'
AFTER = {
    PREFIX+'common/impedance_quality.py': 'da24ab2e98343042a764199465907935bd2eb3d07ae5b9f905913f0857d42fab',
    PREFIX+'common/eeg_settings.py': 'a90774add55180ad2e1477996b63e95a25b2bb10a0ee7ea8f9dfa07e9d2dbbe0',
    PREFIX+'common/impedance_record.py': 'dc437d13d2e488a361671949fc6dc9199a7148447191720bf2287c36a65a09d3',
    PREFIX+'control/dashboard.html': 'b787ed400e657e77770ce9a627b07cc28636ab0c2faf6b524944d559ff992ff3',
    PREFIX+'framework/eeg_qc.py': 'b362e196b9f2d568e9e64037667a0cfbf31e2b7c12751344cbf7fdb730a9b78d',
}
DEFAULT_INVENTORY = {
    'cp_0014': {'address':'192.168.20.111','hostkey_alias':'192.168.1.103','applied_release':RELEASE},
    'cp_0015': {'address':'192.168.20.109','hostkey_alias':'192.168.1.119','applied_release':RELEASE},
    'cp_0018': {'address':'192.168.20.110','hostkey_alias':'192.168.1.121','applied_release':RELEASE},
    'cp_0020': {'address':'192.168.20.106','hostkey_alias':'192.168.20.106'},
    'cp_0022': {'address':'192.168.20.107','hostkey_alias':'192.168.20.107','status':'offline_last_known'},
}

def registered():
    raw=(RESOURCE/'manifest.json').read_bytes(); bundle=(RESOURCE/'bundle.zip').read_bytes()
    tool=(RESOURCE/'transaction.py').read_bytes(); bootstrap=(RESOURCE/'bootstrap.py').read_bytes()
    if (fleet.sha(raw),fleet.sha(bundle),fleet.sha(tool),fleet.sha(bootstrap)) != (MANIFEST_SHA,BUNDLE_SHA,TOOL_SHA,BOOTSTRAP_SHA):
        raise online.Failure('registered release resource hash mismatch')
    stable_raw=(fleet.RESOURCES/'manifest.json').read_bytes()
    if fleet.sha(stable_raw)!=fleet.MANIFEST_SHA256: raise online.Failure('stable manifest hash mismatch')
    stable=json.loads(stable_raw); manifest=json.loads(raw)
    if stable.get('commit')!=fleet.PIN or len(stable.get('files',[]))!=236: raise online.Failure('stable manifest identity mismatch')
    entries=manifest.get('files',[]); paths=[x['path'] for x in entries]
    if (manifest.get('commit')!=fleet.PIN or manifest.get('project')!=online.PROJECT or len(paths)!=5
            or set(paths)!=set(AFTER) or len(set(paths))!=5): raise online.Failure('invalid registered delta manifest')
    before={x['path']:x['sha256'] for x in stable['files']}
    with zipfile.ZipFile(io.BytesIO(bundle)) as z:
        names=z.namelist(); expected={'manifest.json','transaction.py'}|{'payload/'+p for p in paths}
        if len(names)!=7 or set(names)!=expected or z.read('manifest.json')!=raw or z.read('transaction.py')!=tool:
            raise online.Failure('registered archive members mismatch')
        for item in entries:
            path=item['path']; data=z.read('payload/'+path)
            if (item['before_sha256']!=before.get(path) or item['sha256']!=AFTER[path]
                    or fleet.sha(data)!=AFTER[path] or len(data)!=item['size']):
                raise online.Failure('registered before/after mismatch: '+path)
    return manifest,stable,bundle,bootstrap

def refuse_stable_overlay(live):
    files=live.get('files') or {}
    if any(files.get(p,{}).get('sha256')==h for p,h in AFTER.items()):
        raise online.Failure('registered EEG overlay present; stable deployment would downgrade it; explicit reconciliation required')

def classify(live, stable, manifest):
    files=live.get('files') or {}; delta={x['path']:x for x in manifest['files']}
    statuses=[]
    for item in stable['files']:
        path=item['path']; state=files.get(path) or {}; actual=state.get('sha256')
        if state.get('type')!='regular': raise online.Failure('unsafe or missing source: '+path)
        if path in delta:
            d=delta[path]; statuses.append('before' if actual==d['before_sha256'] else 'after' if actual==d['sha256'] else 'unknown')
        elif actual!=item['sha256']: raise online.Failure('non-overlay source differs from reviewed 236 baseline: '+path)
    if statuses==['before']*5: return 'before'
    if statuses==['after']*5: return 'after'
    raise online.Failure('unknown or mixed EEG baseline; explicit reconciliation required')

PROFILE_CODE = r'''import json,pathlib,sys,yaml
p=pathlib.Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2');base=p/'profiles/eeg/orangepi';src=base/'src';primary=p/'orangepi/src'
if src.resolve()!=primary.resolve():raise SystemExit('unreviewed EEG source shadow')
config=yaml.safe_load((base/'configs/devices.yaml').read_text()) or {}
sys.path.insert(0,str(primary/'orangepi_collect/common'))
import eeg_settings
item=eeg_settings.apply(config,base)['eeg']
print(json.dumps({'source':str(src.resolve()),'policy':item.get('impedance_acceptance_policy','strict'),'explicit_policy':(config.get('eeg') or {}).get('impedance_acceptance_policy')}))'''

def read_profile(ssh):
    value=json.loads(ssh.run('python3 -c '+shlex.quote(PROFILE_CODE)))
    if value.get('source')!=online.PROJECT+'/orangepi/src': raise online.Failure('EEG source shadow')
    # Before deployment, absent policy resolves to strict; it becomes advisory after.
    if value.get('explicit_policy') not in (None,'advisory'): raise online.Failure('explicit EEG policy requires separate review')
    return value

def inventory(path):
    if not path.exists(): return {k:dict(v) for k,v in DEFAULT_INVENTORY.items()}
    value=json.loads(path.read_text())
    if not isinstance(value,dict): raise online.Failure('invalid inventory')
    return value

def save_inventory(path, values):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    data=online.canonical(values); temp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()

def stage_apply(ssh, device, bundle, bootstrap, command_file=None):
    if command_file is None: ssh.require_tty()
    stage=ssh.run('umask 077; mktemp -d "$HOME/.headring-stage-XXXXXXXX"').decode().strip()
    if not re.fullmatch(r'/home/orangepi/\.headring-stage-[A-Za-z0-9]{8}',stage): raise online.Failure('unsafe stage path')
    path=stage+'/bundle.zip';ssh.run('umask 077; cat > '+shlex.quote(path),bundle)
    root='/var/lib/headring-eeg-delta/'+MANIFEST_SHA
    code='import base64;exec(compile(base64.b64decode('+repr(base64.b64encode(bootstrap).decode())+'),"bootstrap","exec"))'
    boot='python3 -c '+shlex.quote(code)+' '+shlex.quote(path)+' '+BUNDLE_SHA+' '+MANIFEST_SHA
    command='python3 '+shlex.quote(root+'/transaction.py')+' --device '+shlex.quote(device)+' --manifest-sha256 '+MANIFEST_SHA
    dry='sudo sh -c '+shlex.quote('set -eu; '+boot+'; '+command)
    apply='sudo sh -c '+shlex.quote('set -eu; '+command+' --apply')
    if command_file:
        content={'device':device,'stage':stage,'manifest_sha256':MANIFEST_SHA,
                 'dry_run_argv':ssh.argv[:-1]+['-T',ssh.argv[-1],online.sudo_stdin_command(dry)],
                 'apply_argv':ssh.argv[:-1]+['-T',ssh.argv[-1],online.sudo_stdin_command(apply)],
                 'warning':'Manual execution requires credential input via secure local PTY; recheck idle and baseline. No credentials stored.'}
        fd=os.open(command_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:json.dump(content,f,indent=2,sort_keys=True);f.write('\n')
        return {'status':'staged_not_applied','command_file':str(command_file)}
    ssh.run('sudo sh -c '+shlex.quote('set -eu; '+boot+'; '+command+'; '+command+' --apply'),interactive=True)
    return {'status':'applied','root_bundle':root}

def main(args, parser):
    if args.release not in ALIASES: parser.error('unknown registered release')
    if args.approve_unknown_baseline or args.audit_recordings or args.local_root: parser.error('delta does not permit unknown baseline or recording audit')
    if len(args.targets)!=1: parser.error('delta requires exactly one selected target')
    if args.apply and args.verify: parser.error('--apply already verifies')
    if (args.apply or args.verify) and not args.accepted_plan: parser.error('--apply/--verify require --accepted-plan')
    if args.offline_plan and (args.apply or args.verify or args.accepted_plan): parser.error('offline plan cannot deploy')
    device=args.targets[0]; manifest,stable,bundle,bootstrap=registered()
    if args.offline_plan:
        print(json.dumps({'release':RELEASE,'manifest_sha256':MANIFEST_SHA,'status':'offline_manifest_validated','deploy_ready':False},indent=2));return 0
    inv_path=args.state_dir/'inventory.json'; inv=inventory(inv_path)
    if not inv_path.exists():save_inventory(inv_path,inv)
    entry=inv.get(device,{})
    address=dict(args.address).get(device) or entry.get('address')
    if not address: raise online.Failure('no verified address; supply --address '+device+'=IP')
    values=online.config_for(args.ssh_config,device); alias=values.get('hostkeyalias') or values.get('hostname')
    if entry.get('hostkey_alias') and alias!=entry['hostkey_alias']: raise online.Failure('SSH host key alias differs from inventory')
    ssh=online.SSH(args.ssh_config,device,address,args.bind_interface or 'wlx40a5ef5ff812',args.bind_address or '192.168.20.100')
    try:
        live=online.read_live(ssh,stable);online.gates(live,device);state=classify(live,stable,manifest);profile=read_profile(ssh)
        if state=='after' and profile['policy']!='advisory': raise online.Failure('installed overlay EEG policy is not advisory')
        if args.apply or args.verify:
            plan=online.load_plan(args.state_dir,args.accepted_plan)
            if (plan.get('release')!=RELEASE or plan.get('device')!=device or plan.get('manifest_sha256')!=MANIFEST_SHA
                    or plan.get('address')!=address or plan.get('hostkey_alias')!=alias): raise online.Failure('accepted plan release/device/address mismatch')
            if time.time()-plan.get('created_at',0)>600 or time.time()<plan.get('created_at',0)-30: raise online.Failure('plan expired; make a fresh plan')
            if any(live.get(k)!=plan.get(k) for k in ('files','protected','camd_pid')) or profile!=plan.get('profile') or state!=plan.get('baseline'):
                raise online.Failure('live state drifted since accepted plan')
            if state=='after': result={'status':'already_installed_verified','root_mutation':False,'overlay_files':5,'verified_files':236}
            elif args.verify: raise online.Failure('overlay not installed')
            else:
                result=stage_apply(ssh,device,bundle,bootstrap,args.sudo_command_file)
                if result['status']=='applied':
                    after=online.read_live(ssh,stable);online.gates(after,device)
                    if classify(after,stable,manifest)!='after' or after['protected']!=plan['protected'] or after['camd_pid']!=plan['camd_pid']:
                        raise online.Failure('post-apply verification failed; inspect transaction receipt')
                    profile=read_profile(ssh)
                    if profile['policy']!='advisory': raise online.Failure('post-apply EEG policy is not advisory')
                    result.update(verified_files=236,policy='advisory')
        else:
            plan={'schema':1,'release':RELEASE,'device':device,'address':address,'hostkey_alias':alias,'manifest_sha256':MANIFEST_SHA,
                  'created_at':time.time(),'baseline':state,'files':live['files'],'protected':live['protected'],'camd_pid':live['camd_pid'],'profile':profile}
            digest,path=online.write_plan(args.state_dir,plan)
            result={'status':'plan_ready','plan_sha256':digest,'plan_path':str(path),'baseline':state,'deploy_ready':False}
        entry.update(address=address,hostkey_alias=alias,last_verified_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                     policy=profile['policy'],status='verified')
        if state=='after' or result.get('status')=='applied': entry['applied_release']=RELEASE
        inv[device]=entry;save_inventory(inv_path,inv)
        print(json.dumps({'device':device,'release':RELEASE,'inventory':str(inv_path),**result},indent=2,sort_keys=True))
        return 3 if result.get('status')=='staged_not_applied' else 0
    except (online.Failure,OSError,ValueError,KeyError) as exc:
        if address==entry.get('address'):
            entry['status']='offline' if 'deviceunreachable_or_ssh_failed' in str(exc) else 'rejected'
            entry['last_checked_at']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
            inv[device]=entry;save_inventory(inv_path,inv)
        print(json.dumps({'device':device,'release':RELEASE,'status':'offline_or_rejected','last_known_address':entry.get('address'),
                          'inventory':str(inv_path),'error':str(exc),'required_action':'provide a verified address or reconcile the baseline; no scanning or retry'},indent=2));return 2
